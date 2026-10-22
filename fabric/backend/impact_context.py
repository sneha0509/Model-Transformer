"""Lineage-scoped notebook cells for review-only impact requests."""

import ast
import json
import re

from fabric.backend.model_lineage import match_table


def notebook_cells(path, source):
    if path.suffix == ".ipynb":
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        cells = []
        line = 1
        for number, cell in enumerate(document["cells"], 1):
            if cell.get("cell_type") != "code":
                continue
            code = "".join(cell.get("source", []))
            cells.append({
                "cell": number, "startLine": line, "source": code,
                "parameters": "parameters" in cell.get("metadata", {}).get("tags", []),
            })
            line += code.count("\n") + 1
        return cells
    lines = source.splitlines()
    markers = [
        index for index, line in enumerate(lines)
        if re.match(r"^# (?:CELL|PARAMETERS CELL|MARKDOWN CELL)\s+\*+", line)
        or line.startswith("# %%")
    ]
    if not markers:
        return [{"cell": 1, "startLine": 1, "source": source, "parameters": False}]
    cells = []
    for number, start in enumerate(markers, 1):
        end = markers[number] if number < len(markers) else len(lines)
        if "MARKDOWN" in lines[start] or "[markdown]" in lines[start]:
            continue
        for index in range(start + 1, end):
            if lines[index].startswith("# METADATA "):
                end = index
                break
        cells.append({
            "cell": number, "startLine": start + 2,
            "source": "\n".join(lines[start + 1:end]),
            "parameters": "PARAMETERS CELL" in lines[start],
        })
    return cells


def _select_cells(artifact, evidence_lines):
    cells = artifact["cells"]
    selected = {
        index for index, cell in enumerate(cells)
        if any(cell["startLine"] <= line < cell["startLine"] + len(cell["source"].splitlines())
               for line in evidence_lines)
    }
    dependencies = []
    definitions = {}
    uncertain = False
    for index, cell in enumerate(cells):
        try:
            tree = ast.parse(cell["source"])
        except SyntaxError:
            # SQL/magics and cross-cell syntax cannot be safely sliced as Python.
            uncertain = True
            break
        loads = {node.id for node in ast.walk(tree)
                 if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        stores = {node.id for node in ast.walk(tree)
                  if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
        stores.update(node.name for node in ast.walk(tree)
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                stores.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"createOrReplaceTempView", "createTempView", "createGlobalTempView"}:
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        stores.add("view:" + node.args[0].value.casefold())
                    else:
                        uncertain = True
                elif node.func.attr == "sql":
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        loads.update("view:" + word.casefold()
                                     for word in re.findall(r"[A-Za-z_]\w*", node.args[0].value))
                    else:
                        uncertain = True
        if uncertain:
            break
        dependencies.append({definitions[name] for name in loads if name in definitions})
        definitions.update({name: index for name in stores})
    if uncertain:
        return cells, "Notebook cell dependencies could not be resolved as Python; all code cells in this lineage candidate were retained."
    if not selected:
        return cells, "No cell-level lineage evidence was resolved; all code cells in this lineage candidate were retained."
    # Follow data consumers first, then add their prerequisite definitions. Imports
    # added in the second pass must not pull unrelated consumers into the request.
    forward = set(selected)
    for index, parents in enumerate(dependencies):
        if parents & forward:
            forward.add(index)
    selected = forward | {index for index, cell in enumerate(cells) if cell["parameters"]}
    pending = list(selected)
    while pending:
        for parent in dependencies[pending.pop()] - selected:
            selected.add(parent)
            pending.append(parent)
    return [cell for index, cell in enumerate(cells) if index in selected], None


def _table_source(table):
    parts = table["name"].replace("`", "").split(".")
    binding = table["defaultLakehouse"]
    scope = {
        "workspace": parts[-4] if len(parts) >= 4 else binding.get("default_lakehouse_workspace_id", ""),
        "lakehouse": parts[-3] if len(parts) >= 3 else binding.get("default_lakehouse", ""),
    }
    return {"table": table["name"], "scope": {key: value for key, value in scope.items() if value}}


def scope_catalog(catalog, changes):
    """Backtrack producers, follow affected consumers, then retain caller context."""
    tables = {table["key"]: table for table in catalog["lineage"]["tables"]}
    by_name = {}
    for table in tables.values():
        by_name.setdefault(table["name"].replace("`", "").split(".")[-1].casefold(), []).append(table)
    notebooks = {item["key"]: item for item in catalog["artifacts"] if item["type"] == "Notebook"}
    table_changes = {}
    notebook_changes = {}
    evidence = {}
    scope_issues = []

    def mark_notebook(key, ids, table_key=None):
        notebook_changes.setdefault(key, set()).update(ids)
        if key not in notebooks or table_key is None:
            return
        contexts = [notebooks[key]["context"]] + [
            link for link in catalog["lineage"]["pipelineLinks"] if link["targetKey"] == key
        ]
        for context in contexts:
            for dependency in context["reads"] + context["writes"]:
                if dependency["tableKey"] == table_key:
                    evidence.setdefault(key, set()).update(item["line"] for item in dependency["evidence"])

    seeds = {}
    for change in changes:
        for source in change["sources"]:
            matches = [(table, match_table(source, table)) for table in tables.values()]
            matches = [(table, status) for table, status in matches if status]
            if not any(table["writers"] for table, _ in matches):
                scope_issues.append({
                    "file": "", "message": f"No static notebook producer found for {change['table']} ({source['table']}).",
                })
            for table, status in matches:
                seeds.setdefault(table["key"], set()).add(change["id"])
                if status == "unverified":
                    scope_issues.append({
                        "file": "", "message": f"Unverified lineage candidate for {source['table']}: {table['name']}; source scope is not proven.",
                    })
        if not change["sources"]:
            scope_issues.append({
                "file": "", "message": f"No physical source resolved for {change['table']}.{change['column']}; notebook lineage cannot be proven.",
            })

    def traverse(direction):
        pending = [(key, ids) for key, ids in seeds.items()]
        visited = {}
        while pending:
            key, ids = pending.pop()
            fresh = ids - visited.setdefault(key, set())
            if not fresh:
                continue
            visited[key].update(fresh)
            table_changes.setdefault(key, set()).update(fresh)
            table = tables[key]
            source = _table_source(table)
            for candidate in by_name[source["table"].split(".")[-1].casefold()]:
                # An unscoped candidate must not bridge two conflicting known scopes.
                if candidate["key"] != key and match_table(source, candidate) == "confirmed":
                    pending.append((candidate["key"], fresh))
            for owner in table["writers" if direction == "upstream" else "readers"]:
                notebook_key = owner["notebookKey"]
                mark_notebook(notebook_key, fresh, key)
                if notebook_key not in notebooks:
                    continue
                contexts = [notebooks[notebook_key]["context"]] + [
                    link for link in catalog["lineage"]["pipelineLinks"]
                    if link["targetKey"] == notebook_key
                ]
                cells, _ = _select_cells(notebooks[notebook_key], evidence.get(notebook_key, set()))
                ranges = [(cell["startLine"], cell["startLine"] + len(cell["source"].splitlines()) - 1)
                          for cell in cells]
                for context in contexts:
                    for dependency in context["reads" if direction == "upstream" else "writes"]:
                        if not any(start <= proof["line"] <= end
                                   for proof in dependency["evidence"] for start, end in ranges):
                            continue
                        next_key = dependency["tableKey"]
                        if next_key in tables:
                            pending.append((next_key, fresh))

    traverse("upstream")
    all_table_changes = {key: set(ids) for key, ids in table_changes.items()}
    table_changes = {}
    traverse("downstream")
    affected_tables = dict(table_changes)
    for key, ids in table_changes.items():
        all_table_changes.setdefault(key, set()).update(ids)
    callers = []
    pipeline_links = []
    pipeline_keys = set()
    changed = True
    while changed:
        changed = False
        for link in catalog["lineage"]["notebookLinks"]:
            target, caller = link["targetKey"], link["notebookKey"]
            if link["status"] == "resolved" and target in notebook_changes:
                ids = notebook_changes[target] - notebook_changes.get(caller, set())
                if ids:
                    mark_notebook(caller, ids)
                    changed = True
                evidence.setdefault(caller, set()).add(link["evidence"]["line"])
                if link not in callers:
                    callers.append(link)
        for link in catalog["lineage"]["pipelineLinks"]:
            if link["targetKey"] in notebook_changes or link["targetKey"] in pipeline_keys:
                if link not in pipeline_links:
                    pipeline_links.append(link)
                    pipeline_keys.add(link["pipelineKey"])
                    changed = True

    selected_model = catalog["selection"]["semanticModel"]["path"].replace("\\", "/").strip("/")
    scoped = []
    inventory = {}
    for artifact in catalog["artifacts"]:
        key = artifact["key"]
        ids = notebook_changes.get(key, set())
        if artifact["type"] == "SemanticModel":
            ids = {change["id"] for change in changes} if key == selected_model else {
                change_id for table in artifact["context"]["tables"] for source in table["sources"]
                for table_key, change_ids in affected_tables.items()
                if match_table(source, tables[table_key]) for change_id in change_ids
            }
        if not ids:
            inventory[key] = {"status": "out_of_scope"}
            continue
        item = {**artifact, "changeIds": sorted(ids)}
        inventory[key] = {}
        relevant_notebooks = {key}
        if artifact["type"] == "Notebook":
            cells, gap = _select_cells(artifact, evidence.get(key, set()))
            item["cells"] = cells
            item["sourceRanges"] = {
                file: [(cell["startLine"], cell["startLine"] + len(cell["source"].splitlines()) - 1)
                       for cell in cells] for file in artifact["files"]
            }
            item["context"] = {
                **artifact["context"], "selectedCells": [cell["cell"] for cell in cells],
                "totalCodeCells": len(artifact["cells"]),
            }
            ranges = next(iter(item["sourceRanges"].values()))
            for operation in ("reads", "writes"):
                item["context"][operation] = [
                    dependency for dependency in artifact["context"][operation]
                    if any(start <= proof["line"] <= end
                           for proof in dependency["evidence"] for start, end in ranges)
                ]
            item["context"]["calls"] = [
                call for call in artifact["context"]["calls"]
                if any(start <= call["line"] <= end for start, end in ranges)
            ]
            if gap:
                scope_issues.append({"file": key, "message": gap})
            inventory[key] = {
                "selectedCells": item["context"]["selectedCells"],
                "totalCodeCells": len(artifact["cells"]),
            }
        else:
            relevant_notebooks = set(notebook_changes)
        related_calls = [
            link for link in callers
            if link["notebookKey"] in relevant_notebooks or link["targetKey"] in relevant_notebooks
        ]
        related_pipelines = [
            link for link in pipeline_links if link["targetKey"] in relevant_notebooks
        ]
        ancestors = {link["pipelineKey"] for link in related_pipelines}
        while True:
            parents = [
                link for link in pipeline_links
                if link["targetKey"] in ancestors and link not in related_pipelines
            ]
            if not parents:
                break
            related_pipelines.extend(parents)
            ancestors.update(link["pipelineKey"] for link in parents)
        item["lineage"] = {
            "tables": [{
                **table,
                "readers": [owner for owner in table["readers"] if owner["notebookKey"] in relevant_notebooks],
                "writers": [owner for owner in table["writers"] if owner["notebookKey"] in relevant_notebooks],
            } for table_key, table in tables.items()
                       if all_table_changes.get(table_key, set()) & ids and (
                           artifact["type"] == "SemanticModel" or any(
                               owner["notebookKey"] == key for owner in table["readers"] + table["writers"]
                           )
                       )],
            "notebookLinks": related_calls,
            "pipelineLinks": related_pipelines,
        }
        scoped.append(item)
    return {
        **catalog, "artifacts": scoped, "inventoryScope": inventory,
        "issues": catalog["issues"] + scope_issues,
    }
