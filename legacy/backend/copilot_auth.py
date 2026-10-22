"""GitHub Copilot authentication helpers for browser-backed UI flows."""

import asyncio
import shutil
import subprocess
import threading

from copilot import CopilotClient
from shared.paths import PROJECT_ROOT


_COPILOT_LOGIN_LOCK = threading.Lock()


async def _get_copilot_auth_status(client_factory):
    async with client_factory(
        use_logged_in_user=True,
        working_directory=str(PROJECT_ROOT),
    ) as client:
        return await client.get_auth_status()


def get_copilot_auth_status(client_factory=CopilotClient):
    """Return the current signed-in-user status from the Copilot runtime."""
    return asyncio.run(_get_copilot_auth_status(client_factory))


def _launch_copilot_login():
    executable = shutil.which("copilot")
    if not executable:
        raise RuntimeError(
            "GitHub Copilot CLI was not found. Install it or add the copilot command to PATH."
        )

    try:
        subprocess.run(
            [executable, "login"],
            cwd=PROJECT_ROOT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"GitHub Copilot browser authentication failed with exit code {exc.returncode}."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not start GitHub Copilot browser authentication: {exc}") from exc


def ensure_copilot_authenticated(
    client_factory=CopilotClient,
    login_runner=None,
):
    """Open Copilot browser login only when no reusable authentication exists."""
    with _COPILOT_LOGIN_LOCK:
        status = get_copilot_auth_status(client_factory)
        if status.isAuthenticated:
            return status

        (login_runner or _launch_copilot_login)()
        status = get_copilot_auth_status(client_factory)
        if not status.isAuthenticated:
            detail = str(status.statusMessage or "").strip()
            message = "GitHub Copilot authentication did not complete."
            raise RuntimeError(f"{message} {detail}".strip())

        return status
