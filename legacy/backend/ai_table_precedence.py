"""Generate table refresh precedence from saved semantic-model metadata."""

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from copilot import CopilotClient
from copilot.session_events import AssistantMessageData
from dotenv import load_dotenv
from shared.paths import PROJECT_ROOT


DEFAULT_INPUT_DIRECTORY = PROJECT_ROOT / "output" / "ai_refresh_metadata"
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "output" / "ai_table_precedence"
DEFAULT_COPILOT_MODEL = "gpt-5"

SYSTEM_PROMPT = """
You plan semantic-model table refresh precedence.

Return every selected table exactly once, ordered from the table that should be
refreshed first to the table that should be refreshed last.

Rules:
1. Use only relationships whose fromTable or toTable matches a selected table.
2. For an active One-to-Many relationship, place the table on the One side
   before the table on the Many side when both tables are selected.
3. Use direction to understand propagation. For Both-direction relationships,
   still prefer the One side first, but account for dependency cycles.
4. Inactive relationships are context only and must not create a hard
   dependency.
5. Break cycles or otherwise equal precedence by preferring the smaller refresh
   footprint: fewer partitions, then fewer rows, then fewer columns.
6. Do not invent table names, relationships, or metadata.
7. Give a concise reason for each position.
""".strip()


def generate_table_precedence(
    metadata_path=None,
    output_directory=None,
    model=None,
):
    """Ask GitHub Copilot to order tables and save the validated result."""
    source_path = (
        Path(metadata_path)
        if metadata_path
        else find_latest_metadata_file(DEFAULT_INPUT_DIRECTORY)
    )
    metadata = load_metadata(source_path)
    llm_order = request_table_order(metadata, model=model)
    result = build_table_precedence_output(metadata, llm_order, source_path.name)
    output_path = save_table_precedence(result, output_directory)
    return result, output_path


def find_latest_metadata_file(input_directory):
    """Return the most recently modified JSON metadata file."""
    directory = Path(input_directory)
    metadata_files = list(directory.glob("*.json")) if directory.is_dir() else []
    if not metadata_files:
        raise FileNotFoundError(f"No AI refresh metadata JSON files found in {directory}.")
    return max(metadata_files, key=lambda path: path.stat().st_mtime_ns)


def load_metadata(metadata_path):
    """Load and validate the saved AI refresh metadata document."""
    path = Path(metadata_path)
    with path.open(encoding="utf-8") as metadata_file:
        metadata = json.load(metadata_file)

    if not isinstance(metadata, dict):
        raise ValueError("AI refresh metadata must be a JSON object.")

    selected_tables = metadata.get("selectedTables")
    if not isinstance(selected_tables, list) or not selected_tables:
        raise ValueError("AI refresh metadata must contain at least one selected table.")

    table_names = []
    for table in selected_tables:
        table_name = str(table.get("tableName") or "").strip() if isinstance(table, dict) else ""
        if not table_name:
            raise ValueError("Every selected table must have a non-empty tableName.")
        if table_name in table_names:
            raise ValueError(f"Duplicate selected table in metadata: {table_name}.")
        table_names.append(table_name)

    return metadata


def request_table_order(metadata, model=None, client_factory=CopilotClient):
    """Request a schema-constrained table order from GitHub Copilot."""
    model_name = str(model or os.getenv("COPILOT_MODEL") or DEFAULT_COPILOT_MODEL).strip()
    if not model_name:
        raise RuntimeError("COPILOT_MODEL cannot be empty.")

    table_names = [table["tableName"] for table in metadata["selectedTables"]]
    response_schema = {
        "type": "object",
        "properties": {
            "orderedTables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tableName": {
                            "type": "string",
                            "enum": table_names,
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["tableName", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["orderedTables"],
        "additionalProperties": False,
    }
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        "Determine table refresh precedence for this metadata:\n"
        f"{json.dumps(_metadata_for_copilot(metadata), ensure_ascii=False)}"
    )
    response_content = asyncio.run(
        _request_table_order_with_copilot(
            prompt,
            response_schema,
            model_name,
            client_factory,
        )
    )

    try:
        return json.loads(response_content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub Copilot returned a non-JSON precedence response.") from exc


async def _request_table_order_with_copilot(
    prompt,
    response_schema,
    model_name,
    client_factory,
):
    """Use signed-in Copilot credentials and return structured response content."""
    async with client_factory(
        use_logged_in_user=True,
        working_directory=str(PROJECT_ROOT),
    ) as client:
        async with await client.create_session(
            model=model_name,
            skip_custom_instructions=True,
            enable_skills=False,
        ) as session:
            response = await session.send_and_wait(
                prompt,
                response_schema=response_schema,
                timeout=120,
            )

    if response is None or not isinstance(response.data, AssistantMessageData):
        raise RuntimeError("GitHub Copilot did not return an assistant response.")
    return response.data.content


def build_table_precedence_output(metadata, llm_order, source_file_name):
    """Validate the Copilot order and enrich it with trusted source counts."""
    ordered_tables = llm_order.get("orderedTables") if isinstance(llm_order, dict) else None
    if not isinstance(ordered_tables, list):
        raise ValueError("GitHub Copilot output must contain an orderedTables array.")

    source_tables = {
        table["tableName"]: table
        for table in metadata["selectedTables"]
    }
    ordered_names = [
        str(table.get("tableName") or "").strip()
        for table in ordered_tables
        if isinstance(table, dict)
    ]
    expected_names = set(source_tables)

    if len(ordered_names) != len(ordered_tables) or len(ordered_names) != len(source_tables):
        raise ValueError("GitHub Copilot must return every selected table exactly once.")
    if len(set(ordered_names)) != len(ordered_names):
        raise ValueError("GitHub Copilot returned a duplicate table.")
    if set(ordered_names) != expected_names:
        missing = sorted(expected_names - set(ordered_names))
        unexpected = sorted(set(ordered_names) - expected_names)
        raise ValueError(
            "GitHub Copilot returned an invalid table set. "
            f"Missing: {missing}; unexpected: {unexpected}."
        )

    reasons = {
        table["tableName"]: str(table.get("reason") or "").strip()
        for table in ordered_tables
    }
    table_precedence = []
    for index, table_name in enumerate(ordered_names, start=1):
        table_output = {
            "precedence": index,
            "tableName": table_name,
            "rows": _integer_or_zero(source_tables[table_name].get("rows")),
            "columns": _integer_or_zero(source_tables[table_name].get("columns")),
            "partitionCount": _integer_or_zero(
                source_tables[table_name].get("partitionCount")
            ),
        }
        table_model_definition = _get_table_model_definition(metadata, table_name)
        if table_model_definition is not None:
            table_output["modelDefinition"] = table_model_definition
        table_output["reason"] = reasons[table_name]
        table_precedence.append(table_output)

    return {
        "workspaceName": str(metadata.get("workspaceName") or ""),
        "workspaceId": str(metadata.get("workspaceId") or ""),
        "modelName": str(metadata.get("modelName") or ""),
        "modelId": str(metadata.get("modelId") or ""),
        "sourceMetadataFile": str(source_file_name),
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "tablePrecedence": table_precedence,
    }


def _metadata_for_copilot(metadata):
    """Return precedence inputs without source partition TMDL."""
    return {
        key: value
        for key, value in metadata.items()
        if key != "modelDefinition"
    }


def _get_table_model_definition(metadata, table_name):
    """Copy source partition TMDL for one ordered table without LLM involvement."""
    model_definition = metadata.get("modelDefinition")
    if not isinstance(model_definition, dict):
        return None

    partition_queries = model_definition.get("partitionQueries")
    matching_partitions = []
    for partition in partition_queries if isinstance(partition_queries, list) else []:
        if not isinstance(partition, dict):
            continue
        if (
            str(partition.get("tableName") or "").strip().casefold()
            != table_name.casefold()
        ):
            continue

        matching_partitions.append({
            "partitionName": partition.get("partitionName"),
            "query": partition.get("query"),
        })

    return {
        "format": str(model_definition.get("format") or "TMDL"),
        "partitionQueries": matching_partitions,
    }


def save_table_precedence(result, output_directory=None):
    """Create the output directory and save a timestamped precedence JSON file."""
    directory = Path(output_directory) if output_directory else DEFAULT_OUTPUT_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)

    model_name = str(result.get("modelName") or result.get("modelId") or "model")
    safe_model_name = re.sub(r"[^A-Za-z0-9._-]+", "_", model_name).strip("._") or "model"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = directory / f"{safe_model_name}_table_precedence_{timestamp}.json"

    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(result, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")

    return output_path


def _integer_or_zero(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def main():
    """Run table-precedence generation from the command line."""
    parser = argparse.ArgumentParser(
        description="Generate table precedence using the GitHub Copilot SDK."
    )
    parser.add_argument(
        "metadata_path",
        nargs="?",
        help="Metadata JSON path. Defaults to the newest file in output\\ai_refresh_metadata.",
    )
    args = parser.parse_args()

    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)
    _, output_path = generate_table_precedence(args.metadata_path)
    print(output_path)


if __name__ == "__main__":
    main()
