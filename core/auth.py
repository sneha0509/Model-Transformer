"""Authentication helpers for Azure CLI-backed Power BI and Fabric API calls."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from azure.identity import AzureCliCredential


POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
AZURE_CLI_PATHS = (
    Path("C:/Program Files/Microsoft SDKs/Azure/CLI2/wbin/az.cmd"),
    Path("C:/Program Files/Microsoft SDKs/Azure/CLI2/wbin/az"),
    Path("C:/Program Files (x86)/Microsoft SDKs/Azure/CLI2/wbin/az.cmd"),
    Path("C:/Program Files (x86)/Microsoft SDKs/Azure/CLI2/wbin/az"),
)


def resolve_azure_cli_command():
    """Find an Azure CLI executable and make its folder available to child processes."""
    path_command = shutil.which("az") or shutil.which("az.cmd")
    candidates = [Path(path_command)] if path_command else []
    candidates.extend(AZURE_CLI_PATHS)

    for candidate in candidates:
        if candidate.exists():
            cli_folder = str(candidate.parent)
            path_parts = os.environ.get("PATH", "").split(os.pathsep)
            if cli_folder.lower() not in {part.lower() for part in path_parts if part}:
                os.environ["PATH"] = os.pathsep.join([cli_folder, os.environ.get("PATH", "")])
            return str(candidate)

    return None


def get_azure_cli_account():
    """Return the active Azure CLI account, raising a user-facing error if unavailable."""
    az_command = resolve_azure_cli_command()
    if not az_command:
        checked_paths = ", ".join(str(path) for path in AZURE_CLI_PATHS)
        raise RuntimeError(f"Azure CLI was not found. Checked PATH and these locations: {checked_paths}")

    try:
        result = subprocess.run(
            [az_command, "account", "show", "--output", "json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Azure CLI was not found. Install Azure CLI and run az login.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Azure CLI did not respond. Confirm az login works in this terminal.") from exc

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Run az login, then try again."
        raise RuntimeError(f"Azure CLI is not signed in. {message}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Azure CLI returned an unreadable account response.") from exc


def get_power_bi_access_token():
    """Acquire an access token for the Power BI REST API using Azure CLI credentials."""
    if not resolve_azure_cli_command():
        raise RuntimeError("Azure CLI was not found. Install Azure CLI and run az login.")

    credential = AzureCliCredential(process_timeout=15)
    token = credential.get_token(POWER_BI_SCOPE)
    return token.token


def get_fabric_access_token():
    """Acquire an access token for the Fabric REST API using Azure CLI credentials."""
    if not resolve_azure_cli_command():
        raise RuntimeError("Azure CLI was not found. Install Azure CLI and run az login.")

    credential = AzureCliCredential(process_timeout=15)
    token = credential.get_token(FABRIC_SCOPE)
    return token.token


def build_user(account):
    """Convert an Azure CLI account response into the user shape expected by the UI."""
    account_user = account.get("user") or {}
    name = account_user.get("name") or account.get("name") or "Azure CLI user"

    return {
        "name": name,
        "email": account_user.get("name", ""),
        "tenantId": account.get("tenantId", ""),
        "subscription": account.get("name", ""),
    }