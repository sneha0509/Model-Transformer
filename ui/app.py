import json
import os

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

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
def index():
    return render_template("index.html")


@model_transformer.post("/selected-tables")
def selected_tables():
    selection = normalize_selected_tables_payload(get_request_payload())
    return render_template("selected_tables.html", selection=selection)


@model_transformer.post("/saved-batches")
def saved_batches():
    saved = normalize_saved_batches_payload(get_request_payload())
    saved_json = json.dumps(saved, indent=4)
    return render_template("saved_batches.html", saved=saved, saved_json=saved_json)


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