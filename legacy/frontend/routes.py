import json

from flask import Blueprint, redirect, render_template, url_for

from legacy.backend.normalization import normalize_selected_tables_payload
from legacy.backend.presets import load_preset
from shared.backend.routes import get_request_payload
from shared.frontend.routes import require_page_login


legacy_frontend = Blueprint(
    "legacy", __name__, template_folder="templates", static_folder="static",
    url_prefix="/legacy",
)
legacy_frontend.before_request(require_page_login)


@legacy_frontend.route("")
def home():
    return render_template("legacy/home.html")


@legacy_frontend.post("/selected-tables")
def selected_tables():
    selection = normalize_selected_tables_payload(get_request_payload())
    return render_template("legacy/selected_tables.html", selection=selection, can_create_new_preset=False, can_overwrite_preset=False)


@legacy_frontend.get("/selected-tables/<preset_id>")
def view_selected_tables_preset(preset_id):
    try:
        saved = load_preset(preset_id)
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError):
        return redirect(url_for("legacy.home"))

    selection = normalize_selected_tables_payload(saved)
    return render_template("legacy/selected_tables.html", selection=selection, can_create_new_preset=True, can_overwrite_preset=True)


@legacy_frontend.post("/saved-batches")
def saved_batches():
    selection = normalize_selected_tables_payload(get_request_payload())
    return render_template("legacy/selected_tables.html", selection=selection, can_create_new_preset=False, can_overwrite_preset=False)


@legacy_frontend.get("/saved-batches/<preset_id>")
def view_saved_batch(preset_id):
    return redirect(url_for("legacy.view_selected_tables_preset", preset_id=preset_id))
