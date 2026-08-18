"""Build-time license inventory helpers for portable distributions.

The portable archive is assembled from a source archive and an externally
fetched Python runtime.  This module keeps the provenance and license checks
in one standard-library-only place so the release builder cannot silently
turn an incomplete runtime into a distributable archive.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

RUNTIME_ORIGIN_FORMAT = "connection-analysis-runtime-origin"
RUNTIME_ORIGIN_SCHEMA_VERSION = "1.0"

_LICENSE_NAMES = {"license", "copying", "notice", "copyright"}
_LICENSE_DIR_NAMES = {"license", "licenses"}
_NON_TEXT_LICENSE_DIR_SUFFIXES = {".py", ".pyc", ".pyo", ".so", ".dll", ".dylib"}


def sha256_bytes(content: bytes) -> str:
    """Return the SHA-256 digest used in release inventories."""

    return hashlib.sha256(content).hexdigest()


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_relative(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"runtime origin {field} must be a relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise ValueError(f"runtime origin {field} contains an unsafe path: {value!r}")
    return path.as_posix()


def find_license_files(root: Path) -> tuple[str, ...]:
    """Find likely license and notice documents below an extracted runtime.

    The result is deliberately conservative: files are selected by the
    conventional names used by the providers, license directories, and the
    ``share/doc/**/copyright`` convention.  The provider-specific result is
    recorded in runtime-origin.json and validated again during packaging.
    """

    root = root.resolve()
    if not root.is_dir() or _is_link_or_reparse_point(root):
        raise ValueError(f"runtime license root is not a real directory: {root}")
    found: set[str] = set()
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        if _is_link_or_reparse_point(path) or not path.is_file():
            continue
        relative = path.relative_to(root)
        parts = tuple(part.casefold() for part in relative.parts)
        name = parts[-1]
        conventional_name = (
            name in _LICENSE_NAMES
            or name.startswith("license.")
            or name.startswith("copying.")
            or name.startswith("notice.")
        )
        in_license_directory = any(part in _LICENSE_DIR_NAMES for part in parts[:-1]) and path.suffix.casefold() not in _NON_TEXT_LICENSE_DIR_SUFFIXES
        in_share_doc = len(parts) >= 3 and parts[0:2] == ("share", "doc") and name == "copyright"
        if conventional_name or in_license_directory or in_share_doc:
            found.add(relative.as_posix())
    return tuple(sorted(found))


def validate_runtime_origin(payload: Any) -> dict[str, Any]:
    """Validate and return runtime provenance metadata."""

    if not isinstance(payload, dict):
        raise ValueError("runtime origin metadata must be an object")
    if payload.get("format") != RUNTIME_ORIGIN_FORMAT:
        raise ValueError("runtime origin metadata has an unsupported format")
    if payload.get("schema_version") != RUNTIME_ORIGIN_SCHEMA_VERSION:
        raise ValueError("runtime origin metadata has an unsupported schema version")
    for field in ("provider", "python_version", "target", "source_url", "archive_filename", "archive_sha256"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"runtime origin metadata field is missing: {field}")
    parsed_url = urlsplit(payload["source_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("runtime origin source_url must be an absolute HTTP(S) URL")
    archive_filename = PurePosixPath(payload["archive_filename"])
    if (
        archive_filename.is_absolute()
        or PureWindowsPath(payload["archive_filename"]).drive
        or archive_filename.parts != (payload["archive_filename"],)
    ):
        raise ValueError("runtime origin archive_filename must be a file name")
    archive_sha256 = payload["archive_sha256"]
    if len(archive_sha256) != 64 or any(character not in "0123456789abcdef" for character in archive_sha256):
        raise ValueError("runtime origin archive_sha256 must be a lowercase SHA-256 digest")
    provider_release = payload.get("provider_release")
    if provider_release is not None and (not isinstance(provider_release, str) or not provider_release.strip()):
        raise ValueError("runtime origin provider_release must be a non-empty string or null")
    license_files = payload.get("license_files")
    if not isinstance(license_files, list) or not license_files:
        raise ValueError("runtime origin license_files must contain at least one path")
    normalized = [_safe_relative(value, field="license_files") for value in license_files]
    if len(normalized) != len(set(normalized)):
        raise ValueError("runtime origin license_files contains duplicate paths")
    result = dict(payload)
    result["license_files"] = normalized
    return result


def read_runtime_origin(path: Path) -> dict[str, Any]:
    """Read and validate a runtime-origin.json file."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime origin metadata is unreadable: {path}") from exc
    return validate_runtime_origin(payload)


def write_runtime_origin(path: Path, payload: dict[str, Any]) -> None:
    """Validate and write deterministic runtime provenance metadata."""

    validated = validate_runtime_origin(payload)
    path.write_text(json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def render_third_party_notices(
    *,
    version: str,
    runtime_origin: dict[str, Any] | None,
    runtime_license_paths: tuple[str, ...],
) -> str:
    """Render a portable-package inventory without making legal guarantees."""

    lines = [
        "# Third-party notices",
        "",
        "This file is an inventory of the components and license texts included in this portable package.",
        "It is not a legal opinion or a substitute for reviewing the original license texts.",
        "",
        "## Connection Analysis Mapping",
        "",
        f"- Version: `{version}`",
        "- License: MIT",
        "- License text: `LICENSE.txt` and `licenses/ConnectionAnalysisMapping-MIT.txt`",
        "",
    ]
    if runtime_origin is None:
        lines.extend(
            [
                "## Python runtime",
                "",
                "This portable package does not bundle a Python runtime.",
                "Python 3.11 or newer must be provided by the host environment.",
                "No third-party runtime license is included in this package.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## Bundled Python runtime",
                "",
                f"- Provider: `{runtime_origin['provider']}`",
                f"- Provider release: `{runtime_origin.get('provider_release') or 'not specified'}`",
                f"- Python version: `{runtime_origin['python_version']}`",
                f"- Target: `{runtime_origin['target']}`",
                f"- Source archive: `{runtime_origin['archive_filename']}`",
                f"- Source URL: <{runtime_origin['source_url']}>",
                f"- Verified archive SHA-256: `{runtime_origin['archive_sha256']}`",
                "- Origin metadata: `runtime/runtime.json` and `runtime/runtime-origin.json`",
                "",
                "License and notice texts copied from the verified runtime:",
                "",
            ]
        )
        for path in runtime_license_paths:
            lines.append(f"- `licenses/runtime/{path}` (source runtime path: `{path}`)")
        lines.append("")
    lines.extend(
        [
            "## Optional dependencies",
            "",
            "Tree-sitter language packs, SQLGlot, and other optional parser dependencies are not bundled in the base portable runtime.",
            "If they are installed separately for analysis, their licenses are governed by that environment and are not represented by this package inventory.",
            "",
        ]
    )
    return "\n".join(lines)
