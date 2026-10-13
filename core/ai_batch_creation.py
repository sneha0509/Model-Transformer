"""Create AI-planned refresh batches from saved table precedence."""

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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIRECTORY = PROJECT_ROOT / "output" / "ai_table_precedence"
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "output" / "ai_batch_creation"
DEFAULT_COPILOT_MODEL = "gpt-5"

SYSTEM_PROMPT = """
You create efficient semantic-model refresh batches from an already validated
table precedence list.

Rules:
1. Return every table exactly once.
2. Preserve precedence exactly. Reading batches in order and tables within each
   batch must reproduce the input table order.
3. Use rows, columns, partition count, and every partition TMDL query to
   estimate refresh cost and query complexity.
4. Group adjacent simple or moderate tables when their combined refresh cost is
   reasonable.
5. Put a table in its own batch when its volume or partition query is complex
   enough that grouping it could delay or destabilize other tables.
6. Do not invent, omit, duplicate, or reorder tables.
7. Prefer the fewest safe batches, but correctness and predictable execution
   are more important than minimizing the batch count.
""".strip()


def generate_ai_batches(precedence_path=None, output_directory=None, model=None):
    """Ask GitHub Copilot to batch the precedence list and save the result."""
    source_path = (
        Path(precedence_path)
        if precedence_path
        else find_latest_precedence_file(DEFAULT_INPUT_DIRECTORY)
    )
    precedence = load_precedence(source_path)
    llm_batches = request_batch_plan(precedence, model=model)
    result = build_batch_output(precedence, llm_batches, source_path.name)
    output_path = save_batch_output(result, output_directory)
    return result, output_path


def find_latest_precedence_file(input_directory):
    """Return the most recently modified table-precedence JSON file."""
    directory = Path(input_directory)
    files = list(directory.glob("*.json")) if directory.is_dir() else []
    if not files:
        raise FileNotFoundError(
            f"No AI table precedence JSON files found in {directory}."
        )
    return max(files, key=lambda path: path.stat().st_mtime_ns)


def load_precedence(precedence_path):
    """Load and validate a saved table-precedence document."""
    path = Path(precedence_path)
    with path.open(encoding="utf-8") as precedence_file:
        precedence = json.load(precedence_file)

    if not isinstance(precedence, dict):
        raise ValueError("AI table precedence must be a JSON object.")

    tables = precedence.get("tablePrecedence")
    if not isinstance(tables, list) or not tables:
        raise ValueError("AI table precedence must contain at least one table.")

    table_names = []
    for expected_precedence, table in enumerate(tables, start=1):
        if not isinstance(table, dict):
            raise ValueError("Every table precedence entry must be an object.")
        table_name = str(table.get("tableName") or "").strip()
        if not table_name:
            raise ValueError("Every table precedence entry must have a tableName.")
        if table_name in table_names:
            raise ValueError(f"Duplicate table in precedence input: {table_name}.")
        if table.get("precedence") != expected_precedence:
            raise ValueError("Table precedence values must be sequential starting at 1.")
        table_names.append(table_name)

    return precedence


def request_batch_plan(precedence, model=None, client_factory=CopilotClient):
    """Request schema-constrained batches from GitHub Copilot."""
    model_name = str(
        model or os.getenv("COPILOT_MODEL") or DEFAULT_COPILOT_MODEL
    ).strip()
    if not model_name:
        raise RuntimeError("COPILOT_MODEL cannot be empty.")

    table_names = [
        table["tableName"]
        for table in precedence["tablePrecedence"]
    ]
    response_schema = {
        "type": "object",
        "properties": {
            "batches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tables": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": table_names,
                            },
                        },
                    },
                    "required": ["tables"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["batches"],
        "additionalProperties": False,
    }
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        "Create refresh batches for this ordered table metadata:\n"
        f"{json.dumps({'tablePrecedence': precedence['tablePrecedence']}, ensure_ascii=False)}"
    )
    response_content = asyncio.run(
        _request_batches_with_copilot(
            prompt,
            response_schema,
            model_name,
            client_factory,
        )
    )

    try:
        return json.loads(response_content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub Copilot returned a non-JSON batch response.") from exc


async def _request_batches_with_copilot(
    prompt,
    response_schema,
    model_name,
    client_factory,
):
    """Use signed-in Copilot credentials and return structured batch content."""
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


def build_batch_output(precedence, llm_batches, source_file_name):
    """Validate the Copilot batches and build the persisted batch plan."""
    batches = llm_batches.get("batches") if isinstance(llm_batches, dict) else None
    if not isinstance(batches, list) or not batches:
        raise ValueError("GitHub Copilot output must contain a non-empty batches array.")

    expected_names = [
        table["tableName"]
        for table in precedence["tablePrecedence"]
    ]
    flattened_names = []
    normalized_batches = []

    for index, batch in enumerate(batches, start=1):
        tables = batch.get("tables") if isinstance(batch, dict) else None
        if not isinstance(tables, list) or not tables:
            raise ValueError("Every GitHub Copilot batch must contain at least one table.")

        normalized_tables = [str(table or "").strip() for table in tables]
        if any(not table for table in normalized_tables):
            raise ValueError("GitHub Copilot returned an empty table name.")

        flattened_names.extend(normalized_tables)
        normalized_batches.append({
            "name": f"Batch {index}",
            "tables": normalized_tables,
        })

    if flattened_names != expected_names:
        raise ValueError(
            "GitHub Copilot batches must contain every table exactly once in "
            "table precedence order."
        )

    return {
        "workspaceName": str(precedence.get("workspaceName") or ""),
        "workspaceId": str(precedence.get("workspaceId") or ""),
        "modelName": str(precedence.get("modelName") or ""),
        "modelId": str(precedence.get("modelId") or ""),
        "sourcePrecedenceFile": str(source_file_name),
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "batches": normalized_batches,
    }


def save_batch_output(result, output_directory=None):
    """Create the output directory and save a timestamped final batch JSON file."""
    directory = Path(output_directory) if output_directory else DEFAULT_OUTPUT_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)

    model_name = str(result.get("modelName") or result.get("modelId") or "model")
    safe_model_name = re.sub(r"[^A-Za-z0-9._-]+", "_", model_name).strip("._") or "model"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = directory / f"{safe_model_name}_ai_batches_{timestamp}.json"

    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(result, output_file, indent=2, ensure_ascii=False)
        output_file.write("\n")

    return output_path


def main():
    """Run AI batch creation from the command line."""
    parser = argparse.ArgumentParser(
        description="Create refresh batches from AI table precedence."
    )
    parser.add_argument(
        "precedence_path",
        nargs="?",
        help=(
            "Precedence JSON path. Defaults to the newest file in "
            "output\\ai_table_precedence."
        ),
    )
    args = parser.parse_args()

    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)
    _, output_path = generate_ai_batches(args.precedence_path)
    print(output_path)


if __name__ == "__main__":
    main()
