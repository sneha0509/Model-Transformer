import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from dotenv import load_dotenv

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential, CredentialUnavailableError
from flask import Flask, jsonify, render_template, request


model_transformer = Flask(__name__, static_folder="templates/static")
POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWER_BI_GROUPS_URL = "https://api.powerbi.com/v1.0/myorg/groups"
POWER_BI_GROUP_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
FABRIC_ITEM_DEFINITION_URL = "https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{item_id}/getDefinition"
AZURE_CLI_PATHS = (
    Path("C:/Program Files/Microsoft SDKs/Azure/CLI2/wbin/az.cmd"),
    Path("C:/Program Files/Microsoft SDKs/Azure/CLI2/wbin/az"),
    Path("C:/Program Files (x86)/Microsoft SDKs/Azure/CLI2/wbin/az.cmd"),
    Path("C:/Program Files (x86)/Microsoft SDKs/Azure/CLI2/wbin/az"),
)


def resolve_azure_cli_command():
    path_command = shutil.which("az") or shutil.which("az.cmd")
    candidates = [Path(path_command)] if path_command else []
    candidates.extend(AZURE_CLI_PATHS)

    for candidate in candidates:
        if candidate.exists():
            cli_folder = str(candidate.parent)
            path_parts = os.environ.get("PATH", "").split(os.pathsep)
            if cli_folder.lower() not in {part.lower() for part in path_parts if part}:
                os.environ["PATH"] = os.pathsep.join([cli_folder, os.environ.get("PATH", "")])
            return str(candidate)

    return None


def get_azure_cli_account():
    az_command = resolve_azure_cli_command()
    if not az_command:
        checked_paths = ", ".join(str(path) for path in AZURE_CLI_PATHS)
        raise RuntimeError(f"Azure CLI was not found. Checked PATH and these locations: {checked_paths}")

    try:
        result = subprocess.run(
            [az_command, "account", "show", "--output", "json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Azure CLI was not found. Install Azure CLI and run az login.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Azure CLI did not respond. Confirm az login works in this terminal.") from exc

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Run az login, then try again."
        raise RuntimeError(f"Azure CLI is not signed in. {message}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Azure CLI returned an unreadable account response.") from exc


def get_power_bi_access_token():
    if not resolve_azure_cli_command():
        raise RuntimeError("Azure CLI was not found. Install Azure CLI and run az login.")

    credential = AzureCliCredential(process_timeout=15)
    token = credential.get_token(POWER_BI_SCOPE)
    return token.token


def get_fabric_access_token():
    if not resolve_azure_cli_command():
        raise RuntimeError("Azure CLI was not found. Install Azure CLI and run az login.")

    credential = AzureCliCredential(process_timeout=15)
    token = credential.get_token(FABRIC_SCOPE)
    return token.token


def get_power_bi_workspaces(access_token):
    response = requests.get(
        POWER_BI_GROUPS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"$top": 5000},
        timeout=30,
    )
    response.raise_for_status()
    return [normalize_workspace(workspace) for workspace in response.json().get("value", [])]


def power_bi_get(access_token, url):
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("value", [])


def power_bi_get_json(access_token, url, params=None):
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def power_bi_get_json_or_none(access_token, url, params=None):
    try:
        return power_bi_get_json(access_token, url, params=params)
    except requests.HTTPError:
        return None


def get_power_bi_workspace_assets(access_token, workspace_id):
    workspace_url = POWER_BI_GROUP_URL.format(workspace_id=workspace_id)
    datasets = power_bi_get(access_token, f"{workspace_url}/datasets")
    reports = power_bi_get(access_token, f"{workspace_url}/reports")
    dataset_owners = {dataset.get("id"): dataset.get("configuredBy") for dataset in datasets if dataset.get("id")}

    return {
        "models": [normalize_model(dataset, workspace_id) for dataset in datasets],
        "reports": [normalize_report(report, dataset_owners) for report in reports],
    }


def get_power_bi_asset_details(access_token, fabric_token, workspace_id, category, asset_id):
    workspace_url = POWER_BI_GROUP_URL.format(workspace_id=workspace_id)
    normalized_category = category.lower()

    if normalized_category == "report":
        report = power_bi_get_json(access_token, f"{workspace_url}/reports/{asset_id}")
        dataset_id = report.get("datasetId")
        dataset = power_bi_get_json_or_none(access_token, f"{workspace_url}/datasets/{dataset_id}") if dataset_id else None
        semantic_metadata = get_semantic_model_metadata(access_token, fabric_token, workspace_id, dataset_id, dataset)
        return build_report_details(report, semantic_metadata)

    dataset = power_bi_get_json(access_token, f"{workspace_url}/datasets/{asset_id}")
    semantic_metadata = get_semantic_model_metadata(access_token, fabric_token, workspace_id, asset_id, dataset)
    return build_model_details(dataset, semantic_metadata, workspace_id)


def get_semantic_model_metadata(access_token, fabric_token, workspace_id, dataset_id, dataset):
    if not dataset_id:
        return empty_semantic_metadata(dataset)

    workspace_url = POWER_BI_GROUP_URL.format(workspace_id=workspace_id)
    refreshes = power_bi_get_json_or_none(access_token, f"{workspace_url}/datasets/{dataset_id}/refreshes", {"$top": 1})
    schedule = power_bi_get_json_or_none(access_token, f"{workspace_url}/datasets/{dataset_id}/refreshSchedule")
    datasources = power_bi_get_json_or_none(access_token, f"{workspace_url}/datasets/{dataset_id}/datasources")
    push_tables = power_bi_get_json_or_none(access_token, f"{workspace_url}/datasets/{dataset_id}/tables")
    definition_metadata = get_fabric_definition_metadata(fabric_token, workspace_id, dataset_id)

    tables = definition_metadata.get("tables") or normalize_push_tables(push_tables)
    return {
        "owner": (dataset or {}).get("configuredBy") or "Unavailable",
        "connectionMode": infer_connection_mode(dataset or {}, datasources, definition_metadata),
        "tables": tables,
        "tableCount": len(tables),
        "hasPartitions": any(table.get("partitionCount", 0) > 0 for table in tables),
        "partitionCount": sum(table.get("partitionCount", 0) for table in tables),
        "lastRefresh": normalize_last_refresh(refreshes),
        "refreshSchedule": normalize_refresh_schedule(schedule),
        "datasourceTypes": normalize_datasource_types(datasources),
        "metadataSource": definition_metadata.get("source") or ("Power BI tables API" if tables else "Power BI API"),
    }


def empty_semantic_metadata(dataset):
    return {
        "connectionMode": infer_connection_mode(dataset or {}, None, {}),
        "tables": [],
        "tableCount": 0,
        "hasPartitions": False,
        "partitionCount": 0,
        "lastRefresh": normalize_last_refresh(None),
        "refreshSchedule": normalize_refresh_schedule(None),
        "datasourceTypes": [],
        "owner": (dataset or {}).get("configuredBy") or "Unavailable",
        "metadataSource": "Unavailable",
    }


def get_fabric_definition_metadata(fabric_token, workspace_id, item_id):
    if not fabric_token or not item_id:
        return {}

    url = FABRIC_ITEM_DEFINITION_URL.format(workspace_id=workspace_id, item_id=item_id)
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {fabric_token}", "Content-Type": "application/json"},
            json={"format": "TMDL"},
            timeout=30,
        )
        if response.status_code == 202:
            result = get_fabric_operation_result(fabric_token, response.headers.get("Location"))
        else:
            response.raise_for_status()
            result = response.json()
    except requests.RequestException:
        return {}

    return parse_fabric_definition(result)


def get_fabric_operation_result(fabric_token, operation_url):
    if not operation_url:
        return None

    for attempt in range(3):
        response = requests.get(operation_url, headers={"Authorization": f"Bearer {fabric_token}"}, timeout=30)
        response.raise_for_status()
        operation = response.json()
        if operation.get("status") == "Succeeded":
            result_url = response.headers.get("Location")
            if not result_url:
                return None
            result_response = requests.get(result_url, headers={"Authorization": f"Bearer {fabric_token}"}, timeout=30)
            result_response.raise_for_status()
            return result_response.json()
        if operation.get("status") in {"Failed", "Cancelled"}:
            return None
        if attempt < 2:
            time.sleep(0.5)

    return None


def parse_fabric_definition(result):
    parts = ((result or {}).get("definition") or {}).get("parts") or []
    tables = []

    for part in parts:
        path = part.get("path", "")
        if not path.startswith("definition/tables/") or not path.endswith(".tmdl"):
            continue

        text = decode_fabric_part(part)
        if not text:
            continue

        table_name = path.rsplit("/", 1)[-1].removesuffix(".tmdl")
        partition_count = sum(1 for line in text.splitlines() if line.lstrip().startswith("partition "))
        tables.append({"name": table_name, "partitionCount": partition_count, "hasPartitions": partition_count > 0})

    return {"tables": tables, "source": "Fabric definition"} if tables else {}


def decode_fabric_part(part):
    payload = part.get("payload")
    if not payload:
        return ""

    try:
        return base64.b64decode(payload).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def normalize_push_tables(push_tables):
    return [
        {"name": table.get("name") or "Unnamed table", "partitionCount": 0, "hasPartitions": False}
        for table in (push_tables or {}).get("value", [])
    ]


def normalize_last_refresh(refreshes):
    latest = ((refreshes or {}).get("value") or [None])[0]
    if not latest:
        return {"status": "Unavailable", "time": "Unavailable", "type": "Unavailable"}

    return {
        "status": latest.get("status") or "Unavailable",
        "time": latest.get("endTime") or latest.get("startTime") or "Unavailable",
        "type": latest.get("refreshType") or "Unavailable",
    }


def normalize_refresh_schedule(schedule):
    if not schedule:
        return {"enabled": False, "times": [], "days": [], "timeZone": "Unavailable"}

    return {
        "enabled": bool(schedule.get("enabled")),
        "times": schedule.get("times") or [],
        "days": schedule.get("days") or [],
        "timeZone": schedule.get("localTimeZoneId") or "Unavailable",
    }


def normalize_datasource_types(datasources):
    values = (datasources or {}).get("value") or []
    return sorted({value.get("datasourceType") for value in values if value.get("datasourceType")})


def infer_connection_mode(dataset, datasources, definition_metadata):
    target_storage_mode = (dataset or {}).get("targetStorageMode")
    datasource_types = normalize_datasource_types(datasources)

    if target_storage_mode == "DirectQuery":
        return "DirectQuery"
    if "AnalysisServices" in datasource_types:
        return "Live connection"
    if target_storage_mode in {"Abf", "PremiumFiles"}:
        return "Import mode"
    if definition_metadata.get("tables"):
        return "Import mode"
    return target_storage_mode or "Unavailable"


def build_model_details(dataset, semantic_metadata, workspace_id):
    model = normalize_model(dataset, workspace_id)
    return {
        **model,
        "itemKind": "Semantic model",
        "connectionMode": semantic_metadata["connectionMode"],
        "tableCount": semantic_metadata["tableCount"],
        "partitionCount": semantic_metadata["partitionCount"],
        "hasPartitions": semantic_metadata["hasPartitions"],
        "tables": semantic_metadata["tables"],
        "lastRefresh": semantic_metadata["lastRefresh"],
        "refreshSchedule": semantic_metadata["refreshSchedule"],
        "datasourceTypes": semantic_metadata["datasourceTypes"],
        "owner": semantic_metadata["owner"],
        "metadataSource": semantic_metadata["metadataSource"],
    }


def build_report_details(report, semantic_metadata):
    normalized_report = normalize_report(report)
    return {
        **normalized_report,
        "itemKind": "Report",
        "owner": report.get("createdBy") or semantic_metadata["owner"],
        "connectionMode": "Live connection to semantic model" if report.get("datasetId") else "Unavailable",
        "semanticModelConnectionMode": semantic_metadata["connectionMode"],
        "tableCount": semantic_metadata["tableCount"],
        "partitionCount": semantic_metadata["partitionCount"],
        "hasPartitions": semantic_metadata["hasPartitions"],
        "tables": semantic_metadata["tables"],
        "lastRefresh": semantic_metadata["lastRefresh"],
        "refreshSchedule": semantic_metadata["refreshSchedule"],
        "datasourceTypes": semantic_metadata["datasourceTypes"],
        "metadataSource": semantic_metadata["metadataSource"],
    }


def normalize_workspace(workspace):
    workspace_type = workspace.get("type") or "Workspace"

    return {
        "id": workspace.get("id"),
        "name": workspace.get("name") or "Unnamed workspace",
        "type": workspace_type,
    }


def normalize_model(dataset, workspace_id):
    owner = dataset.get("configuredBy") or "Owner unavailable"
    refreshable = dataset.get("isRefreshable")
    model_id = dataset.get("id")
    dataset_url = dataset.get("webUrl", "")
    model_url = f"{dataset_url.rstrip('/')}/details" if dataset_url else ""
    if not model_url and model_id:
        model_url = f"https://app.powerbi.com/groups/{workspace_id}/datasets/{model_id}/details"

    return {
        "category": "Model",
        "id": model_id,
        "name": dataset.get("name") or "Unnamed model",
        "type": "Model",
        "owner": owner,
        "configuredBy": owner,
        "createdDate": dataset.get("createdDate", ""),
        "targetStorageMode": dataset.get("targetStorageMode", ""),
        "addRowsAPIEnabled": dataset.get("addRowsAPIEnabled"),
        "isEffectiveIdentityRequired": dataset.get("isEffectiveIdentityRequired"),
        "isEffectiveIdentityRolesRequired": dataset.get("isEffectiveIdentityRolesRequired"),
        "semanticModelUrl": model_url,
        "webUrl": model_url,
    }


def normalize_report(report, dataset_owners=None):
    report_type = report.get("reportType") or "Report"
    owner = report.get("createdBy") or (dataset_owners or {}).get(report.get("datasetId")) or "Owner unavailable"

    return {
        "category": "Report",
        "id": report.get("id"),
        "name": report.get("name") or "Unnamed report",
        "type": report_type,
        "owner": owner,
        "datasetId": report.get("datasetId", ""),
        "embedUrl": report.get("embedUrl", ""),
        "status": "Report",
        "webUrl": report.get("webUrl", ""),
    }


def get_initials(value):
    parts = [part for part in value.replace("@", " ").replace(".", " ").split() if part]
    if not parts:
        return "AZ"
    return "".join(part[0] for part in parts[:2]).upper()


def build_user(account):
    account_user = account.get("user") or {}
    name = account_user.get("name") or account.get("name") or "Azure CLI user"

    return {
        "name": name,
        "email": account_user.get("name", ""),
        "tenantId": account.get("tenantId", ""),
        "subscription": account.get("name", ""),
        "initials": get_initials(name),
    }


@model_transformer.route("/")
def index():
    return render_template("index.html")


def normalize_selected_tables_payload(payload):
    payload = payload if isinstance(payload, dict) else {}
    selected_tables = payload.get("selectedTables") if isinstance(payload, dict) else []
    if not isinstance(selected_tables, list):
        selected_tables = []

    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    user_name = str(user.get("name") or "Azure CLI user")
    user_email = str(user.get("email") or "")
    user_tenant_id = str(user.get("tenantId") or "")

    return {
        "workspaceName": str(payload.get("workspaceName") or "Unavailable"),
        "workspaceId": str(payload.get("workspaceId") or "Unavailable"),
        "modelName": str(payload.get("modelName") or "Unavailable"),
        "modelId": str(payload.get("modelId") or "Unavailable"),
        "selectedTables": [str(table).strip() for table in selected_tables if str(table).strip()],
        "user": {
            "name": user_name,
            "email": user_email,
            "tenantId": user_tenant_id,
            "subscription": str(user.get("subscription") or ""),
            "initials": str(user.get("initials") or get_initials(user_name)),
            "detail": user_email or user_tenant_id or "Signed in with Azure CLI",
        },
    }


def normalize_saved_batches_payload(payload):
    payload = payload if isinstance(payload, dict) else {}
    batches = payload.get("batches") if isinstance(payload.get("batches"), list) else []
    batch_creation_settings = payload.get("batchCreationSettings") if isinstance(payload.get("batchCreationSettings"), dict) else {}

    normalized_batches = []
    for index, batch in enumerate(batches, start=1):
        batch = batch if isinstance(batch, dict) else {}
        tables = batch.get("tables") if isinstance(batch.get("tables"), list) else []
        normalized_batches.append({
            "name": str(batch.get("name") or f"Batch {index}"),
            "tables": [str(table).strip() for table in tables if str(table).strip()],
        })

    if not normalized_batches:
        normalized_batches.append({"name": "Batch 1", "tables": []})

    selected_tables = payload.get("selectedTables") if isinstance(payload.get("selectedTables"), list) else []
    unassigned_tables = payload.get("unassignedTables") if isinstance(payload.get("unassignedTables"), list) else []
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    user_name = str(user.get("name") or "Azure CLI user")
    timeout_minutes = batch_creation_settings.get("timeoutMinutes")
    max_parallelism = batch_creation_settings.get("maxParallelism")
    retry_count = batch_creation_settings.get("retryCount")
    commit_mode = str(batch_creation_settings.get("commitMode") or "transactional")

    try:
        timeout_minutes = int(timeout_minutes)
    except (TypeError, ValueError):
        timeout_minutes = 30
    try:
        max_parallelism = int(max_parallelism)
    except (TypeError, ValueError):
        max_parallelism = 4
    try:
        retry_count = int(retry_count)
    except (TypeError, ValueError):
        retry_count = 3

    if timeout_minutes < 1:
        timeout_minutes = 30
    if max_parallelism < 1:
        max_parallelism = 4
    if retry_count < 0:
        retry_count = 3
    if commit_mode not in {"transactional", "partial-batch"}:
        commit_mode = "transactional"

    saved = {
        "savedAt": str(payload.get("savedAt") or "Unavailable"),
        "workspaceName": str(payload.get("workspaceName") or "Unavailable"),
        "workspaceId": str(payload.get("workspaceId") or "Unavailable"),
        "modelName": str(payload.get("modelName") or "Unavailable"),
        "modelId": str(payload.get("modelId") or "Unavailable"),
        "selectedTables": [str(table).strip() for table in selected_tables if str(table).strip()],
        "unassignedTables": [str(table).strip() for table in unassigned_tables if str(table).strip()],
        "batchCreationSettings": {
            "timeoutMinutes": timeout_minutes,
            "commitMode": commit_mode,
            "maxParallelism": max_parallelism,
            "retryCount": retry_count,
        },
        "batches": normalized_batches,
        "user": {
            "name": user_name,
            "email": str(user.get("email") or ""),
            "tenantId": str(user.get("tenantId") or ""),
            "subscription": str(user.get("subscription") or ""),
        },
    }
    saved["batchCount"] = len(saved["batches"])
    saved["assignedTableCount"] = sum(len(batch["tables"]) for batch in saved["batches"])
    saved["unassignedTableCount"] = len(saved["unassignedTables"])
    saved["selectedTableCount"] = len(saved["selectedTables"])
    return saved


@model_transformer.post("/selected-tables")
def selected_tables():
    payload = request.get_json(silent=True) if request.is_json else None
    if payload is None:
        try:
            payload = json.loads(request.form.get("payload", "{}"))
        except json.JSONDecodeError:
            payload = {}

    selection = normalize_selected_tables_payload(payload)
    return render_template("selected_tables.html", selection=selection)


@model_transformer.post("/saved-batches")
def saved_batches():
    payload = request.get_json(silent=True) if request.is_json else None
    if payload is None:
        try:
            payload = json.loads(request.form.get("payload", "{}"))
        except json.JSONDecodeError:
            payload = {}

    saved = normalize_saved_batches_payload(payload)
    saved_json = json.dumps(saved, indent=4)
    return render_template("saved_batches.html", saved=saved, saved_json=saved_json)


@model_transformer.post("/api/login")
def login_with_azure_cli():
    try:
        account = get_azure_cli_account()
        token = get_power_bi_access_token()
        workspaces = get_power_bi_workspaces(token)
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
        token = get_power_bi_access_token()
        assets = get_power_bi_workspace_assets(token, workspace_id)
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
        token = get_power_bi_access_token()
        fabric_token = get_fabric_access_token()
        details = get_power_bi_asset_details(token, fabric_token, workspace_id, category, asset_id)
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
