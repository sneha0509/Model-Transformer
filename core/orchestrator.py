"""Application service layer that wires authentication clients to UI workflows."""

from core.auth import build_user, get_azure_cli_account, get_fabric_access_token, get_power_bi_access_token
from core.fabric_client import FabricClient
from core.powerbi_client import PowerBIClient
from core.semantic_model import get_power_bi_asset_details


def create_power_bi_client():
    """Build a Power BI client with a fresh Azure CLI-backed access token."""
    return PowerBIClient(get_power_bi_access_token())


def create_fabric_client():
    """Build a Fabric client with a fresh Azure CLI-backed access token."""
    return FabricClient(get_fabric_access_token())


def get_login_context():
    """Return the signed-in user and accessible workspaces for the home page."""
    account = get_azure_cli_account()
    power_bi_client = create_power_bi_client()
    return {"user": build_user(account), "workspaces": power_bi_client.get_workspaces()}


def get_workspace_assets(workspace_id):
    """Return semantic models and reports available in a selected workspace."""
    power_bi_client = create_power_bi_client()
    return power_bi_client.get_workspace_assets(workspace_id)


def get_asset_details(workspace_id, category, asset_id):
    """Return enriched report or semantic model details for the selected asset."""
    power_bi_client = create_power_bi_client()
    fabric_client = create_fabric_client()
    return get_power_bi_asset_details(power_bi_client, fabric_client, workspace_id, category, asset_id)