"""Fabric REST and Azure DevOps clients for repository selection and cloning."""

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import time
from urllib.parse import quote
from uuid import UUID

import requests

from shared.backend.auth import get_browser_credential


FABRIC_WORKSPACES_URL = "https://api.fabric.microsoft.com/v1/workspaces"
FABRIC_GIT_CONNECTION_URL = (
    "https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/git/connection"
)
AZURE_DEVOPS_PROFILE_URL = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me"
AZURE_DEVOPS_ACCOUNTS_URL = "https://app.vssps.visualstudio.com/_apis/accounts"
AZURE_DEVOPS_REPOSITORIES_URL = (
    "https://dev.azure.com/{organization}/_apis/git/repositories"
)
AZURE_DEVOPS_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"


def azure_devops_client():
    token = get_browser_credential().get_token(AZURE_DEVOPS_SCOPE).token
    return AzureDevOpsClient(token)


class RepositoryCloneError(RuntimeError):
    def __init__(self, repository, message, exit_code=None):
        self.details = {
            "repository": repository["name"],
            "branch": repository["branchName"],
            "gitError": message,
        }
        exit_status = ""
        if exit_code is not None:
            self.details["gitExitCode"] = exit_code
            exit_status = f"Git exit code: {exit_code}. "
        super().__init__(
            "Could not clone the Azure DevOps repository. "
            f"Repository: {repository['name']}; branch: {repository['branchName']}. "
            f"{exit_status}{message}"
        )


class SemanticModelResolutionError(ValueError):
    def __init__(self, message, models):
        super().__init__(message)
        self.models = models


class FabricWorkspaceClient:
    def __init__(self, access_token):
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def list_workspaces(self):
        workspaces = []
        continuation_token = None
        seen_tokens = set()

        while True:
            params = {}
            if continuation_token:
                params["continuationToken"] = continuation_token

            response = requests.get(
                FABRIC_WORKSPACES_URL,
                headers=self.headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            workspaces.extend(payload.get("value", []))

            continuation_token = payload.get("continuationToken")
            if not continuation_token:
                break
            if continuation_token in seen_tokens:
                raise RuntimeError("Fabric returned a repeated workspace continuation token.")
            seen_tokens.add(continuation_token)

        return sorted(
            (
                {
                    "id": workspace.get("id", ""),
                    "displayName": workspace.get("displayName", ""),
                    "type": workspace.get("type", ""),
                }
                for workspace in workspaces
                if workspace.get("id") and workspace.get("displayName")
            ),
            key=lambda workspace: workspace["displayName"].casefold(),
        )

    def get_git_connection(self, workspace_id):
        response = requests.get(
            FABRIC_GIT_CONNECTION_URL.format(workspace_id=workspace_id),
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_semantic_model_definition(self, workspace_id, model_name, model_id=None):
        workspace_id = str(UUID(workspace_id))
        url = f"{FABRIC_WORKSPACES_URL}/{workspace_id}/semanticModels"
        models = []
        continuation_token = None
        seen_tokens = set()
        while True:
            params = {"continuationToken": continuation_token} if continuation_token else {}
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            models.extend(payload.get("value", []))
            continuation_token = payload.get("continuationToken")
            if not continuation_token:
                break
            if continuation_token in seen_tokens:
                raise RuntimeError("Fabric returned a repeated semantic model continuation token.")
            seen_tokens.add(continuation_token)
        available = sorted(
            [{"id": str(UUID(model["id"])), "name": model["displayName"]}
             for model in models],
            key=lambda model: (model["name"].casefold(), model["id"]),
        )
        if model_id is not None:
            model_id = str(UUID(model_id))
            matches = [model for model in available if model["id"] == model_id]
        else:
            matches = [model for model in available
                       if model["name"].strip().casefold() == model_name.strip().casefold()]
        if not matches:
            if not available:
                message = (
                    "No accessible semantic models were returned by this Fabric workspace. "
                    "Verify that the model is deployed here and that your account can access it, "
                    "or open a preset for the correct workspace."
                )
            elif model_id is not None:
                message = (
                    "The saved live semantic model is no longer available in this workspace. "
                    "Select its matching Fabric semantic model below."
                )
            else:
                message = (
                    f"The repository model '{model_name}' does not match a live semantic model name "
                    "in this workspace. Select its matching Fabric semantic model below."
                )
            raise SemanticModelResolutionError(message, available)
        if len(matches) != 1:
            raise SemanticModelResolutionError(
                f"Multiple Fabric semantic models match '{model_name}'. Select the correct model by ID below.",
                available,
            )
        model = matches[0]
        model_id = model["id"]
        response = requests.post(
            f"{url}/{model_id}/getDefinition", headers=self.headers,
            params={"format": "TMSL"}, timeout=30,
        )
        response.raise_for_status()
        if response.status_code == 202:
            operation_id = response.headers.get("x-ms-operation-id")
            if not operation_id:
                raise RuntimeError("Fabric did not return an operation ID for semantic model definition retrieval.")
            operation_url = f"https://api.fabric.microsoft.com/v1/operations/{UUID(operation_id)}"
            deadline = time.monotonic() + 180
            while True:
                delay = response.headers.get("Retry-After", "1")
                try:
                    delay = max(1, int(delay))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Fabric returned an invalid Retry-After value.") from exc
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    raise RuntimeError("Timed out waiting for the Fabric semantic model definition.")
                time.sleep(delay)
                response = requests.get(
                    operation_url, headers=self.headers, timeout=min(30, remaining - delay),
                )
                response.raise_for_status()
                state = response.json()
                status = state.get("status")
                if status == "Succeeded":
                    response = requests.get(
                        f"{operation_url}/result", headers=self.headers, timeout=30,
                    )
                    response.raise_for_status()
                    break
                if status in {"Failed", "Cancelled"}:
                    error = state.get("error", {})
                    raise RuntimeError(f"Fabric model definition retrieval {status.lower()}: {error}")
                if status not in {"NotStarted", "Running"}:
                    raise RuntimeError(f"Fabric returned an unexpected definition operation status: {status}.")
        return {
            "semanticModel": model,
            "definition": response.json().get("definition"),
        }


class AzureDevOpsClient:
    def __init__(self, access_token):
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def list_repositories(self):
        profile_response = requests.get(
            AZURE_DEVOPS_PROFILE_URL,
            headers=self.headers,
            params={"api-version": "7.1"},
            timeout=30,
        )
        profile_response.raise_for_status()
        member_id = profile_response.json().get("id")
        if not member_id:
            raise RuntimeError("Azure DevOps did not return an ID for the signed-in user.")

        accounts_response = requests.get(
            AZURE_DEVOPS_ACCOUNTS_URL,
            headers=self.headers,
            params={"memberId": member_id, "api-version": "7.1"},
            timeout=30,
        )
        accounts_response.raise_for_status()

        repositories = []
        for account in accounts_response.json().get("value", []):
            organization = account.get("accountName")
            if not organization:
                continue
            repositories.extend(self._list_organization_repositories(organization))

        return sorted(
            repositories,
            key=lambda repository: (
                repository["organization"].casefold(),
                repository["projectName"].casefold(),
                repository["name"].casefold(),
            ),
        )

    def list_semantic_models(self, repository):
        repository_url = (
            f"https://dev.azure.com/{quote(repository['organization'], safe='')}/"
            f"{quote(repository['projectName'], safe='')}/_apis/git/repositories/"
            f"{quote(repository.get('id') or repository['name'], safe='')}"
        )
        response = requests.get(
            repository_url,
            headers=self.headers,
            params={"api-version": "7.1"},
            timeout=30,
        )
        response.raise_for_status()
        details = response.json()
        branch = repository.get("branchName") or details.get("defaultBranch", "")
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/"):]
        resolved_repository = {
            **repository,
            "id": details["id"],
            "name": details["name"],
            "branchName": branch,
            "url": details.get("webUrl") or details.get("remoteUrl", ""),
        }
        if not branch:
            return {"repository": resolved_repository, "semanticModels": []}

        version_params = {
            "api-version": "7.1",
            "versionDescriptor.version": branch,
            "versionDescriptor.versionType": "branch",
        }
        items_response = requests.get(
            f"{repository_url}/items",
            headers=self.headers,
            params={**version_params, "scopePath": "/", "recursionLevel": "Full"},
            timeout=30,
        )
        items_response.raise_for_status()
        paths = {
            item["path"] for item in items_response.json().get("value", [])
            if item.get("path") and not item.get("isFolder")
        }
        model_folders = {
            str(PurePosixPath(path).parent) for path in paths
            if PurePosixPath(path).name.lower() in {"definition.pbism", "model.bim"}
        }
        models = []
        for folder in sorted(model_folders):
            folder_path = PurePosixPath(folder)
            name = folder_path.name or details["name"]
            for suffix in (".SemanticModel", ".Dataset"):
                if name.lower().endswith(suffix.lower()):
                    name = name[:-len(suffix)]
                    break

            platform_path = str(folder_path / ".platform")
            if platform_path in paths:
                platform_response = requests.get(
                    f"{repository_url}/items",
                    headers=self.headers,
                    params={
                        **version_params,
                        "path": platform_path,
                        "includeContent": "true",
                        "$format": "json",
                    },
                    timeout=30,
                )
                platform_response.raise_for_status()
                try:
                    platform = json.loads(platform_response.json()["content"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Could not read semantic model metadata at {platform_path}."
                    ) from exc
                metadata = platform.get("metadata") if isinstance(platform, dict) else None
                if not isinstance(metadata, dict):
                    raise RuntimeError(
                        f"Invalid semantic model metadata at {platform_path}."
                    )
                if metadata.get("type") != "SemanticModel":
                    continue
                name = metadata.get("displayName") or name

            models.append({"name": name, "path": folder})

        return {
            "repository": resolved_repository,
            "semanticModels": sorted(models, key=lambda model: (model["name"].casefold(), model["path"])),
        }

    def download_repository(self, repository, destination):
        url = (
            f"https://dev.azure.com/{quote(repository['organization'], safe='')}/"
            f"{quote(repository['projectName'], safe='')}/_git/"
            f"{quote(repository['name'], safe='')}"
        )
        environment = os.environ.copy()
        environment.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "http.https://dev.azure.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: {self.headers['Authorization']}",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
        })
        try:
            result = subprocess.run(
                [
                    "git", "-c", "core.longpaths=true", "clone",
                    "--single-branch", "--branch", repository["branchName"],
                    "--config", "core.longpaths=true", "--",
                    url, str(Path(destination).absolute()),
                ],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RepositoryCloneError(
                repository, "Git was not found. Install Git and add it to the server's PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RepositoryCloneError(
                repository, "Git clone timed out after 600 seconds. Try again."
            ) from exc
        except OSError as exc:
            raise RepositoryCloneError(
                repository, f"Could not start Git. {exc}"
            ) from exc
        if result.returncode:
            message = result.stderr.strip() or "Git exited without an error message."
            authorization = self.headers["Authorization"]
            for secret in (authorization, authorization.partition(" ")[2]):
                if secret:
                    message = message.replace(secret, "[redacted]")
            raise RepositoryCloneError(repository, message[:4000], result.returncode)

    def _list_organization_repositories(self, organization):
        repositories = []
        continuation_token = None
        seen_tokens = set()
        url = AZURE_DEVOPS_REPOSITORIES_URL.format(
            organization=quote(organization, safe="")
        )

        while True:
            params = {"api-version": "7.1"}
            if continuation_token:
                params["continuationToken"] = continuation_token

            response = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            for repository in response.json().get("value", []):
                project = repository.get("project") or {}
                if not repository.get("id") or not repository.get("name"):
                    continue
                repositories.append({
                    "id": repository["id"],
                    "name": repository["name"],
                    "organization": organization,
                    "projectId": project.get("id", ""),
                    "projectName": project.get("name", ""),
                    "url": repository.get("webUrl") or repository.get("remoteUrl", ""),
                })

            continuation_token = response.headers.get("x-ms-continuationtoken")
            if not continuation_token:
                break
            if continuation_token in seen_tokens:
                raise RuntimeError(
                    "Azure DevOps returned a repeated repository continuation token."
                )
            seen_tokens.add(continuation_token)

        return repositories
