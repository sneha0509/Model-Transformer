"""Filesystem paths for repository snapshots, including long Windows paths."""

import os
from pathlib import Path


def filesystem_path(path):
    path = Path(path)
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)
