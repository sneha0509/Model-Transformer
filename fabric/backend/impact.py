"""Review-only schema change analysis of a Fabric repository snapshot."""

from datetime import datetime, timezone
from difflib import unified_diff
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
from fabric.backend.notebook_parsing import parse_python
from shared.paths import PROJECT_ROOT


REPORTS_DIR = PROJECT_ROOT / "output" / "fabric" / "impact"
MAX_ARTIFACT_CHARACTERS = 100_000
MAX_ACTION_ITEMS = 4
MAX_ACTION_CHARACTERS = 240
OBJECT_TYPES = {
    "notebook_code", "column", "measure", "relationship", "calculated_column",
    "calculated_table", "partition", "hierarchy", "role", "table", "expression",
    "pipeline_activity", "pipeline_parameter", "pipeline_variable", "other",
}
LIMITATIONS = [
    "Review only: no notebook, repository file, source schema or live Fabric item is changed.",
    "The selected model schema comes from Fabric; notebook and other model coverage is limited to the local repository snapshot.",
    "AI findings and suggested replacements require human review and runtime/model validation.",
    "AI citations for generated fixes are not source-verified: locations and excerpts require manual review. Impacts without fixes must include verified source evidence from their named target.",
    "Static table lineage is not proof of column-level flow; dynamic references and scope ambiguity remain gaps.",
    "Notebook requests contain lineage-relevant code cells and their static dependencies, not whole notebook exports. Unresolved dependencies are explicit gaps.",
    "Notebook requests retain original cell numbers and exported-code line numbers; outputs, markdown and export metadata are not sent.",
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
    if not isinstance(value, str) or len(value) > MAX_ARTIFACT_CHARACTERS or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be source text of at most {MAX_ARTIFACT_CHARACTERS} characters.")
    return value


def _action_items(value, label):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ACTION_ITEMS:
        raise ValueError(f"{label} must contain 1 to {MAX_ACTION_ITEMS} short action items, not a paragraph.")
    for item in value:
        text = _text(item, f"{label} item", MAX_ACTION_CHARACTERS)
        if len(text.splitlines()) != 1:
            raise ValueError(f"{label} items must be single-line text, not paragraphs.")


def replacement_sources(artifact):
    """Bind generated replacements to exact, content-addressed source entities."""
    if artifact["type"] == "Notebook":
        parts = [{
            **cell, "file": next(iter(artifact["files"])),
            "objectType": "notebook_code", "objectName": f"Cell {cell['cell']}",
        } for cell in artifact.get("cells", [])]
    else:
        parts = artifact.get("sourceSegments", [])
    targets = {(target["objectType"], target["objectName"]) for target in artifact.get("targets", [])}
    sources = []
    for part in parts:
        identity = (part.get("objectType"), part.get("objectName"))
        fingerprint = json.dumps([
            artifact["key"], part["file"], *identity, part["startLine"], part["source"],
        ], ensure_ascii=True)
        sources.append({
            **part, "sourceId": hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
            "editable": identity in targets,
        })
    return sources


def _entity_fixes(impact, artifact):
    """Expand and merge source-checked edits within their exact target entity."""
    if artifact["type"] == "Notebook" and "targets" in artifact:
        target = next(item for item in artifact["targets"] if (
            item["objectType"] == impact["objectType"] and item["objectName"] == impact["objectName"]
        ))
        segments = [{
            **cell, "file": next(iter(artifact["files"])),
        } for cell in artifact["cells"] if (
            cell["cell"] == target.get("cell") or target["objectName"] == f"Cell {cell['cell']}"
        )]
    elif any("objectName" in part for part in artifact.get("sourceSegments", [])):
        segments = [part for part in artifact["sourceSegments"] if (
            part["objectType"] == impact["objectType"] and part["objectName"] == impact["objectName"]
        )]
    else:
        return impact["proposedFixes"]
    grouped = {}
    for fix in impact["proposedFixes"]:
        matches = [index for index, part in enumerate(segments)
                   if part["file"] == fix["file"] and fix["before"] in part["source"]]
        if len(matches) != 1 or segments[matches[0]]["source"].count(fix["before"]) != 1:
            raise ValueError("Proposed code must uniquely match the affected target entity, not another object or prerequisite.")
        grouped.setdefault(matches[0], []).append(fix)
    replacements = []
    for index, fixes in grouped.items():
        part = segments[index]
        before = part["source"]
        edits = sorted(((before.index(fix["before"]), fix) for fix in fixes), key=lambda edit: edit[0])
        previous_end = 0
        pieces = []
        for start, fix in edits:
            if start < previous_end:
                raise ValueError("Proposed fixes overlap within the affected entity; return one combined replacement.")
            pieces.extend((before[previous_end:start], fix["after"]))
            previous_end = start + len(fix["before"])
        after = "".join([*pieces, before[previous_end:]])
        _code_text(after, "Proposed entity code", allow_empty=True)
        if after == before:
            raise ValueError("Proposed entity code must differ from the original source.")
        if after and Path(part["file"]).suffix.casefold() in {".bim", ".json"}:
            try:
                proposed = json.loads(after)
            except ValueError as exc:
                raise ValueError("Proposed entity code must be valid JSON.") from exc
            if type(proposed) is not type(json.loads(before)):
                raise ValueError("Proposed entity code must preserve the JSON entity type.")
        elif after and Path(part["file"]).suffix.casefold() in {".py", ".ipynb"}:
            try:
                parse_python(before, part["file"])
            except SyntaxError:
                # Magic and non-Python cells need their own language parser.
                pass
            else:
                try:
                    parse_python(after, part["file"])
                except SyntaxError as exc:
                    raise ValueError(f"Proposed entity code must be valid Python: {exc.msg}.") from exc
        replacement = {
            **fixes[0], "before": before, "after": after, "line": part["startLine"],
        }
        if "cell" in part:
            replacement["cells"] = [part["cell"]]
        replacements.append(replacement)
    return replacements


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


def _has_target_evidence(impact, sources):
    for source in sources.values():
        if (source.get("objectType"), source.get("objectName")) != (
            impact["objectType"], impact["objectName"],
        ):
            continue
        lines = source["source"].splitlines()
        for citation in impact["evidence"]:
            if citation["file"] != source["file"]:
                continue
            offset = citation["line"] - source["startLine"]
            excerpt = citation["excerpt"].splitlines()
            if 0 <= offset and offset + len(excerpt) <= len(lines) and (
                "\n".join(excerpt) in "\n".join(lines[offset:offset + len(excerpt)])
            ):
                return True
    return False


def validate_result(result, changes, artifacts):
    """Validate response structure, target scope and proposed replacements."""
    if not isinstance(result, dict):
        raise ValueError("Copilot must return a JSON object.")
    summary = _text(result.get("summary"), "AI summary", 8000)
    gaps = result.get("gaps")
    impacts = result.get("impacts")
    unaffected = result.get("unaffectedTargets", [])
    if not isinstance(gaps, list) or not isinstance(impacts, list) or len(impacts) > 500:
        raise ValueError("Copilot must return impacts and gaps arrays (at most 500 impacts).")
    if not isinstance(unaffected, list) or len(unaffected) > 500:
        raise ValueError("Copilot must return an unaffectedTargets array (at most 500 targets).")
    gaps = [_text(gap, "AI analysis gap", 4000) for gap in gaps]
    allowed = {artifact["key"]: artifact for artifact in artifacts}
    sources = {
        artifact["key"]: {source["sourceId"]: source for source in replacement_sources(artifact)}
        for artifact in artifacts
    }
    change_ids = {change["id"] for change in changes}
    assessed = {}
    for is_impact, impact in [(True, item) for item in impacts] + [(False, item) for item in unaffected]:
        if not isinstance(impact, dict):
            raise ValueError("Each AI target assessment must be an object.")
        artifact_key = impact.get("artifactKey")
        artifact = allowed.get(artifact_key) if isinstance(artifact_key, str) else None
        ids = impact.get("changeIds")
        if artifact is None or not isinstance(ids, list) or not ids or any(
            not isinstance(value, str) or value not in change_ids for value in ids
        ):
            raise ValueError("Copilot referenced an unknown artifact or column change.")
        if not isinstance(impact.get("objectType"), str) or impact["objectType"] not in OBJECT_TYPES:
            raise ValueError("Copilot returned an unsupported model object type.")
        _text(impact.get("objectName"), "AI objectName", 8000)
        if is_impact:
            if impact.get("severity") not in ("high", "medium", "low") or impact.get("confidence") not in (
                "high", "medium", "low",
            ):
                raise ValueError("Copilot returned an invalid severity or confidence.")
            _action_items(impact.get("impact"), "AI impact")
            _action_items(impact.get("recommendation"), "AI recommendation")
        else:
            _text(impact.get("reason"), "No-impact reason", 1000)
            if any(field in impact for field in ("impact", "recommendation", "proposedFixes")):
                raise ValueError("Unaffected targets must not contain impacts or fixes.")
        if "targets" in artifact and not any(
            impact["objectType"] == target["objectType"]
            and impact["objectName"] == target["objectName"]
            and set(ids) <= set(target["changeIds"])
            for target in artifact["targets"]
        ):
            raise ValueError("Copilot referenced an object outside the code-identified targets.")
        identity = (artifact_key, impact["objectType"], impact["objectName"])
        statuses = assessed.setdefault(identity, {True: set(), False: set()})
        if statuses[not is_impact].intersection(ids):
            raise ValueError("Copilot marked the same target and change as both impacted and unaffected.")
        statuses[is_impact].update(ids)
        evidence = impact.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Every AI target assessment must cite source evidence.")
        for citation in evidence:
            if not isinstance(citation, dict):
                raise ValueError("AI evidence must be an object.")
            file, line = citation.get("file"), citation.get("line")
            _text(file, "Evidence file")
            if type(line) is not int or line < 1:
                raise ValueError("Evidence line must be a positive integer.")
            excerpt = _code_text(citation.get("excerpt"), "Evidence excerpt")
            end_line = line + len(excerpt.splitlines()) - 1
            for cell in artifact.get("cells", []):
                if file in artifact["files"] and (
                    cell["startLine"] <= line <= end_line < cell["startLine"] + len(cell["source"].splitlines())
                ):
                    citation["cell"] = cell["cell"]
                    citation["cellLine"] = line - cell["startLine"] + 1
                    break
        if not is_impact:
            continue
        fixes = impact.get("proposedFixes")
        steps = impact.get("validationSteps")
        if not isinstance(fixes, list):
            raise ValueError("Every AI impact must include a proposedFixes array.")
        if not isinstance(steps, list):
            raise ValueError("Every AI impact must include a validationSteps array.")
        for step in steps:
            _text(step, "Validation step", 4000)
        reason = impact.get("fixUnavailableReason")
        if not fixes:
            impact["fixUnavailableReason"] = _text(reason, "Fix unavailable reason", 1000)
            if not _has_target_evidence(impact, sources[artifact_key]):
                raise ValueError("An impact without a fix must include matching source evidence from its named target.")
            continue
        if reason is not None:
            raise ValueError("An impact with generated fixes must have a null fix unavailable reason.")
        impact["fixUnavailableReason"] = None
        for fix in fixes:
            if not isinstance(fix, dict):
                raise ValueError("Each proposed fix must be an object.")
            if "sourceId" in fix:
                source_id = _text(fix["sourceId"], "Fix sourceId", 100)
                source = sources[artifact_key].get(source_id)
                if source is None:
                    raise ValueError(f"Copilot referenced an unknown sourceId for {impact['objectName']}.")
                if not source["editable"] or (
                    source.get("objectType"), source.get("objectName")
                ) != (impact["objectType"], impact["objectName"]):
                    raise ValueError("The replacement sourceId must identify the named target, not another object or prerequisite.")
                if "file" in fix and fix["file"] != source["file"]:
                    raise ValueError("Copilot supplied a conflicting source file for the replacement sourceId.")
                if "before" in fix and fix["before"] != source["source"]:
                    raise ValueError("Copilot supplied conflicting original source for the replacement sourceId.")
                fix.pop("sourceId")
                fix.update(file=source["file"], before=source["source"])
            file = fix.get("file")
            code = artifact["files"].get(file) if isinstance(file, str) else None
            before = _code_text(fix.get("before"), "Original code")
            after = _code_text(fix.get("after"), "Proposed code", allow_empty=True)
            _action_items([fix.get("rationale")], "Fix rationale")
            if code is None or before not in code or before == after:
                raise ValueError(
                    "Copilot proposed a replacement that does not match the supplied source "
                    f"for {impact['objectName']} in {file!r}. Use the target's sourceId and return changed after code."
                )
            if "sourceSegments" in artifact and not any(
                part["file"] == file and before in part["source"] for part in artifact["sourceSegments"]
            ):
                raise ValueError("Copilot proposed a replacement outside supplied model or pipeline objects.")
            if artifact["type"] == "Notebook" and "sourceRanges" in artifact:
                matches = [cell for cell in artifact["cells"] if before in cell["source"]]
                if not matches:
                    raise ValueError("Copilot proposed a replacement outside supplied notebook cells.")
                fix["cells"] = [cell["cell"] for cell in matches]
        impact["proposedFixes"] = _entity_fixes(impact, artifact)
        for fix in impact["proposedFixes"]:
            file, before, after = fix["file"], fix["before"], fix["after"]
            path = file.replace("\\", "/")
            fix["diff"] = "".join(
                line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
                for line in unified_diff(
                    before.splitlines(keepends=True), after.splitlines(keepends=True),
                    fromfile=f"a/{path}", tofile=f"b/{path}",
                )
            )
    for artifact in artifacts:
        missing = []
        for target in artifact.get("targets", []):
            statuses = assessed.get(
                (artifact["key"], target["objectType"], target["objectName"]),
                {True: set(), False: set()},
            )
            if not set(target["changeIds"]) <= statuses[True] | statuses[False]:
                missing.append(target["objectName"])
        if missing:
            message = f"AI did not resolve {len(missing)} code-identified targets in {artifact['key']}: {', '.join(missing)[:2000]}. No code was generated for these targets."
            if message not in gaps:
                gaps.append(message)
    return {"summary": summary, "impacts": impacts, "unaffectedTargets": unaffected, "gaps": gaps}


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
        "schemaVersion": 5, "reportId": report_id, "presetId": preset_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(), "revision": catalog["revision"],
        "selection": catalog["selection"],
        "instructions": catalog.get("instructions", ""),
        "schemaRevision": catalog.get("schemaRevision"),
        "mode": "recommendations_only", "coverage": "partial" if gaps else "lineage_scope_analyzed",
        "changes": changes, "impacts": impacts, "gaps": gaps, "limitations": LIMITATIONS,
        "unaffectedTargets": [target for result in results for target in result.get("unaffectedTargets", [])],
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
