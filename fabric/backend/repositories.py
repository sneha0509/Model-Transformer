"""Shared local repository checkouts for Fabric presets."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from threading import Lock
import time
from uuid import uuid4

from fabric.backend.clients import azure_devops_client
from fabric.backend.filesystem import filesystem_path
from fabric.backend.selection import normalize_repository
from shared.paths import PROJECT_ROOT


REPOS_DIR = PROJECT_ROOT / "repo"
_download_lock = Lock()
_ACCESS_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)


def _folder_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)[:80].rstrip(" .")
    if not name:
        name = "source"
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if name.split(".")[0].upper() in reserved:
        name = "_" + name
    return name


def _read_metadata(path):
    try:
        with path.open(encoding="utf-8") as file:
            metadata = json.load(file)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid local repository metadata at {path}.") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError(f"Invalid local repository metadata at {path}.")
    return metadata


def _write_metadata(path, metadata):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as file:
            temporary_path = Path(file.name)
            json.dump(metadata, file, indent=4)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _repository_directory(root, name, identity, repository_key):
    metadata = {"identity": identity}
    for directory in root.iterdir():
        if directory.is_dir() and not directory.is_symlink():
            if _read_metadata(directory / ".repository.json") == metadata:
                return directory
    name = _folder_name(name)
    for candidate in (root / name, root / f"{name}--{repository_key[:12]}"):
        if not candidate.exists():
            candidate.mkdir()
            _write_metadata(candidate / ".repository.json", metadata)
            return candidate
    raise RuntimeError(f"Local repository folder is already in use: {root / name}.")


def _branch_directory(directory, branch, branch_key):
    name = _folder_name(branch.replace("/", "__"))
    candidates = (directory / name, directory / f"{name}--{branch_key[:12]}")
    for candidate in candidates:
        if candidate.is_dir():
            metadata = _read_metadata(candidate / ".snapshot.json")
            if metadata is not None and metadata.get("branchName") == branch:
                return candidate
    for candidate in candidates:
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Local branch folder is already in use: {directory / name}.")


def _remove_readonly(function, path, exc):
    if isinstance(exc, PermissionError) and Path(path).is_file():
        mode = Path(path).stat().st_mode
        if not mode & stat.S_IWRITE:
            os.chmod(path, mode | stat.S_IWRITE)
            function(path)
            return
    raise exc


def _retry_access_denied(operation):
    for delay in _ACCESS_RETRY_DELAYS:
        try:
            return operation()
        except PermissionError:
            time.sleep(delay)
    return operation()


def _rename_directory(source, destination):
    _retry_access_denied(lambda: source.rename(destination))


def _remove_directory(path):
    _retry_access_denied(
        lambda: shutil.rmtree(path, onexc=_remove_readonly) if path.exists() else None
    )


def _recover_interrupted_refresh(path, display_path):
    backups = [
        candidate for candidate in path.parent.glob(".previous-*")
        if candidate.is_dir() and not candidate.is_symlink()
    ]
    staging_directories = [
        candidate for candidate in path.parent.glob(".refresh-*")
        if candidate.is_dir() and not candidate.is_symlink()
    ]
    if path.exists():
        for temporary in backups + staging_directories:
            _remove_directory(temporary)
        return
    if len(backups) > 1:
        raise RuntimeError(
            f"Multiple interrupted repository refreshes exist beside {display_path}. "
            "Restore the required .previous-* folder as code before trying again."
        )
    if backups:
        _rename_directory(backups[0], path)
    for staging in staging_directories:
        _remove_directory(staging)


def _refresh_repository(repository, path, display_path):
    suffix = uuid4().hex
    staging = path.parent / f".refresh-{suffix}"
    backup = path.parent / f".previous-{suffix}"
    staging_display = display_path.parent / staging.name
    staging.mkdir()
    downloaded = False
    try:
        azure_devops_client().download_repository(repository, staging_display)
        downloaded = True
    finally:
        if not downloaded and staging.exists():
            _remove_directory(staging)

    _rename_directory(path, backup)
    replaced = False
    try:
        _rename_directory(staging, path)
        replaced = True
    finally:
        if not replaced and backup.exists() and not path.exists():
            _rename_directory(backup, path)
        if not replaced and staging.exists() and path.exists():
            _remove_directory(staging)
    _remove_directory(backup)


def ensure_repository(repository, refresh=False):
    repository = normalize_repository(repository)
    if not repository["id"] or not repository["branchName"]:
        raise ValueError("Select a repository with a branch before downloading it.")
    branch = repository["branchName"].removeprefix("refs/heads/")
    repository = {**repository, "branchName": branch}
    identity = [
        repository["organization"].casefold(),
        repository["projectName"].casefold(),
        repository["id"].casefold(),
    ]
    repository_key = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()[:32]
    branch_key = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:32]
    with _download_lock:
        root = filesystem_path(REPOS_DIR)
        legacy_path = root / repository_key / branch_key
        if legacy_path.is_dir():
            if refresh:
                _refresh_repository(
                    repository, legacy_path, REPOS_DIR / repository_key / branch_key
                )
            return REPOS_DIR / repository_key / branch_key
        root.mkdir(parents=True, exist_ok=True)
        directory = _repository_directory(root, repository["name"], identity, repository_key)
        snapshot = _branch_directory(directory, branch, branch_key)
        path = snapshot / "code"
        display_path = REPOS_DIR / directory.name / snapshot.name / "code"
        _recover_interrupted_refresh(path, display_path)
        metadata = {"branchName": branch}
        if path.exists():
            if path.is_dir() and _read_metadata(snapshot / ".snapshot.json") == metadata:
                if refresh:
                    _refresh_repository(repository, path, display_path)
                return display_path
            raise RuntimeError(
                f"An incomplete Git clone exists at {display_path}. "
                "If no clone is running, remove only that incomplete code folder and try again."
            )
        client = azure_devops_client()
        snapshot.mkdir(exist_ok=True)
        _write_metadata(snapshot / ".snapshot.json", {**metadata, "complete": False})
        path.mkdir()
        completed = False
        try:
            client.download_repository(repository, display_path)
            _write_metadata(snapshot / ".snapshot.json", metadata)
            completed = True
        finally:
            if not completed and path.exists():
                _remove_directory(path)
    return display_path
