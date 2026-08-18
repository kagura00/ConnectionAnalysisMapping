"""Versioning and migrations for persistent workspace data.

The workspace registry is mutable application data.  It is intentionally
versioned separately from the immutable Graph Contract and bundle schemas:
registry migrations may add defaults or rename storage metadata, while an
incompatible analysis graph must be regenerated instead of being guessed at.
"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKSPACE_FORMAT = "connection-analysis-workspace"
WORKSPACE_SCHEMA_VERSION = "1.1"
LEGACY_WORKSPACE_SCHEMA_VERSION = "1.0"


class DataSchemaError(ValueError):
    """Raised when persistent workspace data cannot be migrated safely."""


def _migration_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalised_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def migrate_workspace_registry(payload: Any) -> tuple[dict[str, Any], str | None]:
    """Return a current registry payload and the source version if migrated.

    Version 1.0 was the first central-workspace format.  It did not require
    layout and validation metadata in every repository record, even though
    newer readers need those fields to safely scope persistent data.  The
    migration derives deterministic current paths and neutral defaults.
    ``Workspace`` moves an existing legacy data directory to the derived path
    before it writes the migrated registry, so analysis and configuration data
    remain available after the upgrade.
    """

    if not isinstance(payload, dict) or payload.get("format") != WORKSPACE_FORMAT:
        raise DataSchemaError("workspace registry format is invalid")
    version = payload.get("schema_version", LEGACY_WORKSPACE_SCHEMA_VERSION)
    if version == WORKSPACE_SCHEMA_VERSION:
        return copy.deepcopy(payload), None
    if version != LEGACY_WORKSPACE_SCHEMA_VERSION:
        raise DataSchemaError(f"workspace registry schema_version is unsupported: {version!r}")

    repositories = payload.get("repositories")
    if not isinstance(repositories, list):
        raise DataSchemaError("workspace registry repositories must be an array")
    migrated = copy.deepcopy(payload)
    migration_time = _migration_timestamp()
    migrated_repositories: list[dict[str, Any]] = []
    for original in repositories:
        if not isinstance(original, dict):
            raise DataSchemaError("workspace registry repository entry must be an object")
        record = copy.deepcopy(original)
        repository_id = record.get("repository_id")
        if not isinstance(repository_id, str) or not repository_id:
            raise DataSchemaError("legacy repository entry has no repository_id")
        legacy_data_path = record.get("data_path") or f"repositories/{repository_id}"
        if not isinstance(legacy_data_path, str) or not legacy_data_path:
            raise DataSchemaError(f"legacy repository entry has an invalid data_path: {legacy_data_path!r}")
        data_path = f"repositories/{repository_id}"
        record["data_path"] = data_path
        for field, default_name in (
            ("analysis_path", "analysis.json"),
            ("bundle_path", "bundle"),
            ("config_path", "config.toml"),
            ("layout_path", "layout.json"),
        ):
            legacy_path = record.get(field) or f"{legacy_data_path}/{default_name}"
            if not isinstance(legacy_path, str) or not legacy_path:
                raise DataSchemaError(f"legacy repository {repository_id!r} has an invalid {field}")
            try:
                relative_path = Path(legacy_path).relative_to(Path(legacy_data_path))
            except ValueError as exc:
                raise DataSchemaError(f"legacy repository {repository_id!r} has an unsafe {field}") from exc
            if not relative_path.parts or ".." in relative_path.parts:
                raise DataSchemaError(f"legacy repository {repository_id!r} has an unsafe {field}")
            record[field] = (Path(data_path) / relative_path).as_posix()
        record.setdefault("storage_mode", "central")
        record.setdefault("last_commit", None)
        record.setdefault("last_analysis_sha256", None)
        record.setdefault("git_remote", None)
        record.setdefault("config_sha256", None)
        record.setdefault("validation_status", "pending")
        registered_at = record.get("registered_at")
        record.setdefault("registered_at", registered_at if isinstance(registered_at, str) and registered_at else migration_time)
        updated_at = record.get("updated_at")
        record.setdefault("updated_at", updated_at if isinstance(updated_at, str) and updated_at else record["registered_at"])
        if not record.get("normalized_path"):
            record["normalized_path"] = _normalised_path(record.get("absolute_path"))
        if not isinstance(record.get("normalized_path"), str) or not record["normalized_path"]:
            raise DataSchemaError(f"legacy repository {repository_id!r} has no usable path")
        migrated_repositories.append(record)
    migrated["schema_version"] = WORKSPACE_SCHEMA_VERSION
    migrated["repositories"] = migrated_repositories
    migrated.setdefault("active_repository_id", None)
    return migrated, LEGACY_WORKSPACE_SCHEMA_VERSION
