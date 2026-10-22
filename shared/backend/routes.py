import json

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from flask import Blueprint, jsonify, request, session
from werkzeug.exceptions import BadRequest

from shared.backend.auth import build_user, clear_browser_credential, get_power_bi_access_token


shared_backend = Blueprint("shared_api", __name__, url_prefix="/api")


def get_request_payload():
    if request.is_json:
        return request.get_json()
    try:
        return json.loads(request.form.get("payload", "{}"))
    except json.JSONDecodeError as exc:
        raise BadRequest("Invalid JSON payload.") from exc


def require_api_login():
    if not session.get("user"):
        return jsonify({"message": "Sign in with Microsoft to continue."}), 401


@shared_backend.post("/login")
def login_with_browser():
    try:
        user = build_user(get_power_bi_access_token())
    except (ClientAuthenticationError, CredentialUnavailableError) as exc:
        return jsonify({"message": f"Browser authentication failed. Try signing in again. {exc}"}), 401
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 401

    session.clear()
    session["user"] = user
    return jsonify({"user": user})


@shared_backend.post("/logout")
def logout():
    session.clear()
    clear_browser_credential()
    return jsonify({"message": "Signed out."})