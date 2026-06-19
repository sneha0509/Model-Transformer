import base64

import requests

try:
    from .shared_Scripts import (
        infer_connection_mode,
        normalize_datasource_types,
        normalize_last_refresh,
        normalize_model,
        normalize_push_tables,
        normalize_refresh_schedule,
        normalize_report,
    )
except ImportError:
    from shared_Scripts import (
        infer_connection_mode,
        normalize_datasource_types,
        normalize_last_refresh,
        normalize_model,
        normalize_push_tables,
        normalize_refresh_schedule,
        normalize_report,
    )


def get_power_bi_asset_details(power_bi_client, fabric_client, workspace_id, category, asset_id):
    normalized_category = category.lower()

    if normalized_category == "report":
        report = power_bi_client.get_report(workspace_id, asset_id)
        dataset_id = report.get("datasetId")
        dataset = power_bi_client.get_dataset(workspace_id, dataset_id) if dataset_id else None
        semantic_metadata = get_semantic_model_metadata(power_bi_client, fabric_client, workspace_id, dataset_id, dataset)
        return build_report_details(report, semantic_metadata)

    dataset = power_bi_client.get_dataset(workspace_id, asset_id)
    semantic_metadata = get_semantic_model_metadata(power_bi_client, fabric_client, workspace_id, asset_id, dataset)
    return build_model_details(dataset, semantic_metadata, workspace_id)


def get_semantic_model_metadata(power_bi_client, fabric_client, workspace_id, dataset_id, dataset):
    if not dataset_id:
        return empty_semantic_metadata(dataset)

    refreshes = power_bi_client.get_dataset_refreshes(workspace_id, dataset_id)
    schedule = power_bi_client.get_dataset_refresh_schedule(workspace_id, dataset_id)
    datasources = power_bi_client.get_dataset_datasources(workspace_id, dataset_id)
    push_tables = power_bi_client.get_dataset_tables(workspace_id, dataset_id)
    definition_metadata = get_fabric_definition_metadata(fabric_client, workspace_id, dataset_id)

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


def get_fabric_definition_metadata(fabric_client, workspace_id, item_id):
    try:
        result = fabric_client.get_item_definition(workspace_id, item_id)
    except requests.RequestException:
        return {}

    return parse_fabric_definition(result)


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