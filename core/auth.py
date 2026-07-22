"""Authentication helpers for browser-backed Power BI and Fabric API calls."""

import jwt
from azure.identity import InteractiveBrowserCredential


POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_browser_credential = None


def get_browser_credential():
    """Return one credential so acquired tokens are reused for later API calls."""
    global _browser_credential
    if _browser_credential is None:
        _browser_credential = InteractiveBrowserCredential()
    return _browser_credential


def get_power_bi_access_token():
    """Acquire an access token for Power BI using interactive browser login."""
    token = get_browser_credential().get_token(POWER_BI_SCOPE)
    return token.token


def get_fabric_access_token():
    """Acquire an access token for Fabric using interactive browser login."""
    token = get_browser_credential().get_token(FABRIC_SCOPE)
    return token.token


def build_user(access_token):
    """Convert access-token identity claims into the user shape expected by the UI."""
    claims = jwt.decode(access_token, options={"verify_signature": False, "verify_aud": False})
    email = claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name") or ""

    return {
        "name": claims.get("name") or email or "Microsoft user",
        "email": email,
        "tenantId": claims.get("tid", ""),
        "subscription": "",
    }