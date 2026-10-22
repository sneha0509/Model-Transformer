import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from shared.app import create_app
from fabric.backend.clients import AzureDevOpsClient, RepositoryCloneError
from fabric.backend.filesystem import filesystem_path
from fabric.backend.presets import list_presets, load_preset, save_preset
from fabric.backend.repositories import _remove_readonly, ensure_repository


WORKSPACE_ID = "1455b6a2-c120-4c1c-dda7-92bafe99bec3"
USER = {"name": "Test User", "email": "test@example.com", "tenantId": "tenant"}
REPOSITORY = {
    "id": "repo-id",
    "name": "Models",
    "organization": "Contoso",
    "projectName": "Analytics",
    "branchName": "main",
}
SELECTION = {
    "workspace": {"id": WORKSPACE_ID, "displayName": "Finance"},
    "repository": REPOSITORY,
    "semanticModel": {"name": "Sales", "path": "/Sales.SemanticModel"},
}


class FabricTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.presets_dir = Path(temporary.name) / "fabric"
        directory_patch = patch(
            "fabric.backend.presets.PRESETS_DIR", self.presets_dir
        )
        directory_patch.start()
        self.addCleanup(directory_patch.stop)
        repository_patch = patch(
            "fabric.backend.repositories.REPOS_DIR", Path(temporary.name) / "repo"
        )
        repository_patch.start()
        self.addCleanup(repository_patch.stop)
        for target in ("fabric.backend.lineage.REPORTS_DIR", "fabric.backend.routes.REPORTS_DIR"):
            report_patch = patch(target, Path(temporary.name) / "reports")
            report_patch.start()
            self.addCleanup(report_patch.stop)
        client_patch = patch("fabric.backend.repositories.azure_devops_client")
        self.repository_client = client_patch.start().return_value
        self.addCleanup(client_patch.stop)
        self.repository_client.download_repository.side_effect = (
            lambda _repository, destination: (destination / "README.md").write_text(
                "First snapshot", encoding="utf-8"
            )
        )
        with self.client.session_transaction() as session:
            session["user"] = USER

    def test_fabric_page_contains_workspace_and_repository_flow(self):
        response = self.client.get("/fabric")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="workspaceSearch"', html)
        self.assertIn('id="workspaceList"', html)
        self.assertIn('id="repositorySection"', html)
        self.assertIn('id="selectionSection"', html)
        self.assertIn('id="semanticModelList"', html)
        self.assertIn('id="createPresetButton"', html)
        self.assertIn('id="presetGrid"', html)
        self.assertIn("/fabric/static/js/home.js", html)
        self.assertNotIn("/api/legacy", html)

    @patch("fabric.backend.routes.get_fabric_access_token", return_value="fabric-token")
    @patch("fabric.backend.routes.FabricWorkspaceClient")
    def test_lists_accessible_fabric_workspaces(self, client_type, _get_token):
        client_type.return_value.list_workspaces.return_value = [
            {"id": WORKSPACE_ID, "displayName": "Finance", "type": "Workspace"}
        ]

        response = self.client.get("/api/fabric/workspaces")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["workspaces"][0]["displayName"], "Finance")
        client_type.assert_called_once_with("fabric-token")

    @patch("fabric.backend.routes.get_fabric_access_token", return_value="fabric-token")
    @patch("fabric.backend.routes.FabricWorkspaceClient")
    def test_returns_linked_azure_devops_repository(self, client_type, _get_token):
        client_type.return_value.get_git_connection.return_value = {
            "gitConnectionState": "ConnectedAndInitialized",
            "gitProviderDetails": {
                "gitProviderType": "AzureDevOps",
                "organizationName": "Contoso",
                "projectName": "Analytics",
                "repositoryName": "Fabric Models",
                "branchName": "main",
                "directoryName": "/models",
            },
        }

        response = self.client.get(
            f"/api/fabric/workspaces/{WORKSPACE_ID}/repository"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["connected"])
        self.assertEqual(response.json["repository"]["name"], "Fabric Models")
        self.assertEqual(
            response.json["repository"]["url"],
            "https://dev.azure.com/Contoso/Analytics/_git/Fabric%20Models",
        )

    @patch("fabric.backend.clients.AzureDevOpsClient")
    @patch("fabric.backend.clients.get_browser_credential")
    @patch("fabric.backend.routes.get_fabric_access_token", return_value="fabric-token")
    @patch("fabric.backend.routes.FabricWorkspaceClient")
    def test_returns_repository_choices_when_workspace_is_not_linked(
        self,
        fabric_client_type,
        _get_fabric_token,
        get_credential,
        azure_devops_client_type,
    ):
        fabric_client_type.return_value.get_git_connection.return_value = {
            "gitConnectionState": "NotConnected",
            "gitProviderDetails": None,
        }
        token = MagicMock(token="azure-devops-token")
        get_credential.return_value.get_token.return_value = token
        azure_devops_client_type.return_value.list_repositories.return_value = [{
            "id": "repo-id",
            "name": "Models",
            "organization": "Contoso",
            "projectId": "project-id",
            "projectName": "Analytics",
            "url": "https://dev.azure.com/Contoso/Analytics/_git/Models",
        }]

        response = self.client.get(
            f"/api/fabric/workspaces/{WORKSPACE_ID}/repository"
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["connected"])
        self.assertEqual(response.json["repositories"][0]["name"], "Models")
        azure_devops_client_type.assert_called_once_with("azure-devops-token")

    @patch("fabric.backend.routes._azure_devops_client")
    def test_semantic_model_api_uses_selected_repository(self, get_client):
        get_client.return_value.list_semantic_models.return_value = {
            "repository": REPOSITORY,
            "semanticModels": [SELECTION["semanticModel"]],
        }
        response = self.client.post(
            "/api/fabric/semantic-models", json={"repository": REPOSITORY}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["semanticModels"], [SELECTION["semanticModel"]])
        self.assertEqual(
            get_client.return_value.list_semantic_models.call_args.args[0]["branchName"],
            "main",
        )

    def test_preset_is_persisted_listed_and_opens_a_mapping_page(self):
        response = self.client.post("/api/fabric/presets", json=SELECTION)
        self.assertEqual(response.status_code, 201)
        preset_id = response.json["presetId"]
        self.assertEqual(load_preset(preset_id)["semanticModel"], SELECTION["semanticModel"])
        presets = self.client.get("/api/fabric/presets")
        self.assertEqual(presets.json["presets"][0]["presetId"], preset_id)
        page = self.client.get(f"/fabric/presets/{preset_id}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Sales", html)
        self.assertIn("Finance", html)
        self.assertIn("Models", html)
        self.assertIn("Local repository", html)
        self.assertTrue(Path(response.json["localRepositoryPath"]).is_dir())
        self.repository_client.download_repository.assert_called_once()
        self.assertIn('data-auto-process="true"', html)
        self.assertIn("Preparing repository and lineage data", html)
        self.assertNotIn('id="semanticModelPresetLayout"', html)

        processing = self.client.post(
            f"/api/fabric/presets/{preset_id}/process", json={"refresh": False}
        )
        progress = processing.get_data(as_text=True)
        self.assertEqual(processing.mimetype, "text/event-stream")
        self.assertIn('"stage": "repository", "state": "active"', progress)
        self.assertIn('"stage": "lineage", "state": "complete"', progress)
        self.assertIn('"stage": "complete", "state": "complete"', progress)

        page = self.client.get(f"/fabric/presets/{preset_id}")
        html = page.get_data(as_text=True)
        self.assertIn('id="semanticModelPresetLayout"', html)
        self.assertIn("Fabric artifact mapping", html)
        self.assertIn("No Fabric pipeline folders found", html)
        self.assertIn("Refresh repository &amp; lineage", html)
        self.assertIn('class="fabric-action-button impact-launch-button"', html)
        self.assertIn("Impact Analysis", html)
        self.assertIn("/fabric/static/js/lineage.js", html)
        self.assertIn("/fabric/static/js/lineage-graph.js", html)
        self.assertIn('id="lineageGraph"', html)
        self.assertIn('id="lineageGraphTable"', html)
        self.assertIn('<input id="lineageGraphReads" type="checkbox">', html)
        self.assertIn("Below 55% zoom", html)
        self.assertIn('id="lineageFitFlow" disabled', html)
        self.assertIn('id="lineageClearFlow" disabled', html)
        self.assertIn("Connected items are grouped by flow, not by type", html)
        self.assertIn('id="lineageGraphData"', html)
        self.assertNotIn("/api/legacy", html)

    def test_old_mapping_is_rebuilt_from_local_snapshot_without_refreshing_repository(self):
        saved = save_preset(SELECTION)
        preset_id = saved["presetId"]
        with patch("fabric.frontend.routes.load_mapping", return_value={
            "schemaVersion": 2, "repositoryPath": saved["localRepositoryPath"],
            "graph": {"version": 1, "modelPath": SELECTION["semanticModel"]["path"]},
        }):
            page = self.client.get(f"/fabric/presets/{preset_id}")
        self.assertIn('data-auto-process="true"', page.get_data(as_text=True))
        self.assertNotIn('id="lineageGraph"', page.get_data(as_text=True))
        self.repository_client.download_repository.reset_mock()
        processing = self.client.post(
            f"/api/fabric/presets/{preset_id}/process", json={"refresh": False}
        )
        self.assertIn('"stage": "complete"', processing.get_data(as_text=True))
        self.repository_client.download_repository.assert_not_called()
        page = self.client.get(f"/fabric/presets/{preset_id}")
        self.assertIn('id="lineageGraph"', page.get_data(as_text=True))
        self.assertIn('data-auto-process="false"', page.get_data(as_text=True))

    def test_one_preset_per_source_model_and_distinct_paths_do_not_collide(self):
        saved = save_preset(SELECTION)
        self.assertEqual(save_preset(SELECTION)["presetId"], saved["presetId"])
        another = {
            **SELECTION,
            "semanticModel": {"name": "Sales", "path": "/Other/Sales.SemanticModel"},
        }
        self.assertNotEqual(save_preset(another)["presetId"], saved["presetId"])
        self.assertEqual(len(list_presets()), 2)
        self.assertEqual(
            {preset["localRepositoryPath"] for preset in list_presets()},
            {saved["localRepositoryPath"]},
        )
        self.repository_client.download_repository.assert_called_once()

    def test_presets_for_different_workspaces_share_the_repository(self):
        first = save_preset(SELECTION)
        second = save_preset({
            **SELECTION,
            "workspace": {
                "id": "2455b6a2-c120-4c1c-dda7-92bafe99bec3",
                "displayName": "Operations",
            },
        })
        self.assertNotEqual(first["presetId"], second["presetId"])
        self.assertEqual(first["localRepositoryPath"], second["localRepositoryPath"])
        self.repository_client.download_repository.assert_called_once()

    def test_opening_an_old_preset_downloads_missing_repository_once(self):
        saved = save_preset(SELECTION)
        snapshot = Path(saved["localRepositoryPath"])
        del saved["localRepositoryPath"]
        (self.presets_dir / f"{saved['presetId']}.json").write_text(
            json.dumps(saved), encoding="utf-8"
        )
        (snapshot / "README.md").unlink()
        snapshot.rmdir()
        self.repository_client.download_repository.reset_mock()

        for _ in range(2):
            response = self.client.post(
                f"/api/fabric/presets/{saved['presetId']}/process",
                json={"refresh": False},
            )
            self.assertIn('"stage": "complete"', response.get_data(as_text=True))
        self.repository_client.download_repository.assert_called_once()

    def test_cached_preset_opens_after_app_restart_without_authentication(self):
        saved = save_preset(SELECTION)
        restarted = create_app({"TESTING": True, "SECRET_KEY": "test-secret"}).test_client()
        with restarted.session_transaction() as session:
            session["user"] = USER
        with patch("fabric.backend.repositories.azure_devops_client") as get_client:
            response = restarted.get(f"/fabric/presets/{saved['presetId']}")
        self.assertEqual(response.status_code, 200)
        get_client.assert_not_called()

    def test_opening_preset_reports_download_failure(self):
        saved = save_preset(SELECTION)
        snapshot = Path(saved["localRepositoryPath"])
        (snapshot / "README.md").unlink()
        snapshot.rmdir()
        self.repository_client.download_repository.side_effect = requests.Timeout("timeout")

        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.post(
                f"/api/fabric/presets/{saved['presetId']}/process",
                json={"refresh": False},
            )
            progress = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('"state": "error"', progress)
        self.assertIn("Could not download", progress)
        self.assertFalse(snapshot.exists())

    def test_download_failure_does_not_save_preset_and_can_be_retried(self):
        self.repository_client.download_repository.side_effect = requests.Timeout("timeout")

        response = self.client.post("/api/fabric/presets", json=SELECTION)

        self.assertEqual(response.status_code, 502)
        self.assertIn("download", response.json["message"])
        self.assertEqual(list_presets(), [])
        self.repository_client.download_repository.side_effect = None
        self.assertEqual(
            self.client.post("/api/fabric/presets", json=SELECTION).status_code, 201
        )
        self.assertEqual(self.repository_client.download_repository.call_count, 2)

    def test_repository_access_errors_are_reported_without_saving_preset(self):
        response = requests.Response()
        response.status_code = 403
        self.repository_client.download_repository.side_effect = requests.HTTPError(
            response=response
        )
        result = self.client.post("/api/fabric/presets", json=SELECTION)
        self.assertEqual(result.status_code, 403)
        self.assertIn("denied access", result.json["message"])
        self.assertEqual(list_presets(), [])

    def test_clone_error_reports_git_details_without_saving_preset(self):
        self.repository_client.download_repository.side_effect = RepositoryCloneError(
            REPOSITORY, "fatal: Remote branch main not found in upstream origin.", 128
        )

        with self.assertLogs(self.app.logger, level="WARNING"):
            result = self.client.post("/api/fabric/presets", json=SELECTION)

        self.assertEqual(result.status_code, 502)
        self.assertIn("Remote branch main not found", result.json["message"])
        self.assertIn("Git exit code: 128", result.json["message"])
        self.assertIn("Repository: Models; branch: main", result.json["message"])
        self.assertEqual(result.json["details"]["gitExitCode"], 128)
        self.assertEqual(list_presets(), [])

    def test_preset_processing_stream_displays_git_clone_error(self):
        saved = save_preset(SELECTION)
        with patch(
            "fabric.backend.routes.ensure_repository",
            side_effect=RepositoryCloneError(
                REPOSITORY, "fatal: Requested repository does not exist.", 128
            ),
        ), self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.post(
                f"/api/fabric/presets/{saved['presetId']}/process",
                json={"refresh": False},
            )
            progress = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"state": "error"', progress)
        self.assertIn("Requested repository does not exist", progress)

    def test_manual_refresh_downloads_fresh_repository_and_rebuilds_lineage(self):
        saved = save_preset(SELECTION)
        snapshot = Path(saved["localRepositoryPath"])
        self.assertEqual((snapshot / "README.md").read_text(), "First snapshot")

        self.repository_client.download_repository.side_effect = (
            lambda _repository, destination: (destination / "README.md").write_text(
                "Refreshed snapshot", encoding="utf-8"
            )
        )
        response = self.client.post(
            f"/api/fabric/presets/{saved['presetId']}/process",
            json={"refresh": True},
        )
        progress = response.get_data(as_text=True)

        self.assertIn("Downloading a fresh repository snapshot", progress)
        self.assertIn('"stage": "complete"', progress)
        self.assertEqual((snapshot / "README.md").read_text(), "Refreshed snapshot")
        self.assertEqual(self.repository_client.download_repository.call_count, 2)

    def test_missing_model_and_invalid_repository_return_validation_errors(self):
        response = self.client.post("/api/fabric/presets", json={
            "workspace": SELECTION["workspace"], "repository": REPOSITORY,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("semantic model", response.json["message"])
        response = self.client.post("/api/fabric/semantic-models", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Repository", response.json["message"])
        self.assertEqual(self.client.get("/fabric/presets/invalid").status_code, 404)
        self.repository_client.download_repository.assert_not_called()

    def test_delete_removes_only_selected_preset_and_keeps_shared_repository(self):
        saved = save_preset(SELECTION)
        other = save_preset({
            **SELECTION,
            "semanticModel": {"name": "Other", "path": "/Other.SemanticModel"},
        })
        legacy_file = self.presets_dir.parent / "legacy.json"
        legacy_file.write_text("{}", encoding="utf-8")
        snapshot_file = Path(saved["localRepositoryPath"]) / "README.md"

        response = self.client.delete(f"/api/fabric/presets/{saved['presetId']}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("deleted", response.json["message"])
        self.assertFalse((self.presets_dir / f"{saved['presetId']}.json").exists())
        self.assertEqual(
            [preset["presetId"] for preset in self.client.get("/api/fabric/presets").json["presets"]],
            [other["presetId"]],
        )
        self.assertEqual(self.client.get(f"/fabric/presets/{saved['presetId']}").status_code, 404)
        self.assertEqual(self.client.get(f"/fabric/presets/{other['presetId']}").status_code, 200)
        self.assertEqual(snapshot_file.read_text(encoding="utf-8"), "First snapshot")
        self.assertTrue(legacy_file.exists())
        self.repository_client.download_repository.assert_called_once()

    def test_deleted_preset_can_be_recreated_without_downloading_repository(self):
        saved = save_preset(SELECTION)
        self.assertEqual(
            self.client.delete(f"/api/fabric/presets/{saved['presetId']}").status_code, 200
        )

        response = self.client.post("/api/fabric/presets", json=SELECTION)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["presetId"], saved["presetId"])
        self.assertEqual(response.json["localRepositoryPath"], saved["localRepositoryPath"])
        self.repository_client.download_repository.assert_called_once()

    def test_delete_rejects_invalid_ids_and_reports_missing_presets(self):
        saved = save_preset(SELECTION)
        for preset_id in ("invalid", "..", "g" * 64):
            with self.subTest(preset_id=preset_id):
                response = self.client.delete(f"/api/fabric/presets/{preset_id}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("Invalid Fabric preset ID", response.json["message"])
        response = self.client.delete("/api/fabric/presets/" + "a" * 64)
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.json["message"])
        self.assertEqual(len(list_presets()), 1)
        self.assertEqual(load_preset(saved["presetId"])["presetId"], saved["presetId"])

    def test_delete_reports_filesystem_errors_and_preserves_preset(self):
        saved = save_preset(SELECTION)
        with patch(
            "fabric.backend.routes.delete_saved_preset",
            side_effect=PermissionError("Access denied"),
        ):
            response = self.client.delete(f"/api/fabric/presets/{saved['presetId']}")
        self.assertEqual(response.status_code, 500)
        self.assertIn("Could not delete", response.json["message"])
        self.assertEqual(len(list_presets()), 1)

    def test_corrupted_preset_can_still_be_deleted(self):
        saved = save_preset(SELECTION)
        path = self.presets_dir / f"{saved['presetId']}.json"
        path.write_text("invalid JSON", encoding="utf-8")

        response = self.client.delete(f"/api/fabric/presets/{saved['presetId']}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(path.exists())

    def test_fabric_model_and_preset_routes_require_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        for method, path in (
            ("get", "/api/fabric/presets"),
            ("post", "/api/fabric/presets"),
            ("delete", "/api/fabric/presets/" + "a" * 64),
            ("post", "/api/fabric/semantic-models"),
        ):
            with self.subTest(path=path, method=method):
                self.assertEqual(getattr(self.client, method)(path).status_code, 401)
        self.assertEqual(self.client.get("/fabric/presets/" + "a" * 64).status_code, 302)


class AzureDevOpsModelTests(unittest.TestCase):
    @patch("fabric.backend.clients.requests.get")
    def test_model_discovery_deduplicates_tmsl_and_reads_tmdl_display_name(self, get):
        repo_response = MagicMock()
        repo_response.json.return_value = {
            "id": "repo-id", "name": "Models", "defaultBranch": "refs/heads/main",
        }
        items_response = MagicMock()
        items_response.json.return_value = {"value": [
            {"path": "/Sales.SemanticModel/definition.pbism"},
            {"path": "/Sales.SemanticModel/model.bim"},
            {"path": "/Folder/Tmdl.SemanticModel/definition.pbism"},
            {"path": "/Folder/Tmdl.SemanticModel/.platform"},
            {"path": "/Folder/Tmdl.SemanticModel/definition", "isFolder": True},
            {"path": "/Report.Report/definition.pbir"},
        ]}
        platform_response = MagicMock()
        platform_response.json.return_value = {"content": json.dumps({
            "metadata": {"type": "SemanticModel", "displayName": "Customer Insights"}
        })}
        get.side_effect = [repo_response, items_response, platform_response]

        result = AzureDevOpsClient("token").list_semantic_models({
            **REPOSITORY, "branchName": "feature/models",
        })

        self.assertEqual(result["semanticModels"], [
            {"name": "Customer Insights", "path": "/Folder/Tmdl.SemanticModel"},
            {"name": "Sales", "path": "/Sales.SemanticModel"},
        ])
        self.assertEqual(
            get.call_args_list[1].kwargs["params"]["versionDescriptor.version"],
            "feature/models",
        )
        self.assertEqual(get.call_args_list[1].kwargs["params"]["recursionLevel"], "Full")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["scopePath"], "/")

    @patch("fabric.backend.clients.requests.get")
    def test_manual_repository_uses_default_branch_and_empty_repo_returns_no_models(self, get):
        repo_response = MagicMock()
        repo_response.json.return_value = {
            "id": "repo-id", "name": "Models", "defaultBranch": "refs/heads/develop",
        }
        items_response = MagicMock()
        items_response.json.return_value = {"value": []}
        get.side_effect = [repo_response, items_response]
        result = AzureDevOpsClient("token").list_semantic_models({
            **REPOSITORY, "branchName": "",
        })
        self.assertEqual(result["repository"]["branchName"], "develop")
        self.assertEqual(result["semanticModels"], [])

        get.reset_mock()
        get.side_effect = None
        repo_response.json.return_value = {"id": "repo-id", "name": "Models"}
        get.return_value = repo_response
        result = AzureDevOpsClient("token").list_semantic_models({
            **REPOSITORY, "branchName": "",
        })
        self.assertEqual(result["semanticModels"], [])
        get.assert_called_once()


class RepositorySnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        directory_patch = patch("fabric.backend.repositories.REPOS_DIR", self.root / "repo")
        directory_patch.start()
        self.addCleanup(directory_patch.stop)

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_snapshot_uses_repository_and_branch_names(self, get_client):
        snapshot = ensure_repository(REPOSITORY)
        self.assertEqual(snapshot, self.root / "repo" / "Models" / "main" / "code")
        self.assertNotIn("\\\\?\\", str(snapshot))
        self.assertTrue((snapshot.parent / ".snapshot.json").is_file())
        get_client.return_value.download_repository.assert_called_once()

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_readable_names_do_not_mix_different_repositories_or_branches(self, get_client):
        first = ensure_repository({**REPOSITORY, "branchName": "feature/models"})
        another_branch = ensure_repository({**REPOSITORY, "branchName": "feature__models"})
        another_repo = ensure_repository({**REPOSITORY, "id": "different-repo-id"})

        self.assertEqual(first.parent.name, "feature__models")
        self.assertTrue(another_branch.parent.name.startswith("feature__models--"))
        self.assertTrue(another_repo.parent.parent.name.startswith("Models--"))
        self.assertNotEqual(first, another_branch)
        self.assertNotEqual(first.parent.parent, another_repo.parent.parent)
        self.assertEqual(ensure_repository({**REPOSITORY, "branchName": "feature/models"}), first)
        self.assertEqual(get_client.return_value.download_repository.call_count, 3)

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_windows_reserved_names_are_safe(self, _get_client):
        snapshot = ensure_repository({
            **REPOSITORY, "name": "CON", "branchName": "feature/a:b?c",
        })
        self.assertEqual(snapshot.parent.parent.name, "_CON")
        self.assertEqual(snapshot.parent.name, "feature__a_b_c")
        self.assertTrue(snapshot.resolve().is_relative_to((self.root / "repo").resolve()))

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_previous_hashed_snapshots_are_reused_without_download(self, get_client):
        identity = [
            REPOSITORY["organization"].casefold(),
            REPOSITORY["projectName"].casefold(),
            REPOSITORY["id"].casefold(),
        ]
        repository_key = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()[:32]
        branch_key = hashlib.sha256(b"main").hexdigest()[:32]
        previous = self.root / "repo" / repository_key / branch_key
        previous.mkdir(parents=True)
        (previous / "local.txt").write_text("Existing code", encoding="utf-8")

        self.assertEqual(ensure_repository(REPOSITORY), previous)
        self.assertEqual((previous / "local.txt").read_text(encoding="utf-8"), "Existing code")
        get_client.assert_not_called()

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_invalid_cache_metadata_is_reported_without_downloading(self, get_client):
        snapshot = ensure_repository(REPOSITORY)
        (snapshot.parent.parent / ".repository.json").write_text("invalid", encoding="utf-8")
        get_client.reset_mock()

        with self.assertRaisesRegex(RuntimeError, "Invalid local repository metadata"):
            ensure_repository(REPOSITORY)
        get_client.assert_not_called()

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_existing_snapshot_skips_authentication_and_keeps_local_changes(self, get_client):
        snapshot = ensure_repository(REPOSITORY)
        local_file = snapshot / "local.txt"
        local_file.write_text("Keep local changes", encoding="utf-8")
        get_client.reset_mock()
        get_client.side_effect = AssertionError("Cached repositories must not authenticate")

        self.assertEqual(ensure_repository(REPOSITORY), snapshot)
        self.assertEqual(local_file.read_text(encoding="utf-8"), "Keep local changes")
        get_client.assert_not_called()

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_cache_identity_ignores_model_directory_and_repository_name(self, get_client):
        first = ensure_repository(REPOSITORY)
        second = ensure_repository({
            **REPOSITORY, "name": "Renamed", "directoryName": "/Another",
            "branchName": "refs/heads/main", "organization": "contoso",
        })
        self.assertEqual(first, second)
        get_client.return_value.download_repository.assert_called_once()

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_different_sources_and_branches_do_not_share_snapshots(self, get_client):
        selections = [
            REPOSITORY,
            {**REPOSITORY, "id": "another-repo"},
            {**REPOSITORY, "organization": "Another"},
            {**REPOSITORY, "branchName": "feature/models"},
            {**REPOSITORY, "branchName": "Main"},
        ]
        paths = {ensure_repository(repository) for repository in selections}
        self.assertEqual(len(paths), len(selections))
        self.assertEqual(get_client.return_value.download_repository.call_count, len(selections))

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_partial_failure_cleans_direct_checkout_and_retries(self, get_client):
        def fail(_repository, destination):
            destination = filesystem_path(destination)
            (destination / "partial.txt").write_text("incomplete", encoding="utf-8")
            objects = destination / ".git" / "objects"
            objects.mkdir(parents=True)
            readonly = objects / "partial-object"
            readonly.write_text("incomplete", encoding="utf-8")
            readonly.chmod(stat.S_IREAD)
            raise RepositoryCloneError(REPOSITORY, "Connection lost", 128)

        get_client.return_value.download_repository.side_effect = fail
        with self.assertRaisesRegex(RepositoryCloneError, "Connection lost"):
            ensure_repository(REPOSITORY)
        self.assertEqual(list((self.root / "repo").rglob(".download-*")), [])
        self.assertEqual(list((self.root / "repo").rglob("partial.txt")), [])
        self.assertFalse((self.root / "repo" / "Models" / "main" / "code").exists())

        get_client.return_value.download_repository.side_effect = None
        self.assertTrue(ensure_repository(REPOSITORY).is_dir())
        self.assertEqual(get_client.return_value.download_repository.call_count, 2)

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_failed_refresh_preserves_cached_snapshot(self, get_client):
        snapshot = ensure_repository(REPOSITORY)
        (snapshot / "README.md").write_text("Cached snapshot", encoding="utf-8")

        def fail(_repository, destination):
            (destination / "partial.txt").write_text("incomplete", encoding="utf-8")
            raise RepositoryCloneError(REPOSITORY, "Connection lost", 128)

        get_client.return_value.download_repository.side_effect = fail
        with self.assertRaisesRegex(RepositoryCloneError, "Connection lost"):
            ensure_repository(REPOSITORY, refresh=True)

        self.assertEqual((snapshot / "README.md").read_text(), "Cached snapshot")
        self.assertEqual(list(snapshot.parent.glob(".refresh-*")), [])
        self.assertEqual(list(snapshot.parent.glob(".previous-*")), [])

    @patch("fabric.backend.repositories.time.sleep")
    @patch("fabric.backend.repositories.azure_devops_client")
    def test_refresh_retries_transient_access_denied_errors(self, get_client, sleep):
        snapshot = ensure_repository(REPOSITORY)
        (snapshot / "README.md").write_text("Cached snapshot", encoding="utf-8")
        get_client.return_value.download_repository.side_effect = (
            lambda _repository, destination: (destination / "README.md").write_text(
                "Refreshed snapshot", encoding="utf-8"
            )
        )
        real_rename = os.rename
        failed_sources = set()

        def transient_access_denied(source, destination):
            source_name = Path(source).name
            if (
                source_name == "code" or source_name.startswith(".refresh-")
            ) and source_name not in failed_sources:
                failed_sources.add(source_name)
                raise PermissionError(5, "Access is denied", source)
            return real_rename(source, destination)

        with patch(
            "fabric.backend.repositories.os.rename",
            side_effect=transient_access_denied,
        ):
            self.assertEqual(ensure_repository(REPOSITORY, refresh=True), snapshot)

        self.assertEqual((snapshot / "README.md").read_text(), "Refreshed snapshot")
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(list(snapshot.parent.glob(".refresh-*")), [])
        self.assertEqual(list(snapshot.parent.glob(".previous-*")), [])

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_interrupted_refresh_restores_cached_snapshot(self, get_client):
        snapshot = ensure_repository(REPOSITORY)
        (snapshot / "README.md").write_text("Cached snapshot", encoding="utf-8")
        backup = snapshot.parent / ".previous-interrupted"
        staging = snapshot.parent / ".refresh-interrupted"
        snapshot.rename(backup)
        staging.mkdir()
        (staging / "README.md").write_text("Uncommitted snapshot", encoding="utf-8")
        get_client.reset_mock()

        self.assertEqual(ensure_repository(REPOSITORY), snapshot)

        self.assertEqual((snapshot / "README.md").read_text(), "Cached snapshot")
        self.assertEqual(list(snapshot.parent.glob(".refresh-*")), [])
        self.assertEqual(list(snapshot.parent.glob(".previous-*")), [])
        get_client.assert_not_called()

    @patch("fabric.backend.repositories.azure_devops_client")
    @patch("fabric.backend.repositories.os.rename", side_effect=AssertionError("No folder rename"))
    def test_clone_writes_directly_to_final_path(self, _rename, get_client):
        expected = self.root / "repo" / "Models" / "main" / "code"

        def clone(_repository, destination):
            self.assertEqual(destination, expected)
            self.assertTrue(destination.is_dir())
            self.assertEqual(list(destination.parent.parent.glob(".download-*")), [])
            metadata = json.loads((destination.parent / ".snapshot.json").read_text())
            self.assertFalse(metadata["complete"])
            (destination / "README.md").write_text("Cloned directly", encoding="utf-8")

        get_client.return_value.download_repository.side_effect = clone
        self.assertEqual(ensure_repository(REPOSITORY), expected)
        self.assertEqual((expected / "README.md").read_text(), "Cloned directly")
        self.assertEqual(
            json.loads((expected.parent / ".snapshot.json").read_text()),
            {"branchName": "main"},
        )

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_interrupted_clone_is_reported_without_reusing_or_deleting_files(self, get_client):
        snapshot = ensure_repository(REPOSITORY)
        (snapshot.parent / ".snapshot.json").write_text(
            json.dumps({"branchName": "main", "complete": False}), encoding="utf-8"
        )
        (snapshot / "partial.txt").write_text("incomplete", encoding="utf-8")
        get_client.reset_mock()

        with self.assertRaisesRegex(RuntimeError, "incomplete Git clone"):
            ensure_repository(REPOSITORY)

        self.assertEqual((snapshot / "partial.txt").read_text(), "incomplete")
        get_client.assert_not_called()

    @patch("fabric.backend.repositories.azure_devops_client")
    def test_real_git_clone_supports_long_paths_full_branch_and_cache_reuse(self, get_client):
        source = self.root / "origin"
        source.mkdir()
        run = subprocess.run

        def git(*arguments):
            return run(
                ["git", "-c", "core.longpaths=true", "-C", str(source), *arguments],
                capture_output=True, text=True, check=True,
            )

        git("init", "--initial-branch=main")
        (source / "README.md").write_text("main branch", encoding="utf-8")
        git("add", "--all")
        git("-c", "user.name=Clone Test", "-c", "user.email=clone@example.com",
            "commit", "--no-gpg-sign", "-m", "Initial commit")
        relative_path = (
            Path("FabricArtifacts") / "Publish" / "UnifiedSalesOfferPublish" / "DataPipeline"
            / "DimAgreementDeliveryContractRenewals_CESUSOPPPublishPipeline.DataPipeline"
            / "pipeline-content.json"
        )
        repository = {
            **REPOSITORY,
            "name": "Repository-" + "x" * 60,
            "branchName": "team/" + "branch" * 12,
        }
        git("switch", "--create", repository["branchName"])
        files = {
            Path("README.md"): "selected branch",
            Path(".hidden"): "hidden",
            Path("Sales.SemanticModel") / "definition.pbism": "{}",
            Path("Other.SemanticModel") / "definition" / "tables" / "Sales.tmdl": "table Sales",
            relative_path: '{"pipeline": "complete"}',
        }
        for relative, content in files.items():
            target = filesystem_path(source / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git("add", "--all")
        git("-c", "user.name=Clone Test", "-c", "user.email=clone@example.com",
            "commit", "--no-gpg-sign", "-m", "Selected branch files")
        get_client.return_value = AzureDevOpsClient("clone-test-token")
        root = filesystem_path(self.root / "repo")

        def clone_local(arguments, **kwargs):
            self.assertTrue(arguments[-2].startswith("https://dev.azure.com/"))
            arguments = [*arguments]
            arguments[-2] = source.as_uri()
            return run(arguments, **kwargs)

        try:
            with patch("fabric.backend.clients.subprocess.run", side_effect=clone_local) as clone:
                snapshot = ensure_repository({**repository, "directoryName": "/Sales.SemanticModel"})
            clone.assert_called_once()
            self.assertGreater(len(str(snapshot / relative_path)), 260)
            for relative, content in files.items():
                self.assertEqual(
                    filesystem_path(snapshot / relative).read_text(encoding="utf-8"), content
                )
            self.assertTrue(filesystem_path(snapshot / ".git").is_dir())
            config = filesystem_path(snapshot / ".git" / "config").read_text(encoding="utf-8")
            self.assertIn("longpaths = true", config)
            self.assertNotIn("clone-test-token", config)
            self.assertNotIn("extraheader", config.lower())
            branch = run(
                ["git", "-C", str(snapshot), "branch", "--show-current"],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(branch.stdout.strip(), repository["branchName"])
            self.assertEqual(list(filesystem_path(snapshot.parent.parent).glob(".download-*")), [])
            filesystem_path(snapshot / "README.md").write_text("local change", encoding="utf-8")
            get_client.side_effect = AssertionError("Existing snapshots must not authenticate")
            self.assertEqual(ensure_repository(repository), snapshot)
            self.assertEqual(
                filesystem_path(snapshot / "README.md").read_text(encoding="utf-8"), "local change"
            )
        finally:
            if root.is_dir():
                shutil.rmtree(root, onexc=_remove_readonly)


class RepositoryCloneTests(unittest.TestCase):
    @patch("fabric.backend.clients.subprocess.run")
    def test_git_clones_selected_branch_directly_with_environment_only_authentication(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        repository = {
            **REPOSITORY, "name": "Fabric Models", "projectName": "Analytics & BI",
            "branchName": "feature/models", "url": "https://untrusted.invalid/repo",
        }
        client = AzureDevOpsClient("clone-test-token")
        destination = Path("repo") / "Models" / "feature__models" / "code"
        environment_before = dict(os.environ)

        client.download_repository(repository, destination)

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:5], ["git", "-c", "core.longpaths=true", "clone", "--single-branch"])
        self.assertEqual(arguments[arguments.index("--branch") + 1], "feature/models")
        self.assertEqual(arguments[-2:], [
            "https://dev.azure.com/Contoso/Analytics%20%26%20BI/_git/Fabric%20Models",
            str(destination.absolute()),
        ])
        self.assertNotIn("clone-test-token", str(arguments))
        self.assertNotIn("untrusted.invalid", str(arguments))
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "http.https://dev.azure.com/.extraHeader")
        self.assertEqual(
            environment["GIT_CONFIG_VALUE_0"], f"Authorization: {client.headers['Authorization']}"
        )
        self.assertEqual(environment["GIT_CONFIG_KEY_1"], "credential.helper")
        self.assertEqual(environment["GIT_CONFIG_VALUE_1"], "")
        self.assertEqual(run.call_args.kwargs["timeout"], 600)
        self.assertEqual(dict(os.environ), environment_before)

    @patch("fabric.backend.clients.subprocess.run", side_effect=FileNotFoundError("git"))
    def test_missing_git_has_an_actionable_error(self, _run):
        with self.assertRaisesRegex(RepositoryCloneError, "Install Git"):
            AzureDevOpsClient("token").download_repository(REPOSITORY, Path("repo"))

    @patch(
        "fabric.backend.clients.subprocess.run",
        side_effect=PermissionError("Access denied when starting Git"),
    )
    def test_git_process_permission_failure_is_reported_as_a_clone_error(self, _run):
        with self.assertRaisesRegex(RepositoryCloneError, "Could not start Git.*Access denied"):
            AzureDevOpsClient("token").download_repository(REPOSITORY, Path("repo"))

    @patch(
        "fabric.backend.clients.subprocess.run",
        side_effect=subprocess.TimeoutExpired("git", 600),
    )
    def test_clone_timeout_is_reported(self, _run):
        with self.assertRaisesRegex(RepositoryCloneError, "timed out after 600 seconds"):
            AzureDevOpsClient("token").download_repository(REPOSITORY, Path("repo"))

    @patch("fabric.backend.clients.subprocess.run")
    def test_git_errors_report_exit_code_and_redact_authorization(self, run):
        client = AzureDevOpsClient("clone-test-token")
        authorization = client.headers["Authorization"]
        run.return_value = subprocess.CompletedProcess(
            [], 128, "", f"fatal: Authentication failed. {authorization} clone-test-token"
        )

        with self.assertRaises(RepositoryCloneError) as caught:
            client.download_repository(REPOSITORY, Path("repo"))

        self.assertEqual(caught.exception.details["gitExitCode"], 128)
        self.assertIn("Authentication failed", str(caught.exception))
        self.assertNotIn(authorization, str(caught.exception))
        self.assertNotIn("clone-test-token", str(caught.exception.details))

    @patch("fabric.backend.clients.subprocess.run")
    def test_git_failure_without_stderr_is_reported(self, run):
        run.return_value = subprocess.CompletedProcess([], 128, "", "")
        with self.assertRaisesRegex(RepositoryCloneError, "without an error message"):
            AzureDevOpsClient("token").download_repository(REPOSITORY, Path("repo"))


class RepositoryFilesystemTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows extended-length paths")
    def test_windows_paths_are_extended_and_idempotent(self):
        extended = filesystem_path(Path("repo") / "Models")
        self.assertTrue(str(extended).startswith("\\\\?\\"))
        self.assertEqual(filesystem_path(extended), extended)

    @unittest.skipUnless(os.name == "nt", "Windows UNC paths")
    def test_unc_prefix_is_preserved(self):
        self.assertEqual(
            str(filesystem_path(r"\\server\share\repo")),
            r"\\?\UNC\server\share\repo",
        )


if __name__ == "__main__":
    unittest.main()
