import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from legacy.backend import ai_batch_creation, ai_powered_refresh, ai_table_precedence
from legacy.backend import batch_processing, presets
from shared.app import create_app
from shared.paths import PROJECT_ROOT


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user"] = {"name": "Test User", "email": "test@example.com"}
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.preset_dir = Path(self.directory.name)
        patcher = patch.object(presets, "PRESETS_DIR", self.preset_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.payload = {
            "presetName": "Demo", "workspaceId": "workspace", "workspaceName": "Demo Workspace",
            "modelId": "model", "modelName": "Demo Model",
            "selectedTables": ["Sales", "Products"], "allTables": ["Sales", "Products"],
            "batches": [{"name": "Batch 1", "tables": ["Sales", "Products"]}],
            "batchCreationSettings": {
                "timeoutMinutes": 30, "maxParallelism": 1,
                "retryCount": 3, "commitMode": "transactional",
            },
            "user": {"name": "Test User"},
        }

    def test_existing_data_paths_are_preserved(self):
        self.assertEqual(batch_processing.PRESETS_DIR, PROJECT_ROOT / "presets")
        self.assertEqual(ai_powered_refresh.DEFAULT_OUTPUT_DIRECTORY,
                         PROJECT_ROOT / "output" / "ai_refresh_metadata")
        self.assertEqual(ai_table_precedence.DEFAULT_INPUT_DIRECTORY,
                         ai_powered_refresh.DEFAULT_OUTPUT_DIRECTORY)
        self.assertEqual(ai_batch_creation.DEFAULT_INPUT_DIRECTORY,
                         ai_table_precedence.DEFAULT_OUTPUT_DIRECTORY)
        self.assertEqual(ai_batch_creation.DEFAULT_OUTPUT_DIRECTORY,
                         PROJECT_ROOT / "output" / "ai_batch_creation")

    def test_presets_round_trip_and_render_with_legacy_urls(self):
        response = self.client.post("/api/legacy/presets", json=self.payload)
        self.assertEqual(response.status_code, 200)
        preset_id = response.json["presetId"]
        response = self.client.get("/api/legacy/presets")
        self.assertEqual(response.json[0]["id"], preset_id)
        html = self.client.get(f"/legacy/selected-tables/{preset_id}").get_data(as_text=True)
        self.assertIn("Demo Model", html)
        self.assertIn('href="/legacy"', html)
        self.assertIn("/api/legacy/refresh-jobs", html)
        self.assertIn("/api/legacy/ai-batch-creation", html)
        self.assertIn('id="headerSignOutButton"', html)
        self.assertNotIn("`/api/workspaces/", html)
        self.assertNotIn('"/api/presets"', html)
        updated = {**self.payload, "presetName": "Updated"}
        self.assertEqual(self.client.put(f"/api/legacy/presets/{preset_id}", json=updated).status_code, 200)
        self.assertEqual(presets.load_preset(preset_id)["presetName"], "Updated")
        self.assertEqual(self.client.delete(f"/api/legacy/presets/{preset_id}").status_code, 200)
        self.assertEqual(self.client.get("/api/legacy/presets").json, [])

    def test_selection_post_supports_json_and_existing_form_payload(self):
        for kwargs in ({"json": self.payload}, {"data": {"payload": json.dumps(self.payload)}}):
            with self.subTest(kwargs=kwargs):
                response = self.client.post("/legacy/selected-tables", **kwargs)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Demo Model", response.get_data(as_text=True))
        self.assertEqual(self.client.post("/legacy/saved-batches", json=self.payload).status_code, 200)

    def test_invalid_json_reports_bad_request(self):
        response = self.client.post("/legacy/selected-tables", data={"payload": "{"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid JSON payload", response.get_data(as_text=True))

    def test_missing_preset_redirects_to_legacy(self):
        response = self.client.get("/legacy/selected-tables/missing")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/legacy")
        response = self.client.get("/legacy/saved-batches/demo")
        self.assertEqual(response.location, "/legacy/selected-tables/demo")

    def test_home_keeps_existing_controls_and_legacy_endpoints(self):
        html = self.client.get("/legacy").get_data(as_text=True)
        self.assertIn("Saved Presets", html)
        self.assertIn("+ Add New Preset", html)
        self.assertIn("/api/legacy/workspaces", html)
        self.assertIn("/legacy/selected-tables", html)
        self.assertNotIn("/api/login", html)

    def test_workspaces_are_loaded_by_legacy(self):
        workspaces = [{"id": "workspace", "name": "Demo Workspace"}]
        with patch("legacy.backend.routes.get_workspaces", return_value=workspaces):
            response = self.client.get("/api/legacy/workspaces")
        self.assertEqual(response.json, {"workspaces": workspaces})

    def test_model_service_routes_preserve_payloads(self):
        with patch("legacy.backend.routes.get_workspace_assets", return_value={"models": []}) as assets:
            self.assertEqual(self.client.get("/api/legacy/workspaces/workspace/assets").json, {"models": []})
            assets.assert_called_once_with("workspace")
        with patch("legacy.backend.routes.get_asset_details", return_value={"id": "model"}) as details:
            self.assertEqual(self.client.get("/api/legacy/workspaces/workspace/assets/Model/model/details").json, {"id": "model"})
            details.assert_called_once_with("workspace", "Model", "model")
        with patch("legacy.backend.routes.get_advanced_table_details", return_value={"tables": []}) as advanced:
            response = self.client.post("/api/legacy/workspaces/workspace/models/model/advanced-table-data",
                                        json=self.payload)
            self.assertEqual(response.json, {"tables": []})
            advanced.assert_called_once_with("workspace", "model", ["Sales", "Products"], ["Sales", "Products"])
        with patch("legacy.backend.routes.get_ai_refresh_metadata", return_value={"outputFile": "metadata.json"}) as metadata:
            response = self.client.post("/api/legacy/workspaces/workspace/models/model/ai-refresh-metadata",
                                        json=self.payload)
            self.assertEqual(response.json, {"outputFile": "metadata.json"})
            metadata.assert_called_once_with("Demo Workspace", "workspace", "Demo Model", "model",
                                             ["Sales", "Products"], ["Sales", "Products"])

    def test_ai_plan_endpoints_keep_saved_file_handoffs(self):
        with patch("legacy.backend.routes.generate_table_precedence",
                   return_value=({"tables": ["Sales"]}, Path("precedence.json"))):
            response = self.client.post("/api/legacy/ai-table-precedence")
            self.assertEqual(response.json["outputFile"], "precedence.json")
        with patch("legacy.backend.routes.generate_ai_batches",
                   return_value=({"batches": []}, Path("batches.json"))) as batches:
            response = self.client.post("/api/legacy/ai-batch-creation",
                                        json={"precedenceFile": "precedence.json"})
            self.assertEqual(response.json["outputFile"], "batches.json")
            batches.assert_called_once_with(ai_batch_creation.DEFAULT_INPUT_DIRECTORY / "precedence.json")
        self.assertEqual(self.client.post("/api/legacy/ai-batch-creation",
                                          json={"precedenceFile": "..\\bad.json"}).status_code, 400)

    def test_refresh_job_routes_still_use_existing_manager(self):
        job = {"id": "job", "status": "Running"}
        with patch("legacy.backend.routes.batch_refresh_manager") as manager:
            manager.start.return_value = job
            manager.get.return_value = job
            manager.cancel.return_value = {**job, "status": "Cancelled"}
            response = self.client.post("/api/legacy/refresh-jobs", json={"presetId": "Demo"})
            self.assertEqual(response.status_code, 202)
            manager.start.assert_called_with(self.preset_dir / "Demo.json")
            response = self.client.post("/api/legacy/refresh-jobs", json={"aiBatchFile": "batch.json"})
            self.assertEqual(response.status_code, 202)
            manager.start.assert_called_with(ai_batch_creation.DEFAULT_OUTPUT_DIRECTORY / "batch.json")
            self.assertEqual(self.client.get("/api/legacy/refresh-jobs/job").json, job)
            self.assertEqual(self.client.delete("/api/legacy/refresh-jobs/job").json["status"], "Cancelled")
            manager.get.return_value = None
            self.assertEqual(self.client.get("/api/legacy/refresh-jobs/missing").status_code, 404)

    def test_saved_refresh_plan_preserves_parallelism_and_commit_modes(self):
        preset_id = presets.save_preset(self.payload)
        plan = batch_processing.load_refresh_plan(self.preset_dir / f"{preset_id}.json")
        self.assertEqual(plan["workspace_id"], "workspace")
        self.assertEqual(plan["model_id"], "model")
        batches = batch_processing.build_job_batches(plan)
        self.assertEqual([len(batch["tables"]) for batch in batches[0]["subBatches"]], [1, 1])
        self.assertEqual(batch_processing.normalize_api_commit_mode("partial-batch"), "partialBatch")
        self.assertEqual(batch_processing.normalize_api_commit_mode("transactional"), "transactional")


if __name__ == "__main__":
    unittest.main()
