import json
import os

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for

from core.normalization import normalize_selected_tables_payload
from core.orchestrator import get_asset_details, get_login_context, get_workspace_assets
from core.presets import (
    delete_preset as delete_saved_preset,
    list_presets as list_saved_presets,
    load_preset,
    save_preset as create_saved_preset,
    update_preset as update_saved_preset,
)


model_transformer = Flask(__name__, static_folder="templates/static")


def get_request_payload():
    payload = request.get_json(silent=True) if request.is_json else None
    if payload is not None:
        return payload

    try:
        return json.loads(request.form.get("payload", "{}"))
    except json.JSONDecodeError:
        return {}


@model_transformer.route("/")
def home():
    return render_template("home.html")


@model_transformer.post("/selected-tables")
def selected_tables():
    selection = normalize_selected_tables_payload(get_request_payload())
    return render_template("selected_tables.html", selection=selection, can_create_new_preset=False, can_overwrite_preset=False)


@model_transformer.get("/selected-tables/<preset_id>")
def view_selected_tables_preset(preset_id):
    try:
        saved = load_preset(preset_id)
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError):
        return redirect(url_for("home"))

    selection = normalize_selected_tables_payload(saved)
    return render_template("selected_tables.html", selection=selection, can_create_new_preset=True, can_overwrite_preset=True)


@model_transformer.post("/saved-batches")
def saved_batches():
    selection = normalize_selected_tables_payload(get_request_payload())
    return render_template("selected_tables.html", selection=selection, can_create_new_preset=False, can_overwrite_preset=False)


@model_transformer.get("/saved-batches/<preset_id>")
def view_saved_batch(preset_id):
    return redirect(url_for("view_selected_tables_preset", preset_id=preset_id))


@model_transformer.get("/api/presets")
def list_presets():
    return jsonify(list_saved_presets())


@model_transformer.post("/api/presets")
def save_preset():
    payload = get_request_payload()
    try:
        preset_id = create_saved_preset(payload)
    except OSError as exc:
        return jsonify({"message": f"Could not save preset: {exc}"}), 500
    return jsonify({"presetId": preset_id})


@model_transformer.put("/api/presets/<preset_id>")
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


@model_transformer.delete("/api/presets/<preset_id>")
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


@model_transformer.post("/api/login")
def login_with_azure_cli():
    try:
        login_context = get_login_context()
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Azure CLI authentication failed. Run az login, then try again. {exc}"}), 401
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        message = "Power BI workspace lookup failed. Confirm this account can access Power BI/Fabric workspaces."
        return jsonify({"message": message}), status_code
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Power BI API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    return jsonify(login_context)


@model_transformer.get("/api/workspaces/<workspace_id>/assets")
def workspace_assets(workspace_id):
    try:
        assets = get_workspace_assets(workspace_id)
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Azure CLI authentication failed. Run az login, then try again. {exc}"}), 401
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        message = "Could not load models and reports for this workspace. Confirm this account has workspace access."
        return jsonify({"message": message}), status_code
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Power BI API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    return jsonify(assets)


@model_transformer.get("/api/workspaces/<workspace_id>/assets/<category>/<asset_id>/details")
def asset_details(workspace_id, category, asset_id):
    try:
        details = get_asset_details(workspace_id, category, asset_id)
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Azure CLI authentication failed. Run az login, then try again. {exc}"}), 401
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        message = "Could not load metadata for this item. Confirm this account has item access."
        return jsonify({"message": message}), status_code
    except requests.RequestException as exc:
        return jsonify({"message": f"Could not reach the Power BI API. {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    return jsonify(details)


def main():
    load_dotenv(dotenv_path=".env", override=False)
    model_transformer.run(debug=int(os.getenv("DEBUG")))


if __name__ == "__main__":
    main()