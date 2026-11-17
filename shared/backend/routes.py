import json

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError, InteractiveBrowserCredential
from flask import Blueprint, jsonify, request, session
from werkzeug.exceptions import BadRequest

from shared.backend.auth import (
    POWER_BI_SCOPE,
    build_user,
    clear_browser_credential,
    has_browser_credential,
    store_browser_credential,
)


shared_backend = Blueprint("shared_api", __name__, url_prefix="/api")


def get_request_payload():
    if request.is_json:
        return request.get_json()
    try:
        return json.loads(request.form.get("payload", "{}"))
    except json.JSONDecodeError as exc:
        raise BadRequest("Invalid JSON payload.") from exc


def require_api_login():
    if not session.get("user") or not has_browser_credential():
        return jsonify({"message": "Sign in with Microsoft to continue."}), 401


@shared_backend.post("/login")
def login_with_browser():
    try:
        credential = InteractiveBrowserCredential()
        user = build_user(credential.get_token(POWER_BI_SCOPE).token)
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Browser authentication failed. Try signing in again. {exc}"}), 401
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    previous_credential_id = session.get("credential_id")
    credential_id = store_browser_credential(credential)
    session.clear()
    session["credential_id"] = credential_id
    session["user"] = user
    if previous_credential_id is not None:
        clear_browser_credential(previous_credential_id)
    return jsonify({"user": user})


@shared_backend.post("/logout")
def logout():
    clear_browser_credential()
    session.clear()
    return jsonify({"message": "Signed out."})
