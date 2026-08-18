"""Safe installation and rollback of a repository-local core."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

CORE_MANIFEST_NAME = "core-manifest.json"
CORE_MANIFEST_VERSION = "1"
CORE_SCHEMA_VERSION = "1.0"
PACKAGE_NAME = "connection-analysis-mapping"


class DistributionError(ValueError):
    """Raised when a core archive or target layout is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    """Validated metadata about a source archive."""

    path: Path
    root_name: str
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CoreInstallResult:
    """Result of installing a core archive."""

    core_path: Path
    version: str
    sha256: str
    backup_path: Path | None


@dataclass(frozen=True, slots=True)
class CoreRollbackResult:
    """Result of restoring a previous core backup."""

    core_path: Path
    restored_backup_name: str
    saved_current_path: Path | None


def _is_link_or_reparse_point(path: Path) -> bool:
    """Detect symlinks and Windows junction/reparse points before mutation."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DistributionError(f"cannot inspect installation path: {path}") from exc
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _ensure_directory_is_real(path: Path, *, label: str) -> None:
    if _is_link_or_reparse_point(path):
        raise DistributionError(f"{label} must not be a symlink or junction: {path}")
    if path.exists() and not path.is_dir():
        raise DistributionError(f"{label} is not a directory: {path}")


def _resolve_install_base(target_root: Path, install_dir: str | Path) -> Path:
    root = target_root.resolve()
    relative = Path(install_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise DistributionError("install_dir must be a relative directory inside the target root")
    if not relative.parts:
        raise DistributionError("install_dir must name a child directory inside the target root")

    raw_base = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if _is_link_or_reparse_point(current):
            raise DistributionError(f"install directory must not contain a symlink or junction: {current}")
    base = raw_base.resolve()
    try:
        base.relative_to(root)
    except ValueError as exc:
        raise DistributionError("install_dir resolves outside the target root") from exc
    if not base.is_dir() or _is_link_or_reparse_point(base):
        raise DistributionError(f"target is not initialized: {base}")
    return base


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _core_content_sha256(core: Path) -> str:
    """Hash core files deterministically, excluding the self-describing manifest."""

    digest = hashlib.sha256()
    for path in sorted(core.rglob("*"), key=lambda item: item.relative_to(core).as_posix()):
        if _is_link_or_reparse_point(path):
            raise DistributionError(f"core contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(core).as_posix()
        if relative == CORE_MANIFEST_NAME:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _member_parts(name: str) -> tuple[str, ...]:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        raise DistributionError(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or PureWindowsPath(name).drive
        or any(":" in part or part in {"", ".", ".."} for part in path.parts)
    ):
        raise DistributionError(f"unsafe archive member path: {name!r}")
    return path.parts


def _read_archive_version(archive: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise DistributionError("archive pyproject.toml is not a regular file")
    try:
        payload = tomllib.loads(extracted.read().decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise DistributionError("archive pyproject.toml is invalid") from exc
    project = payload.get("project")
    if not isinstance(project, dict) or project.get("name") != PACKAGE_NAME:
        raise DistributionError(f"archive project name must be {PACKAGE_NAME!r}")
    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise DistributionError("archive project version is missing")
    return version


def inspect_archive(archive_path: Path) -> ArchiveInfo:
    """Validate the source archive without extracting or executing it."""

    archive = archive_path.resolve()
    if not archive.is_file():
        raise DistributionError(f"source archive does not exist: {archive}")
    try:
        with tarfile.open(archive, mode="r:*") as handle:
            members = handle.getmembers()
            if not members:
                raise DistributionError("source archive is empty")
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                raise DistributionError("source archive contains duplicate member names")

            member_parts = {member: _member_parts(member.name) for member in members}
            top_levels = {parts[0] for parts in member_parts.values()}
            if len(top_levels) != 1:
                raise DistributionError("source archive must contain exactly one top-level directory")
            root_name = next(iter(top_levels))

            for member in members:
                if not (member.isdir() or member.isreg()):
                    raise DistributionError(f"archive contains unsupported entry: {member.name}")

            required = {
                (root_name, "pyproject.toml"),
                (root_name, "src", "connection_map", "__init__.py"),
                (root_name, "src", "connection_map", "web", "index.html"),
            }
            available = set(member_parts.values())
            missing = sorted("/".join(parts) for parts in required - available)
            if missing:
                raise DistributionError(f"archive is missing required files: {', '.join(missing)}")

            pyproject_member = next(member for member, parts in member_parts.items() if parts == (root_name, "pyproject.toml"))
            version = _read_archive_version(handle, pyproject_member)
    except tarfile.TarError as exc:
        raise DistributionError(f"source archive cannot be read: {archive}") from exc

    return ArchiveInfo(path=archive, root_name=root_name, version=version, sha256=_sha256(archive))


def _extract_archive(info: ArchiveInfo, staging: Path) -> None:
    try:
        with tarfile.open(info.path, mode="r:*") as handle:
            for member in handle.getmembers():
                parts = _member_parts(member.name)
                if parts[0] != info.root_name:
                    raise DistributionError(f"archive top-level directory changed: {member.name}")
                relative_parts = parts[1:]
                if not relative_parts:
                    continue
                if relative_parts == (CORE_MANIFEST_NAME,):
                    raise DistributionError(f"archive must not contain {CORE_MANIFEST_NAME}")
                destination = staging.joinpath(*relative_parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                extracted = handle.extractfile(member)
                if extracted is None:
                    raise DistributionError(f"archive entry is not readable: {member.name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as output:
                    shutil.copyfileobj(extracted, output)
                try:
                    os.chmod(destination, member.mode & 0o777)
                except OSError:
                    pass
    except tarfile.TarError as exc:
        raise DistributionError(f"source archive cannot be extracted: {info.path}") from exc


def _write_manifest(staging: Path, info: ArchiveInfo) -> None:
    manifest = {
        "manifest_version": CORE_MANIFEST_VERSION,
        "package": PACKAGE_NAME,
        "core_version": info.version,
        "schema_version": CORE_SCHEMA_VERSION,
        "source_archive": info.path.name,
        "archive_sha256": info.sha256,
        "core_sha256": _core_content_sha256(staging),
        "installed_at": datetime.now(UTC).isoformat(),
    }
    (staging / CORE_MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _unique_backup_path(backups: Path, prefix: str, digest: str = "") -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{digest[:12]}" if digest else ""
    candidate = backups / f"{prefix}-{timestamp}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = backups / f"{prefix}-{timestamp}{suffix}-{counter}"
        counter += 1
    return candidate


def _rename_with_retry(source: Path, destination: Path) -> None:
    """Rename a directory while tolerating short Windows file-lock races."""

    for attempt in range(5):
        try:
            source.rename(destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (2**attempt))


def _current_core_digest(core: Path) -> str:
    manifest_path = core / CORE_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "legacy"
    digest = manifest.get("archive_sha256")
    return digest if isinstance(digest, str) else "legacy"


def _validate_core_backup(core: Path) -> None:
    """Reject a backup whose manifest or content no longer matches."""

    manifest_path = core / CORE_MANIFEST_NAME
    if _is_link_or_reparse_point(manifest_path) or not manifest_path.is_file():
        raise DistributionError(f"core backup manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DistributionError(f"core backup manifest is invalid: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise DistributionError(f"core backup manifest is not an object: {manifest_path}")
    if manifest.get("manifest_version") != CORE_MANIFEST_VERSION or manifest.get("package") != PACKAGE_NAME:
        raise DistributionError(f"core backup manifest has an incompatible package: {manifest_path}")
    if not isinstance(manifest.get("core_version"), str) or not manifest["core_version"].strip():
        raise DistributionError(f"core backup manifest has no core version: {manifest_path}")
    if manifest.get("schema_version") != CORE_SCHEMA_VERSION or not _is_sha256(manifest.get("archive_sha256")):
        raise DistributionError(f"core backup manifest has invalid version or archive hash: {manifest_path}")
    content_hash = manifest.get("core_sha256")
    if not _is_sha256(content_hash):
        raise DistributionError(
            f"core backup has no verifiable content hash: {manifest_path}; reinstall it before rollback"
        )
    if _core_content_sha256(core) != content_hash:
        raise DistributionError(f"core backup content hash does not match its manifest: {core}")


def install_core(target_root: Path, archive_path: Path, install_dir: str | Path = ".connection-map") -> CoreInstallResult:
    """Install a source archive, preserving the target-specific files."""

    return _install_core_from_info(target_root, inspect_archive(archive_path), install_dir)


def _install_core_from_info(
    target_root: Path,
    info: ArchiveInfo,
    install_dir: str | Path = ".connection-map",
) -> CoreInstallResult:
    """Install an archive that was already inspected before target mutation."""

    base = _resolve_install_base(target_root, install_dir)
    archive = info.path
    staging = Path(tempfile.mkdtemp(prefix=".core-staging-", dir=base))
    core = base / "core"
    backup_path: Path | None = None
    try:
        _extract_archive(info, staging)
        _write_manifest(staging, info)
        if _sha256(archive) != info.sha256:
            raise DistributionError("source archive changed during installation")

        if _is_link_or_reparse_point(core) or (core.exists() and not core.is_dir()):
            raise DistributionError(f"existing core is not a directory: {core}")
        if core.exists():
            backups = base / "backups"
            _ensure_directory_is_real(backups, label="backup directory")
            backups.mkdir(parents=True, exist_ok=True)
            backup_path = _unique_backup_path(backups, "core", _current_core_digest(core))
            _rename_with_retry(core, backup_path)
        try:
            _rename_with_retry(staging, core)
        except Exception:
            if backup_path is not None and backup_path.exists() and not core.exists():
                _rename_with_retry(backup_path, core)
            raise
        return CoreInstallResult(core, info.version, info.sha256, backup_path)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _select_backup(backups: Path, requested: str | None) -> Path:
    _ensure_directory_is_real(backups, label="backup directory")
    if not backups.is_dir():
        raise DistributionError(f"no core backups found: {backups}")
    if requested is not None:
        candidate_name = Path(requested)
        if candidate_name.is_absolute() or candidate_name.parts != (requested,):
            raise DistributionError("backup must be a directory name inside .connection-map/backups")
        candidate = backups / requested
        if not candidate.is_dir() or _is_link_or_reparse_point(candidate):
            raise DistributionError(f"core backup does not exist: {candidate}")
        return candidate
    candidates = sorted(
        (
            path
            for path in backups.iterdir()
            if path.is_dir()
            and not _is_link_or_reparse_point(path)
            and path.name.startswith("core-")
            and not path.name.startswith("core-rollback-")
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    if not candidates:
        raise DistributionError(f"no core backups found: {backups}")
    return candidates[0]


def rollback_core(
    target_root: Path,
    install_dir: str | Path = ".connection-map",
    backup: str | None = None,
) -> CoreRollbackResult:
    """Restore a previous core backup and preserve the currently active core."""

    base = _resolve_install_base(target_root, install_dir)
    backups = base / "backups"
    selected = _select_backup(backups, backup)
    _validate_core_backup(selected)
    core = base / "core"
    if _is_link_or_reparse_point(core) or (core.exists() and not core.is_dir()):
        raise DistributionError(f"existing core is not a directory: {core}")

    saved_current: Path | None = None
    if core.exists():
        saved_current = _unique_backup_path(backups, "core-rollback", _current_core_digest(core))
        _rename_with_retry(core, saved_current)
    try:
        _rename_with_retry(selected, core)
    except Exception:
        if saved_current is not None and saved_current.exists() and not core.exists():
            _rename_with_retry(saved_current, core)
        raise
    return CoreRollbackResult(core, selected.name, saved_current)
