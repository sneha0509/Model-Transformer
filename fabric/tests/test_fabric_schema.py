import unittest
from unittest.mock import MagicMock, patch

import requests

from fabric.backend.clients import FabricWorkspaceClient, SemanticModelResolutionError


WORKSPACE_ID = "1455b6a2-c120-4c1c-dda7-92bafe99bec3"
MODEL_ID = "5a7640e2-2d70-437f-a34b-3056cb7ae5cf"
OPERATION_ID = "350c5fe4-1878-4fda-947f-7a1da82d8098"
MODEL = {"id": MODEL_ID, "displayName": "Sales"}


def response(payload, status=200, headers=None):
    result = MagicMock()
    result.status_code = status
    result.headers = headers or {}
    result.json.return_value = payload
    return result


class FabricSchemaTests(unittest.TestCase):
    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_resolves_model_across_pages_and_gets_tmsl_definition(self, get, post):
        get.side_effect = [
            response({"value": [{"id": WORKSPACE_ID, "displayName": "Other"}], "continuationToken": "next"}),
            response({"value": [MODEL]}),
        ]
        definition = {"parts": [{"path": "model.bim", "payload": "encoded", "payloadType": "InlineBase64"}]}
        post.return_value = response({"definition": definition})
        result = FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "sales")
        self.assertEqual(result["semanticModel"], {"id": MODEL_ID, "name": "Sales"})
        self.assertEqual(result["definition"], definition)
        self.assertEqual(get.call_args.kwargs["params"], {"continuationToken": "next"})
        self.assertEqual(post.call_args.args[0],
                         f"https://api.fabric.microsoft.com/v1/workspaces/{WORKSPACE_ID}/semanticModels/{MODEL_ID}/getDefinition")
        self.assertEqual(post.call_args.kwargs["params"], {"format": "TMSL"})

    @patch("fabric.backend.clients.time.sleep")
    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_polls_long_running_definition_and_fetches_result(self, get, post, sleep):
        get.side_effect = [
            response({"value": [MODEL]}),
            response({"status": "Running"}, headers={"Retry-After": "2"}),
            response({"status": "Succeeded"}),
            response({"definition": {"parts": []}}),
        ]
        post.return_value = response({}, 202, {
            "x-ms-operation-id": OPERATION_ID, "Retry-After": "1",
            "Location": "https://untrusted.example/ignored",
        })
        result = FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "Sales")
        self.assertEqual(result["definition"], {"parts": []})
        self.assertEqual(get.call_args.args[0],
                         f"https://api.fabric.microsoft.com/v1/operations/{OPERATION_ID}/result")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertTrue(all(call.args[0].startswith("https://api.fabric.microsoft.com/")
                            for call in get.call_args_list))

    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_missing_or_ambiguous_model_does_not_guess(self, get, post):
        for models in ([], [MODEL, {**MODEL, "id": WORKSPACE_ID}]):
            with self.subTest(models=models):
                get.return_value = response({"value": models})
                with self.assertRaises(ValueError):
                    FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "Sales")
        post.assert_not_called()

    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_name_mismatch_returns_actual_workspace_models_for_explicit_selection(self, get, post):
        get.return_value = response({"value": [MODEL]})
        with self.assertRaises(SemanticModelResolutionError) as error:
            FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "UnifiedPropensityTool")
        self.assertEqual(error.exception.models, [{"id": MODEL_ID, "name": "Sales"}])
        self.assertIn("Select its matching Fabric semantic model", str(error.exception))
        post.assert_not_called()

    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_saved_id_resolves_renamed_model_even_when_old_name_matches_another_model(self, get, post):
        get.return_value = response({"value": [
            {**MODEL, "displayName": "UnifiedPropensityTool Production"},
            {"id": WORKSPACE_ID, "displayName": "UnifiedPropensityTool"},
        ]})
        post.return_value = response({"definition": {"parts": []}})
        result = FabricWorkspaceClient("test-token").get_semantic_model_definition(
            WORKSPACE_ID, "UnifiedPropensityTool", model_id=MODEL_ID,
        )
        self.assertEqual(result["semanticModel"],
                         {"id": MODEL_ID, "name": "UnifiedPropensityTool Production"})
        self.assertIn(f"/{MODEL_ID}/getDefinition", post.call_args.args[0])

    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_stale_binding_does_not_switch_to_a_same_named_model(self, get, post):
        get.return_value = response({"value": [MODEL]})
        with self.assertRaises(SemanticModelResolutionError) as error:
            FabricWorkspaceClient("test-token").get_semantic_model_definition(
                WORKSPACE_ID, "Sales", model_id=WORKSPACE_ID,
            )
        self.assertIn("no longer available", str(error.exception))
        post.assert_not_called()

    @patch("fabric.backend.clients.requests.get")
    def test_empty_workspace_explains_deployment_or_access_requirement(self, get):
        get.return_value = response({"value": []})
        with self.assertRaises(SemanticModelResolutionError) as error:
            FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "Sales")
        self.assertEqual(error.exception.models, [])
        self.assertIn("deployed here", str(error.exception))

    @patch("fabric.backend.clients.requests.get")
    def test_repeated_model_continuation_token_is_an_error(self, get):
        get.return_value = response({"value": [], "continuationToken": "repeat"})
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "Sales")
        self.assertEqual(get.call_count, 2)

    @patch("fabric.backend.clients.time.sleep")
    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_failed_operation_is_not_returned_as_an_empty_schema(self, get, post, _sleep):
        get.side_effect = [
            response({"value": [MODEL]}),
            response({"status": "Failed", "error": {"message": "Definition unavailable"}}),
        ]
        post.return_value = response({}, 202, {"x-ms-operation-id": OPERATION_ID})
        with self.assertRaisesRegex(RuntimeError, "Definition unavailable"):
            FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "Sales")

    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_missing_operation_id_is_an_error(self, get, post):
        get.return_value = response({"value": [MODEL]})
        post.return_value = response({}, 202)
        with self.assertRaisesRegex(RuntimeError, "operation ID"):
            FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "Sales")

    @patch("fabric.backend.clients.time.monotonic", side_effect=[0, 180])
    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_operation_timeout_is_explicit(self, get, post, _time):
        get.return_value = response({"value": [MODEL]})
        post.return_value = response({}, 202, {"x-ms-operation-id": OPERATION_ID})
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "Sales")
        self.assertEqual(get.call_count, 1)

    @patch("fabric.backend.clients.requests.post")
    @patch("fabric.backend.clients.requests.get")
    def test_authorization_errors_are_propagated(self, get, post):
        denied = response({}, status=403)
        denied.raise_for_status.side_effect = requests.HTTPError(response=denied)
        get.return_value = denied
        with self.assertRaises(requests.HTTPError):
            FabricWorkspaceClient("test-token").get_semantic_model_definition(WORKSPACE_ID, "Sales")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
