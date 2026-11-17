"""Fabric-only semantic model preset persistence."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import tempfile
from threading import Lock
from uuid import UUID

from fabric.backend.repositories import ensure_repository
from fabric.backend.selection import normalize_repository, required_text
from shared.paths import PROJECT_ROOT


PRESETS_DIR = PROJECT_ROOT / "presets" / "fabric"
_save_lock = Lock()


def _preset_path(preset_id):
    if not isinstance(preset_id, str) or not re.fullmatch(r"[a-f0-9]{64}", preset_id):
        raise ValueError("Invalid Fabric preset ID.")
    return PRESETS_DIR / f"{preset_id}.json"


def load_preset(preset_id):
    with _preset_path(preset_id).open(encoding="utf-8") as file:
        saved = json.load(file)
    if not isinstance(saved, dict) or saved.get("presetId") != preset_id:
        raise ValueError(f"Invalid Fabric preset data: {preset_id}.")
    return saved


def list_presets():
    with _save_lock:
        if not PRESETS_DIR.exists():
            return []
        paths = sorted(
            PRESETS_DIR.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [load_preset(path.stem) for path in paths]


def delete_preset(preset_id):
    with _save_lock:
        _preset_path(preset_id).unlink()


def bind_live_semantic_model(preset_id, model):
    binding = {"id": str(UUID(required_text(model, "id"))), "name": required_text(model, "name")}
    with _save_lock:
        preset = load_preset(preset_id)
        preset["liveSemanticModel"] = binding
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=PRESETS_DIR, suffix=".tmp", delete=False,
            ) as file:
                temporary_path = file.name
                json.dump(preset, file, indent=4)
            os.replace(temporary_path, _preset_path(preset_id))
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)
    return preset


def save_preset(payload):
    if not isinstance(payload, dict):
        raise ValueError("Fabric preset selection is required.")
    workspace = payload.get("workspace")
    model = payload.get("semanticModel")
    if not isinstance(workspace, dict) or not isinstance(model, dict):
        raise ValueError("Select a workspace and one semantic model.")
    workspace = {
        "id": str(UUID(required_text(workspace, "id"))),
        "displayName": required_text(workspace, "displayName"),
    }
    repository = normalize_repository(payload.get("repository"))
    if not repository["id"] or not repository["branchName"]:
        raise ValueError("Select a repository with a branch before creating a preset.")
    model_path = required_text(model, "path")
    if not model_path.startswith("/") or ".." in PurePosixPath(model_path).parts:
        raise ValueError("Invalid semantic model repository path.")
    model = {"name": required_text(model, "name"), "path": model_path}
    local_repository_path = str(ensure_repository(repository))
    identity = [
        workspace["id"],
        repository["organization"].casefold(),
        repository["projectName"].casefold(),
        repository["id"].casefold(),
        repository["branchName"],
        model["path"],
    ]
    preset_id = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
    saved = {
        "presetId": preset_id,
        "presetName": model["name"],
        "workspace": workspace,
        "repository": repository,
        "semanticModel": model,
        "localRepositoryPath": local_repository_path,
        "savedAt": datetime.now(timezone.utc).isoformat(),
    }

    with _save_lock:
        path = _preset_path(preset_id)
        if path.exists():
            return load_preset(preset_id)
        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=PRESETS_DIR, suffix=".tmp", delete=False
            ) as file:
                temporary_path = file.name
                json.dump(saved, file, indent=4)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)
    return saved
