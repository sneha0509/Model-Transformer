"""Persistence helpers for table-selection and batch preset JSON files."""

import json
import re
from pathlib import Path

from core.normalization import normalize_saved_batches_payload


PRESETS_DIR = Path("presets")
PRESET_ID_PATTERN = re.compile(r"[\w.-]+")


def validate_preset_id(preset_id):
    """Reject preset IDs that could escape the local presets directory."""
    if not PRESET_ID_PATTERN.fullmatch(preset_id or ""):
        raise ValueError("Invalid preset ID.")


def get_preset_path(preset_id):
    """Resolve a validated preset ID to its JSON file path."""
    validate_preset_id(preset_id)
    return PRESETS_DIR / f"{preset_id}.json"


def load_preset(preset_id):
    """Load a saved preset and add its file-derived ID to the payload."""
    preset_path = get_preset_path(preset_id)
    if not preset_path.exists():
        raise FileNotFoundError("Preset not found.")

    with open(preset_path, encoding="utf-8") as file:
        saved = json.load(file)
    saved["presetId"] = preset_id
    return saved


def list_presets():
    """Return preset summaries sorted by most recently modified first."""
    presets = []
    if not PRESETS_DIR.exists():
        return presets

    files = sorted(PRESETS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for file_path in files:
        try:
            with open(file_path, encoding="utf-8") as file:
                data = json.load(file)
            preset_name = data.get("presetName") or data.get("modelName") or "Unnamed preset"
            presets.append({
                "id": file_path.stem,
                "modelName": preset_name,
                "presetName": preset_name,
                "workspaceName": data.get("workspaceName", "Unavailable"),
                "savedAt": data.get("savedAt", ""),
                "batchCount": data.get("batchCount", 0),
                "assignedTableCount": data.get("assignedTableCount", 0),
            })
        except Exception:
            # Skip malformed preset files so one bad file does not break the picker.
            pass
    return presets


def save_preset(payload):
    """Normalize and save a new preset, choosing a unique file name if needed."""
    saved = normalize_saved_batches_payload(payload)
    preset_id_base = re.sub(r"[^\w]+", "_", saved.get("presetName") or saved.get("modelName") or "preset").strip("_") or "preset"
    preset_id = preset_id_base

    PRESETS_DIR.mkdir(exist_ok=True)
    preset_path = PRESETS_DIR / f"{preset_id}.json"
    suffix = 2
    while preset_path.exists():
        # Preserve existing presets by suffixing duplicate names instead of overwriting.
        preset_id = f"{preset_id_base}_{suffix}"
        preset_path = PRESETS_DIR / f"{preset_id}.json"
        suffix += 1

    saved["presetId"] = preset_id
    with open(preset_path, "w", encoding="utf-8") as file:
        json.dump(saved, file, indent=4)
    return preset_id


def update_preset(preset_id, payload):
    """Replace an existing preset with normalized payload data."""
    preset_path = get_preset_path(preset_id)
    if not preset_path.exists():
        raise FileNotFoundError("Preset not found.")

    payload = payload if isinstance(payload, dict) else {}
    payload["presetId"] = preset_id
    saved = normalize_saved_batches_payload(payload)
    saved["presetId"] = preset_id

    with open(preset_path, "w", encoding="utf-8") as file:
        json.dump(saved, file, indent=4)
    return preset_id


def delete_preset(preset_id):
    """Delete a saved preset by ID."""
    preset_path = get_preset_path(preset_id)
    if not preset_path.exists():
        raise FileNotFoundError("Preset not found.")

    preset_path.unlink()