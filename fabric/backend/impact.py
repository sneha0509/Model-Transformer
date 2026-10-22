"""Review-only schema change analysis of a Fabric repository snapshot."""

from datetime import datetime, timezone
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from fabric.backend.filesystem import filesystem_path
from fabric.backend.impact_context import notebook_cells
from fabric.backend.lineage import EXCLUDED_DIRECTORIES, _notebook_source, scan_repository
from fabric.backend.model_lineage import _blocks, _property, read_model
from shared.paths import PROJECT_ROOT


REPORTS_DIR = PROJECT_ROOT / "output" / "fabric" / "impact"
MAX_ARTIFACT_CHARACTERS = 100_000
OBJECT_TYPES = {
    "notebook_code", "column", "measure", "relationship", "calculated_column",
    "calculated_table", "partition", "hierarchy", "role", "table", "expression",
    "pipeline_activity", "pipeline_parameter", "pipeline_variable", "other",
}
LIMITATIONS = [
    "Review only: no notebook, repository file, source schema or live Fabric item is changed.",
    "The selected model schema comes from Fabric; notebook and other model coverage is limited to the local repository snapshot.",
    "AI findings and suggested replacements require human review and runtime/model validation.",
    "Static table lineage is not proof of column-level flow; dynamic references and scope ambiguity remain gaps.",
    "Notebook requests contain lineage-relevant code cells and their static dependencies, not whole notebook exports. Unresolved dependencies are explicit gaps.",
    "Notebook evidence retains original cell numbers and exported-code line numbers; outputs, markdown and export metadata are not sent.",
    "Model dependencies are static references, not proof that every dependent object needs an edit. DAX context, dynamic expressions and runtime schemas require validation.",
    "Pipeline invocation and literal table metadata identify review candidates; indirect dataset references and dynamic pipeline expressions may require manual tracing.",
]


def _text(value, label, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be non-empty text of at most {maximum} characters.")
    return value.strip()


def _read(root, file):
    if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact file is outside the repository snapshot or is a symbolic link.")
    return file.read_text(encoding="utf-8-sig")


def _code_text(value, label, allow_empty=False):
    if not isinstance(value, str) or len(value) > 20000 or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be source text of at most 20000 characters.")
    return value


def _schema(root, model, files, issues):
    sources = {
        table["name"]: table
        for table in read_model(root, model, lambda file, message, line=None: issues.append({
            "file": file, "message": message, "line": line,
        }))
    }
    tables = []
    for file, content in files.items():
        if file.endswith("model.bim"):
            document = json.loads(content)
            definitions = document.get("model", document).get("tables", [])
            for table in definitions:
                columns = [{
                    "name": column["name"], "dataType": column.get("dataType", ""),
                    "sourceColumn": column.get("sourceColumn", ""),
                    "kind": column.get("type", "data"),
                } for column in table.get("columns", [])]
                tables.append({"name": table["name"], "columns": columns})
        elif "tables" in Path(file).parts and file.endswith(".tmdl"):
            for name, _, body, _ in _blocks(content, "table"):
                columns = [{
                    "name": column, "dataType": _property(block, "dataType"),
                    "sourceColumn": _property(block, "sourceColumn"),
                    "kind": "calculated" if tail.startswith("=") else "data",
                } for column, tail, block, _ in _blocks(body, "column")]
                tables.append({"name": name, "columns": columns})
    for table in tables:
        table["sources"] = sources.get(table["name"], {}).get("sources", [])
        table["sourceStatus"] = sources.get(table["name"], {}).get("status", "unresolved")
        names = [column["name"] for column in table["columns"]]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate columns in table {table['name']}.")
    if len({table["name"] for table in tables}) != len(tables):
        raise ValueError("Duplicate model table names.")
    return tables


def live_schema(preset, client, model_id=None):
    model_id = model_id or preset.get("liveSemanticModel", {}).get("id")
    arguments = (preset["workspace"]["id"], preset["semanticModel"]["name"])
    result = client.get_semantic_model_definition(
        *arguments, **({"model_id": model_id} if model_id else {}),
    )
    definition = result.get("definition")
    if not isinstance(definition, dict) or not isinstance(definition.get("parts"), list):
        raise RuntimeError("Fabric did not return a semantic model definition with parts.")
    parts = [
        part for part in definition["parts"]
        if isinstance(part, dict) and part.get("path") == "model.bim"
    ]
    if len(parts) != 1 or parts[0].get("payloadType") != "InlineBase64":
        raise RuntimeError("Fabric did not return a single model.bim part in the TMSL definition.")
    try:
        content = base64.b64decode(parts[0]["payload"], validate=True).decode("utf-8-sig")
        document = json.loads(content)
    except (binascii.Error, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Fabric returned an invalid model.bim definition payload.") from exc
    issues = []
    with tempfile.TemporaryDirectory(prefix="fabric-impact-schema-") as temporary:
        root = filesystem_path(temporary)
        (root / "model.bim").write_text(content, encoding="utf-8")
        try:
            tables = _schema(root, {"path": "/"}, {"model.bim": content}, issues)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"Could not read the Fabric semantic model schema: {exc}") from exc
    if not tables:
        raise RuntimeError("Fabric returned a semantic model definition without tables.")
    # Formatting from the service is not part of the schema selection identity.
    revision = hashlib.sha256(json.dumps({
        "model": result["semanticModel"], "definition": document,
    }, sort_keys=True).encode("utf-8")).hexdigest()
    file = str(Path(preset["semanticModel"]["path"].replace("\\", "/").strip("/")) / "model.bim")
    for table in tables:
        for source in table["sources"]:
            for evidence in source["evidence"]:
                evidence["file"] = file
    for issue in issues:
        issue["file"] = file
    return {
        "revision": revision, "tables": tables, "semanticModel": result["semanticModel"],
        "files": {file: content}, "issues": issues,
    }


def build_catalog(preset, current_schema=None):
    location = preset.get("localRepositoryPath")
    if not isinstance(location, str) or not location:
        raise ValueError("Prepare this preset's repository and lineage before running impact analysis.")
    root = filesystem_path(location)
    if not root.is_dir():
        raise FileNotFoundError("Repository snapshot is missing. Open the preset and prepare it first.")
    mapping = scan_repository(root)
    issues = list(mapping["issues"])
    artifacts = []
    for notebook in mapping["notebooks"]:
        file = root / notebook["file"]
        try:
            _read(root, file)
            code, _, _ = _notebook_source(file)
            artifacts.append({
                "key": notebook["key"], "name": notebook["name"], "type": "Notebook",
                "files": {notebook["file"]: code},
                "cells": notebook_cells(file, code),
                "context": {key: notebook[key] for key in (
                    "reads", "writes", "calls", "defaultLakehouse",
                )},
            })
        except (OSError, ValueError) as exc:
            issues.append({"file": notebook["file"], "message": f"Could not read notebook: {exc}"})
    for pipeline in mapping["pipelines"]:
        try:
            artifacts.append({
                "key": pipeline["key"], "name": pipeline["name"], "type": "DataPipeline",
                "files": {pipeline["file"]: _read(root, root / pipeline["file"])},
                "context": {},
            })
        except (OSError, ValueError) as exc:
            issues.append({"file": pipeline["file"], "message": f"Could not read pipeline: {exc}"})

    selected_path = preset["semanticModel"]["path"].replace("\\", "/").strip("/")
    selected_tables = None
    for directory, folders, filenames in os.walk(root):
        folders[:] = sorted(name for name in folders
                            if name.casefold() not in EXCLUDED_DIRECTORIES
                            and not (Path(directory) / name).is_symlink())
        folder = Path(directory)
        relative = folder.relative_to(root).as_posix()
        is_model = (
            folder.suffix.casefold() == ".semanticmodel"
            or "definition.pbism" in filenames or "model.bim" in filenames
            or relative == selected_path
        )
        if not is_model:
            continue
        if relative == selected_path and current_schema is not None:
            folders[:] = []
            continue
        model = {"name": folder.stem, "path": "/" + relative}
        files = {}
        try:
            bim = folder / "model.bim"
            if bim.is_file():
                files[str(bim.relative_to(root))] = _read(root, bim)
            else:
                definition = folder / "definition"
                if definition.is_symlink():
                    raise ValueError("Symbolic-link model definitions are not supported.")
                for parent, children, names in os.walk(definition):
                    if any((Path(parent) / name).is_symlink() for name in children):
                        issues.append({"file": relative, "message": "Symbolic-link definition folders were excluded."})
                    children[:] = sorted(name for name in children
                                         if not (Path(parent) / name).is_symlink())
                    for name in sorted(names):
                        if name.endswith(".tmdl"):
                            file = Path(parent) / name
                            files[str(file.relative_to(root))] = _read(root, file)
            tables = _schema(root, model, files, issues)
            if not files or not tables:
                raise ValueError("No readable model tables were found in the exported definition.")
            artifacts.append({
                "key": relative, "name": model["name"], "type": "SemanticModel",
                "files": files, "context": {"tables": tables},
            })
            if relative == selected_path:
                selected_tables = tables
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            if relative == selected_path:
                raise ValueError(f"Could not read the selected model schema: {exc}") from exc
            issues.append({"file": relative, "message": f"Semantic model was not analyzed: {exc}"})
        folders[:] = []
    if current_schema is not None:
        selected_tables = current_schema["tables"]
        artifacts.append({
            "key": selected_path, "name": current_schema["semanticModel"]["name"],
            "type": "SemanticModel", "origin": "fabric_api",
            "files": current_schema["files"], "context": {"tables": selected_tables},
        })
        issues.extend(current_schema["issues"])
    if selected_tables is None:
        raise ValueError("Selected semantic model was not found in this repository snapshot.")
    artifacts.sort(key=lambda item: item["key"])
    lineage = {
        key: mapping[key] for key in ("tables", "pipelineLinks", "notebookLinks")
    }
    fingerprint = json.dumps(
        {"artifacts": artifacts, "lineage": lineage, "issues": issues},
        sort_keys=True, ensure_ascii=True,
    )
    return {
        "revision": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest(),
        "tables": selected_tables, "artifacts": artifacts,
        "lineage": lineage, "issues": issues,
        "schemaRevision": current_schema["revision"] if current_schema is not None else None,
        "selection": {
            "workspace": preset["workspace"],
            "semanticModel": {
                **preset["semanticModel"],
                **(current_schema["semanticModel"] if current_schema is not None else {}),
            },
            "repository": {
                key: preset["repository"].get(key, "")
                for key in ("id", "name", "organization", "projectName", "branchName")
            },
        },
    }


def validate_changes(payload, catalog):
    if not isinstance(payload, dict):
        raise ValueError("Select impacted tables and columns and describe the changes.")
    if payload.get("revision") != catalog["revision"]:
        raise ValueError("Semantic model schema changed. Reload the selection pane before analyzing.")
    _text(payload.get("instructions"), "Analysis instructions", 20000)
    changes = payload.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 200:
        raise ValueError("Select between 1 and 200 columns to change.")
    tables = {table["name"]: table for table in catalog["tables"]}
    validated = []
    seen = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("Each column change must be a JSON object.")
        table_name = _text(change.get("table"), "Table", 512)
        column_name = _text(change.get("column"), "Column", 512)
        if table_name not in tables:
            raise ValueError(f"Unknown selected model table: {table_name}.")
        columns = {column["name"]: column for column in tables[table_name]["columns"]}
        if column_name not in columns or (table_name, column_name) in seen:
            raise ValueError(f"Unknown or duplicate column: {table_name}.{column_name}.")
        seen.add((table_name, column_name))
        validated.append({
            "id": f"change-{len(validated) + 1}", "table": table_name, "column": column_name,
            "current": columns[column_name], "sources": tables[table_name]["sources"],
            "sourceStatus": tables[table_name]["sourceStatus"],
        })
    return validated


def validate_result(result, changes, artifacts):
    """Reject invented citations and replacements before persisting AI output."""
    if not isinstance(result, dict):
        raise ValueError("Copilot must return a JSON object.")
    summary = _text(result.get("summary"), "AI summary", 8000)
    gaps = result.get("gaps")
    impacts = result.get("impacts")
    if not isinstance(gaps, list) or not isinstance(impacts, list) or len(impacts) > 500:
        raise ValueError("Copilot must return impacts and gaps arrays (at most 500 impacts).")
    gaps = [_text(gap, "AI analysis gap", 4000) for gap in gaps]
    allowed = {artifact["key"]: artifact for artifact in artifacts}
    change_ids = {change["id"] for change in changes}
    for impact in impacts:
        if not isinstance(impact, dict):
            raise ValueError("Each AI impact must be an object.")
        artifact_key = impact.get("artifactKey")
        artifact = allowed.get(artifact_key) if isinstance(artifact_key, str) else None
        ids = impact.get("changeIds")
        if artifact is None or not isinstance(ids, list) or not ids or any(
            not isinstance(value, str) or value not in change_ids for value in ids
        ):
            raise ValueError("Copilot referenced an unknown artifact or column change.")
        if not isinstance(impact.get("objectType"), str) or impact["objectType"] not in OBJECT_TYPES:
            raise ValueError("Copilot returned an unsupported model object type.")
        if impact.get("severity") not in ("high", "medium", "low") or impact.get("confidence") not in (
            "high", "medium", "low",
        ):
            raise ValueError("Copilot returned an invalid severity or confidence.")
        for field in ("objectName", "impact", "recommendation"):
            _text(impact.get(field), f"AI {field}", 8000)
        if "targets" in artifact and not any(
            impact["objectType"] == target["objectType"]
            and impact["objectName"] == target["objectName"]
            and set(ids) <= set(target["changeIds"])
            for target in artifact["targets"]
        ):
            raise ValueError("Copilot referenced an object outside the code-identified targets.")
        evidence = impact.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Every AI impact must cite source evidence.")
        for citation in evidence:
            if not isinstance(citation, dict):
                raise ValueError("AI evidence must be an object.")
            file, line = citation.get("file"), citation.get("line")
            code = artifact["files"].get(file) if isinstance(file, str) else None
            if code is None or type(line) is not int or not 1 <= line <= len(code.splitlines()):
                raise ValueError("Copilot cited a file or line outside the supplied artifact.")
            excerpt = _code_text(citation.get("excerpt"), "Evidence excerpt")
            end_line = line + len(excerpt.splitlines()) - 1
            ranges = artifact.get("sourceRanges", {}).get(file)
            if ranges is not None and not any(start <= line <= end_line <= end for start, end in ranges):
                raise ValueError("Copilot cited source ranges that were not supplied.")
            if "sourceSegments" in artifact and not any(
                part["file"] == file and part["startLine"] <= line <= end_line <= part["endLine"]
                and excerpt in "\n".join(part["source"].splitlines()[
                    line - part["startLine"]:end_line - part["startLine"] + 1
                ]) for part in artifact["sourceSegments"]
            ):
                raise ValueError("Copilot cited object source that was not supplied.")
            if excerpt not in "\n".join(code.splitlines()[line - 1:line - 1 + len(excerpt.splitlines())]):
                raise ValueError("Copilot evidence does not match the cited source lines.")
            for cell in artifact.get("cells", []):
                if cell["startLine"] <= line <= end_line < cell["startLine"] + len(cell["source"].splitlines()):
                    citation["cell"] = cell["cell"]
                    citation["cellLine"] = line - cell["startLine"] + 1
                    break
        fixes = impact.get("proposedFixes")
        steps = impact.get("validationSteps")
        if not isinstance(fixes, list) or not isinstance(steps, list) or not steps:
            raise ValueError("Every AI impact must include proposedFixes and validationSteps arrays.")
        for step in steps:
            _text(step, "Validation step", 4000)
        for fix in fixes:
            if not isinstance(fix, dict):
                raise ValueError("Each proposed fix must be an object.")
            file = fix.get("file")
            code = artifact["files"].get(file) if isinstance(file, str) else None
            before = _code_text(fix.get("before"), "Original code")
            after = _code_text(fix.get("after"), "Proposed code", allow_empty=True)
            _text(fix.get("rationale"), "Fix rationale", 8000)
            if code is None or before not in code or before == after:
                raise ValueError("Copilot proposed a replacement that does not match the supplied source.")
            if "sourceSegments" in artifact and not any(
                part["file"] == file and before in part["source"] for part in artifact["sourceSegments"]
            ):
                raise ValueError("Copilot proposed a replacement outside supplied model or pipeline objects.")
            if artifact["type"] == "Notebook" and "sourceRanges" in artifact:
                matches = [cell for cell in artifact["cells"] if before in cell["source"]]
                if not matches:
                    raise ValueError("Copilot proposed a replacement outside supplied notebook cells.")
                fix["cells"] = [cell["cell"] for cell in matches]
    for artifact in artifacts:
        missing = [target["objectName"] for target in artifact.get("targets", []) if not any(
            impact["artifactKey"] == artifact["key"] and impact["objectName"] == target["objectName"]
            and impact["objectType"] == target["objectType"] for impact in impacts
        )]
        if missing:
            message = f"AI did not explain {len(missing)} code-identified targets in {artifact['key']}: {', '.join(missing)[:2000]}. Review the static targets manually."
            if message not in gaps:
                gaps.append(message)
    return {"summary": summary, "impacts": impacts, "gaps": gaps}


def save_report(preset_id, catalog, changes, results, skipped):
    if not re.fullmatch(r"[a-f0-9]{64}", preset_id):
        raise ValueError("Invalid Fabric preset ID.")
    report_id = uuid4().hex
    impacts = [impact for result in results for impact in result["impacts"]]
    gaps = list(catalog["issues"]) + [
        {"file": "", "message": gap} for result in results for gap in result["gaps"]
    ] + skipped
    analyzed = {key for result in results for key in result["analyzedArtifacts"]}
    report = {
        "schemaVersion": 3, "reportId": report_id, "presetId": preset_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(), "revision": catalog["revision"],
        "selection": catalog["selection"],
        "instructions": catalog.get("instructions", ""),
        "schemaRevision": catalog.get("schemaRevision"),
        "mode": "recommendations_only", "coverage": "partial" if gaps else "lineage_scope_analyzed",
        "changes": changes, "impacts": impacts, "gaps": gaps, "limitations": LIMITATIONS,
        "summaries": [result["summary"] for result in results],
        "impactLineage": catalog.get("impactLineage", {"nodes": [], "edges": []}),
        "targets": catalog.get("targets", []),
        "aiUsage": catalog.get("aiUsage", {"requests": 0, "cacheHits": 0}),
        "inventory": [{
            "key": artifact["key"], "name": artifact["name"], "type": artifact["type"],
            "origin": artifact.get("origin", "repository_snapshot"),
            "status": "analyzed" if artifact["key"] in analyzed else "skipped",
            **catalog.get("inventoryScope", {}).get(artifact["key"], {}),
        } for artifact in catalog["artifacts"]],
    }
    directory = filesystem_path(REPORTS_DIR / preset_id)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         suffix=".tmp", delete=False) as file:
            temporary = file.name
            json.dump(report, file, indent=2)
        os.replace(temporary, directory / f"{report_id}.json")
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    return report


def report_path(preset_id, report_id):
    if not re.fullmatch(r"[a-f0-9]{64}", preset_id) or not re.fullmatch(r"[a-f0-9]{32}", report_id):
        raise ValueError("Invalid Fabric impact report ID.")
    return filesystem_path(REPORTS_DIR / preset_id / f"{report_id}.json")
