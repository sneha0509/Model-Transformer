"""Power BI REST client for workspace, report, and semantic model discovery."""

import requests

from core.normalization import normalize_model, normalize_report, normalize_workspace


POWER_BI_GROUPS_URL = "https://api.powerbi.com/v1.0/myorg/groups"
POWER_BI_GROUP_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"


class PowerBIClient:
    """Wrap the Power BI REST calls used by the Model Transformer UI."""

    def __init__(self, access_token):
        self.access_token = access_token

    @property
    def headers(self):
        """Authorization headers shared by Power BI requests."""
        return {"Authorization": f"Bearer {self.access_token}"}

    def get_values(self, url):
        """Return the Power BI collection payload from an endpoint."""
        return self.get_json(url).get("value", [])

    def get_json(self, url, params=None):
        """Perform a GET request and raise if the Power BI API reports an error."""
        response = requests.get(
            url,
            headers=self.headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_json_or_none(self, url, params=None):
        """Return JSON for optional endpoints, suppressing HTTP errors as unavailable data."""
        try:
            return self.get_json(url, params=params)
        except requests.HTTPError:
            return None

    def workspace_url(self, workspace_id):
        """Build the base URL for a Power BI workspace."""
        return POWER_BI_GROUP_URL.format(workspace_id=workspace_id)

    def get_workspaces(self):
        """Return normalized workspaces available to the signed-in user."""
        response = requests.get(
            POWER_BI_GROUPS_URL,
            headers=self.headers,
            params={"$top": 5000},
            timeout=30,
        )
        response.raise_for_status()
        return [normalize_workspace(workspace) for workspace in response.json().get("value", [])]

    def get_workspace_assets(self, workspace_id):
        """Return normalized semantic models and reports for a workspace."""
        workspace_url = self.workspace_url(workspace_id)
        datasets = self.get_values(f"{workspace_url}/datasets")
        reports = self.get_values(f"{workspace_url}/reports")
        dataset_owners = {dataset.get("id"): dataset.get("configuredBy") for dataset in datasets if dataset.get("id")}

        return {
            "models": [normalize_model(dataset, workspace_id) for dataset in datasets],
            "reports": [normalize_report(report, dataset_owners) for report in reports],
        }

    def get_dataset(self, workspace_id, dataset_id):
        """Return a single dataset/semantic model from a workspace."""
        return self.get_json(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}")

    def get_report(self, workspace_id, report_id):
        """Return a single report from a workspace."""
        return self.get_json(f"{self.workspace_url(workspace_id)}/reports/{report_id}")

    def get_dataset_refreshes(self, workspace_id, dataset_id, top=1):
        """Return recent refresh history for a dataset when the endpoint is available."""
        return self.get_json_or_none(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}/refreshes", {"$top": top})

    def get_dataset_refresh_schedule(self, workspace_id, dataset_id):
        """Return the configured refresh schedule for a dataset when available."""
        return self.get_json_or_none(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}/refreshSchedule")

    def get_dataset_datasources(self, workspace_id, dataset_id):
        """Return datasource metadata for a dataset when available."""
        return self.get_json_or_none(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}/datasources")

    def get_dataset_tables(self, workspace_id, dataset_id):
        """Return table metadata for push datasets when available."""
        return self.get_json_or_none(f"{self.workspace_url(workspace_id)}/datasets/{dataset_id}/tables")