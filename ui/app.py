import json
import os
import re
from pathlib import Path

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for

try:
    from .base import build_user, get_azure_cli_account, get_fabric_access_token, get_power_bi_access_token
    from .clients import FabricClient, PowerBIClient
    from .semantic_model import get_power_bi_asset_details
    from .shared_Scripts import normalize_saved_batches_payload, normalize_selected_tables_payload
except ImportError:
    from base import build_user, get_azure_cli_account, get_fabric_access_token, get_power_bi_access_token
    from clients import FabricClient, PowerBIClient
    from semantic_model import get_power_bi_asset_details
    from shared_Scripts import normalize_saved_batches_payload, normalize_selected_tables_payload


model_transformer = Flask(__name__, static_folder="templates/static")

PRESETS_DIR = Path("presets")


def get_request_payload():
    payload = request.get_json(silent=True) if request.is_json else None
    if payload is not None:
        return payload

    try:
        return json.loads(request.form.get("payload", "{}"))
    except json.JSONDecodeError:
        return {}


def create_power_bi_client():
    return PowerBIClient(get_power_bi_access_token())


def create_fabric_client():
    return FabricClient(get_fabric_access_token())


@model_transformer.route("/")
def home():
    return render_template("home.html")


@model_transformer.post("/selected-tables")
def selected_tables():
    selection = normalize_selected_tables_payload(get_request_payload())
    return render_template("selected_tables.html", selection=selection)


@model_transformer.get("/selected-tables/<preset_id>")
def view_selected_tables_preset(preset_id):
    if not re.fullmatch(r"[\w.-]+", preset_id):
        return redirect(url_for("home"))
    preset_path = PRESETS_DIR / f"{preset_id}.json"
    if not preset_path.exists():
        return redirect(url_for("home"))
    try:
        with open(preset_path, encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        return redirect(url_for("home"))

    saved["presetId"] = preset_id
    selection = normalize_selected_tables_payload(saved)
    return render_template("selected_tables.html", selection=selection)


@model_transformer.post("/saved-batches")
def saved_batches():
    selection = normalize_selected_tables_payload(get_request_payload())
    return render_template("selected_tables.html", selection=selection)


@model_transformer.get("/saved-batches/<preset_id>")
def view_saved_batch(preset_id):
    return redirect(url_for("view_selected_tables_preset", preset_id=preset_id))


@model_transformer.get("/api/presets")
def list_presets():
    presets = []
    if PRESETS_DIR.exists():
        files = sorted(PRESETS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files:
            try:
                with open(f, encoding="utf-8") as file:
                    data = json.load(file)
                preset_name = data.get("presetName") or data.get("modelName") or "Unnamed preset"
                presets.append({
                    "id": f.stem,
                    "modelName": preset_name,
                    "presetName": preset_name,
                    "workspaceName": data.get("workspaceName", "Unavailable"),
                    "savedAt": data.get("savedAt", ""),
                    "batchCount": data.get("batchCount", 0),
                    "assignedTableCount": data.get("assignedTableCount", 0),
                })
            except Exception:
                pass
    return jsonify(presets)


@model_transformer.post("/api/presets")
def save_preset():
    payload = get_request_payload()
    saved = normalize_saved_batches_payload(payload)
    preset_id_base = re.sub(r"[^\w]+", "_", saved.get("presetName") or saved.get("modelName") or "preset").strip("_") or "preset"
    preset_id = preset_id_base
    try:
        PRESETS_DIR.mkdir(exist_ok=True)
        preset_path = PRESETS_DIR / f"{preset_id}.json"
        suffix = 2
        while preset_path.exists():
            preset_id = f"{preset_id_base}_{suffix}"
            preset_path = PRESETS_DIR / f"{preset_id}.json"
            suffix += 1
        saved["presetId"] = preset_id
        with open(preset_path, "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=4)
    except OSError as exc:
        return jsonify({"message": f"Could not save preset: {exc}"}), 500
    return jsonify({"presetId": preset_id})


@model_transformer.put("/api/presets/<preset_id>")
def update_preset(preset_id):
    if not re.fullmatch(r"[\w.-]+", preset_id):
        return jsonify({"message": "Invalid preset ID."}), 400

    preset_path = PRESETS_DIR / f"{preset_id}.json"
    if not preset_path.exists():
        return jsonify({"message": "Preset not found."}), 404

    payload = get_request_payload()
    if not isinstance(payload, dict):
        payload = {}
    payload["presetId"] = preset_id
    saved = normalize_saved_batches_payload(payload)
    saved["presetId"] = preset_id

    try:
        with open(preset_path, "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=4)
    except OSError as exc:
        return jsonify({"message": f"Could not update preset: {exc}"}), 500

    return jsonify({"presetId": preset_id})


@model_transformer.delete("/api/presets/<preset_id>")
def delete_preset(preset_id):
    if not re.fullmatch(r"[\w.-]+", preset_id):
        return jsonify({"message": "Invalid preset ID."}), 400

    preset_path = PRESETS_DIR / f"{preset_id}.json"
    if not preset_path.exists():
        return jsonify({"message": "Preset not found."}), 404

    try:
        preset_path.unlink()
    except OSError as exc:
        return jsonify({"message": f"Could not delete preset: {exc}"}), 500

    return jsonify({"message": "Preset deleted."})


@model_transformer.post("/api/login")
def login_with_azure_cli():
    try:
        account = get_azure_cli_account()
        power_bi_client = create_power_bi_client()
        workspaces = power_bi_client.get_workspaces()
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

    return jsonify({"user": build_user(account), "workspaces": workspaces})


@model_transformer.get("/api/workspaces/<workspace_id>/assets")
def workspace_assets(workspace_id):
    try:
        power_bi_client = create_power_bi_client()
        assets = power_bi_client.get_workspace_assets(workspace_id)
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
        power_bi_client = create_power_bi_client()
        fabric_client = create_fabric_client()
        details = get_power_bi_asset_details(power_bi_client, fabric_client, workspace_id, category, asset_id)
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