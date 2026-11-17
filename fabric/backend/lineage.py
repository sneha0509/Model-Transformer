"""Static, evidence-backed lineage for downloaded Fabric Git artifacts."""

import ast
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile

from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError, UnsupportedError
from sqlglot.optimizer.scope import traverse_scope

from fabric.backend.filesystem import filesystem_path
from fabric.backend.model_lineage import build_model_graph
from fabric.backend.notebook_parsing import parse_python, parse_spark_sql, sql_placeholders
from shared.paths import PROJECT_ROOT


REPORTS_DIR = PROJECT_ROOT / "output" / "fabric" / "lineage"
EXCLUDED_DIRECTORIES = {".git", "legacy", "venv", "node_modules", "__pycache__"}
LIMITATIONS = [
    "Static analysis of the downloaded snapshot only; notebooks are never executed.",
    "All Fabric pipelines and notebooks in the snapshot are included, not only the selected model.",
    "Table reads/writes are notebook-level dependencies, not column-level or per-output data flow.",
    "Notebook defaults and pipeline parameter overrides are separate execution contexts.",
    "Unqualified tables use exported default lakehouse bindings. Names and GUIDs are not assumed equivalent.",
    "Dynamic expressions, missing artifacts, unsupported code and parse errors are reported as gaps.",
    "CTEs and session temporary views are excluded from persisted table dependencies.",
    "The selected-model graph follows table producers and their callers; the full inventory remains available below.",
    "Dashed graph relationships are table-name matches whose workspace/lakehouse scope is unverified, not confirmed lineage.",
]


def _issue(issues, path, message, line=None):
    item = {"file": path, "line": line, "message": message}
    if item not in issues:
        issues.append(item)


def _json(path):
    with path.open(encoding="utf-8-sig") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def _inventory(root, issues):
    items = []
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(
            name for name in folders
            if name.casefold() not in EXCLUDED_DIRECTORIES
            and not (Path(directory) / name).is_symlink()
        )
        folder = Path(directory)
        kind = {".Notebook": "Notebook", ".DataPipeline": "DataPipeline"}.get(folder.suffix)
        if not kind:
            continue
        relative = str(folder.relative_to(root))
        platform = {}
        if ".platform" in files:
            try:
                platform = _json(folder / ".platform")
            except (ValueError, OSError) as exc:
                _issue(issues, relative, f"Could not read .platform: {exc}")
        else:
            _issue(issues, relative, "Missing .platform; exported logical ID is unavailable.")
        try:
            metadata = _object(platform.get("metadata", {}), ".platform metadata")
            config = _object(platform.get("config", {}), ".platform config")
        except ValueError as exc:
            _issue(issues, relative, str(exc))
            metadata, config = {}, {}
        if metadata.get("type", kind) != kind:
            _issue(issues, relative, "Folder type conflicts with .platform metadata.")
            continue
        content_name = "pipeline-content.json" if kind == "DataPipeline" else "notebook-content.py"
        if kind == "Notebook" and content_name not in files:
            content_name = next((name for name in ("notebook-content.ipynb", "notebook-content.scala")
                                 if name in files), content_name)
        logical_id = config.get("logicalId", "")
        if not isinstance(logical_id, str):
            _issue(issues, relative, ".platform logicalId must be a string.")
            logical_id = ""
        name = metadata.get("displayName") or folder.stem
        if not isinstance(name, str):
            _issue(issues, relative, ".platform displayName must be a string.")
            name = folder.stem
        items.append({
            "key": relative,
            "id": logical_id,
            "name": name,
            "type": kind,
            "file": str((folder / content_name).relative_to(root)),
        })
    return items


def _expression(node, env):
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id, "{" + node.id + "}")
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                evaluated = _expression(value.value, env)
                if value.format_spec is not None:
                    return "{" + ast.unparse(node) + "}"
                parts.append(str(evaluated))
            else:
                parts.append(str(value.value))
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _expression(node.left, env), _expression(node.right, env)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_expression(value, env) for value in node.elts]
    if isinstance(node, ast.Dict):
        if all(key is not None for key in node.keys):
            return {_expression(key, env): _expression(value, env) for key, value in zip(node.keys, node.values)}
    if isinstance(node, ast.Subscript):
        value, key = _expression(node.value, env), _expression(node.slice, env)
        if isinstance(value, dict) and key in value:
            return value[key]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        value = _expression(node.func.value, env)
        if isinstance(value, str) and "{" not in value and not node.args and not node.keywords:
            if node.func.attr == "lower":
                return value.lower()
            if node.func.attr == "upper":
                return value.upper()
            if node.func.attr == "strip":
                return value.strip()
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = _expression(node.left, env), _expression(node.comparators[0], env)
        if not any(isinstance(value, str) and "{" in value for value in (left, right)):
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
    return "{" + ast.unparse(node) + "}"


def _notebook_source(path):
    if path.suffix == ".ipynb":
        notebook = _json(path)
        chunks = []
        parameter_lines = set()
        line = 1
        cells = notebook.get("cells", [])
        if not isinstance(cells, list):
            raise ValueError("Notebook cells must be a JSON array.")
        for cell in cells:
            cell = _object(cell, "Notebook cell")
            if cell.get("cell_type") != "code":
                continue
            cell_source = cell.get("source", [])
            if not isinstance(cell_source, (str, list)) or (
                isinstance(cell_source, list) and not all(isinstance(line, str) for line in cell_source)
            ):
                raise ValueError("Notebook cell source must be a string or an array of strings.")
            chunk = "".join(cell_source)
            chunks.append(chunk)
            count = chunk.count("\n") + 1
            cell_metadata = _object(cell.get("metadata", {}), "Notebook cell metadata")
            if "parameters" in cell_metadata.get("tags", []):
                parameter_lines.update(range(line, line + count))
            line += count
        source = "\n".join(chunks)
        metadata = _object(notebook.get("metadata", {}), "Notebook metadata")
        dependencies = _object(metadata.get("dependencies", {}), "Notebook dependencies")
        binding = _object(dependencies.get("lakehouse", {}), "Notebook lakehouse binding")
        return source, binding, parameter_lines
    source = path.read_text(encoding="utf-8-sig")
    comment = "//" if path.suffix == ".scala" else "#"
    metadata_lines = []
    for line in source.splitlines():
        if line.startswith(f"{comment} META "):
            metadata_lines.append(line[len(comment) + 6:])
        elif metadata_lines:
            break
    metadata = json.loads("\n".join(metadata_lines)) if metadata_lines else {}
    parameter_lines = set()
    parameter_cell = False
    for number, line in enumerate(source.splitlines(), 1):
        if line.startswith(f"{comment} PARAMETERS CELL"):
            parameter_cell = True
        elif line.startswith((f"{comment} CELL ", f"{comment} METADATA ")):
            parameter_cell = False
        elif parameter_cell:
            parameter_lines.add(number)
    dependencies = _object(metadata.get("dependencies", {}), "Notebook dependencies")
    return source, _object(dependencies.get("lakehouse", {}), "Notebook lakehouse binding"), parameter_lines


class NotebookScanner:
    def __init__(self, item, issues, parameters, binding):
        self.item = item
        self.issues = issues
        self.parameters = parameters
        self.binding = binding
        self.env = {}
        self.tables = {}
        self.views = set()
        self.calls = []

    def issue(self, message, line):
        _issue(self.issues, self.item["file"], message, line)

    def table(self, name, operation, line, method):
        if not isinstance(name, str) or not name.strip():
            self.issue(f"Cannot resolve {method} table argument.", line)
            return
        name = name.strip().replace("`", "")
        if operation == "read" and name.casefold() in self.views:
            return
        dynamic = "{" in name or name.startswith("@")
        if dynamic:
            self.issue(f"Dynamic table reference: {name}", line)
        parts = name.split(".")
        binding = {}
        if len(parts) < 3 and "/" not in name:
            binding = self.binding
        identity = name if "/" in name else name.casefold()
        if binding:
            identity = "|".join((
                str(binding.get("default_lakehouse_workspace_id", "")),
                str(binding.get("default_lakehouse", "")),
                identity,
            ))
        key = (identity, operation)
        entry = self.tables.setdefault(key, {
            "tableKey": identity, "table": name, "operation": operation,
            "status": "dynamic" if dynamic else "resolved",
            "defaultLakehouse": binding, "evidence": [],
        })
        evidence = {"file": self.item["file"], "line": line, "method": method}
        if evidence not in entry["evidence"]:
            entry["evidence"].append(evidence)

    def sql(self, query, line):
        if not isinstance(query, str):
            self.issue("Cannot resolve spark.sql query.", line)
            return
        prepared, placeholders = sql_placeholders(query)
        candidates = [prepared]
        if placeholders:
            self.issue(
                "SQL query contains unresolved expressions; only static dependencies are analyzed: "
                + ", ".join(dict.fromkeys(placeholders.values())), line,
            )
            keys = "|".join(re.escape(key) for key in placeholders)
            # A standalone interpolated WHERE/JOIN clause is not a table identifier.
            without_fragments = re.sub(
                rf"(?m)^[ \t]*(?:{keys})[ \t]*(?=;?[ \t]*\r?$)", "", prepared,
            )
            if without_fragments != prepared:
                candidates.append(without_fragments)
        for index, candidate in enumerate(candidates):
            try:
                statements = parse_spark_sql(candidate)
            except (ParseError, TokenError, UnsupportedError) as exc:
                if index + 1 < len(candidates):
                    continue
                self.issue(f"SQL parse error: {str(exc)[:400]}", line)
                return
            if not any(isinstance(statement, exp.Command) for statement in statements) or index + 1 == len(candidates):
                break
        for statement in statements:
            if statement is None:
                continue
            if isinstance(statement, exp.Command):
                self.issue(f"Unsupported SQL command: {statement.sql()[:120]}", line)
                continue
            target = statement.args.get("this")
            if isinstance(target, exp.Schema):
                target = target.this
            targets = []
            if isinstance(statement, (exp.Create, exp.Insert, exp.Update, exp.Delete, exp.Merge,
                                      exp.Drop, exp.Alter, exp.TruncateTable)):
                if isinstance(target, exp.Table):
                    targets.append(target)
                elif isinstance(statement, exp.TruncateTable):
                    targets.extend(statement.find_all(exp.Table))
            temporary = isinstance(statement, exp.Create) and any(
                isinstance(prop, exp.TemporaryProperty) for prop in statement.find_all(exp.Property)
            )
            # Scope resolution distinguishes physical tables from CTE references.
            try:
                physical = {
                    id(source) for scope in traverse_scope(statement)
                    for _, source in scope.selected_sources.values() if isinstance(source, exp.Table)
                }
            except OptimizeError as exc:
                self.issue(f"Cannot resolve SQL scopes: {exc}", line)
                continue
            cte_names = {cte.alias.casefold() for cte in statement.find_all(exp.CTE)}
            for table in statement.find_all(exp.Table):
                name = ".".join(part.name for part in table.parts)
                for key, value in placeholders.items():
                    name = name.replace(key, value)
                if any(table is target for target in targets):
                    if temporary:
                        self.views.add(name.casefold())
                    else:
                        self.table(name, "write", line, type(statement).__name__)
                        if isinstance(statement, (exp.Update, exp.Delete, exp.Merge)):
                            self.table(name, "read", line, type(statement).__name__)
                elif id(table) in physical or (
                    not isinstance(statement, exp.Query)
                    and (table.db or table.name.casefold() not in cte_names)
                ):
                    self.table(name, "read", line, "spark.sql")

    def expression_calls(self, expression):
        calls = sorted(
            (node for node in ast.walk(expression) if isinstance(node, ast.Call)),
            key=lambda node: (node.lineno, -node.col_offset),
        )
        for call in calls:
            if not isinstance(call.func, ast.Attribute):
                continue
            method = call.func.attr
            receiver = ast.unparse(call.func.value)
            argument = call.args[0] if call.args else next(
                (keyword.value for keyword in call.keywords
                 if keyword.arg in {"sqlQuery", "tableName", "name", "path"}), None
            )
            value = _expression(argument, self.env)
            if method == "sql" and receiver == "spark":
                self.sql(value, call.lineno)
            elif method in {"saveAsTable", "insertInto"}:
                self.table(value, "write", argument.lineno if argument else call.lineno, method)
            elif method == "table" and receiver in {"spark", "spark.read"}:
                self.table(value, "read", call.lineno, method)
            elif method in {"createOrReplaceTempView", "createTempView"}:
                if isinstance(value, str):
                    self.views.add(value.casefold())
            elif method in {"load", "save", "parquet", "delta", "csv", "json", "synapsesql",
                            "forName", "forPath"}:
                if "read" in receiver or "write" in receiver or "DeltaTable" in receiver:
                    operation = "write" if "write" in receiver else "read"
                    self.table(value, operation, call.lineno, method)
            elif method in {"run", "runMultiple"} and receiver.endswith(".notebook"):
                if method == "run":
                    self.calls.append({"reference": value, "line": call.lineno})
                elif isinstance(value, dict) and isinstance(value.get("activities"), list):
                    for activity in value["activities"]:
                        if isinstance(activity, dict):
                            self.calls.append({"reference": activity.get("path"), "line": call.lineno})
                else:
                    self.issue("Cannot statically resolve notebook.runMultiple DAG.", call.lineno)

    def statements(self, statements, parameter_lines):
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                self.expression_calls(statement)
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        if target.id in self.parameters and statement.lineno in parameter_lines:
                            self.env[target.id] = self.parameters[target.id]
                        else:
                            self.env[target.id] = _expression(statement.value, self.env)
            elif isinstance(statement, ast.If):
                self.expression_calls(statement.test)
                condition = _expression(statement.test, self.env)
                if isinstance(condition, bool):
                    self.statements(statement.body if condition else statement.orelse, parameter_lines)
                else:
                    self.issue("Conditional execution cannot be resolved; both branches are included.", statement.lineno)
                    original = self.env.copy()
                    self.statements(statement.body, parameter_lines)
                    left = self.env.copy()
                    self.env = original.copy()
                    self.statements(statement.orelse, parameter_lines)
                    right = self.env
                    self.env = {
                        key: left.get(key) if left.get(key) == right.get(key) else "{" + key + "}"
                        for key in left.keys() | right.keys()
                    }
            elif isinstance(statement, (ast.For, ast.While, ast.FunctionDef, ast.AsyncFunctionDef)):
                self.issue("Loop/function dependencies require runtime analysis.", statement.lineno)
                self.statements(statement.body, parameter_lines)
                if hasattr(statement, "orelse"):
                    self.statements(statement.orelse, parameter_lines)
            elif isinstance(statement, (ast.With, ast.Try)):
                self.issue("Context/exception-dependent execution is included conservatively.", statement.lineno)
                self.statements(statement.body, parameter_lines)
                if isinstance(statement, ast.Try):
                    for handler in statement.handlers:
                        self.statements(handler.body, parameter_lines)
                    self.statements(statement.orelse + statement.finalbody, parameter_lines)
            else:
                self.expression_calls(statement)


def _scan_scala(source, scanner):
    """Resolve literal Spark calls only; never infer Scala variable values."""
    strings = {}
    pieces = []
    end = 0
    for token in re.finditer(r'//[^\n]*|/\*[\s\S]*?\*/|"""[\s\S]*?"""|"(?:\\.|[^"\\])*"', source):
        pieces.append(source[end:token.start()])
        text = token.group()
        is_string = text.startswith('"')
        if is_string:
            strings[token.start()] = (text, token.end())
        masked = re.sub(r"[^\n]", " ", text)
        pieces.append("@" + masked[1:] if is_string else masked)
        end = token.end()
    pieces.append(source[end:])
    masked = "".join(pieces)
    scanner.issue("Scala analysis resolves literal Spark table/SQL calls only; variable, interpolation and cross-cell dependencies are unresolved.", 1)
    calls = re.finditer(
        r"\b(?:spark\s*\.\s*(?:read\s*\.\s*)?(?P<read>table|sql)"
        r"|(?P<write>saveAsTable|insertInto)"
        r"|(?:notebookutils|mssparkutils)\s*\.\s*notebook\s*\.\s*(?P<call>run))\s*\(\s*",
        masked,
    )
    for call in calls:
        line = source.count("\n", 0, call.start()) + 1
        literal = strings.get(call.end())
        method = call["read"] or call["write"] or call["call"]
        if literal is None or not masked[literal[1]:].lstrip().startswith((")", ",")):
            scanner.issue(f"Cannot resolve Scala {method} argument without a complete literal.", line)
            continue
        text, _ = literal
        try:
            value = text[3:-3] if text.startswith('"""') else json.loads(text)
        except ValueError:
            scanner.issue(f"Unsupported Scala string escape in {method}.", line)
            continue
        if call["call"]:
            scanner.calls.append({"reference": value, "line": line})
        elif method == "sql":
            scanner.sql(value, line)
        else:
            scanner.table(value, "write" if call["write"] else "read", line, method)


def _scan_notebook(root, item, parameters, issues):
    try:
        source, binding, parameter_lines = _notebook_source(root / item["file"])
    except (OSError, ValueError, SyntaxError) as exc:
        _issue(issues, item["file"], f"Could not parse notebook: {exc}", getattr(exc, "lineno", None))
        return {"reads": [], "writes": [], "calls": [], "parameters": parameters, "defaultLakehouse": {}}
    scanner = NotebookScanner(item, issues, parameters, binding)
    if Path(item["file"]).suffix == ".scala":
        _scan_scala(source, scanner)
        references = sorted(scanner.tables.values(), key=lambda table: (table["tableKey"], table["operation"]))
        return {
            "reads": [table for table in references if table["operation"] == "read"],
            "writes": [table for table in references if table["operation"] == "write"],
            "calls": scanner.calls, "parameters": parameters, "defaultLakehouse": binding,
        }
    try:
        tree = parse_python(source, item["file"], issues)
    except SyntaxError as exc:
        _issue(issues, item["file"], f"Could not parse notebook: {exc}", exc.lineno)
        return {"reads": [], "writes": [], "calls": [], "parameters": parameters, "defaultLakehouse": binding}
    for number, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("# MAGIC "):
            _issue(issues, item["file"], "Notebook magic is not Python; dependencies require language-specific analysis.", number)
    declared = {
        target.id for node in ast.walk(tree) if isinstance(node, ast.Assign) and node.lineno in parameter_lines
        for target in node.targets if isinstance(target, ast.Name)
    }
    for name in parameters.keys() - declared:
        _issue(issues, item["file"], f"Pipeline parameter {name} is not declared in a parameter cell; notebook defaults are retained.")
    scanner.statements(tree.body, parameter_lines)
    references = sorted(scanner.tables.values(), key=lambda table: (table["tableKey"], table["operation"]))
    return {
        "reads": [table for table in references if table["operation"] == "read"],
        "writes": [table for table in references if table["operation"] == "write"],
        "calls": scanner.calls, "parameters": parameters, "defaultLakehouse": binding,
    }


def scan_repository(repository_path, semantic_model=None):
    root = filesystem_path(repository_path)
    if not root.is_dir():
        raise FileNotFoundError(f"Downloaded repository not found: {repository_path}")
    issues = []
    inventory = _inventory(root, issues)
    notebooks = [item for item in inventory if item["type"] == "Notebook"]
    pipelines = [item for item in inventory if item["type"] == "DataPipeline"]
    by_id = {}
    for item in inventory:
        if item["id"]:
            by_id.setdefault(item["id"].casefold(), []).append(item)
    analyses = {}

    def analyze(notebook, parameters):
        key = (notebook["key"], json.dumps(parameters, sort_keys=True))
        if key not in analyses:
            analyses[key] = _scan_notebook(root, notebook, parameters, issues)
        return analyses[key]

    for notebook in notebooks:
        notebook.update(analyze(notebook, {}))
    links = []

    def activities(node, pipeline, pointer=""):
        if isinstance(node, list):
            for index, child in enumerate(node):
                activities(child, pipeline, f"{pointer}/{index}")
        elif isinstance(node, dict):
            kind = node.get("type")
            properties = node.get("typeProperties", {})
            if kind in {"TridentNotebook", "InvokePipeline", "ExecutePipeline"}:
                if not isinstance(properties, dict):
                    _issue(issues, pipeline["file"], f"Invalid activity typeProperties at {pointer}.")
                    return
                parameter_values = properties.get("parameters", {})
                if not isinstance(parameter_values, dict):
                    _issue(issues, pipeline["file"], f"Invalid activity parameters at {pointer}.")
                    parameter_values = {}
                expected = "Notebook" if kind == "TridentNotebook" else "DataPipeline"
                pipeline_reference = properties.get("pipeline", {})
                if not isinstance(pipeline_reference, dict):
                    _issue(issues, pipeline["file"], f"Invalid pipeline reference at {pointer}.")
                    pipeline_reference = {}
                reference = properties.get("notebookId") if expected == "Notebook" else (
                    properties.get("pipelineId") or pipeline_reference.get("referenceName")
                )
                candidates = by_id.get(str(reference).casefold(), [])
                candidates = [item for item in candidates if item["type"] == expected]
                target = candidates[0] if len(candidates) == 1 else None
                parameters = {}
                for name, parameter in parameter_values.items():
                    value = parameter.get("value") if isinstance(parameter, dict) else parameter
                    if isinstance(value, dict) or (isinstance(value, str) and value.startswith("@")):
                        _issue(issues, pipeline["file"], f"Dynamic parameter {name} at {pointer}.")
                        value = "{" + name + "}"
                    parameters[name] = value
                link = {
                    "pipelineKey": pipeline["key"], "pipeline": pipeline["name"],
                    "activity": node.get("name", ""), "activityPath": pointer,
                    "dependsOn": node.get("dependsOn", []), "kind": kind,
                    "reference": reference, "workspaceId": properties.get("workspaceId", ""),
                    "status": "resolved" if target else "unresolved",
                    "targetKey": target["key"] if target else None,
                    "targetName": target["name"] if target else None,
                    "parameters": parameters, "reads": [], "writes": [],
                    "evidence": {"file": pipeline["file"], "jsonPointer": pointer},
                }
                if target and expected == "Notebook":
                    analysis = analyze(target, parameters)
                    link.update({field: analysis[field] for field in ("reads", "writes")})
                elif not target:
                    _issue(issues, pipeline["file"], f"Unresolved {expected} ID {reference} at {pointer}.")
                links.append(link)
            for key, child in node.items():
                if isinstance(child, (dict, list)):
                    activities(child, pipeline, f"{pointer}/{key}")

    for pipeline in pipelines:
        try:
            activities(_json(root / pipeline["file"]), pipeline)
        except (ValueError, OSError) as exc:
            _issue(issues, pipeline["file"], f"Could not read pipeline: {exc}")
    notebook_links = []
    for notebook in notebooks:
        for call in notebook["calls"]:
            reference = call["reference"]
            candidates = [
                item for item in notebooks if isinstance(reference, str) and reference
                and reference in {item["id"], item["name"], item["key"]}
            ]
            target = candidates[0] if len(candidates) == 1 else None
            notebook_links.append({
                "notebookKey": notebook["key"], "reference": reference,
                "targetKey": target["key"] if target else None,
                "status": "resolved" if target else "unresolved",
                "evidence": {"file": notebook["file"], "line": call["line"]},
            })
            if not target:
                _issue(issues, notebook["file"], f"Unresolved notebook call: {reference}", call["line"])
    tables = {}

    def index_tables(owner, pipeline, reads, writes):
        for dependency in reads + writes:
            table = tables.setdefault(dependency["tableKey"], {
                "key": dependency["tableKey"], "name": dependency["table"],
                "defaultLakehouse": dependency["defaultLakehouse"],
                "readers": [], "writers": [],
            })
            consumers = table["readers" if dependency["operation"] == "read" else "writers"]
            consumer = {"notebookKey": owner, "pipelineKey": pipeline}
            if consumer not in consumers:
                consumers.append(consumer)

    for notebook in notebooks:
        index_tables(notebook["key"], None, notebook["reads"], notebook["writes"])
    for link in links:
        if link["kind"] == "TridentNotebook" and link["targetKey"]:
            index_tables(link["targetKey"], link["pipelineKey"], link["reads"], link["writes"])
    called = {link["targetKey"] for link in links}
    mapping = {
        "schemaVersion": 2, "generatedAt": datetime.now(timezone.utc).isoformat(),
        "repositoryPath": str(repository_path), "limitations": LIMITATIONS,
        "summary": {
            "pipelines": len(pipelines), "notebooks": len(notebooks),
            "pipelineNotebookCalls": sum(link["kind"] == "TridentNotebook" for link in links),
            "resolvedCalls": sum(link["status"] == "resolved" for link in links),
            "tables": len(tables), "readTables": sum(bool(table["readers"]) for table in tables.values()),
            "writtenTables": sum(bool(table["writers"]) for table in tables.values()),
            "uncalledNotebooks": sum(notebook["key"] not in called for notebook in notebooks),
            "gaps": len(issues),
        },
        "pipelines": pipelines, "notebooks": notebooks, "pipelineLinks": links,
        "notebookLinks": notebook_links, "tables": sorted(tables.values(), key=lambda table: table["key"]),
        "issues": issues,
    }
    if semantic_model is not None:
        mapping["graph"], mapping["modelTables"] = build_model_graph(
            root, semantic_model, mapping,
            lambda path, message, line=None: _issue(issues, path, message, line),
        )
        mapping["summary"]["modelTables"] = len(mapping["modelTables"])
        mapping["summary"]["gaps"] = len(issues)
    return mapping


def mapping_csv(mapping):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([
        "pipeline", "activity", "notebook", "context", "parameters", "operation", "table",
        "table_key", "status", "pipeline_file", "activity_json_pointer", "notebook_file", "line", "method",
        "semantic_model", "model_table", "relationship", "scope_status",
    ])

    def safe(value):
        text = str(value) if value is not None else ""
        return "'" + text if text.startswith(("=", "+", "-", "@")) else text

    def rows(pipeline, activity, notebook, context, parameters, reads, writes, evidence):
        for dependency in reads + writes:
            for source in dependency["evidence"]:
                writer.writerow([safe(value) for value in (
                    pipeline, activity, notebook, context, json.dumps(parameters, sort_keys=True),
                    dependency["operation"], dependency["table"], dependency["tableKey"],
                    dependency["status"], evidence.get("file", ""), evidence.get("jsonPointer", ""),
                    source["file"], source["line"], source["method"],
                    "", "", "", "",
                )])

    for link in mapping["pipelineLinks"]:
        if link["kind"] == "TridentNotebook" and link["status"] == "resolved":
            rows(link["pipeline"], link["activity"], link["targetName"], "pipeline", link["parameters"],
                 link["reads"], link["writes"], link["evidence"])
        else:
            writer.writerow([safe(value) for value in (
                link["pipeline"], link["activity"], link["targetName"] or link["reference"],
                link["kind"], json.dumps(link["parameters"]), "", "", "", link["status"],
                link["evidence"]["file"], link["activityPath"], "", "", "",
                "", "", "", "",
            )])
    for notebook in mapping["notebooks"]:
        rows("", "", notebook["name"], "notebook defaults", {}, notebook["reads"], notebook["writes"], {})
    graph = mapping.get("graph")
    if graph:
        nodes = {node["id"]: node for node in graph["nodes"]}
        model = nodes[graph["root"]]["label"]
        for edge in graph["edges"]:
            source, target = nodes[edge["source"]], nodes[edge["target"]]
            for evidence in edge["evidence"] or [{}]:
                writer.writerow([safe(value) for value in (
                    target["label"] if target["type"] == "DataPipeline" else "", "",
                    target["label"] if target["type"] == "Notebook" else "",
                    "model lineage", json.dumps(edge.get("contexts", [])), "",
                    source.get("physicalTable", ""), edge.get("tableKey", ""), edge["status"],
                    "", evidence.get("jsonPointer", ""), evidence.get("file", ""),
                    evidence.get("line", ""), "", model,
                    source["label"] if source["type"] == "Table" else target["label"] if target["type"] == "Table" else "",
                    edge["label"], edge["status"],
                )])
    return stream.getvalue()


def persist_mapping(mapping, report_id):
    if not re.fullmatch(r"[a-f0-9]{64}", report_id):
        raise ValueError("Invalid Fabric mapping report ID.")
    directory = filesystem_path(REPORTS_DIR / report_id)
    directory.mkdir(parents=True, exist_ok=True)
    contents = {
        "mapping.json": json.dumps(mapping, indent=2),
        "mapping.csv": mapping_csv(mapping),
    }
    for name, content in contents.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(content)
            os.replace(temporary, directory / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return REPORTS_DIR / report_id


def load_mapping(report_id):
    if not re.fullmatch(r"[a-f0-9]{64}", report_id):
        raise ValueError("Invalid Fabric mapping report ID.")
    path = filesystem_path(REPORTS_DIR / report_id / "mapping.json")
    with path.open(encoding="utf-8") as file:
        mapping = json.load(file)
    if not isinstance(mapping, dict):
        raise ValueError(f"Invalid Fabric mapping report: {report_id}.")
    return mapping
