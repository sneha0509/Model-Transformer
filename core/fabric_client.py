"""Small Fabric REST client used to retrieve semantic model definitions."""

import time

import requests


FABRIC_ITEM_DEFINITION_URL = "https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{item_id}/getDefinition"


class FabricClient:
    """Wrap Fabric item definition endpoints with token handling and polling."""

    def __init__(self, access_token):
        self.access_token = access_token

    @property
    def headers(self):
        """Authorization headers shared by Fabric requests."""
        return {"Authorization": f"Bearer {self.access_token}"}

    @property
    def json_headers(self):
        """Authorization headers for Fabric requests with a JSON body."""
        return {**self.headers, "Content-Type": "application/json"}

    def get_item_definition(self, workspace_id, item_id):
        """Request an item's TMDL definition, following asynchronous operations when needed."""
        if not self.access_token or not item_id:
            return None

        url = FABRIC_ITEM_DEFINITION_URL.format(workspace_id=workspace_id, item_id=item_id)
        response = requests.post(
            url,
            headers=self.json_headers,
            json={"format": "TMDL"},
            timeout=30,
        )
        if response.status_code == 202:
            # Fabric can accept the request and expose the final payload through an operation URL.
            return self.get_operation_result(response.headers.get("Location"))

        response.raise_for_status()
        return response.json()

    def get_operation_result(self, operation_url):
        """Poll a Fabric operation URL and return its final JSON result when it succeeds."""
        if not operation_url:
            return None

        for attempt in range(3):
            response = requests.get(operation_url, headers=self.headers, timeout=30)
            response.raise_for_status()
            operation = response.json()
            if operation.get("status") == "Succeeded":
                result_url = response.headers.get("Location")
                if not result_url:
                    return None
                result_response = requests.get(result_url, headers=self.headers, timeout=30)
                result_response.raise_for_status()
                return result_response.json()
            if operation.get("status") in {"Failed", "Cancelled"}:
                return None
            if attempt < 2:
                time.sleep(0.5)

        return None