import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fabric.backend import copilot_impact
from fabric.backend.clients import SemanticModelResolutionError
from fabric.backend.impact import (
    MAX_ARTIFACT_CHARACTERS, build_catalog, live_schema, report_path, save_report,
    validate_changes, validate_result,
)
from shared.app import create_app
from fabric.backend.presets import bind_live_semantic_model, load_preset
from fabric.backend.impact_context import notebook_cells, scope_catalog
from fabric.backend.impact_objects import target_catalog


PRESET_ID = "a" * 64
LIVE_MODEL_ID = "5a7640e2-2d70-437f-a34b-3056cb7ae5cf"
NOTEBOOK_FILE = str(Path("Consumer.Notebook") / "notebook-content.py")
CODE = 'orders = spark.table("gold.orders")\norders.select("amount").show()\n'


class ImpactTests(unittest.TestCase):
    def setUp(self):
        model_environment = patch.dict(os.environ, {"FABRIC_IMPACT_MODEL": ""})
        model_environment.start()
        self.addCleanup(model_environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        self.root.mkdir()
        self.reports = Path(temporary.name) / "impact"
        report_patch = patch("fabric.backend.impact.REPORTS_DIR", self.reports)
        report_patch.start()
        self.addCleanup(report_patch.stop)
        self.preset = {
            "presetId": PRESET_ID, "presetName": "Sales",
            "workspace": {"id": "workspace-id", "displayName": "Finance"},
            "repository": {"id": "repo", "name": "Analytics", "branchName": "main"},
            "semanticModel": {"name": "Sales", "path": "/Sales.SemanticModel"},
            "localRepositoryPath": str(self.root),
        }
        self.model("Sales")
        self.model("Other")
        notebook = self.root / "Consumer.Notebook"
        notebook.mkdir()
        (notebook / ".platform").write_text(json.dumps({
            "metadata": {"type": "Notebook", "displayName": "Consumer"},
            "config": {"logicalId": "consumer"},
        }), encoding="utf-8")
        (self.root / NOTEBOOK_FILE).write_text(CODE, encoding="utf-8")
        self.fabric_client = MagicMock()
        self.fabric_client.get_semantic_model_definition.side_effect = self.live_definition
        for target, value in (
            ("fabric.backend.routes.get_fabric_access_token", "fabric-token"),
            ("fabric.backend.routes.FabricWorkspaceClient", self.fabric_client),
        ):
            mocked = patch(target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def live_definition(self, *_args, **_kwargs):
        document = {"model": {
            "expressions": [{
                "name": "DatabaseQuery",
                "expression": 'AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/workspace-id/lakehouse-id")',
            }],
            "tables": [{
                "name": "Sales orders",
                "columns": [
                    {"name": "Amount", "dataType": "decimal", "sourceColumn": "amount"},
                    {"name": "Order ID", "dataType": "int64", "sourceColumn": "order_id"},
                    {"name": "Live only", "dataType": "string", "sourceColumn": "live_only"},
                ],
                "partitions": [{"name": "Orders", "source": {
                    "type": "entity", "entityName": "orders",
                    "schemaName": "gold", "expressionSource": "DatabaseQuery",
                }}],
            }],
        }}
        return {
            "semanticModel": {"id": LIVE_MODEL_ID, "name": "Sales"},
            "definition": {"parts": [{
                "path": "model.bim", "payloadType": "InlineBase64",
                "payload": base64.b64encode(json.dumps(document).encode()).decode(),
            }]},
        }

    def model(self, name, parent=None):
        definition = (parent or self.root) / f"{name}.SemanticModel" / "definition"
        (definition / "tables").mkdir(parents=True)
        (definition / "expressions.tmdl").write_text('''expression DatabaseQuery =
    AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/workspace-id/lakehouse-id")
''', encoding="utf-8")
        (definition / "tables" / "Orders.tmdl").write_text('''table 'Sales orders'
    column Amount
        dataType: decimal
        sourceColumn: amount
    column 'Order ID'
        dataType: int64
        sourceColumn: order_id
    column Derived = [Amount] * 2
        dataType: decimal
    measure Total = SUM('Sales orders'[Amount])
    partition Orders = entity
        mode: directLake
        source
            entityName: orders
            schemaName: gold
            expressionSource: DatabaseQuery
''', encoding="utf-8")
        (definition / "relationships.tmdl").write_text('''relationship OrdersCustomer
    fromColumn: 'Sales orders'.'Order ID'
    toColumn: Customers.ID
''', encoding="utf-8")
        return definition

    def catalog(self):
        return build_catalog(self.preset)

    def payload(self, catalog=None, **overrides):
        payload = {
            "revision": (catalog or self.catalog())["revision"],
            "instructions": "Rename physical amount to revenue; preserve the model display name.",
            "changes": [{
                "table": "Sales orders", "column": "Amount",
            }],
        }
        payload.update(overrides)
        return payload

    def result(self):
        return {
            "summary": "The consumer requires review.", "gaps": [],
            "impacts": [{
                "artifactKey": "Consumer.Notebook", "changeIds": ["change-1"],
                "objectType": "notebook_code", "objectName": "Cell 1",
                "severity": "high", "confidence": "high",
                "impact": "The selected physical column is renamed.",
                "recommendation": "Update the consumer after the producer schema is migrated.",
                "evidence": [{"file": NOTEBOOK_FILE, "line": 2,
                              "excerpt": 'orders.select("amount").show()'}],
                "proposedFixes": [{
                    "file": NOTEBOOK_FILE, "before": 'orders.select("amount")',
                    "after": 'orders.select("revenue")', "rationale": "Use the new source name.",
                }],
                "validationSteps": ["Verify the new output schema and execute the consumer in a test workspace."],
            }],
        }

    def test_catalog_discovers_all_models_and_notebooks_and_preserves_source_mapping(self):
        self.model("Hidden", self.root / "legacy")
        catalog = self.catalog()
        self.assertEqual({artifact["key"] for artifact in catalog["artifacts"]},
                         {"Consumer.Notebook", "Sales.SemanticModel", "Other.SemanticModel"})
        self.assertEqual(catalog["tables"][0]["name"], "Sales orders")
        self.assertEqual(catalog["tables"][0]["columns"][0]["sourceColumn"], "amount")
        self.assertEqual(catalog["tables"][0]["columns"][2]["kind"], "calculated")
        self.assertEqual(catalog["tables"][0]["sources"][0]["table"], "gold.orders")
        other = next(item for item in catalog["artifacts"] if item["key"] == "Other.SemanticModel")
        self.assertTrue(any(file.endswith("relationships.tmdl") for file in other["files"]))
        self.assertEqual(catalog["revision"], self.catalog()["revision"])
        (self.root / NOTEBOOK_FILE).write_text(CODE + "print('changed')", encoding="utf-8")
        self.assertNotEqual(catalog["revision"], self.catalog()["revision"])

    def test_bim_models_are_selectable(self):
        folder = self.root / "Bim.SemanticModel"
        folder.mkdir()
        (folder / "model.bim").write_text(json.dumps({"model": {"tables": [{
            "name": "Products", "columns": [{"name": "SKU", "dataType": "string", "sourceColumn": "sku"}],
            "partitions": [{"name": "Products", "source": {
                "type": "entity", "entityName": "products", "schemaName": "dbo",
            }}],
        }]}}), encoding="utf-8")
        self.preset["semanticModel"] = {"name": "Bim", "path": "/Bim.SemanticModel"}
        self.assertEqual(self.catalog()["tables"][0]["columns"][0]["name"], "SKU")

    def test_ipynb_excludes_outputs_and_uses_exported_code_lines(self):
        (self.root / NOTEBOOK_FILE).unlink()
        file = self.root / "Consumer.Notebook" / "notebook-content.ipynb"
        file.write_text(json.dumps({
            "cells": [
                {"cell_type": "markdown", "source": ["not code"]},
                {"cell_type": "code", "source": [CODE], "outputs": [{"text": "private output"}]},
            ],
        }), encoding="utf-8")
        artifact = next(item for item in self.catalog()["artifacts"] if item["type"] == "Notebook")
        self.assertEqual(list(artifact["files"].values()), [CODE])

    def test_validates_multiple_columns_with_one_shared_instruction(self):
        catalog = self.catalog()
        payload = self.payload(catalog)
        payload["changes"].append({
            "table": "Sales orders", "column": "Order ID",
        })
        payload["instructions"] += " Source IDs become strings; preserve leading zeros."
        changes = validate_changes(payload, catalog)
        self.assertEqual([item["id"] for item in changes], ["change-1", "change-2"])
        self.assertEqual(changes[0]["current"]["sourceColumn"], "amount")
        self.assertNotIn("comment", changes[1])
        self.assertNotIn("changeType", changes[1])

    def test_selection_can_span_multiple_tables(self):
        file = self.root / "Sales.SemanticModel" / "definition" / "tables" / "Customers.tmdl"
        file.write_text('''table Customers
    column ID
        dataType: int64
        sourceColumn: id
    partition Customers = entity
        source
            entityName: customers
            schemaName: gold
            expressionSource: DatabaseQuery
''', encoding="utf-8")
        catalog = self.catalog()
        payload = self.payload(catalog)
        payload["changes"].append({
            "table": "Customers", "column": "ID",
        })
        self.assertEqual({item["table"] for item in validate_changes(payload, catalog)},
                         {"Sales orders", "Customers"})

    def test_rejects_stale_schema_unknown_duplicate_or_empty_instructions(self):
        catalog = self.catalog()
        cases = []
        for key, value in (
            ("revision", "stale"), ("changes", []), ("instructions", ""),
            ("instructions", " "), ("instructions", "x" * 20001),
        ):
            cases.append(self.payload(catalog, **{key: value}))
        for field, value in (("table", "Unknown"), ("column", "Unknown")):
            payload = self.payload(catalog)
            payload["changes"][0][field] = value
            cases.append(payload)
        duplicate = self.payload(catalog)
        duplicate["changes"] *= 2
        cases.append(duplicate)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_changes(payload, catalog)

    def test_shared_instructions_are_required_without_a_consent_gate(self):
        catalog = self.catalog()
        payload = self.payload(catalog)
        self.assertNotIn("consent", payload)
        self.assertEqual(validate_changes(payload, catalog)[0]["column"], "Amount")
        del payload["instructions"]
        with self.assertRaisesRegex(ValueError, "Analysis instructions"):
            validate_changes(payload, catalog)

    def test_live_schema_is_authoritative_and_replaces_selected_snapshot_definition(self):
        current = live_schema(self.preset, self.fabric_client)
        self.assertIn("Live only", [column["name"] for column in current["tables"][0]["columns"]])
        self.assertNotIn("Derived", [column["name"] for column in current["tables"][0]["columns"]])
        payload = self.payload(current)
        payload["changes"][0]["column"] = "Live only"
        self.assertEqual(validate_changes(payload, current)[0]["current"]["sourceColumn"], "live_only")
        catalog = build_catalog(self.preset, current)
        selected = next(item for item in catalog["artifacts"] if item["key"] == "Sales.SemanticModel")
        self.assertEqual(selected["origin"], "fabric_api")
        self.assertEqual(selected["files"], current["files"])
        self.assertEqual(catalog["schemaRevision"], current["revision"])
        self.assertEqual(len(catalog["artifacts"]), 3)
        self.fabric_client.get_semantic_model_definition.assert_called_with("workspace-id", "Sales")

    def test_saved_binding_is_used_for_schema_and_report_identity(self):
        self.preset["liveSemanticModel"] = {"id": LIVE_MODEL_ID, "name": "Sales"}
        current = live_schema(self.preset, self.fabric_client)
        self.fabric_client.get_semantic_model_definition.assert_called_with(
            "workspace-id", "Sales", model_id=LIVE_MODEL_ID,
        )
        catalog = build_catalog(self.preset, current)
        self.assertEqual(catalog["selection"]["semanticModel"]["id"], LIVE_MODEL_ID)
        self.assertEqual(catalog["selection"]["semanticModel"]["path"], "/Sales.SemanticModel")

    def test_live_model_binding_is_saved_atomically_without_changing_repository_selection(self):
        directory = self.root.parent / "presets"
        directory.mkdir()
        file = directory / f"{PRESET_ID}.json"
        file.write_text(json.dumps(self.preset), encoding="utf-8")
        binding = {"id": LIVE_MODEL_ID, "name": "Sales Production"}
        with patch("fabric.backend.presets.PRESETS_DIR", directory):
            saved = bind_live_semantic_model(PRESET_ID, binding)
            self.assertEqual(load_preset(PRESET_ID)["liveSemanticModel"], binding)
        self.assertEqual(saved["semanticModel"], self.preset["semanticModel"])
        self.assertEqual(saved["repository"], self.preset["repository"])
        self.assertFalse(list(directory.glob("*.tmp")))

    def test_invalid_live_definition_has_no_snapshot_fallback(self):
        for definition in (None, {"parts": []}, {"parts": [{
            "path": "model.bim", "payloadType": "InlineBase64", "payload": "invalid!",
        }]}):
            with self.subTest(definition=definition):
                self.fabric_client.get_semantic_model_definition.side_effect = None
                self.fabric_client.get_semantic_model_definition.return_value = {
                    "semanticModel": {"id": "live", "name": "Sales"}, "definition": definition,
                }
                with self.assertRaises(RuntimeError):
                    live_schema(self.preset, self.fabric_client)

    def test_validates_exact_citations_and_replacement_source(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        result = validate_result(self.result(), changes, catalog["artifacts"])
        self.assertEqual(len(result["impacts"]), 1)
        variations = [
            ("artifactKey", "../outside"),
            ("changeIds", ["unknown"]), ("evidence", []),
            ("evidence", [{"file": NOTEBOOK_FILE, "line": 99, "excerpt": "invented"}]),
            ("evidence", [{"file": NOTEBOOK_FILE, "line": 1, "excerpt": 'orders.select("amount")'}]),
            ("evidence", [{"file": "outside.py", "line": 1, "excerpt": "invented"}]),
            ("proposedFixes", [{"file": NOTEBOOK_FILE, "before": "invented", "after": "changed", "rationale": "test"}]),
            ("validationSteps", []), ("severity", "critical"), ("objectType", "unsupported"),
        ]
        for field, value in variations:
            response = self.result()
            response["impacts"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_result(response, changes, catalog["artifacts"])

    def test_drop_fix_may_remove_source_text(self):
        catalog = self.catalog()
        result = self.result()
        result["impacts"][0]["proposedFixes"][0]["after"] = ""
        changes = validate_changes(self.payload(catalog), catalog)
        self.assertEqual(validate_result(result, changes, catalog["artifacts"])["impacts"][0]["proposedFixes"][0]["after"], "")

    def test_reports_persist_and_do_not_modify_snapshot(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        result = validate_result(self.result(), changes, catalog["artifacts"])
        result["analyzedArtifacts"] = ["Consumer.Notebook"]
        report = save_report(PRESET_ID, catalog, changes, [result], [{
            "file": "Other.SemanticModel", "message": "Skipped for test.",
        }])
        self.assertEqual(report["coverage"], "partial")
        self.assertEqual(report["mode"], "recommendations_only")
        saved = json.loads(report_path(PRESET_ID, report["reportId"]).read_text(encoding="utf-8"))
        self.assertEqual(saved, report)
        self.assertEqual(self.catalog()["revision"], catalog["revision"])
        self.assertFalse(list(self.reports.rglob("*.tmp")))
        with self.assertRaises(ValueError):
            report_path("../escape", "b" * 32)

    def test_unreadable_or_unsupported_model_is_an_explicit_gap(self):
        broken = self.root / "Broken.SemanticModel"
        broken.mkdir()
        (broken / "model.bim").write_text("{invalid", encoding="utf-8")
        catalog = self.catalog()
        self.assertTrue(any("Broken.SemanticModel" == issue["file"] for issue in catalog["issues"]))

    def sdk_model(self, model_id="test-model", window=256_000, prompt_limit=240_000,
                  multiplier=None, policy=None):
        return SimpleNamespace(
            id=model_id, capabilities=SimpleNamespace(limits=SimpleNamespace(
                max_context_window_tokens=window, max_prompt_tokens=prompt_limit,
            )),
            billing=SimpleNamespace(multiplier=multiplier),
            policy=SimpleNamespace(state=policy) if policy is not None else None,
        )

    def sdk_client(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get_auth_status = AsyncMock(return_value=SimpleNamespace(isAuthenticated=True))
        client.list_models = AsyncMock(return_value=[self.sdk_model()])
        session = MagicMock(session_id="impact-session")
        session.rpc.model.get_current = AsyncMock(return_value=SimpleNamespace(model_id="test-model"))
        session.disconnect = AsyncMock()
        client.create_session = AsyncMock(return_value=session)
        client.delete_session = AsyncMock()

        async def reply(prompt, **_kwargs):
            artifact = json.loads(prompt)["artifact"]
            result = self.result() if artifact["type"] == "Notebook" else {
                "summary": "Model source mapping needs human review.", "gaps": ["Source scope requires confirmation."],
                "impacts": [],
            }
            return SimpleNamespace(data=SimpleNamespace(content=json.dumps(result)))
        session.send_and_wait = AsyncMock(side_effect=reply)
        return client, session

    def test_sdk_analyzes_every_artifact_with_tools_disabled_and_validates_output(self):
        catalog = self.catalog()
        catalog["instructions"] = self.payload(catalog)["instructions"]
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        with patch.object(copilot_impact, "CopilotClient", return_value=client) as constructor:
            report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertEqual(client.create_session.await_count, len(catalog["artifacts"]))
        self.assertEqual(len(report["impacts"]), 1)
        self.assertTrue(all(item["status"] == "analyzed" for item in report["inventory"]))
        kwargs = client.create_session.call_args.kwargs
        self.assertEqual(kwargs["available_tools"], [])
        self.assertFalse(kwargs["enable_config_discovery"])
        self.assertFalse(kwargs["enable_file_hooks"])
        self.assertEqual(kwargs["on_permission_request"](None, None).kind, "reject")
        self.assertTrue(constructor.call_args.kwargs["use_logged_in_user"])
        self.assertNotIn("GITHUB_TOKEN", constructor.call_args.kwargs["env"])
        self.assertEqual(session.disconnect.await_count, len(catalog["artifacts"]))
        self.assertEqual(client.delete_session.await_count, len(catalog["artifacts"]))
        for call in session.send_and_wait.call_args_list:
            prompt = json.loads(call.args[0])
            self.assertEqual(prompt["analysisInstructions"], catalog["instructions"])
        self.assertEqual(report["instructions"], catalog["instructions"])

    def test_authentication_failure_produces_no_report(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        client, _ = self.sdk_client()
        client.get_auth_status.return_value = SimpleNamespace(isAuthenticated=False)
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            with self.assertRaisesRegex(copilot_impact.CopilotAuthenticationRequired, "copilot login"):
                copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        client.create_session.assert_not_called()
        self.assertFalse(self.reports.exists())

    def test_invalid_output_is_corrected_once_and_revalidated(self):
        catalog = self.catalog()
        catalog["artifacts"] = [item for item in catalog["artifacts"] if item["type"] == "Notebook"]
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        session.send_and_wait.side_effect = [
            SimpleNamespace(data=SimpleNamespace(content='{"impacts": []}')),
            SimpleNamespace(data=SimpleNamespace(content=json.dumps(self.result()))),
        ]
        progress = MagicMock()
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, progress)
        self.assertEqual(len(report["impacts"]), 1)
        self.assertEqual(session.send_and_wait.await_count, 2)
        self.assertIn("failed source/schema validation", session.send_and_wait.call_args.args[0])
        self.assertTrue(any("correcting invalid AI output" in call.args[-1] for call in progress.call_args_list))

    def test_repeated_invalid_ai_output_produces_no_report(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        session.send_and_wait.side_effect = None
        session.send_and_wait.return_value = SimpleNamespace(data=SimpleNamespace(content="{}"))
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "failed validation"):
                copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertEqual(session.send_and_wait.await_count, 2)
        self.assertFalse(self.reports.exists())

    def test_sdk_rpc_errors_are_explicit_failures(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        session.send_and_wait.side_effect = copilot_impact.JsonRpcError(-32603, "SDK unavailable")
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "GitHub Copilot SDK failed"):
                copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertFalse(self.reports.exists())

    def test_invalid_ai_or_timeout_fails_without_success_shaped_report(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        for error in (TimeoutError("Timed out"), ValueError("Malformed response")):
            client, session = self.sdk_client()
            session.send_and_wait.side_effect = error
            with patch.object(copilot_impact, "CopilotClient", return_value=client):
                with self.assertRaises(type(error)):
                    copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
            session.disconnect.assert_awaited_once()
            client.delete_session.assert_awaited_once()
        self.assertFalse(self.reports.exists())

    def test_oversized_artifact_is_not_silently_truncated(self):
        catalog = self.catalog()
        catalog["artifacts"][0]["files"] = {"large.py": "x" * (MAX_ARTIFACT_CHARACTERS + 1)}
        catalog["artifacts"][0]["cells"] = [{
            "cell": 1, "startLine": 1, "source": "x" * (MAX_ARTIFACT_CHARACTERS + 1), "parameters": False,
        }]
        changes = validate_changes(self.payload(catalog), catalog)
        client, _ = self.sdk_client()
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertEqual(report["inventory"][0]["status"], "skipped")
        self.assertTrue(any("character analysis limit" in gap["message"] for gap in report["gaps"]))
        self.assertEqual(report["coverage"], "partial")

    def test_artifact_at_the_exact_character_limit_is_analyzed(self):
        catalog = self.catalog()
        catalog["artifacts"] = [item for item in catalog["artifacts"] if item["type"] == "Notebook"]
        catalog["artifacts"][0]["files"] = {"boundary.py": "x" * MAX_ARTIFACT_CHARACTERS}
        catalog["artifacts"][0]["cells"] = [{
            "cell": 1, "startLine": 1, "source": "x" * MAX_ARTIFACT_CHARACTERS, "parameters": False,
        }]
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        session.send_and_wait.side_effect = None
        session.send_and_wait.return_value = SimpleNamespace(data=SimpleNamespace(content=json.dumps({
            "summary": "No column references in this synthetic boundary artifact.", "impacts": [], "gaps": [],
        })))
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertEqual(report["inventory"][0]["status"], "analyzed")
        session.send_and_wait.assert_awaited_once()

    def test_request_context_limit_is_not_silently_truncated(self):
        catalog = self.catalog()
        catalog["instructions"] = "x" * 180_001
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "No artifacts could be analyzed"):
                copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        client.create_session.assert_not_called()
        session.send_and_wait.assert_not_called()
        self.assertFalse(self.reports.exists())

    def test_backtracks_producers_and_follows_consumers_without_unrelated_branches(self):
        for name, code in {
            "Producer": 'spark.table("bronze.orders").write.saveAsTable("gold.orders")',
            "Bronze": 'spark.table("raw.orders").write.saveAsTable("bronze.orders")',
            "Downstream": 'spark.table("gold.other").select("amount").show()',
            "Unrelated": 'spark.table("bronze.orders").write.saveAsTable("gold.unrelated")',
        }.items():
            folder = self.root / f"{name}.Notebook"
            folder.mkdir()
            (folder / "notebook-content.py").write_text(code, encoding="utf-8")
        (self.root / NOTEBOOK_FILE).write_text(
            CODE + 'orders.write.saveAsTable("gold.other")\n', encoding="utf-8",
        )
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = scope_catalog(catalog, changes)
        keys = {item["key"] for item in scoped["artifacts"]}
        self.assertTrue({"Producer.Notebook", "Bronze.Notebook", "Downstream.Notebook"} <= keys)
        self.assertNotIn("Unrelated.Notebook", keys)
        self.assertEqual(scoped["inventoryScope"]["Unrelated.Notebook"]["status"], "out_of_scope")
        for item in scoped["artifacts"]:
            prompt = json.loads(copilot_impact._prompt(scoped, changes, item))
            self.assertNotIn("gold.unrelated", json.dumps(prompt["repositoryTableLineage"]))

    def test_ipynb_requests_contain_required_cells_and_original_cell_locations(self):
        path = self.root / "Consumer.Notebook" / "notebook-content.ipynb"
        (self.root / NOTEBOOK_FILE).unlink()
        cells = [
            {"cell_type": "markdown", "source": ["private markdown"]},
            {"cell_type": "code", "source": ['from pyspark.sql.functions import col\n']},
            {"cell_type": "code", "source": ['unrelated = spark.table("silver.unrelated")\n']},
            {"cell_type": "code", "source": ['orders = spark.table("gold.orders")\n']},
            {"cell_type": "code", "source": ['result = orders.select(col("amount"))\n'],
             "outputs": [{"text": ["private output"]}]},
            {"cell_type": "code", "source": ['result.show()\n']},
            {"cell_type": "code", "source": ['unrelated.show()\n']},
        ]
        path.write_text(json.dumps({"cells": cells}), encoding="utf-8")
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = scope_catalog(catalog, changes)
        artifact = next(item for item in scoped["artifacts"] if item["type"] == "Notebook")
        prompt = json.loads(copilot_impact._prompt(scoped, changes, artifact))
        supplied = prompt["artifact"]["notebookCells"]
        self.assertEqual([cell["cell"] for cell in supplied], [2, 4, 5, 6])
        self.assertEqual(supplied[1]["startLine"], 5)
        self.assertNotIn("numberedFiles", prompt["artifact"])
        self.assertNotIn("private", json.dumps(prompt))
        self.assertNotIn("unrelated.show", json.dumps(prompt))
        result = self.result()
        file = next(iter(artifact["files"]))
        result["impacts"][0]["evidence"] = [{
            "file": file, "line": 7, "excerpt": 'result = orders.select(col("amount"))',
        }]
        result["impacts"][0]["proposedFixes"] = [{
            "file": file, "before": 'orders.select(col("amount"))',
            "after": 'orders.select(col("revenue"))', "rationale": "Use renamed source.",
        }]
        validated = validate_result(result, changes, [artifact])
        citation = validated["impacts"][0]["evidence"][0]
        self.assertEqual((citation["cell"], citation["cellLine"]), (5, 1))
        self.assertEqual(validated["impacts"][0]["proposedFixes"][0]["cells"], [5])
        result["impacts"][0]["evidence"] = [{
            "file": file, "line": 3, "excerpt": 'unrelated = spark.table("silver.unrelated")',
        }]
        with self.assertRaisesRegex(ValueError, "not supplied"):
            validate_result(result, changes, [artifact])
        result["impacts"][0]["evidence"] = [citation]
        result["impacts"][0]["proposedFixes"][0]["before"] = "unrelated.show()"
        with self.assertRaisesRegex(ValueError, "outside supplied"):
            validate_result(result, changes, [artifact])

    def test_export_cells_preserve_parameters_and_skip_metadata_and_markdown(self):
        path = self.root / NOTEBOOK_FILE
        code = '''# Fabric notebook source
# METADATA ********************
# META {"description": "not sent"}
# PARAMETERS CELL ********************
schema = "gold"
# METADATA ********************
# META {"language": "python"}
# MARKDOWN CELL ********************
# not sent either
# CELL ********************
orders = spark.table(f"{schema}.orders")
# METADATA ********************
# META {"language": "python"}
# CELL ********************
orders.select("amount").show()
# METADATA ********************
# META {"language": "python"}
# CELL ********************
unrelated = spark.table("other.table")
'''
        path.write_text(code, encoding="utf-8")
        cells = notebook_cells(path, code)
        self.assertEqual([cell["cell"] for cell in cells], [1, 3, 4, 5])
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = scope_catalog(catalog, changes)
        artifact = next(item for item in scoped["artifacts"] if item["type"] == "Notebook")
        prompt = copilot_impact._prompt(scoped, changes, artifact)
        self.assertEqual([cell["cell"] for cell in artifact["cells"]], [1, 3, 4])
        self.assertNotIn("# META", prompt)
        self.assertNotIn("not sent", prompt)
        self.assertNotIn("other.table", prompt)

    def test_sql_view_cell_dependencies_are_retained(self):
        code = '''# CELL ********************
orders = spark.table("gold.orders")
# CELL ********************
orders.createOrReplaceTempView("orders_view")
# CELL ********************
transformed = spark.sql("SELECT amount FROM orders_view")
# CELL ********************
transformed.show()
# CELL ********************
spark.range(10).show()
'''
        (self.root / NOTEBOOK_FILE).write_text(code, encoding="utf-8")
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        artifact = next(item for item in scope_catalog(catalog, changes)["artifacts"]
                        if item["type"] == "Notebook")
        self.assertEqual([cell["cell"] for cell in artifact["cells"]], [1, 2, 3, 4])

    def test_token_budget_checks_exact_boundary_schema_system_unicode_and_output(self):
        client, _ = self.sdk_client()
        model = client.list_models.return_value[0]
        prompt = '{"instructions": "中文\\n!@#$%^&*()"}'
        budget = copilot_impact._context_budget(model, prompt)
        expected = sum(len(text.encode("utf-8")) for text in (
            copilot_impact.SYSTEM_PROMPT,
            json.dumps(copilot_impact.RESPONSE_SCHEMA, ensure_ascii=True), prompt,
        )) + copilot_impact.PROTOCOL_TOKEN_RESERVE
        self.assertEqual(budget["inputTokenUpperBound"], expected)
        self.assertGreater(expected, len(prompt))
        model.capabilities.limits.max_prompt_tokens = expected
        self.assertEqual(copilot_impact._context_budget(model, prompt)["inputTokenLimit"], expected)
        model.capabilities.limits.max_prompt_tokens = expected - 1
        self.assertEqual(copilot_impact._context_budget(model, prompt)["inputTokenLimit"], expected - 1)
        model.capabilities.limits.max_prompt_tokens = None
        model.capabilities.limits.max_context_window_tokens = 20_000
        self.assertEqual(copilot_impact._context_budget(model, prompt)["inputTokenLimit"],
                         20_000 - copilot_impact.OUTPUT_TOKENS - copilot_impact.PROTOCOL_TOKEN_RESERVE)

    def test_model_context_overflow_is_blocked_before_send(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        client.list_models.return_value[0].capabilities.limits.max_context_window_tokens = 12_000
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "No artifacts could be analyzed"):
                copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        session.send_and_wait.assert_not_called()
        self.assertFalse(self.reports.exists())

    def test_missing_model_limits_fail_closed(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        for model_id, limit in (("unknown", 256_000), ("test-model", None)):
            with self.subTest(model=model_id):
                client, session = self.sdk_client()
                session.rpc.model.get_current.return_value.model_id = model_id
                client.list_models.return_value[0].capabilities.limits.max_context_window_tokens = limit
                with patch.object(copilot_impact, "CopilotClient", return_value=client):
                    with self.assertRaisesRegex(ValueError, "context limits"):
                        copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
                session.send_and_wait.assert_not_called()
                if limit is None:
                    client.create_session.assert_not_called()
                else:
                    session.disconnect.assert_awaited_once()

    def test_auto_model_without_limits_is_replaced_by_a_concrete_model(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        for configured in ("", "auto", "  auto  "):
            with self.subTest(configured=configured):
                client, session = self.sdk_client()
                client.list_models.return_value.insert(0, self.sdk_model("auto", window=None, prompt_limit=None))
                with patch.dict(os.environ, {"FABRIC_IMPACT_MODEL": configured}), \
                        patch.object(copilot_impact, "CopilotClient", return_value=client):
                    report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
                self.assertTrue(client.create_session.call_args_list)
                self.assertTrue(all(call.kwargs["model"] == "test-model"
                                    for call in client.create_session.call_args_list))
                self.assertTrue(all(item["contextBudget"]["model"] == "test-model"
                                    for item in report["inventory"] if item["status"] == "analyzed"))

    def test_model_selection_prefers_advertised_cost_then_lightweight_models(self):
        premium = self.sdk_model("premium", multiplier=3)
        cheap = self.sdk_model("standard", multiplier=0)
        unknown = self.sdk_model("unknown-mini")
        disabled = self.sdk_model("disabled-mini", multiplier=0, policy="disabled")
        unconfigured = self.sdk_model("unconfigured-mini", multiplier=0, policy="unconfigured")
        broken = self.sdk_model("broken", window=None, multiplier=0)
        models = {model.id: model for model in (premium, unknown, cheap, disabled, unconfigured, broken)}
        self.assertIs(copilot_impact._select_model(models, "request"), cheap)
        cheap.billing.multiplier = None
        premium.billing.multiplier = None
        self.assertIs(copilot_impact._select_model(models, "request"), unknown)
        self.assertIs(copilot_impact._select_model(models, "request", "premium"), premium)

    def test_automatic_model_selection_only_considers_models_that_fit(self):
        small = self.sdk_model("small-mini", window=16_000, prompt_limit=1, multiplier=0)
        fitting = self.sdk_model("larger", multiplier=1)
        models = {model.id: model for model in (small, fitting)}
        self.assertIs(copilot_impact._select_model(models, "request"), fitting)
        self.assertIs(copilot_impact._select_model(models, "request", "small-mini"), small)

    def test_model_selection_ignores_nonfinite_or_negative_billing_metadata(self):
        lightweight = self.sdk_model("a-mini")
        for price in (-1, float("inf"), float("nan"), True):
            with self.subTest(price=price):
                invalid_price = self.sdk_model("b-premium", multiplier=price)
                self.assertIs(copilot_impact._select_model({
                    model.id: model for model in (invalid_price, lightweight)
                }, "request"), lightweight)

    def test_invalid_explicit_model_is_not_silently_replaced_or_sent(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        for model_id in ("missing-model", "disabled", "broken"):
            client.list_models.return_value = [
                self.sdk_model(), self.sdk_model("disabled", policy="disabled"),
                self.sdk_model("broken", window=None),
            ]
            with self.subTest(model=model_id), \
                    patch.dict(os.environ, {"FABRIC_IMPACT_MODEL": model_id}), \
                    patch.object(copilot_impact, "CopilotClient", return_value=client):
                with self.assertRaisesRegex(ValueError, "Available concrete models: test-model"):
                    copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        client.create_session.assert_not_called()
        session.send_and_wait.assert_not_called()

    def test_explicit_concrete_model_is_pinned_in_every_session(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        client.list_models.return_value.append(self.sdk_model("less-expensive-mini", multiplier=0))
        with patch.dict(os.environ, {"FABRIC_IMPACT_MODEL": " test-model "}), \
                patch.object(copilot_impact, "CopilotClient", return_value=client):
            copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertTrue(all(call.kwargs["model"] == "test-model"
                            for call in client.create_session.call_args_list))
        self.assertGreater(session.send_and_wait.await_count, 0)

    def test_no_usable_concrete_model_fails_before_session_creation(self):
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        for available in ([], [self.sdk_model("auto", window=None)]):
            with self.subTest(models=[model.id for model in available]):
                client, session = self.sdk_client()
                client.list_models.return_value = available
                with patch.object(copilot_impact, "CopilotClient", return_value=client):
                    with self.assertRaisesRegex(ValueError, "no enabled concrete models"):
                        copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
                client.create_session.assert_not_called()
                session.send_and_wait.assert_not_called()

    def test_correction_uses_a_fresh_budgeted_session_with_original_cells(self):
        catalog = self.catalog()
        catalog["artifacts"] = [item for item in catalog["artifacts"] if item["type"] == "Notebook"]
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        session.send_and_wait.side_effect = [
            SimpleNamespace(data=SimpleNamespace(content="{}")),
            SimpleNamespace(data=SimpleNamespace(content=json.dumps(self.result()))),
        ]
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertEqual(client.create_session.await_count, 2)
        self.assertEqual(session.disconnect.await_count, 2)
        first, second = [call.args[0] for call in session.send_and_wait.call_args_list]
        self.assertTrue(second.startswith(first))
        self.assertIn("notebookCells", second)
        config = client.create_session.call_args.kwargs
        self.assertEqual(config["model_capabilities"].limits.max_output_tokens, copilot_impact.OUTPUT_TOKENS)
        self.assertFalse(config["infinite_sessions"]["enabled"])
        self.assertEqual(report["inventory"][0]["contextBudget"]["model"], "test-model")

    def test_send_accepts_exact_token_budget_and_rejects_one_token_over(self):
        catalog = self.catalog()
        catalog["artifacts"] = [item for item in catalog["artifacts"] if item["type"] == "Notebook"]
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = target_catalog(scope_catalog(catalog, changes), catalog, changes)
        prompt = copilot_impact._prompt(scoped, changes, scoped["artifacts"][0])
        for over in (0, 1):
            with self.subTest(over=over):
                client, session = self.sdk_client()
                model = client.list_models.return_value[0]
                bound = copilot_impact._context_budget(model, prompt)["inputTokenUpperBound"]
                model.capabilities.limits.max_prompt_tokens = bound - over
                with patch.object(copilot_impact, "CopilotClient", return_value=client):
                    if over:
                        with self.assertRaisesRegex(ValueError, "No artifacts could be analyzed"):
                            copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
                    else:
                        report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
                        self.assertEqual(report["inventory"][0]["contextBudget"]["inputTokenUpperBound"], bound)
                self.assertEqual(session.send_and_wait.await_count, 0 if over else 1)

    def test_correction_that_no_longer_fits_is_not_sent(self):
        catalog = self.catalog()
        catalog["artifacts"] = [item for item in catalog["artifacts"] if item["type"] == "Notebook"]
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = target_catalog(scope_catalog(catalog, changes), catalog, changes)
        prompt = copilot_impact._prompt(scoped, changes, scoped["artifacts"][0])
        client, session = self.sdk_client()
        model = client.list_models.return_value[0]
        model.capabilities.limits.max_prompt_tokens = copilot_impact._context_budget(
            model, prompt,
        )["inputTokenUpperBound"]
        session.send_and_wait.side_effect = None
        session.send_and_wait.return_value = SimpleNamespace(data=SimpleNamespace(content="{}"))
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "No artifacts could be analyzed"):
                copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertEqual(session.send_and_wait.await_count, 1)
        self.assertEqual(client.create_session.await_count, 2)
        self.assertEqual(client.delete_session.await_count, 2)
        self.assertFalse(self.reports.exists())

    def test_unused_large_cells_do_not_trigger_the_selected_source_limit(self):
        code = '''# CELL ********************
orders = spark.table("gold.orders")
orders.select("amount").show()
# CELL ********************
unrelated = "''' + "x" * (MAX_ARTIFACT_CHARACTERS + 1) + '"\n'
        (self.root / NOTEBOOK_FILE).write_text(code, encoding="utf-8")
        catalog = self.catalog()
        catalog["artifacts"] = [item for item in catalog["artifacts"] if item["type"] == "Notebook"]
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        session.send_and_wait.side_effect = None
        session.send_and_wait.return_value = SimpleNamespace(data=SimpleNamespace(content=json.dumps({
            "summary": "The selected cells require migration review.", "impacts": [], "gaps": [],
        })))
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        self.assertEqual(report["inventory"][0]["status"], "analyzed")
        self.assertEqual(report["inventory"][0]["selectedCells"], [1])
        self.assertNotIn("unrelated", session.send_and_wait.call_args.args[0])

    def targeted(self, payload=None):
        catalog = self.catalog()
        changes = validate_changes(payload or self.payload(catalog), catalog)
        return catalog, changes, target_catalog(scope_catalog(catalog, changes), catalog, changes)

    def test_code_identifies_transitive_model_objects_without_unrelated_measures(self):
        path = self.root / "Sales.SemanticModel" / "definition" / "tables" / "Orders.tmdl"
        with path.open("a", encoding="utf-8") as file:
            file.write("""    measure Chained = [Total] * 2
    measure Unrelated = SUM('Sales orders'[Order ID])
    measure Literal = "[Amount]"
    hierarchy Amounts
        level Value
            column: Amount
""")
        catalog, changes, scoped = self.targeted()
        model = next(item for item in scoped["artifacts"] if item["key"] == "Sales.SemanticModel")
        names = {target["objectName"] for target in model["targets"]}
        self.assertTrue({"Sales orders", "Sales orders.Amount", "Sales orders.Total",
                         "Sales orders.Chained", "Sales orders.Derived", "Sales orders.Amounts",
                         "Sales orders.Orders"} <= names)
        self.assertFalse({"Sales orders.Unrelated", "Sales orders.Literal", "OrdersCustomer"} & names)
        prompt = json.loads(copilot_impact._prompt(scoped, changes, model))
        source = json.dumps(prompt["artifact"]["sourceSegments"])
        self.assertNotIn("measure Unrelated", source)
        self.assertNotIn("measure Literal", source)
        self.assertNotIn("numberedFiles", prompt["artifact"])
        edges = scoped["impactLineage"]["edges"]
        targets = {target["objectName"]: target["id"] for target in model["targets"]}
        self.assertIn({"source": targets["Sales orders.Total"], "target": targets["Sales orders.Chained"],
                       "kind": "model_dependency", "confidence": "static"}, edges)
        self.assertEqual(catalog["revision"], self.catalog()["revision"])

    def test_relationship_sort_and_role_dependencies_are_code_targets(self):
        path = self.root / "Sales.SemanticModel" / "definition" / "tables" / "Orders.tmdl"
        with path.open("a", encoding="utf-8") as file:
            file.write("""    column Label
        dataType: string
        sourceColumn: label
        sortByColumn: 'Order ID'
""")
        role = self.root / "Sales.SemanticModel" / "definition" / "roles.tmdl"
        role.write_text("""role Reader
    modelPermission: read
    tablePermission 'Sales orders' = 'Sales orders'[Order ID] > 0
""", encoding="utf-8")
        payload = self.payload(changes=[{"table": "Sales orders", "column": "Order ID"}])
        _, _, scoped = self.targeted(payload)
        model = next(item for item in scoped["artifacts"] if item["key"] == "Sales.SemanticModel")
        names = {target["objectName"] for target in model["targets"]}
        self.assertTrue({"OrdersCustomer", "Sales orders.Label", "Reader"} <= names)

    def test_unqualified_role_filters_keep_their_table_scope(self):
        role = self.root / "Sales.SemanticModel" / "definition" / "roles.tmdl"
        role.write_text("""role Reader
    modelPermission: read
    tablePermission 'Sales orders' = "ignore [Other]" <> "" && [Amount] > 0
    tablePermission Customers = [Amount] > 0
""", encoding="utf-8")
        _, _, scoped = self.targeted()
        model = next(item for item in scoped["artifacts"] if item["key"] == "Sales.SemanticModel")
        self.assertIn("Reader", {target["objectName"] for target in model["targets"]})
        role.write_text("""role Reader
    modelPermission: read
    tablePermission Customers = [Amount] > 0
""", encoding="utf-8")
        _, _, scoped = self.targeted()
        model = next(item for item in scoped["artifacts"] if item["key"] == "Sales.SemanticModel")
        self.assertNotIn("Reader", {target["objectName"] for target in model["targets"]})

    def test_calculated_table_dependency_reaches_its_columns_and_measures(self):
        path = self.root / "Sales.SemanticModel" / "definition" / "tables" / "Computed.tmdl"
        path.write_text("""table Computed
    column Value
        dataType: decimal
    measure Grand = SUM(Computed[Value])
    partition Computed = calculated
        source = SELECTCOLUMNS('Sales orders', "Value", 'Sales orders'[Amount])
""", encoding="utf-8")
        _, _, scoped = self.targeted()
        model = next(item for item in scoped["artifacts"] if item["key"] == "Sales.SemanticModel")
        self.assertTrue({"Computed", "Computed.Value", "Computed.Grand"} <= {
            target["objectName"] for target in model["targets"]
        })

    def test_compact_bim_slicing_preserves_exact_objects_and_blocks_sibling_evidence(self):
        folder = self.root / "Compact.SemanticModel"
        folder.mkdir()
        (folder / "model.bim").write_text(json.dumps({"model": {"tables": [{
            "name": "Orders", "columns": [
                {"name": "Amount", "sourceColumn": "amount", "dataType": "decimal"},
                {"name": "Secret sibling", "sourceColumn": "private", "dataType": "string"},
            ],
            "measures": [
                {"name": "Total", "expression": "SUM(Orders[Amount])"},
                {"name": "Next", "expression": "[Total] * 2"},
                {"name": "Unrelated", "expression": "COUNT(Orders[Secret sibling])"},
            ],
            "partitions": [{"name": "Data", "source": {
                "type": "entity", "entityName": "orders", "schemaName": "gold",
            }}],
        }]}}), encoding="utf-8")
        _, changes, scoped = self.targeted()
        model = next(item for item in scoped["artifacts"] if item["key"] == "Compact.SemanticModel")
        source = "\n".join(part["source"] for part in model["sourceSegments"])
        self.assertIn('"name": "Next"', source)
        self.assertNotIn("Secret sibling", source)
        self.assertNotIn("Unrelated", source)
        result = self.result()
        impact = result["impacts"][0]
        impact.update({"artifactKey": model["key"], "objectType": "measure", "objectName": "Orders.Total"})
        file = next(iter(model["files"]))
        impact["evidence"] = [{"file": file, "line": 1, "excerpt": '"name": "Unrelated"'}]
        impact["proposedFixes"] = []
        with self.assertRaisesRegex(ValueError, "object source"):
            validate_result(result, changes, [model])
        impact["evidence"][0]["excerpt"] = '"name": "Total"'
        self.assertEqual(validate_result(result, changes, [model])["impacts"][0]["objectName"], "Orders.Total")

    def test_code_targets_pipeline_activity_metadata_and_parent_invocations(self):
        for name, logical_id, activities in (
            ("Copy", "copy", [
                {"name": "Orders copy", "type": "Copy", "typeProperties": {
                    "source": {"table": "gold.orders"},
                    "translator": {"mappings": [{"source": {"name": "amount"}, "sink": {"name": "amount"}}]},
                }},
                {"name": "Unrelated activity", "type": "Copy",
                 "typeProperties": {"source": {"table": "gold.private"}}},
            ]),
            ("ParentCopy", "parentcopy", [
                {"name": "Run copy", "type": "ExecutePipeline",
                 "typeProperties": {"pipeline": {"referenceName": "copy"}}},
            ]),
        ):
            folder = self.root / f"{name}.DataPipeline"
            folder.mkdir()
            (folder / ".platform").write_text(json.dumps({
                "metadata": {"type": "DataPipeline"}, "config": {"logicalId": logical_id},
            }), encoding="utf-8")
            (folder / "pipeline-content.json").write_text(
                json.dumps({"properties": {"activities": activities}}), encoding="utf-8",
            )
        _, changes, scoped = self.targeted()
        pipelines = [item for item in scoped["artifacts"] if item["type"] == "DataPipeline"]
        self.assertEqual({item["key"] for item in pipelines}, {"Copy.DataPipeline", "ParentCopy.DataPipeline"})
        copy = next(item for item in pipelines if item["key"] == "Copy.DataPipeline")
        self.assertEqual([target["objectName"] for target in copy["targets"]], ["Orders copy"])
        self.assertEqual(copy["targets"][0]["jsonPointer"], "/properties/activities/0")
        prompt = copilot_impact._prompt(scoped, changes, copy)
        self.assertNotIn("Unrelated activity", prompt)
        self.assertNotIn("gold.private", prompt)
        self.assertTrue(any(edge["kind"] == "invokes" for edge in scoped["impactLineage"]["edges"]))
        result = self.result()
        result["impacts"][0].update({
            "artifactKey": copy["key"], "objectType": "pipeline_activity", "objectName": "Orders copy",
            "evidence": [{"file": next(iter(copy["files"])), "line": 1, "excerpt": '"name": "Orders copy"'}],
            "proposedFixes": [],
        })
        validate_result(result, changes, [copy])

    def test_pipeline_metadata_does_not_bridge_known_source_schema_conflicts(self):
        folder = self.root / "Conflicting.DataPipeline"
        folder.mkdir()
        (folder / "pipeline-content.json").write_text(json.dumps({"properties": {"activities": [{
            "name": "Wrong schema", "type": "Copy",
            "typeProperties": {"source": {"table": "orders", "schema": "silver"}},
        }]}}), encoding="utf-8")
        _, _, scoped = self.targeted()
        self.assertNotIn("Conflicting.DataPipeline", {item["key"] for item in scoped["artifacts"]})

    def test_pipeline_literal_defaults_and_referenced_declarations_are_targeted(self):
        folder = self.root / "Parameterized.DataPipeline"
        folder.mkdir()
        (folder / "pipeline-content.json").write_text(json.dumps({"properties": {
            "parameters": {
                "Table": {"type": "string", "defaultValue": "gold.orders"},
                "Unused": {"type": "string", "defaultValue": "private unrelated value"},
            },
            "variables": {"Column": {"type": "String", "defaultValue": "amount"}},
            "activities": [{
                "name": "Copy orders", "type": "Copy", "typeProperties": {
                    "source": {"table": "@pipeline().parameters.Table"},
                    "column": "@variables('Column')",
                },
            }],
        }}), encoding="utf-8")
        _, changes, scoped = self.targeted()
        pipeline = next(item for item in scoped["artifacts"] if item["key"] == "Parameterized.DataPipeline")
        self.assertEqual({(target["objectType"], target["objectName"]) for target in pipeline["targets"]}, {
            ("pipeline_activity", "Copy orders"), ("pipeline_parameter", "Table"), ("pipeline_variable", "Column"),
        })
        prompt = copilot_impact._prompt(scoped, changes, pipeline)
        self.assertNotIn("private unrelated value", prompt)
        self.assertNotIn("Unused", prompt)
        self.assertIn("gold.orders", prompt)
        self.assertTrue(any(edge["kind"] == "parameter_dependency" for edge in scoped["impactLineage"]["edges"]))

    def test_ai_cannot_invent_targets_even_with_real_source_evidence(self):
        _, changes, scoped = self.targeted()
        result = self.result()
        result["impacts"][0]["objectName"] = "invented function"
        with self.assertRaisesRegex(ValueError, "code-identified targets"):
            validate_result(result, changes, scoped["artifacts"])

    def test_identical_ai_context_is_reused_but_instruction_changes_invalidate_cache(self):
        catalog = self.catalog()
        catalog["instructions"] = self.payload(catalog)["instructions"]
        catalog["artifacts"] = [item for item in catalog["artifacts"] if item["type"] == "Notebook"]
        changes = validate_changes(self.payload(catalog), catalog)
        client, session = self.sdk_client()
        with patch.object(copilot_impact, "CopilotClient", return_value=client):
            first = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
            second = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
            self.assertEqual(session.send_and_wait.await_count, 1)
            self.assertEqual(first["aiUsage"]["requests"], 1)
            self.assertEqual(second["aiUsage"]["requests"], 0)
            self.assertEqual(second["aiUsage"]["cacheHits"], 1)
            self.assertEqual(second["targets"], first["targets"])
            self.assertEqual(second["impactLineage"], first["impactLineage"])
            catalog["instructions"] += " Also preserve numeric precision."
            third = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
            self.assertEqual(third["aiUsage"]["requests"], 1)
            self.assertEqual(session.send_and_wait.await_count, 2)
        self.assertFalse(list(self.reports.rglob("*.tmp")))

    def test_zero_targets_never_connect_to_copilot(self):
        catalog = self.catalog()
        catalog["artifacts"] = []
        changes = validate_changes(self.payload(catalog), catalog)
        with patch.object(copilot_impact, "CopilotClient") as client:
            report = copilot_impact.run_analysis(PRESET_ID, catalog, changes, MagicMock())
        client.assert_not_called()
        self.assertEqual(report["aiUsage"]["requests"], 0)
        self.assertEqual(report["targets"], [])

    def test_cache_identity_includes_source_and_concrete_model(self):
        catalog, changes, scoped = self.targeted()
        notebook = next(item for item in scoped["artifacts"] if item["type"] == "Notebook")
        prompt = copilot_impact._prompt(scoped, changes, notebook)
        initial = copilot_impact._cache_path(PRESET_ID, "model-one", prompt)
        self.assertNotEqual(initial, copilot_impact._cache_path(PRESET_ID, "model-two", prompt))
        (self.root / NOTEBOOK_FILE).write_text(CODE + 'orders.count()\n', encoding="utf-8")
        _, _, updated = self.targeted()
        notebook = next(item for item in updated["artifacts"] if item["type"] == "Notebook")
        self.assertNotEqual(initial, copilot_impact._cache_path(
            PRESET_ID, "model-one", copilot_impact._prompt(updated, changes, notebook),
        ))

    def test_unresolved_notebook_flow_does_not_send_unrelated_cells_to_ai(self):
        (self.root / NOTEBOOK_FILE).write_text("""# CELL ********************
orders = spark.table("gold.orders")
# CELL ********************
query = "SELECT amount FROM unknown_view"
spark.sql(query)
# CELL ********************
orders.select("amount").show()
# CELL ********************
unrelated = "private unrelated data"
""", encoding="utf-8")
        _, changes, scoped = self.targeted()
        notebook = next(item for item in scoped["artifacts"] if item["type"] == "Notebook")
        self.assertEqual([cell["cell"] for cell in notebook["cells"]], [1, 2, 3])
        self.assertNotIn("private unrelated", copilot_impact._prompt(scoped, changes, notebook))
        self.assertTrue(any("omitted dynamic dependencies" in gap["message"] for gap in scoped["issues"]))

    def test_upstream_traversal_excludes_independent_cells_in_a_producer(self):
        folder = self.root / "Producer.Notebook"
        folder.mkdir()
        (folder / "notebook-content.py").write_text('''# CELL ********************
orders = spark.table("bronze.orders")
# CELL ********************
orders.write.saveAsTable("gold.orders")
# CELL ********************
unrelated = spark.table("bronze.unrelated")
# CELL ********************
unrelated.write.saveAsTable("gold.unrelated")
''', encoding="utf-8")
        ignored = self.root / "IgnoredProducer.Notebook"
        ignored.mkdir()
        (ignored / "notebook-content.py").write_text(
            'spark.range(10).write.saveAsTable("bronze.unrelated")', encoding="utf-8",
        )
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = scope_catalog(catalog, changes)
        self.assertNotIn("IgnoredProducer.Notebook", {item["key"] for item in scoped["artifacts"]})
        producer = next(item for item in scoped["artifacts"] if item["key"] == "Producer.Notebook")
        self.assertEqual([cell["cell"] for cell in producer["cells"]], [1, 2])

    def test_scope_excludes_known_conflicts_and_resolves_qualified_upstream_aliases(self):
        binding = {
            "default_lakehouse_workspace_id": "workspace-id",
            "default_lakehouse": "lakehouse-id",
        }
        for name, code, scope in (
            ("Producer", 'spark.table("bronze.orders").write.saveAsTable("gold.orders")', binding),
            ("Upstream", 'spark.range(1).write.saveAsTable("workspace-id.lakehouse-id.bronze.orders")', {}),
            ("Conflict", 'spark.range(1).write.saveAsTable("gold.orders")',
             {**binding, "default_lakehouse": "another-lakehouse"}),
            ("WrongSchema", 'spark.range(1).write.saveAsTable("silver.orders")', binding),
        ):
            folder = self.root / f"{name}.Notebook"
            folder.mkdir()
            header = "# META " + json.dumps({"dependencies": {"lakehouse": scope}}) + "\n"
            (folder / "notebook-content.py").write_text(header + code, encoding="utf-8")
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = scope_catalog(catalog, changes)
        keys = {item["key"] for item in scoped["artifacts"]}
        self.assertTrue({"Producer.Notebook", "Upstream.Notebook"} <= keys)
        self.assertNotIn("Conflict.Notebook", keys)
        self.assertNotIn("WrongSchema.Notebook", keys)

    def test_callers_parent_pipelines_parameters_and_cycles_are_scoped(self):
        caller = self.root / "Caller.Notebook"
        caller.mkdir()
        (caller / ".platform").write_text(json.dumps({
            "metadata": {"type": "Notebook", "displayName": "Caller"},
            "config": {"logicalId": "caller"},
        }), encoding="utf-8")
        (caller / "notebook-content.py").write_text('''# CELL ********************
notebookutils.notebook.run("Consumer")
# CELL ********************
spark.range(5).show()
''', encoding="utf-8")
        (self.root / NOTEBOOK_FILE).write_text(
            CODE + '\nnotebookutils.notebook.run("Caller")\n', encoding="utf-8",
        )
        for name, logical_id, activity in (
            ("Child", "child", {
                "type": "TridentNotebook", "typeProperties": {
                    "notebookId": "caller", "parameters": {"env": {"value": "prod"}},
                },
            }),
            ("Parent", "parent", {
                "type": "InvokePipeline", "typeProperties": {"pipelineId": "child"},
            }),
            ("Ignored", "ignored", {
                "type": "TridentNotebook", "typeProperties": {"notebookId": "absent"},
            }),
        ):
            folder = self.root / f"{name}.DataPipeline"
            folder.mkdir()
            (folder / ".platform").write_text(json.dumps({
                "metadata": {"type": "DataPipeline"}, "config": {"logicalId": logical_id},
            }), encoding="utf-8")
            (folder / "pipeline-content.json").write_text(
                json.dumps({"properties": {"activities": [activity]}}), encoding="utf-8",
            )
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = scope_catalog(catalog, changes)
        artifact = next(item for item in scoped["artifacts"] if item["key"] == "Caller.Notebook")
        self.assertEqual([cell["cell"] for cell in artifact["cells"]], [1])
        self.assertEqual({link["pipelineKey"] for link in artifact["lineage"]["pipelineLinks"]},
                         {"Child.DataPipeline", "Parent.DataPipeline"})
        invocation = next(link for link in artifact["lineage"]["pipelineLinks"] if link["kind"] == "TridentNotebook")
        self.assertEqual(invocation["parameters"], {"env": "prod"})

    def test_unresolved_cell_dependencies_retain_cells_with_an_explicit_gap(self):
        code = '''# CELL ********************
orders = spark.table("gold.orders")
# CELL ********************
query = "SELECT amount FROM unknown_view"
spark.sql(query)
# CELL ********************
orders.select("amount").show()
'''
        (self.root / NOTEBOOK_FILE).write_text(code, encoding="utf-8")
        catalog = self.catalog()
        changes = validate_changes(self.payload(catalog), catalog)
        scoped = scope_catalog(catalog, changes)
        artifact = next(item for item in scoped["artifacts"] if item["type"] == "Notebook")
        self.assertEqual(len(artifact["cells"]), 3)
        self.assertTrue(any("all code cells" in gap["message"] for gap in scoped["issues"]))

    def app_client(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "test"})
        client = app.test_client()
        with client.session_transaction() as session:
            session["user"] = {"name": "Tester"}
        return client

    def test_page_catalog_stream_and_download(self):
        client = self.app_client()
        catalog = live_schema(self.preset, self.fabric_client)
        with patch("fabric.frontend.routes.load_preset", return_value=self.preset), \
                patch("fabric.backend.routes.load_preset", return_value=self.preset):
            page = client.get(f"/fabric/presets/{PRESET_ID}/impact")
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="impactInstructions"', page.get_data(as_text=True))
            self.assertNotIn('id="impactConsent"', page.get_data(as_text=True))
            self.assertNotIn("I approve sending", page.get_data(as_text=True))
            self.assertIn("/fabric/static/js/impact.js", page.get_data(as_text=True))
            response = client.get(f"/api/fabric/presets/{PRESET_ID}/impact/catalog")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("files", response.json)
            self.assertEqual(response.json["tables"], catalog["tables"])

            def analyze(preset_id, snapshot, changes, progress):
                progress(1, 3, "Consumer")
                result = validate_result(self.result(), changes, snapshot["artifacts"])
                result["analyzedArtifacts"] = [item["key"] for item in snapshot["artifacts"]]
                return save_report(preset_id, snapshot, changes, [result], [])

            with patch("fabric.backend.routes.run_analysis", side_effect=analyze):
                response = client.post(f"/api/fabric/presets/{PRESET_ID}/impact/analyze",
                                       json=self.payload(catalog), buffered=True)
            self.assertEqual(response.status_code, 200)
            events = [json.loads(block[6:]) for block in response.get_data(as_text=True).split("\n\n")
                      if block.startswith("data: ")]
            self.assertEqual(events[-1]["state"], "complete")
            report = events[-1]["report"]
            download = client.get(f"/api/fabric/presets/{PRESET_ID}/impact/reports/{report['reportId']}")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.json, report)
            self.assertEqual(report["instructions"], self.payload(catalog)["instructions"])
            download.close()
            self.assertEqual(client.post(f"/api/fabric/presets/{PRESET_ID}/impact/analyze",
                                         json=self.payload(catalog, instructions="")).status_code, 400)

    def test_stream_reports_copilot_failure_explicitly(self):
        client = self.app_client()
        with self.assertLogs(level="ERROR") as logs:
            with patch("fabric.backend.routes.load_preset", return_value=self.preset), \
                    patch("fabric.backend.routes.run_analysis", side_effect=RuntimeError("Copilot unavailable")):
                response = client.post(f"/api/fabric/presets/{PRESET_ID}/impact/analyze",
                                       json=self.payload(live_schema(self.preset, self.fabric_client)), buffered=True)
        self.assertTrue(any("Fabric impact analysis failed" in line for line in logs.output))
        text = response.get_data(as_text=True)
        self.assertIn('"state": "error"', text)
        self.assertIn("Copilot unavailable", text)
        self.assertNotIn('"state": "complete"', text)

    def test_fabric_schema_error_is_visible_without_local_fallback(self):
        client = self.app_client()
        with patch("fabric.backend.routes.load_preset", return_value=self.preset):
            self.fabric_client.get_semantic_model_definition.side_effect = RuntimeError("Fabric schema unavailable")
            with self.assertLogs(level="ERROR"):
                response = client.get(f"/api/fabric/presets/{PRESET_ID}/impact/catalog")
            self.assertEqual(response.status_code, 502)
            self.assertIn("Fabric schema unavailable", response.json["message"])

    def test_name_mismatch_offers_workspace_models_instead_of_a_dead_end(self):
        client = self.app_client()
        options = [{"id": LIVE_MODEL_ID, "name": "Sales Production"}]
        self.fabric_client.get_semantic_model_definition.side_effect = SemanticModelResolutionError(
            "Select the matching live model.", options,
        )
        with patch("fabric.backend.routes.load_preset", return_value=self.preset):
            response = client.get(f"/api/fabric/presets/{PRESET_ID}/impact/catalog")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json["requiresModelSelection"])
        self.assertEqual(response.json["semanticModels"], options)
        self.assertNotIn("tables", response.json)

    def test_selected_live_model_loads_schema_then_saves_its_binding(self):
        client = self.app_client()
        with patch("fabric.backend.routes.load_preset", return_value=self.preset), \
                patch("fabric.backend.routes.bind_live_semantic_model") as bind:
            response = client.post(f"/api/fabric/presets/{PRESET_ID}/impact/catalog",
                                   json={"modelId": LIVE_MODEL_ID})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["tables"]), 1)
        bind.assert_called_once_with(PRESET_ID, {"id": LIVE_MODEL_ID, "name": "Sales"})
        self.fabric_client.get_semantic_model_definition.assert_called_with(
            "workspace-id", "Sales", model_id=LIVE_MODEL_ID,
        )

    def test_failed_explicit_binding_does_not_overwrite_saved_preset(self):
        client = self.app_client()
        self.fabric_client.get_semantic_model_definition.side_effect = RuntimeError("Definition unavailable")
        with patch("fabric.backend.routes.load_preset", return_value=self.preset), \
                patch("fabric.backend.routes.bind_live_semantic_model") as bind, self.assertLogs(level="ERROR"):
            response = client.post(f"/api/fabric/presets/{PRESET_ID}/impact/catalog",
                                   json={"modelId": LIVE_MODEL_ID})
        self.assertEqual(response.status_code, 502)
        bind.assert_not_called()

    def test_catalog_loads_from_fabric_even_without_a_local_repository(self):
        client = self.app_client()
        preset = {key: value for key, value in self.preset.items() if key != "localRepositoryPath"}
        with patch("fabric.backend.routes.load_preset", return_value=preset):
            response = client.get(f"/api/fabric/presets/{PRESET_ID}/impact/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Live only", [column["name"] for column in response.json["tables"][0]["columns"]])

    def test_changed_live_schema_is_rejected_before_analysis(self):
        client = self.app_client()
        catalog = live_schema(self.preset, self.fabric_client)
        payload = self.payload(catalog)
        original = self.live_definition()
        definition = json.loads(base64.b64decode(original["definition"]["parts"][0]["payload"]))
        definition["model"]["tables"][0]["columns"].append({"name": "Added", "dataType": "string"})
        original["definition"]["parts"][0]["payload"] = base64.b64encode(json.dumps(definition).encode()).decode()
        self.fabric_client.get_semantic_model_definition.side_effect = None
        self.fabric_client.get_semantic_model_definition.return_value = original
        with patch("fabric.backend.routes.load_preset", return_value=self.preset), \
                patch("fabric.backend.routes.run_analysis") as analyze:
            response = client.post(f"/api/fabric/presets/{PRESET_ID}/impact/analyze", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("schema changed", response.json["message"])
        analyze.assert_not_called()

    def test_endpoints_require_sign_in(self):
        client = create_app({"TESTING": True}).test_client()
        self.assertEqual(client.get(f"/api/fabric/presets/{PRESET_ID}/impact/catalog").status_code, 401)
        self.assertEqual(client.post(f"/api/fabric/presets/{PRESET_ID}/impact/analyze", json={}).status_code, 401)
        self.assertEqual(client.get(f"/api/fabric/presets/{PRESET_ID}/impact/reports/{'b' * 32}").status_code, 401)


if __name__ == "__main__":
    unittest.main()
