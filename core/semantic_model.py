"""Semantic model detail assembly across Power BI and Fabric metadata sources."""

import base64

import requests

from core.normalization import (
    infer_connection_mode,
    normalize_datasource_types,
    normalize_last_refresh,
    normalize_model,
    normalize_push_tables,
    normalize_refresh_schedule,
    normalize_report,
)


VISIBLE_TABLES_DAX_QUERY = """
EVALUATE
SELECTCOLUMNS(
    FILTER(
        INFO.VIEW.TABLES(),
        [IsPrivate] = FALSE()
            && [ShowAsVariationOnly] = FALSE()
    ),
    "Name", [Name],
    "IsHidden", [IsHidden]
)
ORDER BY [Name]
""".strip()


RELATIONSHIP_METADATA_DAX_QUERY = """
EVALUATE
SELECTCOLUMNS(
    INFO.VIEW.RELATIONSHIPS(),
    "FromTable", [FromTable],
    "FromColumn", [FromColumn],
    "FromCardinality", [FromCardinality],
    "ToTable", [ToTable],
    "ToColumn", [ToColumn],
    "ToCardinality", [ToCardinality],
    "IsActive", [IsActive]
)
ORDER BY [FromTable], [ToTable]
""".strip()


def get_power_bi_asset_details(power_bi_client, fabric_client, workspace_id, category, asset_id):
    """Fetch and enrich details for either a report or a semantic model asset."""
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


def get_advanced_table_metadata(power_bi_client, fabric_client, workspace_id, dataset_id, selected_tables, all_tables=None):
    """Return row counts and definition metadata for selected semantic model tables."""
    selected_table_names = normalize_table_name_list(selected_tables)
    visible_table_names = normalize_table_name_list(all_tables)

    if not selected_table_names:
        return {"tables": []}

    definition_metadata = get_fabric_definition_metadata(fabric_client, workspace_id, dataset_id)
    definition_tables = {
        table.get("name"): table
        for table in definition_metadata.get("tables", [])
        if table.get("name")
    }
    row_counts = get_table_row_counts(power_bi_client, workspace_id, dataset_id, selected_table_names)
    relationships_by_table = get_table_relationship_metadata(
        power_bi_client,
        workspace_id,
        dataset_id,
        selected_table_names,
        visible_table_names or selected_table_names,
    )

    tables = []
    for table_name in selected_table_names:
        definition_table = definition_tables.get(table_name) or {}
        relationship_summary = relationships_by_table.get(table_name) or {"relationshipCount": 0, "relatedTables": []}
        tables.append({
            "name": table_name,
            "rowCount": row_counts.get(table_name),
            "columnCount": definition_table.get("columnCount", 0),
            "partitionCount": definition_table.get("partitionCount", 0),
            "relationshipCount": relationship_summary.get("relationshipCount", 0),
            "relatedTables": relationship_summary.get("relatedTables", []),
        })

    return {"tables": tables}


def normalize_table_name_list(tables):
    """Return unique non-empty table names from UI table payloads."""
    table_names = []
    for table in tables if isinstance(tables, list) else []:
        table_name = str(table.get("name") if isinstance(table, dict) else table or "").strip()
        if table_name and table_name not in table_names:
            table_names.append(table_name)

    return table_names


def get_table_row_counts(power_bi_client, workspace_id, dataset_id, table_names):
    """Run isolated DAX row-count queries so one inaccessible table does not hide all results."""
    row_counts = {}

    for table_name in table_names:
        query = f"EVALUATE ROW(\"RowCount\", COUNTROWS({format_dax_table_name(table_name)}))"
        result = power_bi_client.execute_dax_query(workspace_id, dataset_id, query)
        rows = (((result or {}).get("results") or [{}])[0].get("tables") or [{}])[0].get("rows") or []
        if not rows:
            row_counts[table_name] = None
            continue

        row_count = get_dax_row_value(rows[0], "RowCount")
        try:
            row_counts[table_name] = int(float(row_count))
        except (TypeError, ValueError):
            row_counts[table_name] = None

    return row_counts


def get_table_relationship_metadata(power_bi_client, workspace_id, dataset_id, selected_table_names, visible_table_names=None):
    """Return relationship counts and related tables for selected semantic model tables."""
    result = power_bi_client.execute_dax_query(workspace_id, dataset_id, RELATIONSHIP_METADATA_DAX_QUERY)
    rows = get_dax_result_rows(result)
    selected_lookup = {normalize_table_lookup_key(table_name): table_name for table_name in selected_table_names}
    visible_lookup = {normalize_table_lookup_key(table_name) for table_name in (visible_table_names or [])}
    relationships_by_table = {table_name: {} for table_name in selected_table_names}

    for row in rows:
        from_table = get_dax_row_value(row, "FromTable")
        to_table = get_dax_row_value(row, "ToTable")
        from_selected = selected_lookup.get(normalize_table_lookup_key(from_table))
        to_selected = selected_lookup.get(normalize_table_lookup_key(to_table))

        if from_selected and to_table and table_is_visible(to_table, visible_lookup):
            add_related_table(relationships_by_table[from_selected], from_table, to_table, row)
        if to_selected and from_table and table_is_visible(from_table, visible_lookup):
            add_related_table(relationships_by_table[to_selected], to_table, from_table, row)

    summaries = {}
    for table_name, related_tables in relationships_by_table.items():
        related_table_list = sorted(related_tables.values(), key=lambda item: item["name"].casefold())
        relationship_count = sum(len(related_table.get("relationships", [])) for related_table in related_table_list)
        summaries[table_name] = {
            "relationshipCount": relationship_count,
            "relatedTables": related_table_list,
        }

    return summaries


def table_is_visible(table_name, visible_lookup):
    """Return whether a related table belongs to the original visible table list."""
    return not visible_lookup or normalize_table_lookup_key(table_name) in visible_lookup


def add_related_table(related_tables, source_table, related_table, row):
    """Append a relationship row to a related-table summary."""
    related_table_name = str(related_table or "").strip()
    if not related_table_name:
        return

    related_summary = related_tables.setdefault(related_table_name, {
        "name": related_table_name,
        "relationships": [],
    })
    related_summary["relationships"].append({
        "fromTable": get_dax_row_value(row, "FromTable"),
        "fromColumn": get_dax_row_value(row, "FromColumn"),
        "fromCardinality": get_dax_row_value(row, "FromCardinality"),
        "toTable": get_dax_row_value(row, "ToTable"),
        "toColumn": get_dax_row_value(row, "ToColumn"),
        "toCardinality": get_dax_row_value(row, "ToCardinality"),
        "isActive": get_dax_bool_value(row, "IsActive"),
        "direction": "from" if source_table == get_dax_row_value(row, "FromTable") else "to",
    })


def get_dax_result_rows(result):
    """Return rows from the first table in a parsed executeQueries response."""
    return (((result or {}).get("results") or [{}])[0].get("tables") or [{}])[0].get("rows") or []


def normalize_table_lookup_key(table_name):
    """Normalize table names for case-insensitive relationship matching."""
    return str(table_name or "").strip().casefold()


def format_dax_table_name(table_name):
    """Format a semantic model table name for use in a quoted DAX table reference."""
    escaped_name = str(table_name or "").replace("'", "''")
    return f"'{escaped_name}'"


def get_semantic_model_metadata(power_bi_client, fabric_client, workspace_id, dataset_id, dataset):
    """Gather semantic model metadata from Power BI endpoints and Fabric definitions."""
    if not dataset_id:
        return empty_semantic_metadata(dataset)

    refreshes = power_bi_client.get_dataset_refreshes(workspace_id, dataset_id)
    schedule = power_bi_client.get_dataset_refresh_schedule(workspace_id, dataset_id)
    datasources = power_bi_client.get_dataset_datasources(workspace_id, dataset_id)
    dax_tables = get_dax_table_metadata(power_bi_client, workspace_id, dataset_id)
    push_tables = power_bi_client.get_dataset_tables(workspace_id, dataset_id)
    definition_metadata = get_fabric_definition_metadata(fabric_client, workspace_id, dataset_id)

    tables = (
        enrich_dax_table_metadata(dax_tables, definition_metadata)
        or definition_metadata.get("tables")
        or normalize_push_tables(push_tables)
    )
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
        "metadataSource": get_metadata_source(dax_tables, definition_metadata, tables),
    }


def get_dax_table_metadata(power_bi_client, workspace_id, dataset_id):
    """Return visible semantic model tables from a DAX metadata query."""
    result = power_bi_client.execute_dax_query(workspace_id, dataset_id, VISIBLE_TABLES_DAX_QUERY)
    rows = (((result or {}).get("results") or [{}])[0].get("tables") or [{}])[0].get("rows") or []
    tables = []
    seen_names = set()

    for row in rows:
        table_name = get_dax_row_value(row, "Name")
        if not table_name or table_name in seen_names:
            continue

        seen_names.add(table_name)
        tables.append({
            "name": table_name,
            "isHidden": get_dax_bool_value(row, "IsHidden"),
            "columnCount": 0,
            "partitionCount": 0,
            "hasPartitions": False,
        })

    return tables


def get_dax_row_value(row, column_name):
    """Read a DAX row value by alias, tolerating qualified column names."""
    if not isinstance(row, dict):
        return ""

    value = row.get(column_name)
    if value is None:
        value = row.get(f"[{column_name}]")
    if value is None:
        suffix = f"[{column_name}]"
        value = next((candidate for key, candidate in row.items() if str(key).endswith(suffix)), "")

    return str(value or "").strip()


def get_dax_bool_value(row, column_name):
    """Read a DAX row value as a boolean."""
    value = get_dax_row_value(row, column_name)
    return value.lower() == "true"


def enrich_dax_table_metadata(dax_tables, definition_metadata):
    """Add partition metadata to DAX-selected tables when Fabric has matching table details."""
    definition_tables = {
        table.get("name"): table
        for table in definition_metadata.get("tables", [])
        if table.get("name")
    }

    enriched_tables = []
    for table in dax_tables:
        definition_table = definition_tables.get(table.get("name")) or {}
        column_count = definition_table.get("columnCount", table.get("columnCount", 0))
        partition_count = definition_table.get("partitionCount", table.get("partitionCount", 0))
        enriched_tables.append({
            **table,
            "columnCount": column_count,
            "partitionCount": partition_count,
            "hasPartitions": partition_count > 0,
        })

    return enriched_tables


def get_metadata_source(dax_tables, definition_metadata, tables):
    """Describe which metadata source supplied the table list."""
    if dax_tables:
        return "Power BI DAX query"
    if definition_metadata.get("tables"):
        return definition_metadata.get("source") or "Fabric definition"
    if tables:
        return "Power BI tables API"
    return "Power BI API"


def empty_semantic_metadata(dataset):
    """Return the metadata shape used when a report has no backing dataset."""
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
    """Retrieve and parse a Fabric item definition, returning empty metadata on API errors."""
    try:
        result = fabric_client.get_item_definition(workspace_id, item_id)
    except requests.RequestException:
        return {}

    return parse_fabric_definition(result)


def parse_fabric_definition(result):
    """Parse Fabric TMDL definition parts into table and partition metadata."""
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
        column_count = sum(1 for line in text.splitlines() if line.lstrip().startswith("column "))
        partition_count = sum(1 for line in text.splitlines() if line.lstrip().startswith("partition "))
        tables.append({
            "name": table_name,
            "columnCount": column_count,
            "partitionCount": partition_count,
            "hasPartitions": partition_count > 0,
        })

    return {"tables": tables, "source": "Fabric definition"} if tables else {}


def decode_fabric_part(part):
    """Decode a base64-encoded Fabric definition part into text."""
    payload = part.get("payload")
    if not payload:
        return ""

    try:
        return base64.b64decode(payload).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def build_model_details(dataset, semantic_metadata, workspace_id):
    """Combine normalized dataset fields with enriched semantic model metadata."""
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
    """Combine normalized report fields with metadata from its backing semantic model."""
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