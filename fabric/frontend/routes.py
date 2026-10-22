import json

from flask import Blueprint, abort, current_app, render_template

from fabric.backend.lineage import load_mapping
from fabric.backend.model_lineage import GRAPH_VERSION
from fabric.backend.presets import load_preset
from shared.frontend.routes import require_page_login


fabric_frontend = Blueprint(
    "fabric", __name__, template_folder="templates", static_folder="static",
    url_prefix="/fabric",
)
fabric_frontend.before_request(require_page_login)


@fabric_frontend.get("")
def home():
    return render_template("fabric/home.html")


@fabric_frontend.get("/presets/<preset_id>")
def preset_page(preset_id):
    try:
        preset = load_preset(preset_id)
    except json.JSONDecodeError:
        current_app.logger.exception("Could not load Fabric preset %s", preset_id)
        abort(500, description="Could not load the Fabric preset.")
    except (FileNotFoundError, ValueError):
        abort(404, description="Fabric preset not found.")
    except OSError:
        current_app.logger.exception("Could not load Fabric preset %s", preset_id)
        abort(500, description="Could not load the Fabric preset.")
    try:
        mapping = load_mapping(preset_id)
    except FileNotFoundError:
        mapping = None
    except (json.JSONDecodeError, OSError, ValueError):
        current_app.logger.exception("Could not load Fabric mapping %s", preset_id)
        abort(500, description="Could not load the saved Fabric artifact mapping.")
    if mapping and (
        mapping.get("graph", {}).get("version") != GRAPH_VERSION
        or mapping.get("graph", {}).get("modelPath") != preset["semanticModel"]["path"]
    ):
        mapping = None
    local_repository_path = (
        preset.get("localRepositoryPath")
        or (mapping or {}).get("repositoryPath")
        or "Prepared during repository processing"
    )
    return render_template(
        "fabric/preset.html", preset=preset, local_repository_path=local_repository_path,
        mapping=mapping,
    )


@fabric_frontend.get("/presets/<preset_id>/impact")
def impact_page(preset_id):
    try:
        preset = load_preset(preset_id)
    except FileNotFoundError:
        abort(404, description="Fabric preset not found.")
    except (OSError, ValueError):
        current_app.logger.exception("Could not load Fabric impact preset %s", preset_id)
        abort(500, description="Could not load the Fabric preset.")
    return render_template("fabric/impact.html", preset=preset)