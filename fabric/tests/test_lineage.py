import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fabric.backend.lineage import mapping_csv, persist_mapping, scan_repository
from shared.app import create_app


BINDING = {
    "default_lakehouse": "lakehouse-id",
    "default_lakehouse_name": "Compute",
    "default_lakehouse_workspace_id": "workspace-id",
}
NOTEBOOK_HEADER = "# Fabric notebook source\n" + "\n".join(
    "# META " + line for line in json.dumps({"dependencies": {"lakehouse": BINDING}}, indent=2).splitlines()
) + "\n\n"


class LineageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        self.root.mkdir()
        self.reports = Path(temporary.name) / "reports"
        for target in ("fabric.backend.lineage.REPORTS_DIR", "fabric.backend.routes.REPORTS_DIR"):
            patched = patch(target, self.reports)
            patched.start()
            self.addCleanup(patched.stop)

    def artifact(self, name, kind, logical_id, content, parent="FabricArtifacts"):
        path = self.root / parent / f"{name}.{kind}"
        path.mkdir(parents=True)
        (path / ".platform").write_text(json.dumps({
            "metadata": {"type": kind, "displayName": name},
            "config": {"logicalId": logical_id},
        }), encoding="utf-8")
        filename = "notebook-content.py" if kind == "Notebook" else "pipeline-content.json"
        (path / filename).write_text(
            NOTEBOOK_HEADER + content if kind == "Notebook" else json.dumps(content), encoding="utf-8"
        )
        return path

    def notebook(self, name="Transform", logical_id="notebook-id"):
        return self.artifact(name, "Notebook", logical_id, '''
# PARAMETERS CELL ********************
envName = "prod"
# METADATA ********************
# CELL ********************
if envName.lower() == "prod":
    workspace = "Prod"
else:
    workspace = "Dev"
publish = f"{workspace}.Publish"
# spark.table("commented.out")
spark.sql("""
WITH staged AS (SELECT * FROM bronze.orders)
SELECT * FROM staged JOIN bronze.customers c ON staged.id = c.id
""").createOrReplaceTempView("session_view")
spark.sql("SELECT * FROM session_view").write.mode("overwrite").saveAsTable(
    publish + ".gold.orders"
)
''')

    def activity(self, reference="notebook-id", parameters=None):
        return {
            "name": "Run Transform", "type": "TridentNotebook",
            "typeProperties": {
                "notebookId": reference, "workspaceId": "00000000-0000-0000-0000-000000000000",
                "parameters": parameters if parameters is not None else {"envName": {"value": "dev"}},
            },
            "dependsOn": [],
        }

    def pipeline(self, activities=None):
        return self.artifact("Orchestrate", "DataPipeline", "pipeline-id", {
            "properties": {"activities": activities if activities is not None else [self.activity()]},
        })

    def model(self, tables=None, expression=None, name="Sales"):
        folder = self.root / f"{name}.SemanticModel" / "definition"
        (folder / "tables").mkdir(parents=True)
        (folder / "expressions.tmdl").write_text(
            expression or '''expression DatabaseQuery =
    let database = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/workspace-id/lakehouse-id")
    in database
''', encoding="utf-8",
        )
        for filename, content in (tables or {"Orders": """table 'Sales orders'
    partition Orders = entity
        mode: directLake
        source
            entityName: orders
            schemaName: gold
            expressionSource: DatabaseQuery
"""}).items():
            (folder / "tables" / f"{filename}.tmdl").write_text(content, encoding="utf-8")
        return {"name": name, "path": f"/{name}.SemanticModel"}

    def test_model_graph_includes_writes_and_optional_reads_of_selected_model_tables(self):
        model = self.model()
        self.artifact("Producer", "Notebook", "producer", 'spark.table("bronze.orders").write.saveAsTable("gold.orders")')
        self.artifact("Consumer", "Notebook", "consumer", 'spark.table("gold.orders").write.saveAsTable("gold.other")')
        self.artifact("Unrelated", "Notebook", "unrelated", 'spark.range(10).write.saveAsTable("other.orders")')
        self.pipeline([self.activity("producer", {}), self.activity("consumer", {})])
        self.model(name="NotSelected")
        mapping = scan_repository(self.root, model)
        graph = mapping["graph"]
        self.assertEqual(graph["root"], "model:/Sales.SemanticModel")
        self.assertEqual({node["label"] for node in graph["nodes"]}, {
            "Sales", "Sales orders", "Producer", "Consumer", "Orchestrate",
        })
        self.assertCountEqual([edge["label"] for edge in graph["edges"]], [
            "uses", "written by", "read by", "run by", "run by",
        ])
        self.assertTrue(all(edge["status"] == "confirmed" for edge in graph["edges"]))
        self.assertEqual(mapping["summary"]["modelTables"], 1)
        self.assertEqual(mapping["summary"]["notebooks"], 3)
        self.assertEqual(mapping["issues"], [])
        table = next(node for node in graph["nodes"] if node["type"] == "Table")
        self.assertEqual(table["physicalTable"], "gold.orders")
        self.assertEqual(table["evidence"][0]["partition"], "Orders")
        self.assertEqual(table["evidence"][0]["line"], 2)
        rows = list(csv.DictReader(io.StringIO(mapping_csv(mapping))))
        self.assertTrue(any(row["semantic_model"] == "Sales" and row["relationship"] == "written by" for row in rows))
        self.assertTrue(any(row["semantic_model"] == "Sales" and row["relationship"] == "read by" for row in rows))

    def test_reader_lineage_preserves_pipeline_parameters_and_producer_gap(self):
        model = self.model()
        self.artifact("Reader", "Notebook", "reader", '''
# PARAMETERS CELL ********************
tableName = "gold.other"
# METADATA ********************
# CELL ********************
spark.table(tableName).count()
''')
        self.artifact("WrongSchema", "Notebook", "wrong", 'spark.table("silver.orders").count()')
        self.artifact("Dynamic", "Notebook", "dynamic", 'spark.table(unknown_table).count()')
        self.pipeline([self.activity("reader", {"tableName": {"value": "gold.orders"}})])
        mapping = scan_repository(self.root, model)
        graph = mapping["graph"]
        self.assertEqual({node["label"] for node in graph["nodes"]}, {
            "Sales", "Sales orders", "Reader", "Orchestrate",
        })
        read = next(edge for edge in graph["edges"] if edge["label"] == "read by")
        self.assertEqual(read["status"], "confirmed")
        self.assertTrue(read["evidence"][0]["file"].endswith("notebook-content.py"))
        invocation = next(edge for edge in graph["edges"] if edge["label"] == "run by")
        self.assertEqual(invocation["contexts"][0]["parameters"]["tableName"], "gold.orders")
        self.assertFalse(any(edge["label"] == "written by" for edge in graph["edges"]))
        self.assertTrue(any("No notebook producer found" in issue["message"] for issue in mapping["issues"]))

    def test_notebook_can_have_both_read_and_write_relationships(self):
        model = self.model()
        self.artifact("Both", "Notebook", "both", 'spark.table("gold.orders").write.saveAsTable("gold.orders")')
        graph = scan_repository(self.root, model)["graph"]
        self.assertCountEqual([edge["label"] for edge in graph["edges"]], [
            "uses", "written by", "read by",
        ])
        relationships = [edge for edge in graph["edges"] if edge["label"] != "uses"]
        self.assertEqual(relationships[0]["target"], relationships[1]["target"])

    def test_graph_retains_unmatched_and_calculated_model_tables_with_gaps(self):
        model = self.model({
            "Missing": """table Missing
    partition Missing = entity
        source
            entityName: absent
            schemaName: gold
            expressionSource: DatabaseQuery
""",
            "Calculated": """table Calendar
    partition Calendar = calculated
        source = CALENDAR(DATE(2020,1,1), DATE(2021,1,1))
""",
        })
        mapping = scan_repository(self.root, model)
        self.assertEqual(len(mapping["graph"]["nodes"]), 3)
        self.assertTrue(any("No notebook producer" in item["message"] for item in mapping["issues"]))
        calculated = next(item for item in mapping["modelTables"] if item["name"] == "Calendar")
        self.assertEqual(calculated["status"], "calculated")

    def test_name_only_matches_are_explicitly_unverified_and_conflicting_guids_are_excluded(self):
        workspace = "11111111-1111-1111-1111-111111111111"
        lakehouse = "22222222-2222-2222-2222-222222222222"
        model = self.model(expression=f'''expression DatabaseQuery =
    AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/{workspace}/{lakehouse}")
''')
        self.artifact("Candidate", "Notebook", "candidate", 'spark.range(1).write.saveAsTable("Prod.Publish.gold.orders")')
        self.artifact("Wrong", "Notebook", "wrong", f'spark.range(1).write.saveAsTable("{workspace}.33333333-3333-3333-3333-333333333333.gold.orders")')
        graph_mapping = scan_repository(self.root, model)
        edges = [edge for edge in graph_mapping["graph"]["edges"] if edge["label"] == "written by"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["status"], "unverified")
        self.assertTrue(any("Unverified source scope" in issue["message"] for issue in graph_mapping["issues"]))
        self.assertNotIn("Wrong", {node["label"] for node in graph_mapping["graph"]["nodes"]})

    def test_pipeline_overrides_only_link_invocations_producing_the_model_table(self):
        model = self.model()
        self.artifact("Producer", "Notebook", "producer", '''# PARAMETERS CELL ********************
target = "gold.orders"
# METADATA ********************
# CELL ********************
spark.range(1).write.saveAsTable(target)
''')
        self.pipeline([self.activity("producer", {"target": {"value": "gold.other"}})])
        mapping = scan_repository(self.root, model)
        self.assertIn("Producer", {node["label"] for node in mapping["graph"]["nodes"]})
        self.assertNotIn("Orchestrate", {node["label"] for node in mapping["graph"]["nodes"]})

    def test_model_graph_follows_notebook_callers_and_cyclic_parent_pipelines(self):
        model = self.model()
        self.artifact("Producer", "Notebook", "producer", 'spark.range(1).write.saveAsTable("gold.orders")')
        self.artifact("Caller", "Notebook", "caller", 'notebookutils.notebook.run("Producer", 60)')
        self.pipeline([self.activity("caller", {}), {
            "name": "Cycle", "type": "InvokePipeline", "typeProperties": {"pipelineId": "parent"},
        }])
        self.artifact("Parent", "DataPipeline", "parent", {"properties": {"activities": [{
            "name": "Child", "type": "ExecutePipeline",
            "typeProperties": {"pipeline": {"referenceName": "pipeline-id"}},
        }]}})
        graph = scan_repository(self.root, model)["graph"]
        self.assertEqual({node["label"] for node in graph["nodes"]}, {"Sales", "Sales orders", "Producer", "Caller", "Orchestrate", "Parent"})
        self.assertEqual(len([edge for edge in graph["edges"] if edge["label"] == "invoked by"]), 2)

    def test_bim_entity_m_navigation_sql_and_calculated_partitions(self):
        model = self.model()
        folder = self.root / "Sales.SemanticModel"
        (folder / "model.bim").write_text(json.dumps({"model": {
            "expressions": [{"name": "DatabaseQuery", "expression": 'AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/workspace-id/lakehouse-id")'}],
            "tables": [
                {"name": "Entity", "partitions": [{"name": "Entity", "source": {
                    "type": "entity", "schemaName": "gold", "entityName": "orders", "expressionSource": "DatabaseQuery",
                }}]},
                {"name": "Navigation", "partitions": [{"name": "Navigation", "source": {
                    "type": "m", "expression": ['let Source = DatabaseQuery,', 'Data = Source{[Schema="gold",Item="orders"]}[Data] in Data'],
                }}]},
                {"name": "Query", "partitions": [{"name": "Query", "source": {
                    "type": "query", "query": "WITH x AS (SELECT * FROM gold.orders) SELECT * FROM x",
                }}]},
                {"name": "Calculated", "partitions": [{"name": "Calculated", "source": {
                    "type": "calculated", "expression": "CALENDAR(DATE(2020,1,1), DATE(2021,1,1))",
                }}]},
            ],
        }}), encoding="utf-8")
        self.artifact("Producer", "Notebook", "producer", 'spark.range(1).write.saveAsTable("gold.orders")')
        mapping = scan_repository(self.root, model)
        self.assertEqual(len(mapping["modelTables"]), 4)
        self.assertEqual([table["sources"][0]["table"] for table in mapping["modelTables"] if table["sources"]], ["gold.orders"] * 3)
        self.assertEqual(len([edge for edge in mapping["graph"]["edges"] if edge["label"] == "written by"]), 3)

    def test_tmdl_m_partition_native_query_escaped_names_and_multiple_sources(self):
        model = self.model({"Orders": '''table 'Customer''s orders'
    partition Import = m
        mode: import
        source =
            let
                Source = Sql.Database("server", "database"),
                Data = Value.NativeQuery(Source, "SELECT * FROM gold.orders JOIN gold.customers ON 1=1")
            in Data
    annotation Description = "not part of M"
    partition Archive = m
        source =
            let Source = DatabaseQuery,
                Data = Source{[Item="archive", Schema="gold"]}[Data]
            in Data
'''})
        mapping = scan_repository(self.root, model)
        table = mapping["modelTables"][0]
        self.assertEqual(table["name"], "Customer's orders")
        self.assertEqual({source["table"] for source in table["sources"]}, {"gold.orders", "gold.customers", "gold.archive"})
        self.assertEqual(table["sources"][0]["scope"], {"server": "server", "database": "database"})

    def test_missing_dynamic_and_malformed_model_sources_are_reported(self):
        model = self.model({"Dynamic": '''table Dynamic
    partition Dynamic = m
        source = Expression.Evaluate(runtime)
'''})
        mapping = scan_repository(self.root, model)
        self.assertEqual(mapping["modelTables"][0]["status"], "unresolved")
        self.assertTrue(any("No static physical table" in issue["message"] for issue in mapping["issues"]))
        (self.root / "Sales.SemanticModel" / "model.bim").write_text("[]", encoding="utf-8")
        malformed = scan_repository(self.root, model)
        self.assertTrue(any("Could not read selected semantic model" in issue["message"] for issue in malformed["issues"]))
        missing = scan_repository(self.root, {"name": "Missing", "path": "/Missing.SemanticModel"})
        self.assertEqual(len(missing["graph"]["nodes"]), 1)
        self.assertTrue(missing["issues"])
        with self.assertRaises(ValueError):
            scan_repository(self.root, {"name": "Escape", "path": "/../outside"})

    def test_unqualified_table_matches_do_not_claim_a_confirmed_schema(self):
        model = self.model()
        self.artifact("Producer", "Notebook", "producer", 'spark.range(1).write.saveAsTable("orders")')
        self.artifact("OtherSchema", "Notebook", "other", 'spark.range(1).write.saveAsTable("silver.orders")')
        graph = scan_repository(self.root, model)["graph"]
        edges = [edge for edge in graph["edges"] if edge["label"] == "written by"]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["status"], "unverified")
        self.assertNotIn("OtherSchema", {node["label"] for node in graph["nodes"]})

    def test_commented_m_and_multiple_scopes_do_not_invent_confirmed_sources(self):
        model = self.model({"Orders": '''table Orders
    partition Import = m
        source =
            let
                // Ignored = Source{[Schema="ignored",Item="orders"]}[Data],
                A = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/workspace-id/lakehouse-id"),
                B = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/other-workspace/other-lakehouse"),
                Data = A{[Schema="gold",Item="orders"]}[Data]
            in Data
'''})
        self.artifact("Producer", "Notebook", "producer", 'spark.range(1).write.saveAsTable("gold.orders")')
        mapping = scan_repository(self.root, model)
        sources = mapping["modelTables"][0]["sources"]
        self.assertEqual([source["table"] for source in sources], ["gold.orders"])
        self.assertEqual(sources[0]["scope"], {})
        self.assertEqual(next(edge["status"] for edge in mapping["graph"]["edges"] if edge["label"] == "written by"), "unverified")

    def test_multiple_pipeline_invocations_preserve_all_parameter_evidence(self):
        model = self.model()
        self.artifact("Producer", "Notebook", "producer", '''# PARAMETERS CELL ********************
target = "gold.orders"
# METADATA ********************
# CELL ********************
spark.range(1).write.saveAsTable(target)
''')
        first = self.activity("producer", {"target": {"value": "gold.orders"}})
        second = {**self.activity("producer", {"target": {"value": "gold.orders"}}), "name": "Run again"}
        self.pipeline([first, second])
        mapping = scan_repository(self.root, model)
        invocation = next(edge for edge in mapping["graph"]["edges"] if edge["label"] == "run by")
        self.assertEqual(len(invocation["contexts"]), 2)
        self.assertEqual(len(invocation["evidence"]), 2)
        self.assertEqual(invocation["contexts"][1]["activity"], "Run again")

    def test_full_mapping_resolves_ids_parameters_ctes_temp_views_and_reverse_index(self):
        self.notebook()
        self.pipeline()
        mapping = scan_repository(self.root)
        self.assertEqual(mapping["summary"]["pipelines"], 1)
        self.assertEqual(mapping["summary"]["notebooks"], 1)
        self.assertEqual(mapping["summary"]["resolvedCalls"], 1)
        self.assertEqual(mapping["issues"], [])
        notebook = mapping["notebooks"][0]
        link = mapping["pipelineLinks"][0]
        self.assertEqual({table["table"] for table in notebook["reads"]}, {"bronze.orders", "bronze.customers"})
        self.assertEqual(notebook["writes"][0]["table"], "Prod.Publish.gold.orders")
        self.assertEqual(link["writes"][0]["table"], "Dev.Publish.gold.orders")
        self.assertEqual(link["evidence"]["jsonPointer"], "/properties/activities/0")
        self.assertEqual(link["parameters"], {"envName": "dev"})
        table = next(table for table in mapping["tables"] if table["name"] == "Dev.Publish.gold.orders")
        self.assertEqual(table["writers"], [{"notebookKey": notebook["key"], "pipelineKey": link["pipelineKey"]}])
        self.assertEqual(notebook["reads"][0]["defaultLakehouse"], BINDING)
        self.assertGreater(link["writes"][0]["evidence"][0]["line"], 1)

    def test_nested_activities_missing_ids_and_uncalled_notebooks_are_not_guessed(self):
        self.notebook()
        self.notebook("Uncalled", "uncalled-id")
        self.pipeline([{
            "name": "Branch", "type": "IfCondition",
            "typeProperties": {"ifTrueActivities": [self.activity(), self.activity("missing")]},
        }])
        mapping = scan_repository(self.root)
        self.assertEqual(len(mapping["pipelineLinks"]), 2)
        self.assertEqual(mapping["summary"]["uncalledNotebooks"], 1)
        self.assertEqual(mapping["pipelineLinks"][1]["status"], "unresolved")
        self.assertTrue(any("Unresolved Notebook ID missing" in issue["message"] for issue in mapping["issues"]))

    def test_legacy_and_git_files_are_excluded_and_empty_pipelines_remain_in_inventory(self):
        self.notebook()
        self.notebook("OldNotebook", "old")
        self.artifact("Ignored", "Notebook", "ignored", 'spark.table("bad.table")', parent="legacy")
        self.artifact("Ignored", "Notebook", "ignored", 'spark.table("bad.table")', parent=".git")
        self.pipeline([])
        mapping = scan_repository(self.root)
        self.assertEqual(len(mapping["notebooks"]), 2)
        self.assertEqual(len(mapping["pipelines"]), 1)
        self.assertEqual(mapping["pipelineLinks"], [])
        self.assertEqual(mapping["summary"]["uncalledNotebooks"], 2)

    def test_dynamic_sql_parse_errors_and_missing_content_are_visible(self):
        self.artifact("Dynamic", "Notebook", "dynamic", '''
spark.sql("SELECT * FROM " + runtime_table)
spark.sql("SELECT * FROM")
spark.table(dynamic_table).write.saveAsTable(output_table)
''')
        missing = self.notebook("Missing", "missing")
        (missing / "notebook-content.py").unlink()
        mapping = scan_repository(self.root)
        messages = [issue["message"] for issue in mapping["issues"]]
        self.assertTrue(any("SQL parse error" in message for message in messages))
        self.assertTrue(any("Could not parse notebook" in message for message in messages))
        self.assertTrue(any("Dynamic table reference" in message for message in messages))
        self.assertTrue(any(table["status"] == "dynamic" for table in mapping["notebooks"][0]["reads"]))

    def test_sql_writes_quoted_tables_ctes_and_temporary_views(self):
        self.artifact("SQL", "Notebook", "sql", '''
spark.sql("CREATE OR REPLACE TEMP VIEW v AS SELECT * FROM `bronze`.`orders`")
spark.sql("CREATE TABLE gold.orders AS SELECT * FROM v")
spark.sql("INSERT INTO gold.orders SELECT * FROM bronze.orders")
spark.sql("MERGE INTO gold.orders t USING bronze.orders s ON t.id=s.id WHEN MATCHED THEN UPDATE SET t.id=s.id")
spark.sql("SELECT * FROM delta.`abfss://container@account.dfs.core.windows.net/Orders`")
''')
        mapping = scan_repository(self.root)
        notebook = mapping["notebooks"][0]
        self.assertEqual(mapping["issues"], [])
        self.assertEqual([table["table"] for table in notebook["writes"]], ["gold.orders"])
        self.assertEqual({table["table"] for table in notebook["reads"]}, {
            "bronze.orders", "gold.orders", "delta.abfss://container@account.dfs.core.windows.net/Orders",
        })

    def test_pipeline_parameters_not_in_parameter_cell_are_reported(self):
        self.notebook()
        self.pipeline([self.activity(parameters={"PublishenvName": {"value": "dev"}})])
        mapping = scan_repository(self.root)
        self.assertIn("not declared in a parameter cell", mapping["issues"][0]["message"])
        self.assertEqual(mapping["pipelineLinks"][0]["writes"][0]["table"], "Prod.Publish.gold.orders")

    def test_duplicate_ids_are_ambiguous_not_matched_by_activity_name(self):
        self.notebook()
        self.notebook("Second", "notebook-id")
        self.pipeline()
        mapping = scan_repository(self.root)
        self.assertEqual(mapping["pipelineLinks"][0]["status"], "unresolved")

    def test_notebook_calls_and_child_pipeline_references(self):
        self.notebook()
        self.artifact("Caller", "Notebook", "caller", 'notebookutils.notebook.run("Transform", 60)')
        self.pipeline()
        self.artifact("Parent", "DataPipeline", "parent", {
            "properties": {"activities": [{"name": "Invoke", "type": "InvokePipeline",
                                           "typeProperties": {"pipelineId": "pipeline-id"}}]},
        })
        mapping = scan_repository(self.root)
        self.assertEqual(mapping["notebookLinks"][0]["status"], "resolved")
        self.assertEqual(next(link for link in mapping["pipelineLinks"] if link["kind"] == "InvokePipeline")["status"], "resolved")

    def test_ipynb_parameter_cells_and_default_lakehouse(self):
        path = self.notebook()
        (path / "notebook-content.py").unlink()
        (path / "notebook-content.ipynb").write_text(json.dumps({
            "metadata": {"dependencies": {"lakehouse": BINDING}},
            "cells": [
                {"cell_type": "code", "metadata": {"tags": ["parameters"]}, "source": ['target = "gold.prod"']},
                {"cell_type": "code", "metadata": {}, "source": ['spark.table("bronze.orders").write.saveAsTable(target)']},
            ],
        }), encoding="utf-8")
        self.pipeline([self.activity(parameters={"target": {"value": "gold.dev"}})])
        mapping = scan_repository(self.root)
        self.assertEqual(mapping["issues"], [])
        self.assertEqual(mapping["pipelineLinks"][0]["writes"][0]["table"], "gold.dev")

    def test_reports_persist_evidence_csv_and_downloads_require_authentication(self):
        self.notebook()
        self.pipeline()
        mapping = scan_repository(self.root)
        report_id = "a" * 64
        directory = persist_mapping(mapping, report_id)
        self.assertEqual(json.loads((directory / "mapping.json").read_text(encoding="utf-8")), mapping)
        rows = list(csv.DictReader(io.StringIO(mapping_csv(mapping))))
        self.assertTrue(any(row["pipeline"] == "Orchestrate" and row["table"] == "Dev.Publish.gold.orders" for row in rows))
        self.assertTrue(all(row["notebook_file"] and row["line"] for row in rows))
        app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        client = app.test_client()
        url = f"/api/fabric/presets/{report_id}/mapping/json"
        self.assertEqual(client.get(url).status_code, 401)
        with client.session_transaction() as session:
            session["user"] = {"name": "Test User"}
        with patch("fabric.backend.routes.load_preset", return_value={"presetId": report_id}):
            response = client.get(url)
            self.addCleanup(response.close)
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(response.json["summary"]["resolvedCalls"], 1)
            with client.get(url.replace("/json", "/csv")) as csv_response:
                self.assertEqual(csv_response.status_code, 200)
            self.assertEqual(client.get(url.replace("/json", "/txt")).status_code, 400)
        with patch("fabric.backend.routes.load_preset", side_effect=FileNotFoundError):
            self.assertEqual(client.get(url).status_code, 404)
        with self.assertRaises(ValueError):
            persist_mapping(mapping, "../outside")

    def test_invalid_platform_and_pipeline_content_are_explicit_gaps(self):
        self.notebook()
        pipeline = self.pipeline()
        (pipeline / ".platform").write_text("{", encoding="utf-8")
        (pipeline / "pipeline-content.json").write_text("[]", encoding="utf-8")
        mapping = scan_repository(self.root)
        self.assertTrue(any("Could not read .platform" in issue["message"] for issue in mapping["issues"]))
        self.assertTrue(any("Could not read pipeline" in issue["message"] for issue in mapping["issues"]))

    def test_invalid_metadata_shapes_and_notebook_magics_are_explicit_gaps(self):
        notebook = self.notebook()
        (notebook / ".platform").write_text('{"metadata": [], "config": []}', encoding="utf-8")
        self.artifact("Magic", "Notebook", "magic", "# MAGIC %%sql\n# MAGIC SELECT * FROM bronze.orders")
        self.pipeline([{"name": "Invalid", "type": "TridentNotebook", "typeProperties": []}])
        mapping = scan_repository(self.root)
        self.assertEqual(mapping["summary"]["notebooks"], 2)
        self.assertTrue(any("must be a JSON object" in issue["message"] for issue in mapping["issues"]))
        self.assertTrue(any("Notebook magic" in issue["message"] for issue in mapping["issues"]))
        self.assertTrue(any("Invalid activity typeProperties" in issue["message"] for issue in mapping["issues"]))


if __name__ == "__main__":
    unittest.main()
