def get_initials(value):
    parts = [part for part in value.replace("@", " ").replace(".", " ").split() if part]
    if not parts:
        return "AZ"
    return "".join(part[0] for part in parts[:2]).upper()


def normalize_workspace(workspace):
    workspace_type = workspace.get("type") or "Workspace"

    return {
        "id": workspace.get("id"),
        "name": workspace.get("name") or "Unnamed workspace",
        "type": workspace_type,
    }


def normalize_model(dataset, workspace_id):
    owner = dataset.get("configuredBy") or "Owner unavailable"
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