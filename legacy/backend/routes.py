import json
from pathlib import Path

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from flask import Blueprint, jsonify
from shared.backend.routes import get_request_payload, require_api_login

from legacy.backend.ai_batch_creation import (
    DEFAULT_INPUT_DIRECTORY as AI_BATCH_INPUT_DIRECTORY,
    DEFAULT_OUTPUT_DIRECTORY as AI_BATCH_OUTPUT_DIRECTORY,
    generate_ai_batches,
)
from legacy.backend.ai_powered_refresh import get_ai_refresh_metadata
from legacy.backend.ai_table_precedence import generate_table_precedence
from legacy.backend.batch_processing import batch_refresh_manager
from legacy.backend.copilot_auth import ensure_copilot_authenticated
from legacy.backend.orchestrator import get_advanced_table_details, get_asset_details, get_workspaces, get_workspace_assets
from legacy.backend.presets import (
    delete_preset as delete_saved_preset,
    get_preset_path,
    list_presets as list_saved_presets,
    load_preset,
    save_preset as create_saved_preset,
    update_preset as update_saved_preset,
)


legacy_backend = Blueprint("legacy_api", __name__, url_prefix="/api/legacy")
legacy_backend.before_request(require_api_login)


@legacy_backend.get("/presets")
def list_presets():
    return jsonify(list_saved_presets())


@legacy_backend.post("/presets")
def save_preset():
    payload = get_request_payload()
    try:
        preset_id = create_saved_preset(payload)
        saved = load_preset(preset_id)
    except OSError as exc:
        return jsonify({"message": f"Could not save preset: {exc}"}), 500
    return jsonify({"presetId": preset_id, "presetName": saved.get("presetName", "")})


@legacy_backend.put("/presets/<preset_id>")
def update_preset(preset_id):
    payload = get_request_payload()
    try:
        update_saved_preset(preset_id, payload)
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"message": str(exc)}), 404
    except OSError as exc:
        return jsonify({"message": f"Could not update preset: {exc}"}), 500

    return jsonify({"presetId": preset_id})


@legacy_backend.delete("/presets/<preset_id>")
def delete_preset(preset_id):
    try:
        delete_saved_preset(preset_id)
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"message": str(exc)}), 404
    except OSError as exc:
        return jsonify({"message": f"Could not delete preset: {exc}"}), 500

    return jsonify({"message": "Preset deleted."})


@legacy_backend.post("/refresh-jobs")
def start_refresh_job():
    payload = get_request_payload()
    preset_id = payload.get("presetId") if isinstance(payload, dict) else None
    ai_batch_file = (
        str(payload.get("aiBatchFile") or "").strip()
        if isinstance(payload, dict)
        else ""
    )
    if ai_batch_file and (
        Path(ai_batch_file).name != ai_batch_file
        or Path(ai_batch_file).suffix.lower() != ".json"
    ):
        return jsonify({"message": "Invalid AI batch creation file."}), 400

    try:
        refresh_plan_path = (
            AI_BATCH_OUTPUT_DIRECTORY / ai_batch_file
            if ai_batch_file
            else get_preset_path(preset_id)
        )
        job = batch_refresh_manager.start(refresh_plan_path)
    except (ValueError, json.JSONDecodeError) as exc:
        return jsonify({"message": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"message": str(exc)}), 404
    except OSError as exc:
        return jsonify({"message": f"Could not read the refresh configuration: {exc}"}), 500
    return jsonify(job), 202


@legacy_backend.get("/refresh-jobs/<job_id>")
def refresh_job_status(job_id):
    job = batch_refresh_manager.get(job_id)
    if not job:
        return jsonify({"message": "Refresh job not found."}), 404
    return jsonify(job)


@legacy_backend.delete("/refresh-jobs/<job_id>")
def cancel_refresh_job(job_id):
    job = batch_refresh_manager.cancel(job_id)
    if not job:
        return jsonify({"message": "Refresh job not found."}), 404
    return jsonify(job)


@legacy_backend.get("/workspaces")
def workspaces():
    try:
        workspaces = get_workspaces()
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Browser authentication failed. Try signing in again. {exc}"}), 401
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        message = "Power BI workspace lookup failed. Confirm this account can access Power BI/Fabric workspaces."
        return jsonify({"message": message}), status_code
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Power BI API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    return jsonify({"workspaces": workspaces})


@legacy_backend.get("/workspaces/<workspace_id>/assets")
def workspace_assets(workspace_id):
    try:
        assets = get_workspace_assets(workspace_id)
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Browser authentication failed. Try signing in again. {exc}"}), 401
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        message = "Could not load models and reports for this workspace. Confirm this account has workspace access."
        return jsonify({"message": message}), status_code
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Power BI API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    return jsonify(assets)


@legacy_backend.get("/workspaces/<workspace_id>/assets/<category>/<asset_id>/details")
def asset_details(workspace_id, category, asset_id):
    try:
        details = get_asset_details(workspace_id, category, asset_id)
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Browser authentication failed. Try signing in again. {exc}"}), 401
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        message = "Could not load metadata for this item. Confirm this account has item access."
        return jsonify({"message": message}), status_code
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Power BI API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    return jsonify(details)


@legacy_backend.post("/workspaces/<workspace_id>/models/<model_id>/advanced-table-data")
def advanced_table_data(workspace_id, model_id):
    payload = get_request_payload()
    selected_tables = payload.get("selectedTables") if isinstance(payload, dict) else []
    all_tables = payload.get("allTables") if isinstance(payload, dict) else []

    try:
        details = get_advanced_table_details(workspace_id, model_id, selected_tables, all_tables)
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Browser authentication failed. Try signing in again. {exc}"}), 401
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        message = "Could not fetch advanced table data for this semantic model. Confirm this account can query the model."
        return jsonify({"message": message}), status_code
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Power BI or Fabric API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    return jsonify(details)


@legacy_backend.post("/workspaces/<workspace_id>/models/<model_id>/ai-refresh-metadata")
def ai_refresh_metadata(workspace_id, model_id):
    payload = get_request_payload()
    workspace_name = payload.get("workspaceName") if isinstance(payload, dict) else ""
    model_name = payload.get("modelName") if isinstance(payload, dict) else ""
    selected_tables = payload.get("selectedTables") if isinstance(payload, dict) else []
    all_tables = payload.get("allTables") if isinstance(payload, dict) else []

    try:
        metadata = get_ai_refresh_metadata(
            workspace_name,
            workspace_id,
            model_name,
            model_id,
            selected_tables,
            all_tables,
        )
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Browser authentication failed. Try signing in again. {exc}"}), 401
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        message = "Could not prepare AI refresh metadata. Confirm this account can query the semantic model."
        return jsonify({"message": message}), status_code
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Power BI or Fabric API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401
    except OSError as exc:
        return jsonify({"message": f"Could not save AI refresh metadata: {exc}"}), 500

    return jsonify(metadata)


@legacy_backend.post("/copilot-login")
def copilot_login():
    try:
        status = ensure_copilot_authenticated()
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 502

    return jsonify({
        "authenticated": True,
        "login": status.login,
    })


@legacy_backend.post("/ai-table-precedence")
def ai_table_precedence():
    try:
        precedence, output_path = generate_table_precedence()
    except FileNotFoundError as exc:
        return jsonify({"message": str(exc)}), 404
    except (json.JSONDecodeError, ValueError) as exc:
        return jsonify({"message": f"Could not validate AI table precedence: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": f"Could not set table precedence with GitHub Copilot: {exc}"}), 502
    except OSError as exc:
        return jsonify({"message": f"Could not save AI table precedence: {exc}"}), 500

    return jsonify({
        **precedence,
        "outputFile": output_path.name,
    })


@legacy_backend.post("/ai-batch-creation")
def ai_batch_creation():
    payload = get_request_payload()
    precedence_file = (
        str(payload.get("precedenceFile") or "").strip()
        if isinstance(payload, dict)
        else ""
    )
    if precedence_file and (
        Path(precedence_file).name != precedence_file
        or Path(precedence_file).suffix.lower() != ".json"
    ):
        return jsonify({"message": "Invalid AI table precedence file."}), 400

    precedence_path = (
        AI_BATCH_INPUT_DIRECTORY / precedence_file
        if precedence_file
        else None
    )
    try:
        batches, output_path = generate_ai_batches(precedence_path)
    except FileNotFoundError as exc:
        return jsonify({"message": str(exc)}), 404
    except (json.JSONDecodeError, ValueError) as exc:
        return jsonify({"message": f"Could not validate AI batch creation: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": f"Could not create AI refresh batches with GitHub Copilot: {exc}"}), 502
    except OSError as exc:
        return jsonify({"message": f"Could not save AI refresh batches: {exc}"}), 500

    return jsonify({
        **batches,
        "outputFile": output_path.name,
    })

