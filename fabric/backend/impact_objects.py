"""Deterministic object dependencies and bounded, evidence-backed AI context."""

from collections import deque
import ast
import hashlib
import json
import re

from fabric.backend.model_lineage import _name, match_table
from fabric.backend.notebook_parsing import parse_python


_NAME = r"'(?:[^']|'')*'|[^\s=]+"
_DECLARATION = re.compile(
    rf"^(?P<indent>[ \t]*)(?P<kind>table|column|measure|partition|hierarchy|relationship|role|expression)"
    rf"\s+(?P<name>{_NAME})(?P<tail>[^\n]*)$"
)
_DAX_REFERENCE = re.compile(r"(?:(?P<table>'(?:[^']|'')*'|[A-Za-z_]\w*)\s*)?\[(?P<name>(?:[^\]]|\]\])*)\]")
_RELATIONSHIP_CALL = re.compile(
    r"\b(?:USERELATIONSHIP|CROSSFILTER)\s*\(\s*", re.IGNORECASE,
)
_TMDL_ENDPOINT = re.compile(r"('(?:[^']|'')*'|[^\s.']+)\.('(?:[^']|'')*'|[^\s']+)")


def _id(*parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


def _segment(file, code, start=0, end=None):
    source = code[start:end]
    line = code.count("\n", 0, start) + 1
    return {
        "file": file, "startLine": line,
        "endLine": line + len(source.splitlines()) - 1, "source": source,
    }


def _json_spans(code):
    """Retain exact source spans even for single-line service definitions."""
    decoder = json.JSONDecoder()
    spans = {}

    def skip(position):
        while position < len(code) and code[position].isspace():
            position += 1
        return position

    def value(position, path):
        position = skip(position)
        start = position
        parsed, end = decoder.raw_decode(code, position)
        spans[path] = (start, end)
        if isinstance(parsed, dict):
            position = skip(position + 1)
            for _ in parsed:
                key, position = decoder.raw_decode(code, position)
                position = value(skip(position) + 1, path + (key,))
                position = skip(position)
                if code[position] == ",":
                    position = skip(position + 1)
        elif isinstance(parsed, list):
            position = skip(position + 1)
            for index in range(len(parsed)):
                position = value(position, path + (index,))
                position = skip(position)
                if code[position] == ",":
                    position = skip(position + 1)
        return end

    value(0, ())
    return spans


def _model_objects(artifact):
    objects = []

    def add(kind, name, table, segment, definition=None):
        item = {
            "id": _id(artifact["key"], segment["file"], kind, table, name),
            "artifactKey": artifact["key"], "objectType": kind,
            "objectName": f"{table}.{name}" if table and kind != "table" else name,
            "name": name, "table": table, "segment": segment,
            "definition": definition or {}, "dependencies": set(),
        }
        objects.append(item)
        return item

    for file, code in artifact["files"].items():
        if file.endswith("model.bim"):
            document = json.loads(code)
            base = ("model",) if "model" in document else ()
            model = document.get("model", document)
            spans = _json_spans(code)

            def part(path):
                return _segment(file, code, *spans[path])

            for index, table in enumerate(model.get("tables", [])):
                path = base + ("tables", index)
                name = table["name"]
                add("table", name, name, part(path + ("name",)))
                for collection, kind in (
                    ("columns", "column"), ("measures", "measure"),
                    ("partitions", "partition"), ("hierarchies", "hierarchy"),
                ):
                    for number, definition in enumerate(table.get(collection, [])):
                        object_kind = "calculated_column" if kind == "column" and (
                            definition.get("type") == "calculated" or "expression" in definition
                        ) else kind
                        if kind == "partition" and definition.get("source", {}).get("type") == "calculated":
                            object_kind = "calculated_table"
                        add(object_kind, definition["name"], name,
                            part(path + (collection, number)), definition)
            for collection, kind in (("relationships", "relationship"), ("roles", "role"), ("expressions", "expression")):
                for number, definition in enumerate(model.get(collection, [])):
                    add(kind, definition["name"], "", part(base + (collection, number)), definition)
            continue
        lines = code.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        declarations = [(index, _DECLARATION.match(line.rstrip("\r\n")))
                        for index, line in enumerate(lines)]
        declarations = [(index, match) for index, match in declarations if match]
        table = ""
        for index, match in declarations:
            kind, name, tail = match["kind"], _name(match["name"]), match["tail"].strip()
            if kind == "table":
                table = name
                end = index + 1
            else:
                end = index + 1
                while end < len(lines):
                    text = lines[end]
                    if text.strip() and len(text) - len(text.lstrip()) <= len(match["indent"]):
                        break
                    end += 1
                while end > index + 1 and not lines[end - 1].strip():
                    end -= 1
            object_kind = "calculated_column" if kind == "column" and tail.startswith("=") else kind
            if kind == "partition" and tail.lstrip("= ").casefold() == "calculated":
                object_kind = "calculated_table"
            owner = table if kind in {"table", "column", "measure", "partition", "hierarchy"} else ""
            add(object_kind, name, owner, _segment(file, code, offsets[index], offsets[end]))
    return objects


def _dax_code(source):
    return re.sub(
        r'"(?:[^"]|"")*"|//[^\n]*|--[^\n]*|/\*[\s\S]*?\*/',
        lambda match: " " * len(match[0]), source,
    )


def _wire_model(objects, issues):
    columns, measures, tables = {}, {}, {}
    relationships, incoming = {}, {}
    expressions = {item["name"].casefold(): item for item in objects if item["objectType"] == "expression"}
    for item in objects:
        kind, table, name = item["objectType"], item["table"].casefold(), item["name"].casefold()
        if kind in {"column", "calculated_column"}:
            columns[(table, name)] = item
        elif kind == "measure":
            measures.setdefault(name, []).append(item)
        elif kind == "table":
            tables[table] = item

    def column(item, table, name):
        target = columns.get((table.casefold(), name.casefold()))
        if target:
            item["dependencies"].add(target["id"])
        return target

    for item in objects:
        if item["objectType"] != "relationship":
            continue
        definition, source = item["definition"], item["segment"]["source"]
        endpoints = []
        for side in ("from", "to"):
            if definition:
                table, name = definition.get(side + "Table", ""), definition.get(side + "Column", "")
            else:
                property_match = re.search(rf"(?m)^\s*{side}Column:\s*(.*?)\s*$", source)
                endpoint = property_match[1] if property_match else ""
                match = _TMDL_ENDPOINT.fullmatch(endpoint)
                table, name = (_name(match[1]), _name(match[2])) if match else ("", "")
            target = column(item, table, name)
            if target:
                endpoints.append(target)
            else:
                issues.append({
                    "file": item["segment"]["file"],
                    "message": f"Unresolved {side} endpoint {table}.{name} in relationship {item['name']}; dependent calculation coverage is incomplete.",
                })
        if len(endpoints) != 2:
            continue
        relationships.setdefault(frozenset(target["id"] for target in endpoints), []).append(item)
        active = definition.get("isActive", True) if definition else not re.search(
            r"(?im)^\s*isActive:\s*false\s*$", source,
        )
        if not active:
            continue
        from_table, to_table = (target["table"].casefold() for target in endpoints)
        incoming.setdefault(from_table, []).append((to_table, item["id"]))
        behavior = definition.get("crossFilteringBehavior", "") if definition else ""
        if not definition:
            match = re.search(r"(?m)^\s*crossFilteringBehavior:\s*(\w+)", source)
            behavior = match[1] if match else ""
        if behavior.casefold() == "bothdirections":
            incoming.setdefault(to_table, []).append((from_table, item["id"]))

    for item in objects:
        kind, definition = item["objectType"], item["definition"]
        source = item["segment"]["source"]
        if kind in {"column", "calculated_column", "hierarchy"}:
            names = [definition.get("sortByColumn", "")]
            names.extend(level.get("column", "") for level in definition.get("levels", []))
            if not definition:
                names.extend(_name(match[1]) for match in re.finditer(
                    r"(?m)^\s*(?:sortByColumn|column):\s*(.+?)\s*$", source,
                ))
            for name in names:
                if name:
                    column(item, item["table"], name)
        expression = definition.get("expression", "") if definition else ""
        role_scopes = []
        if kind in {"partition", "calculated_table"} and definition:
            expression = definition.get("source", {}).get("expression", "")
        if kind == "role" and definition:
            parts, offset = [], 0
            for permission in definition.get("tablePermissions", []):
                text = permission.get("filterExpression", "")
                if isinstance(text, list):
                    text = "\n".join(text)
                parts.append(text)
                role_scopes.append((offset, offset + len(text), permission.get("name", "")))
                offset += len(text) + 1
            expression = "\n".join(parts)
        if isinstance(expression, list):
            expression = "\n".join(expression)
        if not definition and kind in {"measure", "calculated_column", "calculated_table", "role", "partition", "expression"}:
            expression = source
            if kind == "role":
                permissions = list(re.finditer(rf"(?m)^\s*tablePermission\s+({_NAME})\s*=", source))
                role_scopes = [
                    (match.end(), permissions[index + 1].start() if index + 1 < len(permissions) else len(source), _name(match[1]))
                    for index, match in enumerate(permissions)
                ]
        if not expression:
            continue
        dax = _dax_code(expression)
        referenced_tables = set()
        for match in _DAX_REFERENCE.finditer(dax):
            name = match["name"].replace("]]", "]")
            owner = _name(match["table"]) if match["table"] else next(
                (table for start, end, table in role_scopes if start <= match.start() < end), item["table"],
            )
            target = columns.get((owner.casefold(), name.casefold()))
            candidates = [target] if target else measures.get(name.casefold(), [])
            if match["table"] and not target:
                candidates = [candidate for candidate in candidates if candidate["table"].casefold() == owner.casefold()]
            if len(candidates) > 1:
                issues.append({"file": item["segment"]["file"],
                               "message": f"Ambiguous DAX reference [{name}] in {item['objectName']}; all candidates retained."})
            item["dependencies"].update(candidate["id"] for candidate in candidates)
            referenced_tables.update(
                candidate["table"].casefold() for candidate in candidates
                if candidate["objectType"] in {"column", "calculated_column"}
            )
        without_references = _DAX_REFERENCE.sub(" ", dax)
        for name, target in tables.items():
            quoted = "'" + target["name"].replace("'", "''") + "'"
            if quoted.casefold() in without_references.casefold() or re.search(
                rf"(?<![\w']){re.escape(target['name'])}(?![\w'])", without_references, re.IGNORECASE,
            ):
                item["dependencies"].add(target["id"])
                referenced_tables.add(name)
        for call in _RELATIONSHIP_CALL.finditer(dax):
            first = _DAX_REFERENCE.match(dax, call.end())
            separator = re.match(r"\s*,\s*", dax[first.end():]) if first else None
            second = _DAX_REFERENCE.match(dax, first.end() + separator.end()) if separator else None
            if not first or not second:
                issues.append({
                    "file": item["segment"]["file"],
                    "message": f"Unresolved relationship function in {item['objectName']}; relationship coverage is incomplete.",
                })
                continue
            endpoints = [
                columns.get((
                    (_name(match["table"]) if match["table"] else item["table"]).casefold(),
                    match["name"].replace("]]", "]").casefold(),
                )) for match in (first, second)
            ]
            matches = relationships.get(
                frozenset(target["id"] for target in endpoints if target), [],
            ) if all(endpoints) else []
            if not matches:
                issues.append({
                    "file": item["segment"]["file"],
                    "message": f"No static relationship resolved for {call[0].strip()} in {item['objectName']}.",
                })
            item["dependencies"].update(target["id"] for target in matches)
        if kind == "measure" or re.search(
            r"\b(?:RELATED|RELATEDTABLE|CALCULATE|CALCULATETABLE)\s*\(", dax, re.IGNORECASE,
        ):
            if kind == "calculated_column":
                referenced_tables.add(item["table"].casefold())
            pending, visited = list(referenced_tables), set()
            while pending:
                table = pending.pop()
                if table in visited:
                    continue
                visited.add(table)
                for parent, relationship_id in incoming.get(table, []):
                    item["dependencies"].add(relationship_id)
                    pending.append(parent)
        if kind in {"partition", "calculated_table", "expression"}:
            for name, target in expressions.items():
                if re.search(rf"(?<!\w){re.escape(target['name'])}(?!\w)", expression, re.IGNORECASE):
                    item["dependencies"].add(target["id"])
        item["dependencies"].discard(item["id"])
    for item in objects:
        if item["objectType"] == "calculated_table" and item["table"].casefold() in tables:
            tables[item["table"].casefold()]["dependencies"].add(item["id"])
            for column_item in objects:
                if column_item["table"] == item["table"] and column_item["objectType"] in {"column", "calculated_column"}:
                    column_item["dependencies"].add(item["id"])
        elif item["objectType"] == "partition" and item["table"].casefold() in tables:
            item["dependencies"].add(tables[item["table"].casefold()]["id"])


def _slice_artifact(artifact, targets, objects):
    by_id = {item["id"]: item for item in objects}
    needed = {item["id"] for item in targets}
    pending = list(needed)
    while pending:
        for parent in by_id[pending.pop()]["dependencies"] - needed:
            needed.add(parent)
            pending.append(parent)
    segments = [{
        **item["segment"], "objectType": item["objectType"], "objectName": item["objectName"],
    } for item in objects if item["id"] in needed]
    unique = {
        (part["file"], part["objectType"], part["objectName"], part["startLine"], part["source"]): part
        for part in segments
    }
    return {
        **artifact, "changeIds": sorted({change_id for item in targets for change_id in item["changeIds"]}),
        "targets": [_public(item) for item in targets],
        "sourceSegments": list(unique.values()),
        "context": {"targetCount": len(targets), "contextObjectCount": len(needed)},
        "sourceRanges": {
            file: [(part["startLine"], part["endLine"]) for part in unique.values() if part["file"] == file]
            for file in artifact["files"]
        },
    }


def model_request_batches(artifact, fits, maximum_targets):
    if len(artifact["targets"]) <= maximum_targets and fits(artifact):
        return [artifact]
    objects = _model_objects(artifact)
    _wire_model(objects, [])
    by_id = {item["id"]: item for item in objects}
    targets = []
    for target in artifact["targets"]:
        item = by_id[target["id"]]
        item.update(target)
        targets.append(item)

    def split(selected):
        request = _slice_artifact(artifact, selected, objects)
        if len(selected) > 1 and (len(selected) > maximum_targets or not fits(request)):
            middle = len(selected) // 2
            return split(selected[:middle]) + split(selected[middle:])
        return [request]

    return split(targets)


def _public(item):
    return {key: value for key, value in item.items() if key not in {"segment", "definition", "dependencies", "name", "table"}}


def _pipeline_metadata(artifact, changes):
    matches = []
    document = json.loads(next(iter(artifact["files"].values())))
    parameters = document.get("properties", {}).get("parameters", {})

    def literal(value):
        if not isinstance(value, str):
            return value
        match = re.fullmatch(r"@pipeline\(\)\.parameters\.(\w+)", value, re.IGNORECASE)
        if match:
            definition = parameters.get(match[1], {})
            if isinstance(definition, dict) and isinstance(definition.get("defaultValue"), str):
                return definition["defaultValue"]
        return value

    def values(value):
        if isinstance(value, dict):
            return [text for key, child in value.items()
                    if key not in {"activities", "ifTrueActivities", "ifFalseActivities", "cases", "defaultActivities"}
                    for text in values(child)]
        if isinstance(value, list):
            return [text for child in value for text in values(child)]
        return [literal(value).casefold().replace("`", "")] if isinstance(value, str) else []

    def visit(value, path=()):
        if isinstance(value, dict):
            if isinstance(value.get("type"), str) and ("typeProperties" in value or "name" in value):
                # Inspect this activity's own metadata, never its child activities.
                metadata = {key: child for key, child in value.items() if key not in {"activities", "ifTrueActivities", "ifFalseActivities", "cases", "defaultActivities"}}
                literals = values(metadata)
                references = []

                def table_references(node):
                    if isinstance(node, dict):
                        table = literal(node.get("table") or node.get("tableName") or node.get("entityName"))
                        schema = literal(node.get("schema") or node.get("schemaName"))
                        if isinstance(table, str):
                            references.append({
                                "name": f"{schema}.{table}" if isinstance(schema, str) and "." not in table else table,
                                "defaultLakehouse": {
                                    "default_lakehouse_workspace_id": node.get("workspaceId", ""),
                                    "default_lakehouse": node.get("lakehouseId", ""),
                                },
                            })
                        for child_key, child in node.items():
                            if child_key not in {"activities", "ifTrueActivities", "ifFalseActivities", "cases", "defaultActivities"}:
                                table_references(child)
                    elif isinstance(node, list):
                        for child in node:
                            table_references(child)

                table_references(metadata)
                ids = {
                    change["id"] for change in changes for source in change["sources"]
                    if (any(match_table(source, candidate) for candidate in references) if references else
                        any(text == source["table"].casefold() for text in literals))
                }
                if ids:
                    matches.append({
                        "path": path, "name": value.get("name") or value["type"],
                        "changeIds": ids,
                    })
            for key, child in value.items():
                visit(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (index,))

    visit(document)
    return matches


def target_catalog(scoped, catalog, changes):
    """Refine table candidates to objects, retaining uncertainty rather than inventing flow."""
    selected_model = catalog["selection"]["semanticModel"]["path"].replace("\\", "/").strip("/")
    artifacts, nodes, edges = [], [], []
    issues = list(scoped["issues"])
    inventory = {key: dict(value) for key, value in scoped["inventoryScope"].items()}
    for artifact in scoped["artifacts"]:
        key = artifact["key"]
        if artifact["type"] == "Notebook":
            targets = []
            names = {name for change in changes if change["id"] in artifact["changeIds"]
                     for name in (change["column"], change["current"].get("sourceColumn")) if name}

            def references_column(cell):
                return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", _dax_code(cell["source"]), re.IGNORECASE)
                           or re.search(rf"""['"]{re.escape(name)}['"]""", cell["source"], re.IGNORECASE)
                           for name in names)

            cells = artifact["cells"]
            uncertain = any(issue.get("file") == key and "all code cells" in issue["message"]
                            for issue in scoped["issues"])
            if uncertain:
                evidence_lines = {
                    proof["line"] for operation in ("reads", "writes")
                    for dependency in artifact["context"][operation] for proof in dependency["evidence"]
                } | {call["line"] for call in artifact["context"]["calls"]}
                retained = {
                    index for index, cell in enumerate(cells)
                    if references_column(cell) or cell["parameters"] or any(
                        cell["startLine"] <= line < cell["startLine"] + len(cell["source"].splitlines())
                        for line in evidence_lines
                    )
                }
                definitions, dependencies = {}, []
                for index, cell in enumerate(cells):
                    try:
                        tree = parse_python(cell["source"], next(iter(artifact["files"])))
                    except SyntaxError:
                        dependencies.append(set())
                        continue
                    loads = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
                    stores = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
                    stores.update(node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef)))
                    stores.update(alias.asname or alias.name.split(".")[0] for node in ast.walk(tree)
                                  if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names)
                    dependencies.append({definitions[name] for name in loads if name in definitions})
                    definitions.update({name: index for name in stores})
                pending = list(retained)
                while pending:
                    for parent in dependencies[pending.pop()] - retained:
                        retained.add(parent)
                        pending.append(parent)
                cells = [cell for index, cell in enumerate(cells) if index in retained]
                issues.append({"file": key, "message": "Unresolved notebook flow: AI receives only evidenced cells and resolvable prerequisites; omitted dynamic dependencies require manual review."})
            for cell in cells:
                direct = references_column(cell)
                try:
                    statements = parse_python(cell["source"], next(iter(artifact["files"]))).body
                except SyntaxError:
                    statements = None
                if not direct and (cell["parameters"] or (
                    statements is not None and all(isinstance(node, (ast.Import, ast.ImportFrom)) for node in statements)
                )):
                    continue
                targets.append({
                    "id": _id(key, "cell", cell["cell"]), "artifactKey": key,
                    "objectType": "notebook_code", "objectName": f"Cell {cell['cell']}",
                    "cell": cell["cell"], "changeIds": artifact["changeIds"],
                    "confidence": "direct_reference" if direct else "lineage_candidate",
                    "reason": "Selected column reference in a lineage-scoped cell." if direct else
                              "Data-flow dependency or schema-level consumer; column flow is not proven.",
                    "evidence": [{"file": next(iter(artifact["files"])), "line": cell["startLine"]}],
                })
            if not targets:
                inventory[key] = {"status": "out_of_scope"}
                continue
            file = next(iter(artifact["files"]))
            item = {
                **artifact, "targets": targets, "cells": cells,
                "sourceRanges": {file: [(cell["startLine"], cell["startLine"] + len(cell["source"].splitlines()) - 1)
                                        for cell in cells]},
            }
            inventory[key]["selectedCells"] = [cell["cell"] for cell in cells]
            nodes.extend(targets)
            artifacts.append(item)
            continue
        if artifact["type"] != "SemanticModel":
            continue
        objects = _model_objects(artifact)
        _wire_model(objects, issues)
        by_id = {item["id"]: item for item in objects}
        seeds = {}
        table_ids = {item["table"].casefold(): item["id"] for item in objects if item["objectType"] == "table"}
        for change in changes:
            for table in artifact["context"]["tables"]:
                selected = key == selected_model and table["name"] == change["table"]
                matching = selected or any(
                    match_table(source, {"name": candidate["table"], "defaultLakehouse": {
                        "default_lakehouse_workspace_id": candidate.get("scope", {}).get("workspace", ""),
                        "default_lakehouse": candidate.get("scope", {}).get("lakehouse", ""),
                    }})
                    for source in change["sources"] for candidate in table["sources"]
                )
                # Transitive output tables retain the selected physical column only
                # as a candidate; arbitrary same-name model columns are not seeds.
                same_source_name = any(
                    source["table"].split(".")[-1].casefold() == candidate["table"].split(".")[-1].casefold()
                    for source in change["sources"] for candidate in table["sources"]
                )
                if not matching and not same_source_name:
                    matching = any(
                        match_table(source, lineage_table)
                        for source in table["sources"] for lineage_table in artifact["lineage"]["tables"]
                    ) and change["id"] in artifact["changeIds"]
                if not matching:
                    continue
                if table["name"].casefold() in table_ids:
                    seeds.setdefault(table_ids[table["name"].casefold()], set()).add(change["id"])
                physical = change["current"].get("sourceColumn") or change["column"]
                matched = [column for column in table["columns"] if (
                    selected and column["name"] == change["column"]
                ) or (not selected and (column.get("sourceColumn") or column["name"]).casefold() == physical.casefold())]
                for definition in matched:
                    for item in objects:
                        if item["table"] == table["name"] and item["name"] == definition["name"] and item["objectType"] in {"column", "calculated_column"}:
                            seeds.setdefault(item["id"], set()).add(change["id"])
                if not matched:
                    issues.append({"file": key, "message": f"No static column mapping found for {change['table']}.{change['column']} in {table['name']}; source bindings are targeted, but derived columns and their calculations require manual tracing."})
        consumers = {}
        for item in objects:
            for parent in item["dependencies"]:
                consumers.setdefault(parent, set()).add(item["id"])
        queue = deque(seeds)
        impacted = {key: set(ids) for key, ids in seeds.items()}
        while queue:
            parent = queue.popleft()
            for child in consumers.get(parent, set()):
                fresh = impacted[parent] - impacted.setdefault(child, set())
                if fresh:
                    impacted[child].update(fresh)
                    queue.append(child)
        # Every affected table's source binding is relevant even if the physical
        # source expression contains no explicit column list (Direct Lake / SELECT *).
        affected_names = {by_id[target]["table"] for target in impacted if by_id[target]["objectType"] == "table"}
        for item in objects:
            if item["objectType"] in {"partition", "calculated_table"} and item["table"] in affected_names:
                impacted.setdefault(item["id"], set()).update(impacted[table_ids[item["table"].casefold()]])
        targets = []
        for object_id, ids in impacted.items():
            item = by_id[object_id]
            item.update({
                "changeIds": sorted(ids), "confidence": "static_candidate",
                "reason": "Selected source mapping or static model dependency; whether an edit is needed depends on the requested change.",
                "evidence": [{"file": item["segment"]["file"], "line": item["segment"]["startLine"]}],
            })
            targets.append(item)
            for parent in item["dependencies"] & impacted.keys():
                edges.append({"source": parent, "target": object_id, "kind": "model_dependency", "confidence": "static"})
        if targets:
            item = _slice_artifact(artifact, targets, objects)
            artifacts.append(item)
            nodes.extend(item["targets"])
        else:
            inventory[key] = {"status": "out_of_scope"}

    # Activities are independent targets, not entire pipeline exports.
    links = {json.dumps(link, sort_keys=True): link for link in catalog["lineage"]["pipelineLinks"]
             if link["status"] == "resolved"}
    affected_ids = {artifact["key"]: set(artifact["changeIds"]) for artifact in artifacts}
    metadata = {}
    for artifact in catalog["artifacts"]:
        if artifact["type"] == "DataPipeline":
            metadata[artifact["key"]] = _pipeline_metadata(artifact, changes)
            for match in metadata[artifact["key"]]:
                affected_ids.setdefault(artifact["key"], set()).update(match["changeIds"])
    changed = True
    while changed:
        changed = False
        for link in links.values():
            fresh = affected_ids.get(link["targetKey"], set()) - affected_ids.get(link["pipelineKey"], set())
            if fresh:
                affected_ids.setdefault(link["pipelineKey"], set()).update(fresh)
                changed = True
    for artifact in catalog["artifacts"]:
        if artifact["type"] != "DataPipeline" or artifact["key"] not in affected_ids:
            continue
        file, code = next(iter(artifact["files"].items()))
        spans = _json_spans(code)
        targets = []
        objects = []
        activities = {
            match["path"]: {
                "name": match["name"], "ids": match["changeIds"],
                "reason": "Static physical-table reference in pipeline metadata; schema mappings and expressions require review.",
            } for match in metadata.get(artifact["key"], [])
        }
        for link in links.values():
            if link["pipelineKey"] != artifact["key"] or link["targetKey"] not in affected_ids:
                continue
            pointer = link["activityPath"]
            path = tuple(int(part) if part.isdigit() else part.replace("~1", "/").replace("~0", "~")
                         for part in pointer.strip("/").split("/"))
            activities[path] = {
                "name": link["activity"] or pointer, "ids": affected_ids[link["targetKey"]],
                "reason": "Invokes an impacted artifact; review parameters and execution dependencies, not an automatic metadata edit.",
            }
        for path, activity in activities.items():
            pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)
            if path not in spans:
                issues.append({"file": file, "message": f"Pipeline activity {pointer} could not be located; not sent to AI."})
                continue
            item = {
                "id": _id(artifact["key"], pointer), "artifactKey": artifact["key"],
                "objectType": "pipeline_activity", "objectName": activity["name"],
                "changeIds": sorted(activity["ids"]), "confidence": "lineage_candidate",
                "reason": activity["reason"],
                "jsonPointer": pointer, "evidence": [{"file": file, "jsonPointer": pointer}],
                "segment": _segment(file, code, *spans[path]), "dependencies": set(),
            }
            targets.append(item)
            objects.append(item)
        metadata_targets = {}
        for activity in list(targets):
            source = activity["segment"]["source"]
            references = [
                ("parameters", match[1]) for match in re.finditer(
                    r"pipeline\(\)\.parameters\.(\w+)", source, re.IGNORECASE,
                )
            ] + [
                ("variables", match[1]) for match in re.finditer(
                    r"""variables\(\s*['"]([^'"]+)['"]\s*\)""", source, re.IGNORECASE,
                )
            ]
            for collection, name in references:
                path = ("properties", collection, name)
                if path not in spans:
                    issues.append({"file": file, "message": f"Pipeline {collection} reference {name} is unresolved; supply its runtime value before editing."})
                    continue
                target_id = _id(artifact["key"], collection, name)
                if target_id not in metadata_targets:
                    metadata_targets[target_id] = {
                        "id": target_id, "artifactKey": artifact["key"],
                        "objectType": "pipeline_parameter" if collection == "parameters" else "pipeline_variable",
                        "objectName": name, "changeIds": [], "confidence": "lineage_candidate",
                        "reason": "Declaration referenced by an impacted activity; validate its value and callers before changing metadata.",
                        "jsonPointer": f"/properties/{collection}/{name}",
                        "evidence": [{"file": file, "jsonPointer": f"/properties/{collection}/{name}"}],
                        "segment": _segment(file, code, *spans[path]), "dependencies": set(),
                    }
                target = metadata_targets[target_id]
                target["changeIds"] = sorted(set(target["changeIds"]) | set(activity["changeIds"]))
                activity["dependencies"].add(target_id)
                edges.append({"source": target_id, "target": activity["id"],
                              "kind": "parameter_dependency", "confidence": "static"})
        targets.extend(metadata_targets.values())
        objects.extend(metadata_targets.values())
        if targets:
            item = _slice_artifact({
                **artifact, "changeIds": sorted(affected_ids[artifact["key"]]),
                "lineage": {"tables": [], "notebookLinks": [], "pipelineLinks": [
                    link for link in links.values() if link["pipelineKey"] == artifact["key"]
                ]},
            }, targets, objects)
            artifacts.append(item)
            nodes.extend(item["targets"])
            inventory[artifact["key"]] = {}
    for artifact in artifacts:
        inventory[artifact["key"]].update({"targetCount": len(artifact["targets"])})
    for change in changes:
        change_id = "change:" + change["id"]
        nodes.append({"id": change_id, "objectType": "changed_column",
                      "objectName": f"{change['table']}.{change['column']}"})
        for artifact in artifacts:
            for target in artifact["targets"]:
                if change["id"] in target["changeIds"]:
                    edges.append({"source": change_id, "target": target["id"],
                                  "kind": "potential_impact", "confidence": target["confidence"]})
    # Artifact-level lineage makes cross-artifact paths explicit alongside object edges.
    artifact_nodes = {artifact["key"]: {
        "id": "artifact:" + artifact["key"], "artifactKey": artifact["key"],
        "objectType": "artifact", "objectName": artifact["name"], "artifactType": artifact["type"],
    } for artifact in artifacts}
    for node in nodes:
        if node.get("artifactKey") in artifact_nodes:
            edges.append({"source": artifact_nodes[node["artifactKey"]]["id"], "target": node["id"],
                          "kind": "contains", "confidence": "static"})
    for link in links.values():
        if link["pipelineKey"] in artifact_nodes and link["targetKey"] in artifact_nodes:
            edges.append({"source": artifact_nodes[link["pipelineKey"]]["id"],
                          "target": artifact_nodes[link["targetKey"]]["id"],
                          "kind": "invokes", "confidence": "static"})
    for link in catalog["lineage"]["notebookLinks"]:
        if link["notebookKey"] in artifact_nodes and link["targetKey"] in artifact_nodes:
            edges.append({"source": artifact_nodes[link["notebookKey"]]["id"],
                          "target": artifact_nodes[link["targetKey"]]["id"],
                          "kind": "calls", "confidence": "static"})
    original_artifacts = {artifact["key"]: artifact for artifact in catalog["artifacts"]}
    for table in catalog["lineage"]["tables"]:
        writers = {owner["notebookKey"] for owner in table["writers"]} & artifact_nodes.keys()
        readers = {owner["notebookKey"] for owner in table["readers"]} & artifact_nodes.keys()
        models = {artifact["key"] for artifact in artifacts if artifact["type"] == "SemanticModel"
                  and any(match_table(source, table)
                          for model_table in original_artifacts[artifact["key"]]["context"]["tables"]
                          for source in model_table["sources"])}
        if not writers and not readers and not models:
            continue
        table_id = "source:" + table["key"]
        nodes.append({"id": table_id, "objectType": "source_table", "objectName": table["name"]})
        for owner in writers:
            edges.append({"source": artifact_nodes[owner]["id"], "target": table_id, "kind": "writes", "confidence": "table_level"})
        for owner in readers | models:
            edges.append({"source": table_id, "target": artifact_nodes[owner]["id"], "kind": "reads", "confidence": "table_level"})
    return {
        **scoped, "artifacts": artifacts, "inventoryScope": inventory, "issues": issues,
        "impactLineage": {"nodes": list(artifact_nodes.values()) + nodes, "edges": edges},
    }
