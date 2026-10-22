import json
from queue import Empty, Queue
from threading import Thread
from urllib.parse import quote
from uuid import UUID

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from flask import Blueprint, Response, current_app, jsonify, request, send_file, stream_with_context

from fabric.backend.clients import (
    FabricWorkspaceClient,
    RepositoryCloneError,
    SemanticModelResolutionError,
    azure_devops_client as _azure_devops_client,
)
from fabric.backend.presets import (
    delete_preset as delete_saved_preset,
    list_presets as get_presets,
    load_preset,
    save_preset,
    bind_live_semantic_model,
)
from fabric.backend.filesystem import filesystem_path
from fabric.backend.lineage import REPORTS_DIR, persist_mapping, scan_repository
from fabric.backend.repositories import ensure_repository
from fabric.backend.selection import normalize_repository
from shared.backend.auth import get_fabric_access_token
from shared.backend.routes import get_request_payload, require_api_login
from fabric.backend.impact import build_catalog, live_schema, report_path, validate_changes
from fabric.backend.copilot_impact import CopilotAuthenticationRequired, run_analysis


fabric_backend = Blueprint("fabric_api", __name__, url_prefix="/api/fabric")
fabric_backend.before_request(require_api_login)

def _authentication_error(exc):
    return jsonify({
        "message": f"Microsoft authentication failed. Try signing in again. {exc}"
    }), 401


def _request_error(exc, service, action):
    status_code = exc.response.status_code if exc.response is not None else 502
    if status_code in {401, 403}:
        message = f"{service} denied access while trying to {action}."
    else:
        message = f"{service} could not {action}."
    return jsonify({"message": message}), status_code


def _azure_devops_url(details):
    organization = quote(str(details.get("organizationName") or ""), safe="")
    project = quote(str(details.get("projectName") or ""), safe="")
    repository = quote(str(details.get("repositoryName") or ""), safe="")
    return f"https://dev.azure.com/{organization}/{project}/_git/{repository}"


@fabric_backend.get("/workspaces")
def list_workspaces():
    try:
        workspaces = FabricWorkspaceClient(get_fabric_access_token()).list_workspaces()
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return _authentication_error(exc)
    except requests.HTTPError as exc:
        return _request_error(exc, "Fabric", "load workspaces")
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Fabric API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502

    return jsonify({"workspaces": workspaces})


@fabric_backend.get("/workspaces/<workspace_id>/repository")
def get_workspace_repository(workspace_id):
    try:
        UUID(workspace_id)
    except ValueError:
        return jsonify({"message": "Invalid Fabric workspace ID."}), 400

    try:
        connection = FabricWorkspaceClient(
            get_fabric_access_token()
        ).get_git_connection(workspace_id)
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return _authentication_error(exc)
    except requests.HTTPError as exc:
        return _request_error(exc, "Fabric", "load the workspace Git connection")
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Fabric API. {exc}"}), 502

    details = connection.get("gitProviderDetails")
    if details and details.get("gitProviderType") == "AzureDevOps":
        return jsonify({
            "connected": True,
            "repository": {
                "name": details.get("repositoryName", ""),
                "organization": details.get("organizationName", ""),
                "projectName": details.get("projectName", ""),
                "branchName": details.get("branchName", ""),
                "directoryName": details.get("directoryName", ""),
                "url": _azure_devops_url(details),
            },
        })

    try:
        repositories = _azure_devops_client().list_repositories()
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return _authentication_error(exc)
    except requests.HTTPError as exc:
        return _request_error(exc, "Azure DevOps", "load repositories")
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach Azure DevOps. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502

    return jsonify({
        "connected": False,
        "connectionState": connection.get("gitConnectionState", "NotConnected"),
        "repositories": repositories,
    })


@fabric_backend.post("/semantic-models")
def list_semantic_models():
    payload = get_request_payload()
    try:
        repository = normalize_repository(
            payload.get("repository") if isinstance(payload, dict) else None
        )
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400

    try:
        result = _azure_devops_client().list_semantic_models(repository)
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return _authentication_error(exc)
    except requests.HTTPError as exc:
        return _request_error(exc, "Azure DevOps", "load repository semantic models")
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach Azure DevOps. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502
    return jsonify(result)


@fabric_backend.get("/presets")
def list_fabric_presets():
    try:
        presets = get_presets()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return jsonify({"message": f"Could not load Fabric presets: {exc}"}), 500
    return jsonify({"presets": presets})


@fabric_backend.post("/presets")
def create_fabric_preset():
    payload = get_request_payload()
    try:
        preset = save_preset(payload)
    except json.JSONDecodeError as exc:
        return jsonify({"message": f"Could not read the existing Fabric preset: {exc}"}), 500
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return _authentication_error(exc)
    except RepositoryCloneError as exc:
        current_app.logger.warning("%s", exc)
        return jsonify({"message": str(exc), "details": exc.details}), 502
    except requests.HTTPError as exc:
        return _request_error(exc, "Azure DevOps", "download the repository")
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not download the Azure DevOps repository. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502
    except OSError as exc:
        return jsonify({"message": f"Could not save Fabric preset: {exc}"}), 500
    return jsonify(preset), 201


@fabric_backend.delete("/presets/<preset_id>")
def delete_fabric_preset(preset_id):
    try:
        delete_saved_preset(preset_id)
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except FileNotFoundError:
        return jsonify({"message": "Fabric preset not found."}), 404
    except OSError as exc:
        return jsonify({"message": f"Could not delete Fabric preset: {exc}"}), 500
    return jsonify({"message": "Fabric preset deleted."})


def _processing_event(stage, state, progress, message):
    return f"data: {json.dumps({'stage': stage, 'state': state, 'progress': progress, 'message': message})}\n\n"


@fabric_backend.post("/presets/<preset_id>/process")
def process_fabric_preset(preset_id):
    try:
        preset = load_preset(preset_id)
    except FileNotFoundError:
        return jsonify({"message": "Fabric preset not found."}), 404
    except json.JSONDecodeError as exc:
        current_app.logger.exception("Could not read Fabric preset %s", preset_id)
        return jsonify({"message": f"Could not read Fabric preset: {exc}"}), 500
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except OSError as exc:
        current_app.logger.exception("Could not load Fabric preset %s", preset_id)
        return jsonify({"message": f"Could not load Fabric preset: {exc}"}), 500

    payload = get_request_payload()
    refresh = isinstance(payload, dict) and payload.get("refresh") is True

    def process():
        action = "Downloading a fresh repository snapshot" if refresh else "Preparing the repository snapshot"
        yield _processing_event("repository", "active", 8, action)
        try:
            repository_path = ensure_repository(preset["repository"], refresh=refresh)
        except RepositoryCloneError as exc:
            current_app.logger.error(
                "Could not clone repository for Fabric preset %s: %s", preset_id, exc
            )
            yield _processing_event("repository", "error", 8, str(exc))
            return
        except (
            ClientAuthenticationError, CredentialUnavailableError,
            requests.RequestException, RuntimeError, OSError, ValueError,
        ) as exc:
            current_app.logger.exception(
                "Could not prepare repository for Fabric preset %s", preset_id
            )
            yield _processing_event(
                "repository", "error", 8,
                f"Could not download the preset repository. {exc}",
            )
            return

        yield _processing_event(
            "repository", "complete", 40,
            "Repository snapshot is ready.",
        )
        yield _processing_event(
            "lineage", "active", 50,
            "Tracing the selected semantic model to tables, notebooks, and pipelines.",
        )
        try:
            mapping = scan_repository(repository_path, preset["semanticModel"])
        except (OSError, ValueError) as exc:
            current_app.logger.exception("Could not map Fabric preset %s", preset_id)
            yield _processing_event(
                "lineage", "error", 50,
                f"Could not scan the downloaded Fabric artifacts. {exc}",
            )
            return

        yield _processing_event(
            "lineage", "complete", 85,
            "Repository lineage analysis is complete.",
        )
        yield _processing_event(
            "reports", "active", 90,
            "Saving the lineage mapping and download reports.",
        )
        try:
            persist_mapping(mapping, preset_id)
        except (OSError, ValueError) as exc:
            current_app.logger.exception("Could not save Fabric mapping %s", preset_id)
            yield _processing_event(
                "reports", "error", 90,
                f"Could not save the Fabric mapping. {exc}",
            )
            return
        yield _processing_event(
            "reports", "complete", 100,
            "Repository and lineage data are ready.",
        )
        yield _processing_event(
            "complete", "complete", 100,
            "Repository and lineage data are ready.",
        )

    return Response(
        stream_with_context(process()),
        content_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@fabric_backend.get("/presets/<preset_id>/mapping/<report_format>")
def download_fabric_mapping(preset_id, report_format):
    if report_format not in {"json", "csv"}:
        return jsonify({"message": "Mapping format must be json or csv."}), 400
    try:
        load_preset(preset_id)
        path = filesystem_path(REPORTS_DIR / preset_id / f"mapping.{report_format}")
        if not path.is_file():
            return jsonify({"message": "Open the Fabric preset to generate its mapping first."}), 404
        return send_file(
            path, as_attachment=True, download_name=f"fabric-mapping-{preset_id[:12]}.{report_format}",
            mimetype="application/json" if report_format == "json" else "text/csv",
        )
    except FileNotFoundError:
        return jsonify({"message": "Fabric preset not found."}), 404
    except json.JSONDecodeError as exc:
        current_app.logger.exception("Could not read Fabric preset %s", preset_id)
        return jsonify({"message": f"Could not read Fabric preset: {exc}"}), 500
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except OSError as exc:
        current_app.logger.exception("Could not download Fabric mapping %s", preset_id)
        return jsonify({"message": f"Could not download Fabric mapping: {exc}"}), 500


@fabric_backend.get("/presets/<preset_id>/impact/catalog")
@fabric_backend.post("/presets/<preset_id>/impact/catalog")
def fabric_impact_catalog(preset_id):
    try:
        model_id = None
        if request.method == "POST":
            payload = get_request_payload()
            if not isinstance(payload, dict) or not isinstance(payload.get("modelId"), str):
                raise ValueError("Select a live Fabric semantic model.")
            model_id = str(UUID(payload["modelId"]))
        catalog = live_schema(
            load_preset(preset_id), FabricWorkspaceClient(get_fabric_access_token()), model_id,
        )
        if model_id is not None:
            bind_live_semantic_model(preset_id, catalog["semanticModel"])
        return jsonify({
            "revision": catalog["revision"], "tables": catalog["tables"],
            "semanticModel": catalog["semanticModel"],
            "issues": catalog["issues"],
        })
    except SemanticModelResolutionError as exc:
        return jsonify({
            "message": str(exc), "requiresModelSelection": True,
            "semanticModels": exc.models,
        }), 409
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return _authentication_error(exc)
    except requests.HTTPError as exc:
        return _request_error(exc, "Fabric", "load the semantic model schema")
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Fabric API. {exc}"}), 502
    except RuntimeError as exc:
        current_app.logger.exception("Could not fetch Fabric schema %s", preset_id)
        return jsonify({"message": str(exc)}), 502
    except FileNotFoundError as exc:
        return jsonify({"message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except OSError as exc:
        current_app.logger.exception("Could not inspect Fabric schema %s", preset_id)
        return jsonify({"message": f"Could not read the repository schema: {exc}"}), 500


@fabric_backend.post("/presets/<preset_id>/impact/analyze")
def analyze_fabric_impact(preset_id):
    try:
        preset = load_preset(preset_id)
        current_schema = live_schema(preset, FabricWorkspaceClient(get_fabric_access_token()))
        payload = get_request_payload()
        changes = validate_changes(payload, current_schema)
        catalog = build_catalog(preset, current_schema)
        catalog["instructions"] = payload["instructions"].strip()
    except SemanticModelResolutionError as exc:
        return jsonify({
            "message": str(exc), "requiresModelSelection": True,
            "semanticModels": exc.models,
        }), 409
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return _authentication_error(exc)
    except requests.HTTPError as exc:
        return _request_error(exc, "Fabric", "load the semantic model schema")
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Fabric API. {exc}"}), 502
    except RuntimeError as exc:
        current_app.logger.exception("Could not fetch Fabric schema %s", preset_id)
        return jsonify({"message": str(exc)}), 502
    except FileNotFoundError as exc:
        return jsonify({"message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except OSError as exc:
        current_app.logger.exception("Could not prepare Fabric impact %s", preset_id)
        return jsonify({"message": f"Could not prepare impact analysis: {exc}"}), 500

    events = Queue()
    app = current_app._get_current_object()

    def work():
        try:
            def progress(index, total, name):
                events.put({
                    "state": "progress", "message": f"Explaining targeted artifact {index} of {total}: {name}",
                })
            report = run_analysis(preset_id, catalog, changes, progress)
            events.put({"state": "complete", "report": report})
        except (CopilotAuthenticationRequired, OSError, RuntimeError, ValueError, TimeoutError) as exc:
            app.logger.exception("Fabric impact analysis failed for %s", preset_id)
            events.put({
                "state": "error", "message": f"Impact analysis failed: {exc}",
            })
        finally:
            events.put(None)

    def stream():
        yield f"data: {json.dumps({'state': 'progress', 'message': 'Building impacted object lineage locally. AI will explain only code-identified targets; unchanged explanations are reused.'})}\n\n"
        worker = Thread(target=work, name=f"fabric-impact-{preset_id[:12]}", daemon=True)
        worker.start()
        while True:
            try:
                event = events.get(timeout=10)
            except Empty:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"
    return Response(
        stream_with_context(stream()), content_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@fabric_backend.get("/presets/<preset_id>/impact/reports/<report_id>")
def download_fabric_impact(preset_id, report_id):
    try:
        load_preset(preset_id)
        path = report_path(preset_id, report_id)
        return send_file(
            path, as_attachment=True, download_name=f"fabric-impact-{report_id}.json",
            mimetype="application/json",
        )
    except FileNotFoundError:
        return jsonify({"message": "Fabric preset or impact report not found."}), 404
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except OSError as exc:
        current_app.logger.exception("Could not download Fabric impact report %s", report_id)
        return jsonify({"message": f"Could not download impact report: {exc}"}), 500