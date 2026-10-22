"""Selected-model sources and their evidence-backed repository relationships."""

from pathlib import PurePosixPath
import json
import re
from urllib.parse import unquote, urlparse
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, UnsupportedError
from sqlglot.optimizer.scope import traverse_scope


GRAPH_VERSION = 2
_NAME = r"'(?:[^']|'')*'|[^\s=]+"
_M_STRING = r'"(?:[^"]|"")*"'


def _name(value):
    value = value.strip()
    return value[1:-1].replace("''", "'") if value.startswith("'") and value.endswith("'") else value


def _m_string(value):
    return value[1:-1].replace('""', '"').replace("#(lf)", "\n").replace("#(cr)", "\r").replace("#(tab)", "\t")


def _m_code(expression):
    return re.sub(
        rf"{_M_STRING}|//[^\n]*|/\*[\s\S]*?\*/",
        lambda match: match[0] if match[0].startswith('"') else "\n" * match[0].count("\n"),
        expression,
    )


def _blocks(text, keyword):
    pattern = rf"(?m)^(?P<indent>[ \t]*){keyword}\s+(?P<name>{_NAME})(?P<tail>[^\n]*)"
    matches = list(re.finditer(pattern, text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield _name(match["name"]), match["tail"].strip(), text[match.end():end], text.count("\n", 0, match.start()) + 1


def _binding(expression):
    expression = _m_code(expression)
    scopes = []
    for lake in re.finditer(rf'AzureStorage\.DataLake\s*\(\s*({_M_STRING})\s*', expression):
        parsed = urlparse(_m_string(lake[1]))
        if parsed.hostname and parsed.hostname.endswith(".dfs.fabric.microsoft.com"):
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if len(parts) >= 2:
                scopes.append({"workspace": parts[0], "lakehouse": parts[1]})
    for database in re.finditer(rf'Sql\.Database\s*\(\s*({_M_STRING})\s*,\s*({_M_STRING})', expression):
        scopes.append({"server": _m_string(database[1]), "database": _m_string(database[2])})
    return scopes[0] if scopes and all(scope == scopes[0] for scope in scopes) else {}


def _property(text, key):
    match = re.search(rf"(?m)^\s*{key}:\s*(.*?)\s*$", text)
    return _name(match[1]) if match else ""


def _sources(source, expressions, evidence, issue):
    kind = source.get("type", "").casefold()
    if kind in {"calculated", "calculatedtable"}:
        return [], "calculated"
    if kind == "entity":
        entity = source.get("entityName")
        schema = source.get("schemaName", "")
        expression_name = source.get("expressionSource", "")
        expression = expressions.get(expression_name, "")
        if not isinstance(entity, str) or not entity:
            issue("Entity partition is missing entityName.")
            return [], "unresolved"
        scope = _binding(expression)
        if not scope:
            issue(f"Source scope could not be resolved for expression {expression_name or '(missing)'}.")
        return [{
            "table": f"{schema}.{entity}" if schema else entity,
            "scope": scope, "evidence": evidence,
        }], "resolved" if scope else "unverified"
    expression = source.get("expression", source.get("query", ""))
    if isinstance(expression, list) and all(isinstance(line, str) for line in expression):
        expression = "\n".join(expression)
    if not isinstance(expression, str):
        issue("Partition expression must be text.")
        return [], "unresolved"
    if kind != "query":
        expression = _m_code(expression)
    # Only literal navigation records and SQL are interpreted; M is never executed.
    scope = _binding(expression)
    if not scope:
        referenced = [
            value for key, value in expressions.items()
            if re.search(rf'(?<![\w])(?:{re.escape(key)}|#"{re.escape(key)}")(?![\w])', expression)
        ]
        scopes = [_binding(value) for value in referenced if _binding(value)]
        if scopes and all(value == scopes[0] for value in scopes):
            scope = scopes[0]
    tables = []
    for record in re.finditer(r"\[([^\[\]]*)\]", expression):
        fields = {
            match[1].casefold(): _m_string(match[2])
            for match in re.finditer(rf'(\w+)\s*=\s*({_M_STRING})', record[1])
        }
        if "schema" in fields and "item" in fields:
            tables.append(f'{fields["schema"]}.{fields["item"]}')
    queries = []
    if kind == "query":
        queries.append(expression)
    queries.extend(_m_string(match[1]) for match in re.finditer(rf'\bQuery\s*=\s*({_M_STRING})', expression))
    queries.extend(_m_string(match[1]) for match in re.finditer(
        rf'Value\.NativeQuery\s*\([^,]+,\s*({_M_STRING})', expression
    ))
    for query in queries:
        try:
            for statement in sqlglot.parse(query, read="tsql"):
                if statement is not None:
                    for scope_item in traverse_scope(statement):
                        tables.extend(
                            ".".join(part.name for part in table.parts)
                            for _, table in scope_item.selected_sources.values() if isinstance(table, exp.Table)
                        )
        except (ParseError, UnsupportedError, OptimizeError) as exc:
            issue(f"Could not parse semantic model SQL: {exc}")
    if not tables:
        issue("No static physical table source found; dynamic or unsupported partition expression.")
    return [
        {"table": table, "scope": scope, "evidence": evidence}
        for table in dict.fromkeys(tables)
    ], "resolved" if tables and scope else "unverified" if tables else "unresolved"


def read_model(root, model, issue):
    raw_path = model["path"]
    path = PurePosixPath(raw_path.replace("\\", "/"))
    if ".." in path.parts or any(":" in part for part in path.parts):
        raise ValueError("Invalid semantic model repository path.")
    folder = root.joinpath(*[part for part in path.parts if part != "/"])
    if not folder.resolve().is_relative_to(root.resolve()):
        raise ValueError("Semantic model path is outside the repository snapshot.")
    expressions = {}
    tables = []

    def text(file):
        return file.read_text(encoding="utf-8-sig")

    def add_table(name, partitions, file, line):
        sources = []
        statuses = []
        for partition_name, source, number in partitions:
            evidence = [{"file": str(file.relative_to(root)), "line": number, "partition": partition_name}]
            found, status = _sources(
                source, expressions, evidence,
                lambda message: issue(str(file.relative_to(root)), message, number),
            )
            sources.extend(found)
            statuses.append(status)
        if not partitions:
            issue(str(file.relative_to(root)), f"Table {name} has no exported partitions.", line)
        tables.append({
            "name": name, "sources": sources,
            "status": "calculated" if statuses and all(status == "calculated" for status in statuses)
            else "partial" if sources and "unresolved" in statuses else "resolved" if sources else "unresolved",
            "evidence": [{"file": str(file.relative_to(root)), "line": line}],
        })

    try:
        bim = folder / "model.bim"
        definition = folder / "definition"
        if bim.is_file():
            document = json.loads(text(bim))
            content = document.get("model", document)
            expressions = {
                item["name"]: "\n".join(item["expression"]) if isinstance(item["expression"], list) else item["expression"]
                for item in content.get("expressions", [])
            }
            for table in content.get("tables", []):
                add_table(table["name"], [
                    (partition["name"], partition["source"], None)
                    for partition in table.get("partitions", [])
                ], bim, None)
        elif definition.is_dir():
            expression_file = definition / "expressions.tmdl"
            if expression_file.is_file():
                expressions = {name: body for name, _, body, _ in _blocks(text(expression_file), "expression")}
            files = sorted((definition / "tables").glob("*.tmdl"))
            for file in files:
                contents = text(file)
                for name, _, body, line in _blocks(contents, "table"):
                    partitions = []
                    for partition, tail, body, number in _blocks(body, "partition"):
                        kind = tail.removeprefix("=").strip()
                        source = {"type": kind}
                        if kind == "entity":
                            source.update({key: _property(body, key) for key in (
                                "entityName", "schemaName", "expressionSource",
                            )})
                        else:
                            match = re.search(r"(?m)^([ \t]*)source\s*=\s*(.*)", body)
                            if match:
                                indent = len(match[1].expandtabs(4))
                                parts = [match[2]]
                                for child in body[match.end():].splitlines()[1:]:
                                    if child.strip() and len(child) - len(child.lstrip()) == 0:
                                        break
                                    if child.strip() and len(child[:len(child) - len(child.lstrip())].expandtabs(4)) <= indent:
                                        break
                                    parts.append(child)
                                source["expression"] = "\n".join(parts).strip().strip("`")
                        partitions.append((partition, source, line + number - 1))
                    add_table(name, partitions, file, line)
        else:
            issue(raw_path, "Selected semantic model has no model.bim or TMDL definition.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        issue(raw_path, f"Could not read selected semantic model: {exc}")
    if not tables:
        issue(raw_path, "No semantic model tables could be read from the selected snapshot.")
    return tables


def _scope_value(value):
    value = str(value).casefold()
    value = value.removesuffix(".lakehouse")
    try:
        return str(UUID(value))
    except ValueError:
        return value


def _is_guid(value):
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def match_table(source, table):
    """Return confirmed/unverified, or None for incompatible table identities."""
    parts = table["name"].replace("`", "").split(".")
    source_parts = source["table"].replace("`", "").split(".")
    if "/" in table["name"] or parts[-1].casefold() != source_parts[-1].casefold():
        return None
    schema_known = len(parts) >= 2 and len(source_parts) >= 2
    if schema_known and parts[-2].casefold() != source_parts[-2].casefold():
        return None
    binding = table["defaultLakehouse"]
    scope = {
        "workspace": parts[-4] if len(parts) >= 4 else binding.get("default_lakehouse_workspace_id", ""),
        "lakehouse": parts[-3] if len(parts) >= 3 else binding.get("default_lakehouse", ""),
    }
    verified = bool(source["scope"]) and schema_known
    for key, expected in source["scope"].items():
        actual = scope.get(key, "")
        expected, actual = _scope_value(expected), _scope_value(actual)
        if not actual or (_is_guid(expected) != _is_guid(actual)):
            verified = False
        elif expected != actual:
            return None
    return "confirmed" if verified else "unverified"


def build_model_graph(root, model, mapping, issue):
    model_tables = read_model(root, model, issue)
    nodes = {}
    edges = {}

    def node(key, kind, label, **details):
        nodes.setdefault(key, {"id": key, "type": kind, "label": label, **details})
        return key

    def edge(source, target, label, status="confirmed", evidence=None, **details):
        key = (source, target, label, status)
        entry = edges.setdefault(key, {
            "id": f"edge:{len(edges)}", "source": source, "target": target, "label": label,
            "status": status, "evidence": [], **details,
        })
        for item in evidence or []:
            if item not in entry["evidence"]:
                entry["evidence"].append(item)
        for context in details.get("contexts", []):
            if context not in entry.setdefault("contexts", []):
                entry["contexts"].append(context)

    model_id = node("model:" + model["path"], "SemanticModel", model["name"], path=model["path"])
    notebooks = {item["key"]: item for item in mapping["notebooks"]}
    pipelines = {item["key"]: item for item in mapping["pipelines"]}
    relevant_notebooks = set()
    relevant_pipelines = set()

    def notebook_node(key):
        item = notebooks[key]
        relevant_notebooks.add(key)
        return node("notebook:" + key, "Notebook", item["name"], path=item["file"])

    def pipeline_node(key):
        item = pipelines[key]
        relevant_pipelines.add(key)
        return node("pipeline:" + key, "DataPipeline", item["name"], path=item["file"])

    for index, table in enumerate(model_tables):
        sources = table["sources"] or [None]
        for number, source in enumerate(sources):
            table_id = node(
                f"table:{index}:{number}", "Table", table["name"],
                physicalTable=source["table"] if source else "", scope=source["scope"] if source else {},
                status=table["status"], evidence=source["evidence"] if source else table["evidence"],
            )
            edge(model_id, table_id, "uses", evidence=table["evidence"])
            matches = [
                (candidate, status) for candidate in mapping["tables"]
                if source and (status := match_table(source, candidate))
            ]
            if source and not any(candidate["writers"] for candidate, _ in matches):
                issue(model["path"], f"No notebook producer found for {table['name']} ({source['table']}).")
            for candidate, status in matches:
                if status == "unverified":
                    issue(model["path"], f"Unverified source scope: {table['name']} -> {candidate['name']}. Schema/workspace/lakehouse identity is not proven.")
                for owners, operation, label in (
                    ("writers", "writes", "written by"), ("readers", "reads", "read by"),
                ):
                    for owner in candidate[owners]:
                        notebook = notebooks[owner["notebookKey"]]
                        pipeline_key = owner["pipelineKey"]
                        contexts = [
                            link for link in mapping["pipelineLinks"]
                            if link["targetKey"] == notebook["key"] and link["pipelineKey"] == pipeline_key
                        ] if pipeline_key else [notebook]
                        evidence = [
                            item for context in contexts for dependency in context[operation]
                            if dependency["tableKey"] == candidate["key"] and dependency["status"] == "resolved"
                            for item in dependency["evidence"]
                        ]
                        if not evidence:
                            continue
                        notebook_id = notebook_node(notebook["key"])
                        edge(table_id, notebook_id, label, status, evidence, tableKey=candidate["key"])
                        if pipeline_key:
                            for context in contexts:
                                if any(dependency["tableKey"] == candidate["key"]
                                       and dependency["status"] == "resolved" for dependency in context[operation]):
                                    edge(notebook_id, pipeline_node(pipeline_key), "run by",
                                         evidence=[context["evidence"]], contexts=[{
                                             "activity": context["activity"], "activityPath": context["activityPath"],
                                             "parameters": context["parameters"],
                                         }])

    # Follow reverse invocation links to include orchestrators, terminating on cycles.
    visited_notebooks = set()
    visited_pipelines = set()
    while relevant_notebooks - visited_notebooks or relevant_pipelines - visited_pipelines:
        for key in sorted(relevant_notebooks - visited_notebooks):
            visited_notebooks.add(key)
            for call in mapping["notebookLinks"]:
                if call["targetKey"] == key and call["status"] == "resolved":
                    edge(notebook_node(key), notebook_node(call["notebookKey"]), "called by", evidence=[call["evidence"]])
            # Invocations of notebook callers are relevant even when the caller has no direct writes.
            if any(call["notebookKey"] == key and call["targetKey"] in relevant_notebooks
                   for call in mapping["notebookLinks"] if call["status"] == "resolved"):
                for link in mapping["pipelineLinks"]:
                    if link["kind"] == "TridentNotebook" and link["targetKey"] == key and link["status"] == "resolved":
                        edge(notebook_node(key), pipeline_node(link["pipelineKey"]), "run by",
                             evidence=[link["evidence"]], contexts=[{
                                 "activity": link["activity"], "activityPath": link["activityPath"],
                                 "parameters": link["parameters"],
                             }])
        for key in sorted(relevant_pipelines - visited_pipelines):
            visited_pipelines.add(key)
            for link in mapping["pipelineLinks"]:
                if link["kind"] != "TridentNotebook" and link["targetKey"] == key and link["status"] == "resolved":
                    edge(pipeline_node(key), pipeline_node(link["pipelineKey"]), "invoked by", evidence=[link["evidence"]])
    return {
        "version": GRAPH_VERSION, "modelPath": model["path"], "root": model_id,
        "nodes": list(nodes.values()), "edges": list(edges.values()),
    }, model_tables
