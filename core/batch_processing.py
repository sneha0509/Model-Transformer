import json
import sys
import threading
import time
import requests
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .auth import build_user, get_fabric_access_token, get_power_bi_access_token
else:
    from auth import build_user, get_fabric_access_token, get_power_bi_access_token


# ===== Configuration =====
PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"
POLL_INTERVAL_SECONDS = 30
DEFAULT_REFRESH_TIMEOUT_SECONDS = 4 * 60 * 60
FAILURE_DIR = Path(__file__).resolve().parent / "failure"

POWER_BI_API_ROOT = "https://api.powerbi.com/v1.0/myorg"
FABRIC_API_ROOT = "https://api.fabric.microsoft.com/v1"


class RefreshCancelled(Exception):
    """Raised when a user cancels an active batch refresh job."""


def resolve_preset_path(preset_arg=None):
    if preset_arg:
        preset_path = Path(preset_arg)
        if not preset_path.is_absolute():
            preset_path = PRESETS_DIR / preset_path
        if preset_path.suffix.lower() != ".json":
            preset_path = preset_path.with_suffix(".json")
        if not preset_path.exists():
            raise FileNotFoundError(f"Preset JSON not found: {preset_path}")
        return preset_path

    preset_files = sorted(PRESETS_DIR.glob("*.json"))
    if not preset_files:
        raise FileNotFoundError(f"No preset JSON files found in {PRESETS_DIR}")
    if len(preset_files) > 1:
        names = ", ".join(preset.name for preset in preset_files)
        raise ValueError(f"Multiple preset JSON files found. Pass one filename as an argument: {names}")
    return preset_files[0]


def require_text(preset, key):
    value = preset.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Preset is missing required text field: {key}")
    return value.strip()


def positive_int_setting(settings, key, default_value):
    value = int(settings.get(key, default_value))
    if value < 1:
        raise ValueError(f"batchCreationSettings.{key} must be greater than 0")
    return value


def timeout_text(timeout_minutes):
    total_seconds = timeout_minutes * 60
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def load_refresh_plan(preset_arg=None):
    preset_path = resolve_preset_path(preset_arg)
    with preset_path.open("r", encoding="utf-8") as preset_file:
        preset = json.load(preset_file)

    workspace_id = require_text(preset, "workspaceId")
    model_id = require_text(preset, "modelId")
    workspace_name = preset.get("workspaceName") or workspace_id
    model_name = preset.get("modelName") or model_id
    batch_settings = preset.get("batchCreationSettings") or {}
    batches = preset.get("batches")

    if not isinstance(batch_settings, dict):
        raise ValueError("Preset field 'batchCreationSettings' must be an object")
    if not isinstance(batches, list) or not batches:
        raise ValueError("Preset field 'batches' must be a non-empty list")

    refresh_batches = []
    for index, batch in enumerate(batches, start=1):
        if not isinstance(batch, dict):
            raise ValueError(f"Batch {index} must be an object")

        tables = batch.get("tables")
        if not isinstance(tables, list) or not tables:
            raise ValueError(f"Batch {index} must contain a non-empty 'tables' list")
        if any(not isinstance(table, str) or not table.strip() for table in tables):
            raise ValueError(f"Batch {index} contains an invalid table name")

        refresh_batches.append({
            "batch_number": batch.get("name") or index,
            "tables": [table.strip() for table in tables],
        })

    return {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "model_id": model_id,
        "model_name": model_name,
        "batch_settings": batch_settings,
        "batches": refresh_batches,
    }

def api_request(method, url, body=None):
    if url.startswith(POWER_BI_API_ROOT):
        access_token = get_power_bi_access_token()
    elif url.startswith(FABRIC_API_ROOT):
        access_token = get_fabric_access_token()
    else:
        raise ValueError(f"Unsupported API URL: {url}")

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    method = method.upper()
    request_kwargs = {"params": body} if method == "GET" else {"json": body}
    r = requests.request(method, url, headers=headers, timeout=60, **request_kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {url} failed: {r.status_code}\n{r.text}")

    return {
        "status": r.status_code,
        "headers": r.headers,
        "json": r.json() if r.content else None,
    }

def start_table_refresh(workspace_id, model_id, tables, batch_settings):
    url = f"{POWER_BI_API_ROOT}/groups/{workspace_id}/datasets/{model_id}/refreshes"
    timeout_minutes = positive_int_setting(batch_settings, "timeoutMinutes", DEFAULT_REFRESH_TIMEOUT_SECONDS // 60)
    body = {
        "type": "Full",
        "commitMode": batch_settings.get("commitMode", "transactional"),
        "retryCount": positive_int_setting(batch_settings, "retryCount", 2),
        "maxParallelism": positive_int_setting(batch_settings, "maxParallelism", min(len(tables), 4)),
        "timeout": timeout_text(timeout_minutes),
        "objects": [{"table": table} for table in tables]
    }
    ##response= GET https://api.powerbi.com/v1.0/myorg/groups/{groupId}/datasets
    response = api_request("POST", url, body)
    location = response["headers"].get("Location")

    if location:
        return location.rstrip("/").split("/")[-1]

    request_id = response["headers"].get("x-ms-request-id")
    if request_id:
        return request_id

    latest_refresh_url = f"{url}?$top=1"
    latest_refreshes = api_request("GET", latest_refresh_url)["json"].get("value", [])
    if latest_refreshes:
        return latest_refreshes[0].get("requestId")

    raise RuntimeError("Refresh started, but Power BI did not return a refresh id.")


def get_refresh_status(workspace_id, dataset_id, refresh_id):
    url = f"{POWER_BI_API_ROOT}/groups/{workspace_id}/datasets/{dataset_id}/refreshes/{refresh_id}"
    return api_request("GET", url)["json"]


def cancel_refresh(workspace_id, dataset_id, refresh_id):
    url = f"{POWER_BI_API_ROOT}/groups/{workspace_id}/datasets/{dataset_id}/refreshes/{refresh_id}"
    api_request("DELETE", url)


def wait_for_refresh(workspace_id, model_id, refresh_id, timeout_seconds, cancel_event=None):
    started_at = time.monotonic()
    last_status = None

    while True:
        if cancel_event and cancel_event.is_set():
            raise RefreshCancelled("Refresh cancelled by user")

        refresh = get_refresh_status(workspace_id, model_id, refresh_id)
        status = refresh.get("status", "Unknown")

        if status != last_status:
            print(f"   Status: {status}")
            last_status = status

        if status == "Completed":
            return

        if status in {"Failed", "Cancelled", "Disabled"}:
            raise RuntimeError(json.dumps(refresh, indent=2))
#monotonic is like a cuurent timestamp which is used to find the net time taken table to get refresh if it more than the thresold time then it will throw timeout error
        if time.monotonic() - started_at > timeout_seconds:
            raise TimeoutError(f"Refresh timed out after {timeout_seconds} seconds")

        if cancel_event:
            cancel_event.wait(POLL_INTERVAL_SECONDS)
        else:
            time.sleep(POLL_INTERVAL_SECONDS)


class BatchRefreshManager:
    """Run one background refresh job at a time and expose status updates to the UI."""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}

    def start(self, preset_path):
        refresh_plan = load_refresh_plan(preset_path)
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "Queued",
            "message": "Waiting to start refresh.",
            "currentRefreshId": None,
            "cancelEvent": threading.Event(),
            "batches": [
                {
                    "name": str(batch["batch_number"]),
                    "status": "Pending",
                    "tables": [{"name": table, "status": "Pending"} for table in batch["tables"]],
                }
                for batch in refresh_plan["batches"]
            ],
        }
        with self._lock:
            self._jobs[job_id] = job

        threading.Thread(
            target=self._run,
            args=(job_id, refresh_plan),
            name=f"batch-refresh-{job_id[:8]}",
            daemon=True,
        ).start()
        return self.get(job_id)

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return {
                key: deepcopy(value)
                for key, value in job.items()
                if key not in {"cancelEvent", "currentRefreshId"}
            }

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job["status"] in {"Completed", "Failed", "Cancelled"}:
                return self.get(job_id)
            job["cancelEvent"].set()
            job["status"] = "Cancelling"
            job["message"] = "Cancelling the active refresh and remaining batches."
            refresh_id = job["currentRefreshId"]

        if refresh_id:
            try:
                cancel_refresh(job["workspaceId"], job["modelId"], refresh_id)
            except Exception as exc:
                with self._lock:
                    job["message"] = f"Cancellation requested. Power BI response: {exc}"
        return self.get(job_id)

    def _set_batch_status(self, job, batch_index, status):
        batch = job["batches"][batch_index]
        batch["status"] = status
        for table in batch["tables"]:
            table["status"] = status

    def _cancel_unfinished(self, job):
        for batch in job["batches"]:
            if batch["status"] in {"Pending", "Running"}:
                batch["status"] = "Cancelled"
                for table in batch["tables"]:
                    if table["status"] in {"Pending", "Running"}:
                        table["status"] = "Cancelled"

    def _run(self, job_id, refresh_plan):
        with self._lock:
            job = self._jobs[job_id]
            job["workspaceId"] = refresh_plan["workspace_id"]
            job["modelId"] = refresh_plan["model_id"]
            job["status"] = "Running"
            job["message"] = "Starting the first batch."

        timeout_seconds = positive_int_setting(
            refresh_plan["batch_settings"],
            "timeoutMinutes",
            DEFAULT_REFRESH_TIMEOUT_SECONDS // 60,
        ) * 60

        try:
            for batch_index, batch in enumerate(refresh_plan["batches"]):
                with self._lock:
                    if job["cancelEvent"].is_set():
                        raise RefreshCancelled("Refresh cancelled by user")
                    self._set_batch_status(job, batch_index, "Running")
                    job["message"] = f"Running batch {batch_index + 1} of {len(refresh_plan['batches'])}."

                refresh_id = start_table_refresh(
                    refresh_plan["workspace_id"],
                    refresh_plan["model_id"],
                    batch["tables"],
                    refresh_plan["batch_settings"],
                )
                with self._lock:
                    job["currentRefreshId"] = refresh_id

                wait_for_refresh(
                    refresh_plan["workspace_id"],
                    refresh_plan["model_id"],
                    refresh_id,
                    timeout_seconds,
                    job["cancelEvent"],
                )
                with self._lock:
                    job["currentRefreshId"] = None
                    self._set_batch_status(job, batch_index, "Completed")

            with self._lock:
                job["status"] = "Completed"
                job["message"] = "All batches completed successfully."
        except RefreshCancelled:
            with self._lock:
                job["currentRefreshId"] = None
                self._cancel_unfinished(job)
                job["status"] = "Cancelled"
                job["message"] = "The refresh was cancelled."
        except Exception as exc:
            with self._lock:
                job["currentRefreshId"] = None
                running_index = next(
                    (index for index, batch in enumerate(job["batches"]) if batch["status"] == "Running"),
                    None,
                )
                if running_index is not None:
                    self._set_batch_status(job, running_index, "Failed")
                job["status"] = "Failed"
                job["message"] = str(exc)


batch_refresh_manager = BatchRefreshManager()


def save_failure(workspace_name, model_name, workspace_id, model_id, batch_number, tables, exc):
    FAILURE_DIR.mkdir(exist_ok=True)
    created_at = datetime.now(timezone.utc)
    timestamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
    failure_path = FAILURE_DIR / f"batch_{batch_number}_{timestamp}_{uuid.uuid4().hex}.json"
    failure_details = {
        "workspaceName": workspace_name,
        "workspaceId": workspace_id,
        "modelName": model_name,
        "modelId": model_id,
        "batchNumber": batch_number,
        "tables": tables,
        "error": str(exc),
    }

    with failure_path.open("x", encoding="utf-8") as failure_file:
        json.dump(failure_details, failure_file, indent=2)
        failure_file.write("\n")

    return failure_path


def run_batches():
    access_token = get_power_bi_access_token()
    authenticated_user = build_user(access_token)
    refresh_plan = load_refresh_plan(sys.argv[1] if len(sys.argv) > 1 else None)
    failed_batches = []
    workspace_id = refresh_plan["workspace_id"]
    workspace_name = refresh_plan["workspace_name"]
    model_id = refresh_plan["model_id"]
    model_name = refresh_plan["model_name"]
    batch_settings = refresh_plan["batch_settings"]
    timeout_seconds = positive_int_setting(batch_settings, "timeoutMinutes", DEFAULT_REFRESH_TIMEOUT_SECONDS // 60) * 60

    print(f"Signed in as: {authenticated_user['name']} ({authenticated_user['email']})")
    print(f"Preset: {refresh_plan['preset_path']}")
    print(f"Workspace: {workspace_name} ({workspace_id}) | Model: {model_name} ({model_id})")

    for batch in refresh_plan["batches"]:
        batch_number = batch["batch_number"]
        tables = batch["tables"]
        print(f"Batch {batch_number} starting: {tables}")
        try:
            refresh_id = start_table_refresh(workspace_id, model_id, tables, batch_settings)
            print(f"   Refresh id: {refresh_id}")
            wait_for_refresh(workspace_id, model_id, refresh_id, timeout_seconds)
            print(f"Batch {batch_number} completed\n")
        except Exception as e:
            failure_path = save_failure(workspace_name, model_name, workspace_id, model_id, batch_number, tables, e)
            print(f"Batch {batch_number} failed.\n Table Name: {tables} Failure saved to: {failure_path}\n")
            failed_batches.append(batch)

    if failed_batches:
        print(f"\n{len(failed_batches)} batch(es) failed")
if __name__ == "__main__":
    run_batches()
