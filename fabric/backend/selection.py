"""Validation shared by Fabric preset and repository selection."""


def required_text(payload, field):
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required.")
    return value.strip()


def normalize_repository(payload):
    if not isinstance(payload, dict):
        raise ValueError("Repository selection is required.")
    repository = {
        field: required_text(payload, field)
        for field in ("name", "organization", "projectName")
    }
    for field in ("id", "branchName", "directoryName", "url"):
        value = payload.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"Invalid repository {field}.")
        repository[field] = value.strip()
    return repository
