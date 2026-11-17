"""Application service layer that wires authentication clients to UI workflows."""

from shared.backend.auth import get_fabric_access_token, get_power_bi_access_token
from legacy.backend.fabric_client import FabricClient
from legacy.backend.powerbi_client import PowerBIClient
from legacy.backend.semantic_model import get_advanced_table_metadata, get_power_bi_asset_details


def create_power_bi_client():
    """Build a Power BI client with a browser-authenticated access token."""
    return PowerBIClient(get_power_bi_access_token())


def create_fabric_client():
    """Build a Fabric client with a browser-authenticated access token."""
    return FabricClient(get_fabric_access_token())


def get_workspaces():
    """Return accessible workspaces only when the Legacy area requests them."""
    return create_power_bi_client().get_workspaces()


def get_workspace_assets(workspace_id):
    """Return semantic models and reports available in a selected workspace."""
    power_bi_client = create_power_bi_client()
    return power_bi_client.get_workspace_assets(workspace_id)


def get_asset_details(workspace_id, category, asset_id):
    """Return enriched report or semantic model details for the selected asset."""
    power_bi_client = create_power_bi_client()
    fabric_client = create_fabric_client()
    return get_power_bi_asset_details(power_bi_client, fabric_client, workspace_id, category, asset_id)


def get_advanced_table_details(workspace_id, model_id, selected_tables, all_tables=None):
    """Return advanced metadata for the currently selected semantic model tables."""
    power_bi_client = create_power_bi_client()
    fabric_client = create_fabric_client()
    return get_advanced_table_metadata(power_bi_client, fabric_client, workspace_id, model_id, selected_tables, all_tables)


def get_semantic_model_definition(workspace_id, model_id):
    """Return the selected semantic model definition in TMDL format."""
    return create_fabric_client().get_semantic_model_definition(workspace_id, model_id)