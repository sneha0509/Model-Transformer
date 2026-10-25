import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fabric.backend import copilot_impact
from fabric.backend import impact as impact_reports
from fabric.backend.impact import replacement_sources, save_report
from fabric.tests.test_impact_objects import model_fixture, targeted_fixture


def available_models():
    return {"test-model": SimpleNamespace(
        id="test-model", policy=None, billing=None,
        capabilities=SimpleNamespace(limits=SimpleNamespace(
            max_context_window_tokens=200_000, max_prompt_tokens=None,
        )),
    )}


class ModelBatchTests(unittest.TestCase):
    def test_large_model_is_batched_without_losing_targets_or_source_ids(self):
        artifact = model_fixture()
        document = json.loads(artifact["files"]["model.bim"])
        orders = document["model"]["tables"][0]
        orders["measures"] = [
            {"name": f"Revenue {index}", "expression": "SUM(Orders[Amount])", "description": "x" * 5000}
            for index in range(30)
        ]
        artifact["files"]["model.bim"] = json.dumps(document)
        _, changes, scoped = targeted_fixture(artifact, [("Orders", "Amount")])
        original = scoped["artifacts"][0]
        self.assertGreater(
            sum(len(part["source"]) for part in original["sourceSegments"]),
            copilot_impact.MAX_ARTIFACT_CHARACTERS,
        )
        requests = copilot_impact._analysis_requests(scoped, changes, available_models(), None)
        self.assertGreater(len(requests), 1)
        original_sources = {
            (source["objectType"], source["objectName"]): source["sourceId"]
            for source in replacement_sources(original)
        }
        target_ids = []
        for request in requests:
            target_ids.extend(target["id"] for target in request["targets"])
            self.assertLessEqual(
                sum(len(part["source"]) for part in request["sourceSegments"]),
                copilot_impact.MAX_ARTIFACT_CHARACTERS,
            )
            for source in replacement_sources(request):
                self.assertEqual(
                    source["sourceId"], original_sources[(source["objectType"], source["objectName"])],
                )
                self.assertEqual(source["editable"], any(
                    target["objectType"] == source["objectType"]
                    and target["objectName"] == source["objectName"]
                    for target in request["targets"]
                ))
        self.assertCountEqual(target_ids, [target["id"] for target in original["targets"]])
        self.assertEqual(len(target_ids), len(set(target_ids)))

    def test_batch_context_preserves_relationships_and_measure_prerequisites(self):
        _, changes, scoped = targeted_fixture(model_fixture(), [("Customers", "Key")])
        with patch.object(copilot_impact, "MODEL_TARGETS_PER_REQUEST", 1):
            requests = copilot_impact._analysis_requests(scoped, changes, available_models(), None)
        request = next(request for request in requests if request["targets"][0]["objectName"] == "Orders.Revenue Twice")
        sources = {source["objectName"]: source for source in replacement_sources(request)}
        self.assertIn("customer_orders", sources)
        self.assertIn("Orders.Revenue", sources)
        self.assertTrue(sources["Orders.Revenue Twice"]["editable"])
        self.assertFalse(sources["Orders.Revenue"]["editable"])
        self.assertFalse(sources["customer_orders"]["editable"])

    def test_single_oversized_entity_is_not_truncated(self):
        artifact = model_fixture()
        document = json.loads(artifact["files"]["model.bim"])
        document["model"]["tables"][0]["measures"] = [{
            "name": "Large", "expression": "SUM(Orders[Amount])", "description": "x" * 110_000,
        }]
        artifact["files"]["model.bim"] = json.dumps(document)
        _, changes, scoped = targeted_fixture(artifact, [("Orders", "Amount")])
        requests = copilot_impact._analysis_requests(scoped, changes, available_models(), None)
        request = next(request for request in requests if any(
            target["objectName"] == "Orders.Large" for target in request["targets"]
        ))
        self.assertEqual(len(request["targets"]), 1)
        self.assertTrue(any("x" * 110_000 in part["source"] for part in request["sourceSegments"]))

    def test_context_window_causes_split_even_below_source_limit(self):
        _, changes, scoped = targeted_fixture(model_fixture(), [("Orders", "Amount")])
        with patch.object(copilot_impact, "_context_budget", side_effect=lambda model, prompt: {
            "inputTokenUpperBound": len(json.loads(prompt)["artifact"]["targetedObjects"]),
            "inputTokenLimit": 2,
        }):
            requests = copilot_impact._analysis_requests(scoped, changes, available_models(), None)
        self.assertGreater(len(requests), 1)
        self.assertTrue(all(len(request["targets"]) <= 2 for request in requests))

    def test_non_model_requests_are_unchanged(self):
        artifact = {"key": "notebook", "type": "Notebook"}
        scoped = {"artifacts": [artifact]}
        self.assertEqual(copilot_impact._analysis_requests(scoped, [], {}, None), [{
            **artifact, "batchIndex": 1, "batchCount": 1,
        }])


class AnalysisBatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def analyze_fixture(self, artifact):
        _, changes, scoped = targeted_fixture(artifact, [("Orders", "Amount")])
        catalog = {**scoped, "revision": "test", "instructions": "Change Amount to double"}
        session = SimpleNamespace(
            session_id="test-session",
            rpc=SimpleNamespace(model=SimpleNamespace(get_current=AsyncMock(
                return_value=SimpleNamespace(model_id="test-model"),
            ))),
            send_and_wait=AsyncMock(return_value=SimpleNamespace(data=SimpleNamespace(content="{}"))),
            disconnect=AsyncMock(),
        )
        client = SimpleNamespace(
            get_auth_status=AsyncMock(return_value=SimpleNamespace(isAuthenticated=True)),
            list_models=AsyncMock(return_value=list(available_models().values())),
            create_session=AsyncMock(return_value=session), delete_session=AsyncMock(),
        )
        manager = AsyncMock()
        manager.__aenter__.return_value = client

        def validated(result, selected_changes, artifacts):
            return {
                "summary": "All supplied targets were assessed.", "impacts": [], "gaps": [],
                "unaffectedTargets": copy.deepcopy(artifacts[0]["targets"]),
            }

        with (
            patch.object(copilot_impact, "scope_catalog", return_value=scoped),
            patch.object(copilot_impact, "target_catalog", return_value=scoped),
            patch.object(copilot_impact, "signed_in_user_environment", return_value={}),
            patch.object(copilot_impact, "CopilotClient", return_value=manager),
            patch.object(copilot_impact, "MODEL_TARGETS_PER_REQUEST", 1),
            patch.object(copilot_impact, "_read_cache", return_value=None),
            patch.object(copilot_impact, "_write_cache"),
            patch.object(copilot_impact, "validate_result", side_effect=validated),
            patch.object(copilot_impact, "save_report", side_effect=lambda *args: args) as saved,
        ):
            await copilot_impact.analyze("a" * 64, catalog, changes, lambda *args: None)
        report_catalog, results, skipped = saved.call_args.args[1], saved.call_args.args[3], saved.call_args.args[4]
        return scoped, changes, report_catalog, results, skipped

    async def test_model_batches_are_analyzed_and_merged_with_per_batch_inventory(self):
        scoped, _, report_catalog, results, skipped = await self.analyze_fixture(model_fixture())
        self.assertFalse(skipped)
        self.assertGreater(len(results), 1)
        self.assertEqual(report_catalog["aiUsage"]["requests"], len(results))
        self.assertEqual(report_catalog["aiUsage"]["targetedArtifacts"], 1)
        inventory = report_catalog["inventoryScope"][scoped["artifacts"][0]["key"]]
        self.assertEqual(len(inventory["batches"]), len(results))
        self.assertTrue(all(batch["status"] == "analyzed" for batch in inventory["batches"]))
        self.assertCountEqual(
            [target["id"] for result in results for target in result["unaffectedTargets"]],
            [target["id"] for target in scoped["artifacts"][0]["targets"]],
        )

    async def test_oversized_target_preserves_other_batches_and_partial_report(self):
        artifact = model_fixture()
        document = json.loads(artifact["files"]["model.bim"])
        document["model"]["tables"][0]["measures"].append({
            "name": "Large", "expression": "SUM(Orders[Amount])", "description": "x" * 110_000,
        })
        artifact["files"]["model.bim"] = json.dumps(document)
        scoped, changes, catalog, results, skipped = await self.analyze_fixture(artifact)
        self.assertEqual(len(skipped), 1)
        self.assertIn("Orders.Large", skipped[0]["message"])
        self.assertGreater(len(results), 0)
        self.assertEqual(catalog["inventoryScope"][artifact["key"]]["status"], "partial")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(impact_reports, "REPORTS_DIR", Path(directory)):
                report = save_report("a" * 64, catalog, changes, results, skipped)
            saved = Path(directory) / ("a" * 64) / f"{report['reportId']}.json"
            persisted = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(persisted["inventory"][0]["status"], "partial")
        self.assertEqual(persisted["coverage"], "partial")
        self.assertCountEqual(
            [target["id"] for target in persisted["targets"]],
            [target["id"] for target in scoped["artifacts"][0]["targets"]],
        )


if __name__ == "__main__":
    unittest.main()
