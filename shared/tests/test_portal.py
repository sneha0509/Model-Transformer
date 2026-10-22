import ast
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from azure.identity import CredentialUnavailableError

from shared.app import create_app
from shared.paths import PROJECT_ROOT


USER = {"name": "Test User", "email": "test@example.com", "tenantId": "tenant"}


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()

    def sign_in(self):
        with patch("shared.backend.routes.get_power_bi_access_token", return_value="test-token"), \
                patch("shared.backend.routes.build_user", return_value=USER):
            return self.client.post("/api/login")

    def test_start_screen_keeps_login_and_hides_dashboard_choices(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Sign In Required", html)
        self.assertIn("Sign in with Microsoft", html)
        self.assertRegex(html, r'id="dashboardPicker"[^>]*hidden')
        self.assertNotIn("Saved Presets", html)

    def test_login_returns_identity_without_workspace_lookup(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"user": USER})
        with self.client.session_transaction() as session:
            self.assertEqual(session["user"], USER)
        html = self.client.get("/").get_data(as_text=True)
        self.assertRegex(html, r'id="signInPrompt"[^>]*hidden')
        self.assertNotRegex(html, r'id="dashboardPicker"[^>]*hidden')
        self.assertIn('href="/legacy"', html)
        self.assertIn('href="/fabric"', html)

    def test_failed_login_does_not_unlock_dashboards(self):
        with patch("shared.backend.routes.get_power_bi_access_token",
                   side_effect=CredentialUnavailableError("No credential")):
            response = self.client.post("/api/login")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Browser authentication failed", response.json["message"])
        self.assertEqual(self.client.get("/fabric").status_code, 302)

    def test_pages_require_login(self):
        for path in ("/legacy", "/fabric", "/legacy/selected-tables/demo"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/")

    def test_legacy_apis_require_login(self):
        for method, path in (
            ("get", "/api/legacy/presets"),
            ("get", "/api/legacy/workspaces"),
            ("post", "/api/legacy/refresh-jobs"),
            ("post", "/api/legacy/ai-table-precedence"),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 401)
                self.assertIn("Sign in", response.json["message"])

    def test_fabric_has_workspace_repository_flow_and_no_legacy_api(self):
        self.sign_in()
        html = self.client.get("/fabric").get_data(as_text=True)
        self.assertIn('id="fabricDashboard"', html)
        self.assertIn('id="workspaceSearch"', html)
        self.assertIn('id="repositorySection"', html)
        self.assertIn('id="selectionSection"', html)
        self.assertIn('href="/"', html)
        self.assertIn('id="headerSignOutButton"', html)
        self.assertIn("Saved Presets", html)
        self.assertNotIn("/api/legacy", html)
        self.assertTrue(any(
            rule.rule.startswith("/api/fabric")
            for rule in self.app.url_map.iter_rules()
        ))

    def test_logout_clears_session_and_cached_credential(self):
        self.sign_in()
        with patch("shared.backend.routes.clear_browser_credential") as clear:
            response = self.client.post("/api/logout")
        self.assertEqual(response.status_code, 200)
        clear.assert_called_once_with()
        self.assertEqual(self.client.get("/legacy").status_code, 302)
        self.assertEqual(self.client.get("/api/legacy/presets").status_code, 401)

    def test_frontend_assets_are_separate_and_public(self):
        for path, expected in (
            ("/shared/static/css/styles.css", "dashboard-options"),
            ("/shared/static/js/session.js", "PortalSession"),
            ("/shared/static/js/home.js", "/api/login"),
            ("/legacy/static/css/styles.css", "preset-grid"),
            ("/fabric/static/css/styles.css", "fabric-dashboard"),
        ):
            with self.subTest(path=path):
                with self.client.get(path) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(expected, response.get_data(as_text=True))

    def test_area_imports_do_not_cross_feature_boundaries(self):
        for area in ("legacy", "fabric", "shared"):
            for path in (PROJECT_ROOT / area).rglob("*.py"):
                if "tests" in path.parts or path.name == "app.py":
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    modules = []
                    if isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    elif isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    for module in modules:
                        root = module.split(".")[0]
                        self.assertNotIn(root, {"core", "ui"})
                        if area == "shared":
                            self.assertNotIn(root, {"legacy", "fabric"})
                        elif area == "fabric":
                            self.assertNotEqual(root, "legacy")
                        else:
                            self.assertNotEqual(root, "fabric")

    @unittest.skipUnless(shutil.which("node"), "Node is needed for JavaScript syntax checks")
    def test_rendered_inline_and_shared_javascript_parses(self):
        self.sign_in()
        pages = [
            self.client.get("/").get_data(as_text=True),
            self.client.get("/legacy").get_data(as_text=True),
            self.client.get("/fabric").get_data(as_text=True),
            self.client.post("/legacy/selected-tables", json={
                "modelName": "Test Model", "selectedTables": ["Sales"],
            }).get_data(as_text=True),
        ]
        scripts = []
        for html in pages:
            for attributes, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S):
                if "application/json" not in attributes and body.strip():
                    scripts.append(body)
        scripts.extend(path.read_text(encoding="utf-8") for path in
                       (PROJECT_ROOT / "shared" / "frontend" / "static" / "js").glob("*.js"))
        for index, script in enumerate(scripts):
            with self.subTest(script=index):
                result = subprocess.run(
                    [shutil.which("node"), "--check"], input=script,
                    text=True, encoding="utf-8", capture_output=True, check=False, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
