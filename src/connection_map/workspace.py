"""Persistent repository catalog and data paths for central workspace mode.

The central workspace is deliberately a small JSON registry plus one directory
per repository.  It does not execute repository code and it never uses a path
from an HTTP request without resolving it through a registered record.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bundle import split_analysis_file, validate_bundle
from .contract import canonical_sha256
from .data_schema import (
    WORKSPACE_FORMAT,
    WORKSPACE_SCHEMA_VERSION,
    DataSchemaError,
    migrate_workspace_registry,
)

WORKSPACE_ENV = "CONNECTION_MAP_WORKSPACE"
PUBLISH_JOURNAL_FORMAT = "connection-analysis-publish-journal"
PUBLISH_JOURNAL_SCHEMA_VERSION = "1.0"


class WorkspaceError(ValueError):
    """Raised when a workspace registry or repository path is invalid."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalise_root(root: Path) -> str:
    return os.path.normcase(str(root.resolve()))


def _git_remote(root: Path) -> str | None:
    git_entry = root / ".git"
    if git_entry.is_dir():
        git_config = git_entry / "config"
    elif git_entry.is_file():
        # Git worktrees use a .git file that points at the real administrative
        # directory instead of embedding .git/config in the repository.
        try:
            marker = git_entry.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        if not marker.lower().startswith("gitdir:"):
            return None
        git_directory = Path(marker.split(":", 1)[1].strip())
        if not git_directory.is_absolute():
            git_directory = root / git_directory
        git_config = git_directory / "config"
    else:
        return None
    try:
        text = git_config.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    in_origin = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped == '[remote "origin"]'
            continue
        if in_origin and stripped.startswith("url") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip()
            return value or None
    return None


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _safe_relative(path: str, *, field: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or not path or path.replace("\\", "/").startswith("/"):
        raise WorkspaceError(f"registry {field} must be a relative path inside the workspace")
    return candidate


def _safe_repository_data_path(root: Path, relative: str, *, field: str) -> Path:
    """Resolve a legacy repository data directory without following links."""

    candidate = _safe_relative(relative, field=field)
    if len(candidate.parts) < 2 or candidate.parts[0] != "repositories":
        raise WorkspaceError(f"registry {field} must be inside repositories")
    current = root
    for part in candidate.parts:
        current /= part
        if _is_link_or_reparse_point(current):
            raise WorkspaceError(f"workspace path must not contain a symlink or junction: {current}")
    return root / candidate


def _is_link_or_reparse_point(path: Path) -> bool:
    """Detect POSIX symlinks and Windows junction/reparse points."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WorkspaceError(f"cannot inspect workspace path: {path}") from exc
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
    except OSError:
        # Windows does not expose a directory descriptor that can be fsynced;
        # the atomic replace still prevents readers from seeing partial JSON.
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@dataclass(slots=True)
class _PublishState:
    journal: Path
    backup: Path | None
    old_analysis_backed_up: bool
    old_bundle_backed_up: bool


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    """A registered repository and paths relative to the workspace data root."""

    repository_id: str
    display_name: str
    absolute_path: str
    normalized_path: str
    storage_mode: str
    data_path: str
    analysis_path: str
    bundle_path: str
    config_path: str
    layout_path: str
    registered_at: str
    updated_at: str
    last_commit: str | None = None
    last_analysis_sha256: str | None = None
    git_remote: str | None = None
    config_sha256: str | None = None
    validation_status: str = "pending"

    @classmethod
    def from_dict(cls, value: Any) -> RepositoryRecord:
        if not isinstance(value, dict):
            raise WorkspaceError("registry repository entry must be an object")
        required = (
            "repository_id",
            "display_name",
            "absolute_path",
            "normalized_path",
            "storage_mode",
            "data_path",
            "analysis_path",
            "bundle_path",
            "config_path",
            "layout_path",
            "registered_at",
            "updated_at",
        )
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise WorkspaceError("registry repository entry has missing or invalid fields")
        if value["storage_mode"] not in {"central", "local"}:
            raise WorkspaceError("registry storage_mode must be central or local")
        relative_paths = {
            key: _safe_relative(value[key], field=key)
            for key in ("data_path", "analysis_path", "bundle_path", "config_path", "layout_path")
        }
        data_path = relative_paths["data_path"]
        expected_data_path = Path("repositories") / value["repository_id"]
        if data_path != expected_data_path:
            raise WorkspaceError("registry data_path must be repositories/<repository_id>")
        for key, relative_path in relative_paths.items():
            if key == "data_path":
                continue
            try:
                relative_path.relative_to(data_path)
            except ValueError as exc:
                raise WorkspaceError(f"registry {key} must be inside data_path") from exc
        for key in ("last_commit", "last_analysis_sha256", "git_remote", "config_sha256"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise WorkspaceError(f"registry {key} must be a string or null")
        status = value.get("validation_status", "pending")
        if status not in {"pending", "running", "valid", "invalid", "cancelled"}:
            raise WorkspaceError("registry validation_status is invalid")
        return cls(
            repository_id=value["repository_id"],
            display_name=value["display_name"],
            absolute_path=value["absolute_path"],
            normalized_path=value["normalized_path"],
            storage_mode=value["storage_mode"],
            data_path=value["data_path"],
            analysis_path=value["analysis_path"],
            bundle_path=value["bundle_path"],
            config_path=value["config_path"],
            layout_path=value["layout_path"],
            registered_at=value["registered_at"],
            updated_at=value["updated_at"],
            last_commit=value.get("last_commit"),
            last_analysis_sha256=value.get("last_analysis_sha256"),
            git_remote=value.get("git_remote"),
            config_sha256=value.get("config_sha256"),
            validation_status=status,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "display_name": self.display_name,
            "absolute_path": self.absolute_path,
            "normalized_path": self.normalized_path,
            "storage_mode": self.storage_mode,
            "data_path": self.data_path,
            "analysis_path": self.analysis_path,
            "bundle_path": self.bundle_path,
            "config_path": self.config_path,
            "layout_path": self.layout_path,
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
            "last_commit": self.last_commit,
            "last_analysis_sha256": self.last_analysis_sha256,
            "git_remote": self.git_remote,
            "config_sha256": self.config_sha256,
            "validation_status": self.validation_status,
        }


class Workspace:
    """Manage the central data directory without touching application files."""

    def __init__(self, data_root: Path):
        self.root = data_root.resolve()
        self.registry_path = self.root / "registry.json"
        self.registry_lock_path = self.root / "registry.json.lock"
        self.repositories_root = self.root / "repositories"
        self._registry_lock_depth = 0

    def _pending_publish_journals(self) -> list[Path]:
        return sorted(self.root.glob(".connection-map-publish-*.json"))

    @contextmanager
    def _registry_lock(self) -> Iterator[None]:
        """Serialize registry read-modify-write operations across processes."""

        self.registry_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.registry_lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    self._registry_lock_depth += 1
                    self._recover_pending_publishes_unlocked()
                    yield
                finally:
                    self._registry_lock_depth -= 1
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._registry_lock_depth += 1
                    self._recover_pending_publishes_unlocked()
                    yield
                finally:
                    self._registry_lock_depth -= 1
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _update_publish_phase(self, journal_path: Path, phase: str) -> None:
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"publish journal is unreadable: {journal_path}") from exc
        payload["phase"] = phase
        _atomic_write_json(journal_path, payload)

    def _recover_pending_publishes_unlocked(self) -> None:
        for journal_path in self._pending_publish_journals():
            try:
                payload = json.loads(journal_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("journal payload is not an object")
                if payload.get("format") != PUBLISH_JOURNAL_FORMAT or payload.get("schema_version") != PUBLISH_JOURNAL_SCHEMA_VERSION:
                    raise ValueError("journal format or schema is unsupported")
                record = RepositoryRecord.from_dict(payload.get("record"))
                phase = payload.get("phase")
                if phase not in {"prepared", "snapshot_installed", "registry_saved", "committed"}:
                    raise ValueError("journal phase is invalid")
                backup_relative = _safe_relative(payload.get("backup_path", ""), field="publish backup")
                expected_backup_parent = Path(record.data_path) / "backups"
                if backup_relative.parent != expected_backup_parent or not backup_relative.name.startswith(".publish-"):
                    raise ValueError("journal backup path is invalid")
                backup = self.path_for(record, backup_relative.as_posix())
                if phase == "committed":
                    if backup.exists():
                        shutil.rmtree(backup)
                    journal_path.unlink(missing_ok=True)
                    continue

                live_analysis = self.path_for(record, record.analysis_path)
                live_bundle = self.path_for(record, record.bundle_path)
                analysis_backup = backup / "analysis.json"
                bundle_backup = backup / "bundle"
                if payload.get("old_analysis_backed_up"):
                    if analysis_backup.exists():
                        if live_analysis.exists():
                            live_analysis.unlink()
                        live_analysis.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(analysis_backup, live_analysis)
                elif live_analysis.exists():
                    live_analysis.unlink()
                if payload.get("old_bundle_backed_up"):
                    if bundle_backup.exists():
                        if live_bundle.exists():
                            shutil.rmtree(live_bundle)
                        live_bundle.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(bundle_backup, live_bundle)
                elif live_bundle.exists():
                    shutil.rmtree(live_bundle)

                previous_manifest = payload.get("previous_manifest")
                manifest_path = self.path_for(record, Path(record.data_path, "manifest.json").as_posix())
                if previous_manifest is None:
                    manifest_path.unlink(missing_ok=True)
                elif isinstance(previous_manifest, str):
                    manifest_path.write_bytes(base64.b64decode(previous_manifest, validate=True))
                else:
                    raise ValueError("journal previous manifest is invalid")

                previous_records = payload.get("previous_records")
                previous_active = payload.get("previous_active_repository_id")
                if not isinstance(previous_records, list) or (previous_active is not None and not isinstance(previous_active, str)):
                    raise ValueError("journal previous registry is invalid")
                self._save(
                    active_repository_id=previous_active,
                    records=[RepositoryRecord.from_dict(item) for item in previous_records],
                )
                if backup.exists():
                    shutil.rmtree(backup)
                journal_path.unlink(missing_ok=True)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, binascii.Error, WorkspaceError) as exc:
                raise WorkspaceError(f"cannot recover publish journal {journal_path}: {exc}") from exc

    def _read_registry_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"invalid workspace registry: {self.registry_path}") from exc
        if not isinstance(payload, dict):
            raise WorkspaceError("workspace registry must be an object")
        return payload

    def _relocate_legacy_repository_data(
        self,
        payload: dict[str, Any],
        migrated: dict[str, Any],
    ) -> list[tuple[Path, Path]]:
        """Move legacy repository directories to their canonical locations."""

        original_records = payload.get("repositories")
        migrated_records = migrated.get("repositories")
        if not isinstance(original_records, list) or not isinstance(migrated_records, list):
            raise WorkspaceError("workspace registry repositories must be an array")
        moved: list[tuple[Path, Path]] = []
        try:
            for original, current in zip(original_records, migrated_records, strict=True):
                if not isinstance(original, dict) or not isinstance(current, dict):
                    raise WorkspaceError("workspace registry repository entry must be an object")
                repository_id = current.get("repository_id")
                old_relative = original.get("data_path") or f"repositories/{repository_id}"
                new_relative = current.get("data_path")
                if not isinstance(repository_id, str) or not isinstance(old_relative, str) or not isinstance(new_relative, str):
                    raise WorkspaceError("workspace registry migration has invalid repository paths")
                old_path = _safe_repository_data_path(self.root, old_relative, field="legacy data path")
                new_path = _safe_repository_data_path(self.root, new_relative, field="data path")
                if old_path == new_path:
                    continue
                if not old_path.exists() and new_path.exists():
                    # A previous process may have moved the directory before it
                    # was interrupted while writing the registry.
                    continue
                if not old_path.exists():
                    continue
                if new_path.exists():
                    raise WorkspaceError(
                        f"cannot migrate repository {repository_id}: both {old_relative!r} and {new_relative!r} exist"
                    )
                new_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old_path, new_path)
                moved.append((old_path, new_path))
        except Exception:
            # A later repository must not leave earlier repositories stranded
            # under their new paths when migration cannot finish as a whole.
            self._restore_legacy_repository_data(moved)
            raise
        return moved

    @staticmethod
    def _restore_legacy_repository_data(moved: list[tuple[Path, Path]]) -> None:
        for old_path, new_path in reversed(moved):
            if new_path.exists() and not old_path.exists():
                old_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(new_path, old_path)

    def _migrate_registry_unlocked(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            migrated, source_version = migrate_workspace_registry(payload)
        except DataSchemaError as exc:
            raise WorkspaceError(str(exc)) from exc
        if source_version is None:
            return migrated
        backup_root = self.root / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / f"registry-{source_version}-to-{WORKSPACE_SCHEMA_VERSION}-{uuid.uuid4().hex}.json"
        shutil.copy2(self.registry_path, backup)
        moved = self._relocate_legacy_repository_data(payload, migrated)
        try:
            _atomic_write_json(self.registry_path, migrated)
        except Exception:
            self._restore_legacy_repository_data(moved)
            raise
        return migrated

    def load(self) -> dict[str, Any]:
        if self._registry_lock_depth == 0 and self._pending_publish_journals():
            # Recovery is serialized with normal registry operations.  A
            # crashed publish must be settled before a caller sees its state.
            with self._registry_lock():
                pass
        if not self.registry_path.exists():
            return {
                "format": WORKSPACE_FORMAT,
                "schema_version": WORKSPACE_SCHEMA_VERSION,
                "active_repository_id": None,
                "repositories": [],
            }
        payload = self._read_registry_payload()
        if payload.get("schema_version") != WORKSPACE_SCHEMA_VERSION and self._registry_lock_depth == 0:
            # Re-read and migrate while holding the same lock used by all
            # registry writers.  This prevents two app processes from both
            # rewriting a legacy registry during startup.
            with self._registry_lock():
                return self.load()
        payload = self._migrate_registry_unlocked(payload)
        if payload.get("format") != WORKSPACE_FORMAT:
            raise WorkspaceError("workspace registry format is invalid")
        if payload.get("schema_version") != WORKSPACE_SCHEMA_VERSION:
            raise WorkspaceError("workspace registry schema_version is unsupported")
        repositories = payload.get("repositories")
        if not isinstance(repositories, list):
            raise WorkspaceError("workspace registry repositories must be an array")
        records = [RepositoryRecord.from_dict(item) for item in repositories]
        ids = [record.repository_id for record in records]
        if len(set(ids)) != len(ids):
            raise WorkspaceError("workspace registry contains duplicate repository IDs")
        active = payload.get("active_repository_id")
        if active is not None and active not in set(ids):
            raise WorkspaceError("workspace registry active repository is unknown")
        return {"format": WORKSPACE_FORMAT, "schema_version": WORKSPACE_SCHEMA_VERSION, "active_repository_id": active, "repositories": records}

    def _save(self, *, active_repository_id: str | None, records: list[RepositoryRecord]) -> None:
        _atomic_write_json(
            self.registry_path,
            {
                "format": WORKSPACE_FORMAT,
                "schema_version": WORKSPACE_SCHEMA_VERSION,
                "active_repository_id": active_repository_id,
                "repositories": [record.to_dict() for record in records],
            },
        )

    def records(self) -> list[RepositoryRecord]:
        return list(self.load()["repositories"])

    def restore_registration(
        self,
        records: list[RepositoryRecord],
        active_repository_id: str | None,
    ) -> None:
        """Restore registry state captured before a registration attempt."""

        record_ids = {record.repository_id for record in records}
        if active_repository_id is not None and active_repository_id not in record_ids:
            raise WorkspaceError("active repository is not present in the restored registry")
        with self._registry_lock():
            self._save(active_repository_id=active_repository_id, records=list(records))

    def get(self, repository_id: str) -> RepositoryRecord:
        for record in self.records():
            if record.repository_id == repository_id:
                return record
        raise WorkspaceError(f"repository is not registered: {repository_id}")

    @staticmethod
    def _match_record(records: list[RepositoryRecord], root: Path) -> RepositoryRecord | None:
        """Find a repository by path, or by an unambiguous Git origin.

        The absolute path is the primary identity because it is local and
        deterministic.  When a repository has moved, its origin is the only
        stable identifier available without writing a marker into the target
        repository.  Refuse to guess when several records share that origin
        (for example, multiple clones of the same project).
        """

        remote = _git_remote(root)
        normalized = _normalise_root(root)
        existing = next((item for item in records if item.normalized_path == normalized), None)
        if existing is not None:
            # A path can be reused for a different clone.  Do not silently
            # attach the new repository to the old snapshot when both origins
            # are available and disagree.
            if remote and existing.git_remote and existing.git_remote != remote:
                # Continue with the origin lookup below.  A record for the
                # replacement clone may already exist at another path.
                existing = None
            else:
                return existing
        if not remote:
            return None
        candidates = [item for item in records if item.git_remote == remote]
        return candidates[0] if len(candidates) == 1 else None

    def find(self, repository_root: Path) -> RepositoryRecord | None:
        """Return the registered record for a path, if it can be identified."""

        root = repository_root.resolve()
        if not root.is_dir():
            raise WorkspaceError(f"repository root is not a directory: {root}")
        return self._match_record(self.records(), root)

    def register(self, repository_root: Path, *, storage_mode: str = "central", display_name: str | None = None) -> RepositoryRecord:
        root = repository_root.resolve()
        if not root.is_dir():
            raise WorkspaceError(f"repository root is not a directory: {root}")
        if storage_mode not in {"central", "local"}:
            raise WorkspaceError("storage_mode must be central or local")
        with self._registry_lock():
            state = self.load()
            records: list[RepositoryRecord] = state["repositories"]
            normalized = _normalise_root(root)
            current_remote = _git_remote(root)
            existing = self._match_record(records, root)
            if existing is not None:
                result = replace(
                    existing,
                    absolute_path=str(root),
                    normalized_path=normalized,
                    display_name=display_name or root.name or existing.display_name,
                    storage_mode=storage_mode,
                    git_remote=current_remote or existing.git_remote,
                    updated_at=_now(),
                )
                records = [result if item.repository_id == existing.repository_id else item for item in records]
            else:
                repository_id = f"repo-{uuid.uuid4().hex[:16]}"
                data_path = Path("repositories") / repository_id
                result = RepositoryRecord(
                    repository_id=repository_id,
                    display_name=display_name or root.name or "repository",
                    absolute_path=str(root),
                    normalized_path=normalized,
                    storage_mode=storage_mode,
                    data_path=data_path.as_posix(),
                    analysis_path=(data_path / "analysis.json").as_posix(),
                    bundle_path=(data_path / "bundle").as_posix(),
                    config_path=(data_path / "config.toml").as_posix(),
                    layout_path=(data_path / "layout.json").as_posix(),
                    registered_at=_now(),
                    updated_at=_now(),
                    git_remote=current_remote,
                )
                records.append(result)
            # Check the lexical path before saving the registry.  Resolving
            # first would hide a junction that redirects one repository's
            # storage directory into another repository's data.
            data_path = self.path_for(result, result.data_path)
            data_existed = data_path.exists()
            try:
                # Prepare the owned directory before publishing the registry
                # entry. A failed preparation must not leave an empty record.
                self.ensure_data_paths(result)
                self._save(active_repository_id=result.repository_id, records=records)
            except Exception:
                if existing is None and not data_existed and data_path.exists() and not _is_link_or_reparse_point(data_path):
                    shutil.rmtree(data_path)
                raise
        return result

    def remove(
        self,
        repository_id: str,
        *,
        remove_data: bool = True,
        active_repository_id: str | None = None,
    ) -> RepositoryRecord:
        """Remove one registry record and, optionally, its owned data.

        This is used for transactional cleanup when analysis fails before a
        first snapshot is published.  The repository data path is resolved
        through the same symlink-safe checks used by all other workspace
        operations, so an invalid registry cannot turn cleanup into a broad
        recursive deletion.
        """

        with self._registry_lock():
            state = self.load()
            current = next((item for item in state["repositories"] if item.repository_id == repository_id), None)
            if current is None:
                raise WorkspaceError(f"repository is not registered: {repository_id}")
            data_path = self.path_for(current, current.data_path)
            if remove_data and data_path.exists():
                if _is_link_or_reparse_point(data_path):
                    raise WorkspaceError(f"workspace path must not contain a symlink or junction: {data_path}")
                shutil.rmtree(data_path)
            remaining_ids = {item.repository_id for item in state["repositories"] if item.repository_id != repository_id}
            active = state["active_repository_id"]
            if active == repository_id:
                active = active_repository_id if active_repository_id in remaining_ids else None
            self._save(
                active_repository_id=active,
                records=[item for item in state["repositories"] if item.repository_id != repository_id],
            )
        return current

    def set_validation(self, repository_id: str, status: str) -> RepositoryRecord:
        if status not in {"pending", "running", "valid", "invalid", "cancelled"}:
            raise WorkspaceError(f"invalid validation status: {status}")
        with self._registry_lock():
            state = self.load()
            current = next((item for item in state["repositories"] if item.repository_id == repository_id), None)
            if current is None:
                raise WorkspaceError(f"repository is not registered: {repository_id}")
            updated = replace(current, validation_status=status, updated_at=_now())
            self._save(
                # Validation is background maintenance.  It must not change
                # the repository currently selected in the browser.
                active_repository_id=state["active_repository_id"],
                records=[updated if item.repository_id == repository_id else item for item in state["repositories"]],
            )
        return updated

    def update_analysis(self, record: RepositoryRecord, document: dict[str, Any]) -> RepositoryRecord:
        """Backward-compatible alias for the atomic central publish operation."""

        return self.publish_analysis(record, document)

    def _record_after_analysis(self, record: RepositoryRecord, document: dict[str, Any]) -> RepositoryRecord:
        analysis_sha = canonical_sha256(document)
        commit_sha = (document.get("meta") or {}).get("target", {}).get("commit_sha")
        return replace(
            record,
            last_commit=commit_sha if isinstance(commit_sha, str) else None,
            last_analysis_sha256=analysis_sha,
            config_sha256=_file_sha256(self.path_for(record, record.config_path)),
            validation_status="pending",
            updated_at=_now(),
        )

    def publish_analysis(self, record: RepositoryRecord, document: dict[str, Any]) -> RepositoryRecord:
        """Publish the snapshot and its registry manifest as one locked operation."""

        with self._registry_lock():
            state = self.load()
            previous_manifest_path = self.path_for(record, Path(record.data_path, "manifest.json").as_posix())
            previous_manifest = previous_manifest_path.read_bytes() if previous_manifest_path.is_file() else None
            previous_records = list(state["repositories"])
            previous_active = state["active_repository_id"]
            journal_path = self.root / f".connection-map-publish-{uuid.uuid4().hex}.json"
            publish_state = self._publish_analysis_unlocked(
                record,
                document,
                journal_path=journal_path,
                previous_records=previous_records,
                previous_active=previous_active,
                previous_manifest=previous_manifest,
            )
            try:
                updated = self._record_after_analysis(record, document)
                self._save(
                    active_repository_id=updated.repository_id,
                    records=[updated if item.repository_id == updated.repository_id else item for item in previous_records],
                )
                self._update_publish_phase(publish_state.journal, "registry_saved")
                self.write_manifest(updated, document)
                self._update_publish_phase(publish_state.journal, "committed")
            except Exception:
                self._restore_publish_state(record, publish_state)
                if previous_manifest is None:
                    previous_manifest_path.unlink(missing_ok=True)
                else:
                    previous_manifest_path.write_bytes(previous_manifest)
                self._save(active_repository_id=previous_active, records=previous_records)
                publish_state.journal.unlink(missing_ok=True)
                raise
            try:
                self._finalize_publish_state(publish_state)
            except OSError:
                # The new snapshot and registry are already committed. Keep
                # the committed journal so the next locked workspace access
                # can finish cleanup without reverting the live result.
                pass
            return updated

    def _publish_analysis_unlocked(
        self,
        record: RepositoryRecord,
        document: dict[str, Any],
        *,
        journal_path: Path,
        previous_records: list[RepositoryRecord],
        previous_active: str | None,
        previous_manifest: bytes | None,
    ) -> _PublishState:
        """Build and publish one central snapshot with a recoverable swap.

        Analysis and bundle files are prepared and validated outside their live
        paths. Existing files are moved to a unique backup before the swap, so
        a failed publish can restore the previous snapshot instead of leaving
        an old analysis paired with a partial bundle.
        """

        data_root = self.path_for(record, record.data_path)
        live_analysis = self.path_for(record, record.analysis_path)
        live_bundle = self.path_for(record, record.bundle_path)
        staging = data_root / f".staging-{uuid.uuid4().hex}"
        backup_root = self.path_for(record, Path(record.data_path, "backups").as_posix())
        backup = backup_root / f".publish-{uuid.uuid4().hex}"
        staging_analysis = staging / "analysis.json"
        staging_bundle = staging / "bundle"
        old_analysis_backed_up = live_analysis.exists()
        old_bundle_backed_up = live_bundle.exists()
        _atomic_write_json(
            journal_path,
            {
                "format": PUBLISH_JOURNAL_FORMAT,
                "schema_version": PUBLISH_JOURNAL_SCHEMA_VERSION,
                "phase": "prepared",
                "record": record.to_dict(),
                "backup_path": backup.relative_to(self.root).as_posix(),
                "old_analysis_backed_up": old_analysis_backed_up,
                "old_bundle_backed_up": old_bundle_backed_up,
                "previous_active_repository_id": previous_active,
                "previous_records": [item.to_dict() for item in previous_records],
                "previous_manifest": base64.b64encode(previous_manifest).decode("ascii") if previous_manifest is not None else None,
            },
        )
        analysis_installed = False
        bundle_installed = False
        try:
            staging.mkdir(parents=True, exist_ok=False)
            staging_analysis.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            split_analysis_file(staging_analysis, staging_bundle, force=True)
            validate_bundle(staging_bundle)
            backup.mkdir(parents=True, exist_ok=False)
            if live_analysis.exists():
                os.replace(live_analysis, backup / "analysis.json")
            if live_bundle.exists():
                os.replace(live_bundle, backup / "bundle")
            os.replace(staging_analysis, live_analysis)
            analysis_installed = True
            os.replace(staging_bundle, live_bundle)
            bundle_installed = True
            self._update_publish_phase(journal_path, "snapshot_installed")
            publish_state = _PublishState(
                journal=journal_path,
                backup=backup if any(backup.iterdir()) else None,
                old_analysis_backed_up=old_analysis_backed_up,
                old_bundle_backed_up=old_bundle_backed_up,
            )
            if publish_state.backup is None:
                backup.rmdir()
            return publish_state
        except Exception:
            if bundle_installed and live_bundle.exists():
                shutil.rmtree(live_bundle)
            if analysis_installed and live_analysis.exists():
                live_analysis.unlink()
            if old_bundle_backed_up and (backup / "bundle").exists():
                os.replace(backup / "bundle", live_bundle)
            if old_analysis_backed_up and (backup / "analysis.json").exists():
                os.replace(backup / "analysis.json", live_analysis)
            if backup.exists():
                try:
                    backup.rmdir()
                except OSError:
                    # Keep a non-empty backup if restoration itself failed.
                    pass
            journal_path.unlink(missing_ok=True)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _restore_publish_state(self, record: RepositoryRecord, state: _PublishState) -> None:
        live_analysis = self.path_for(record, record.analysis_path)
        live_bundle = self.path_for(record, record.bundle_path)
        if live_bundle.exists():
            shutil.rmtree(live_bundle)
        if live_analysis.exists():
            live_analysis.unlink()
        if state.backup is None:
            return
        if state.old_bundle_backed_up and (state.backup / "bundle").exists():
            os.replace(state.backup / "bundle", live_bundle)
        if state.old_analysis_backed_up and (state.backup / "analysis.json").exists():
            os.replace(state.backup / "analysis.json", live_analysis)
        if state.backup.exists():
            state.backup.rmdir()

    def _finalize_publish_state(self, state: _PublishState) -> None:
        if state.backup is not None and state.backup.exists():
            shutil.rmtree(state.backup)
        state.journal.unlink(missing_ok=True)

    def ensure_data_paths(self, record: RepositoryRecord) -> None:
        data_path = self.path_for(record, record.data_path)
        data_path.mkdir(parents=True, exist_ok=True)
        (data_path / "analyzer").mkdir(parents=True, exist_ok=True)
        self.path_for(record, record.bundle_path).mkdir(parents=True, exist_ok=True)

    def path_for(self, record: RepositoryRecord, relative: str) -> Path:
        relative_path = _safe_relative(relative, field="data path")
        lexical = self.root / relative_path
        current = self.root
        for part in relative_path.parts:
            current /= part
            if _is_link_or_reparse_point(current):
                raise WorkspaceError(f"workspace path must not contain a symlink or junction: {current}")
        candidate = lexical.resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"workspace path escapes data root: {relative}") from exc
        return candidate

    def write_manifest(self, record: RepositoryRecord, document: dict[str, Any]) -> Path:
        manifest_path = self.path_for(record, Path(record.data_path, "manifest.json").as_posix())
        bundle_path = self.path_for(record, record.bundle_path)
        layout_path = self.path_for(record, record.layout_path)
        layout_present = layout_path.is_file()
        payload = {
            "format": "connection-analysis-repository-manifest",
            "schema_version": "1.0",
            "repository_id": record.repository_id,
            "display_name": record.display_name,
            "root": record.absolute_path,
            "git_remote": record.git_remote,
            "config_sha256": record.config_sha256,
            "analysis": {"path": Path(record.analysis_path).relative_to(Path(record.data_path)).as_posix(), "sha256": canonical_sha256(document)},
            "bundle": {
                "path": Path(record.bundle_path).relative_to(Path(record.data_path)).as_posix(),
                "present": (bundle_path / "index.json").is_file(),
                "sha256": _file_sha256(bundle_path / "index.json"),
            },
            "layout": {"present": layout_present, "sha256": _file_sha256(layout_path) if layout_present else None},
            "counts": (document.get("meta") or {}).get("counts", {}),
            "updated_at": record.updated_at,
        }
        _atomic_write_json(manifest_path, payload)
        return manifest_path

    def copy_config(self, record: RepositoryRecord, source: Path | None) -> Path:
        destination = self.path_for(record, record.config_path)
        if source is not None and source.resolve() != destination:
            if not source.is_file():
                raise WorkspaceError(f"configuration file is not found: {source}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        elif not destination.exists():
            destination.write_text(
                "[analysis]\n"
                "language = \"python\"\n"
                "include_tests = false\n"
                "follow_symlinks = false\n",
                encoding="utf-8",
            )
        return destination

    def record_payload(self, record: RepositoryRecord) -> dict[str, Any]:
        data_root = self.path_for(record, record.data_path)
        manifest = data_root / "manifest.json"
        return {
            "repository_id": record.repository_id,
            "display_name": record.display_name,
            "absolute_path": record.absolute_path,
            "storage_mode": record.storage_mode,
            "last_commit": record.last_commit,
            "last_analysis_sha256": record.last_analysis_sha256,
            "git_remote": record.git_remote,
            "config_sha256": record.config_sha256,
            "validation_status": record.validation_status,
            "updated_at": record.updated_at,
            "has_analysis": self.path_for(record, record.analysis_path).is_file(),
            "has_bundle": (self.path_for(record, record.bundle_path) / "index.json").is_file(),
            "has_manifest": manifest.is_file(),
        }


def workspace_from_env() -> Workspace | None:
    value = os.environ.get(WORKSPACE_ENV)
    if not value:
        return None
    return Workspace(Path(value))


def workspace_repository_id(root: Path) -> str:
    """Return a deterministic diagnostic identifier for logs, not a registry ID."""

    return hashlib.sha256(_normalise_root(root).encode("utf-8")).hexdigest()[:16]
