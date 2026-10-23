"""Configure the project-local cache for the SDK-managed Copilot runtime."""

import os
from importlib.metadata import version

from shared.paths import PROJECT_ROOT


def configure_copilot_runtime():
    cache_directory = PROJECT_ROOT / ".session" / "copilot-runtime" / version("github-copilot-sdk")
    os.environ["COPILOT_CLI_EXTRACT_DIR"] = str(cache_directory)
