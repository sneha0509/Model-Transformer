"""Authentication helpers for browser-backed Power BI and Fabric API calls."""

from contextlib import contextmanager
from contextvars import ContextVar
import secrets
from threading import RLock

import jwt
from azure.identity import InteractiveBrowserCredential
from flask import has_request_context, session


POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_browser_credentials = {}
_browser_credentials_lock = RLock()
_active_credential_id = ContextVar("active_browser_credential_id", default=None)
_standalone_credential = ContextVar("standalone_browser_credential", default=None)


def get_browser_credential():
    """Return the credential bound to the current session or execution context."""
    credential_id = _active_credential_id.get()
    if credential_id is None and has_request_context():
        credential_id = session.get("credential_id")

    if credential_id is not None:
        with _browser_credentials_lock:
            credential = _browser_credentials.get(credential_id)
        if credential is None:
            raise RuntimeError("Browser authentication is unavailable. Sign in again.")
        return credential

    if has_request_context():
        raise RuntimeError("Browser authentication is unavailable. Sign in again.")

    credential = _standalone_credential.get()
    if credential is None:
        credential = InteractiveBrowserCredential()
        _standalone_credential.set(credential)
    return credential


def store_browser_credential(credential):
    credential_id = secrets.token_urlsafe(32)
    with _browser_credentials_lock:
        _browser_credentials[credential_id] = credential
    return credential_id


def get_browser_credential_id():
    credential_id = _active_credential_id.get()
    if credential_id is None and has_request_context():
        credential_id = session.get("credential_id")
    if credential_id is None:
        raise RuntimeError("Browser authentication is unavailable. Sign in again.")
    return credential_id


def has_browser_credential():
    try:
        get_browser_credential()
    except RuntimeError:
        return False
    return True


def clear_browser_credential(credential_id=None):
    """Forget only the credential bound to the current session."""
    if credential_id is None:
        credential_id = _active_credential_id.get()
        if credential_id is None and has_request_context():
            credential_id = session.get("credential_id")
    if credential_id is not None:
        with _browser_credentials_lock:
            _browser_credentials.pop(credential_id, None)


@contextmanager
def use_browser_credential(credential_id):
    token = _active_credential_id.set(credential_id)
    try:
        yield
    finally:
        _active_credential_id.reset(token)


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