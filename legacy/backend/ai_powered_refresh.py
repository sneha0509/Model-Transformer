"""Build AI-powered refresh metadata from the advanced table data workflow."""

import base64
import binascii
import json
import os
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from shared.paths import PROJECT_ROOT
from legacy.backend.normalization import normalize_lineage_items
from legacy.backend.orchestrator import (
    get_advanced_table_details,
    get_semantic_model_definition,
)


DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "output" / "ai_refresh_metadata"
TMDL_OBJECT_NAME_PATTERN = r"(?:'(?:''|[^'])*'|[^\s=]+)"
TMDL_TABLE_PATTERN = re.compile(
    rf"^[ \t]*table[ \t]+(?P<name>{TMDL_OBJECT_NAME_PATTERN})[ \t]*$"
)
TMDL_PARTITION_PATTERN = re.compile(
    rf"^(?P<indent>[ \t]*)partition[ \t]+(?P<name>{TMDL_OBJECT_NAME_PATTERN})[ \t]*=[ \t]*\S+.*$"
)


def get_ai_refresh_metadata(
    workspace_name,
    workspace_id,
    model_name,
    model_id,
    selected_tables,
    all_tables=None,
):
    """Fetch and format selected-table metadata for AI-powered refresh planning."""
    advanced_data = get_advanced_table_details(
        workspace_id,
        model_id,
        selected_tables,
        all_tables,
    )
    lineage = normalize_lineage_items(
        advanced_data.get("lineage") if isinstance(advanced_data, dict) else []
    )
    model_definition = None
    if _requires_model_definition(lineage):
        model_definition = get_semantic_model_definition(workspace_id, model_id)

    metadata = build_ai_refresh_metadata(
        workspace_name,
        workspace_id,
        model_name,
        model_id,
        advanced_data,
        model_definition=model_definition,
    )
    save_ai_refresh_metadata(metadata)
    return metadata


def build_ai_refresh_metadata(
    workspace_name,
    workspace_id,
    model_name,
    model_id,
    advanced_data,
    model_definition=None,
):
    """Convert advanced table data to the AI-powered refresh JSON contract."""
    tables = advanced_data.get("tables", []) if isinstance(advanced_data, dict) else []
    selected_table_names = [
        str(table.get("name") or "").strip()
        for table in tables
        if isinstance(table, dict) and str(table.get("name") or "").strip()
    ]

    metadata = {
        "workspaceName": str(workspace_name or ""),
        "workspaceId": str(workspace_id or ""),
        "modelName": str(model_name or ""),
        "modelId": str(model_id or ""),
        "lineage": normalize_lineage_items(
            advanced_data.get("lineage") if isinstance(advanced_data, dict) else []
        ),
        "selectedTables": [
            {
                "tableName": str(table.get("name") or ""),
                "rows": _integer_or_zero(table.get("rowCount")),
                "columns": _integer_or_zero(table.get("columnCount")),
                "partitionCount": _integer_or_zero(table.get("partitionCount")),
                "relationCount": len(_flatten_relationships(table)),
                "relationships": _flatten_relationships(table),
            }
            for table in tables
            if isinstance(table, dict) and table.get("name")
        ],
    }
    if model_definition is not None:
        metadata["modelDefinition"] = {
            "format": "TMDL",
            "partitionQueries": extract_tmdl_partition_queries(
                model_definition,
                selected_table_names,
            ),
        }

    return metadata


def extract_tmdl_partition_queries(model_definition, selected_table_names):
    """Return TMDL partition blocks only for the selected model tables."""
    definition = (
        model_definition.get("definition")
        if isinstance(model_definition, dict)
        else None
    )
    parts = definition.get("parts") if isinstance(definition, dict) else []
    partition_queries = []
    selected_table_lookup = {
        str(table_name or "").strip().casefold()
        for table_name in selected_table_names
        if str(table_name or "").strip()
    }

    if not selected_table_lookup:
        return partition_queries

    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue

        path = str(part.get("path") or "")
        if not path.startswith("definition/tables/") or not path.endswith(".tmdl"):
            continue

        tmdl = _decode_tmdl_payload(part, path)
        table_name = _get_tmdl_table_name(tmdl, path)
        if table_name.casefold() not in selected_table_lookup:
            continue
        partition_queries.extend(_extract_table_partition_queries(table_name, tmdl))

    return partition_queries


def _requires_model_definition(lineage):
    return not lineage or any(
        str(item.get("type") or "").strip().casefold() == "sqlanalyticsendpoint"
        for item in lineage
        if isinstance(item, dict)
    )


def _decode_tmdl_payload(part, path):
    payload = part.get("payload")
    if not isinstance(payload, str) or not payload:
        raise ValueError(f"Fabric model definition part has no payload: {path}.")

    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"Fabric returned an invalid TMDL payload for {path}.") from exc


def _get_tmdl_table_name(tmdl, path):
    for line in tmdl.splitlines():
        match = TMDL_TABLE_PATTERN.match(line)
        if match:
            return _unquote_tmdl_name(match.group("name"))
    return PurePosixPath(path).stem


def _extract_table_partition_queries(table_name, tmdl):
    lines = tmdl.splitlines()
    partition_queries = []

    for index, line in enumerate(lines):
        match = TMDL_PARTITION_PATTERN.match(line)
        if not match:
            continue

        header_indent = len(match.group("indent").expandtabs(4))
        block = [line]
        for candidate in lines[index + 1:]:
            if candidate.strip():
                candidate_indent = len(
                    candidate[:len(candidate) - len(candidate.lstrip(" \t"))].expandtabs(4)
                )
                if candidate_indent <= header_indent:
                    break
            block.append(candidate)

        while block and not block[-1].strip():
            block.pop()

        partition_queries.append({
            "tableName": table_name,
            "partitionName": _unquote_tmdl_name(match.group("name")),
            "query": textwrap.dedent("\n".join(block)).strip(),
        })

    return partition_queries


def _unquote_tmdl_name(name):
    value = str(name or "").strip()
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def save_ai_refresh_metadata(metadata, output_directory=None):
    """Create the output directory when needed and save metadata as JSON."""
    directory = Path(output_directory) if output_directory else DEFAULT_OUTPUT_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)

    model_name = str(metadata.get("modelName") or metadata.get("modelId") or "model")
    safe_model_name = re.sub(r"[^A-Za-z0-9._-]+", "_", model_name).strip("._") or "model"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = directory / f"{safe_model_name}_{timestamp}.json"

    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(metadata, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")

    return output_path


def _flatten_relationships(table):
    relationships = []

    for related_table in table.get("relatedTables", []):
        if not isinstance(related_table, dict):
            continue

        for relationship in related_table.get("relationships", []):
            if not isinstance(relationship, dict):
                continue

            relationships.append({
                "fromTable": str(relationship.get("fromTable") or ""),
                "fromColumn": str(relationship.get("fromColumn") or ""),
                "fromCardinality": str(relationship.get("fromCardinality") or ""),
                "toTable": str(relationship.get("toTable") or ""),
                "toColumn": str(relationship.get("toColumn") or ""),
                "toCardinality": str(relationship.get("toCardinality") or ""),
                "isActive": bool(relationship.get("isActive")),
                "direction": _normalize_direction(relationship.get("direction")),
            })

    return relationships


def _integer_or_zero(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_direction(value):
    normalized_value = str(value or "").strip().casefold()
    if normalized_value in {"both", "bidirectional", "bothdirections"}:
        return "Both"
    return "Single"
