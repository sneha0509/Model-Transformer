import time

import requests

try:
    from .shared_Scripts import normalize_model, normalize_report, normalize_workspace
except ImportError:
    from shared_Scripts import normalize_model, normalize_report, normalize_workspace


POWER_BI_GROUPS_URL = "https://api.powerbi.com/v1.0/myorg/groups"
POWER_BI_GROUP_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
FABRIC_ITEM_DEFINITION_URL = "https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{item_id}/getDefinition"


class PowerBIClient:
    def __init__(self, access_token):
        self.access_token = access_token

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    def get_values(self, url):
        return self.get_json(url).get("value", [])

    def get_json(self, url, params=None):
        response = requests.get(
            url,
            headers=self.headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_json_or_none(self, url, params=None):
        try:
            return self.get_json(url, params=params)
        except requests.HTTPError:
            return None

    def workspace_url(self, workspace_id):
        return POWER_BI_GROUP_URL.format(workspace_id=workspace_id)

    def get_workspaces(self):
        response = requests.get(
            POWER_BI_GROUPS_URL,
            headers=self.headers,
            params={"$top": 5000},
            timeout=30,
        )
        response.raise_for_status()
        return [normalize_workspace(workspace) for workspace in response.json().get("value", [])]

    def get_workspace_assets(self, workspace_id):
        workspace_url = self.workspace_url(workspace_id)
        datasets = self.get_values(f"{workspace_url}/datasets")
        reports = self.get_values(f"{workspace_url}/reports")
        dataset_owners = {dataset.get("id"): dataset.get("configuredBy") for dataset in datasets if dataset.get("id")}

        return {
            "models": [normalize_model(dataset, workspace_id) for dataset in datasets],
            "reports": [normalize_report(report, dataset_owners) for report in reports],
        }

    def get_dataset(self, workspace_id, dataset_id):
        return self.get_json(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}")

    def get_report(self, workspace_id, report_id):
        return self.get_json(f"{self.workspace_url(workspace_id)}/reports/{report_id}")

    def get_dataset_refreshes(self, workspace_id, dataset_id, top=1):
        return self.get_json_or_none(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}/refreshes", {"$top": top})

    def get_dataset_refresh_schedule(self, workspace_id, dataset_id):
        return self.get_json_or_none(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}/refreshSchedule")

    def get_dataset_datasources(self, workspace_id, dataset_id):
        return self.get_json_or_none(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}/datasources")

    def get_dataset_tables(self, workspace_id, dataset_id):
        return self.get_json_or_none(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}/tables")


class FabricClient:
    def __init__(self, access_token):
        self.access_token = access_token

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    @property
    def json_headers(self):
        return {**self.headers, "Content-Type": "application/json"}

    def get_item_definition(self, workspace_id, item_id):
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
            return self.get_operation_result(response.headers.get("Location"))

        response.raise_for_status()
        return response.json()

    def get_operation_result(self, operation_url):
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