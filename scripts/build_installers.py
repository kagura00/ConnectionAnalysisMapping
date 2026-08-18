"""Build GitHub Release installer media into an ignored directory."""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import textwrap
import tomllib
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

try:
    from scripts.build_release_manifest import write_manifest
except ModuleNotFoundError:  # Executed directly as scripts/build_installers.py.
    from build_release_manifest import write_manifest

try:
    from scripts.third_party_notices import (
        read_runtime_origin,
        render_third_party_notices,
        sha256_bytes,
    )
except ModuleNotFoundError:  # Executed directly as scripts/build_installers.py.
    from third_party_notices import read_runtime_origin, render_third_party_notices, sha256_bytes


PAYLOAD_MARKER = "__CONNECTION_MAP_PAYLOAD__"

# The launcher cannot import the embedded package until the archive has been
# checked.  Keep this bootstrap limited to Python's standard library so a
# malformed payload cannot replace the validation code through PYTHONPATH.
_POSIX_BOOTSTRAP = r'''from __future__ import annotations

import runpy
import shutil
import sys
import tarfile
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath

PACKAGE_NAME = "connection-analysis-mapping"
payload = Path(sys.argv[1]).resolve()
extract_root = Path(sys.argv[2]).resolve()


def member_parts(name: str) -> tuple[str, ...]:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        raise ValueError(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).drive or any(":" in part or part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive member path: {name!r}")
    return path.parts


with tarfile.open(payload, mode="r:*") as archive:
    members = archive.getmembers()
    if not members:
        raise ValueError("source archive is empty")
    names = [member.name for member in members]
    if len(names) != len(set(names)):
        raise ValueError("source archive contains duplicate member names")
    parts_by_member = {member: member_parts(member.name) for member in members}
    top_levels = {parts[0] for parts in parts_by_member.values()}
    if len(top_levels) != 1:
        raise ValueError("source archive must contain exactly one top-level directory")
    root_name = next(iter(top_levels))
    for member in members:
        if not (member.isdir() or member.isreg()):
            raise ValueError(f"archive contains unsupported entry: {member.name}")
    required = {
        (root_name, "pyproject.toml"),
        (root_name, "src", "connection_map", "__init__.py"),
        (root_name, "src", "connection_map", "__main__.py"),
        (root_name, "src", "connection_map", "cli.py"),
        (root_name, "src", "connection_map", "installer.py"),
        (root_name, "src", "connection_map", "web", "index.html"),
    }
    available = set(parts_by_member.values())
    if required - available:
        missing = ", ".join("/".join(parts) for parts in sorted(required - available))
        raise ValueError(f"archive is missing required files: {missing}")
    pyproject_member = next(member for member, parts in parts_by_member.items() if parts == (root_name, "pyproject.toml"))
    pyproject_file = archive.extractfile(pyproject_member)
    if pyproject_file is None:
        raise ValueError("archive pyproject.toml is not a regular file")
    try:
        project = tomllib.loads(pyproject_file.read().decode("utf-8")).get("project", {})
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("archive pyproject.toml is invalid") from exc
    if not isinstance(project, dict) or project.get("name") != PACKAGE_NAME:
        raise ValueError(f"archive project name must be {PACKAGE_NAME!r}")
    if not isinstance(project.get("version"), str) or not project["version"].strip():
        raise ValueError("archive project version is missing")

    source_root = extract_root / root_name
    for member, parts in parts_by_member.items():
        relative_parts = parts[1:]
        if not relative_parts:
            continue
        destination = source_root.joinpath(*relative_parts)
        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        extracted = archive.extractfile(member)
        if extracted is None:
            raise ValueError(f"archive entry is not readable: {member.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            shutil.copyfileobj(extracted, output)

sys.path.insert(0, str(source_root / "src"))
sys.argv = ["connection-map-installer", "--cli", "--archive", str(payload), *sys.argv[3:]]
runpy.run_module("connection_map.installer", run_name="__main__")
'''


def _project_version(project_file: Path) -> str:
    payload = tomllib.loads(project_file.read_text(encoding="utf-8"))
    version = payload.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("project.version is missing")
    return version


def _find_source_archive(dist: Path, version: str, requested: Path | None) -> Path:
    archive = requested or dist / f"connection_analysis_mapping-{version}.tar.gz"
    archive = archive.resolve()
    if not archive.is_file():
        raise ValueError(f"source archive does not exist: {archive}")
    return archive


def _copy_python_distributions(dist: Path, output: Path, source_archive: Path, version: str) -> list[Path]:
    wheel_candidates = sorted(dist.glob(f"connection_analysis_mapping-{version}-*.whl"))
    if not wheel_candidates:
        raise ValueError(f"no wheel found for version {version} in {dist}")
    copied = [output / source_archive.name]
    shutil.copy2(source_archive, copied[0])
    for wheel in wheel_candidates:
        destination = output / wheel.name
        shutil.copy2(wheel, destination)
        copied.append(destination)
    return copied


def _archive_member_parts(name: str) -> tuple[str, ...]:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        raise ValueError(f"unsafe source archive member: {name!r}")
    parts = PurePosixPath(name).parts
    if not parts or PureWindowsPath(name).drive or any(":" in part or part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe source archive member: {name!r}")
    return parts


def _write_zip_entry(archive: zipfile.ZipFile, name: str, content: str | bytes, *, mode: int = 0o644) -> None:
    """Write a reproducible regular file entry with explicit Unix metadata."""

    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100000 | mode) << 16
    archive.writestr(info, content.encode("utf-8") if isinstance(content, str) else content)


def _find_portable_runtime(runtime: Path) -> tuple[Path, Path, str]:
    """Find a self-contained Python runtime and return root, executable, version."""

    root = runtime.resolve()
    if not root.is_dir():
        raise ValueError(f"portable Python runtime is not a directory: {root}")
    candidates = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name in {"python.exe", "python3.13", "python3", "python"}
    ]
    if not candidates:
        raise ValueError(f"portable Python runtime has no python executable: {root}")
    executable = candidates[0]
    # python-build-standalone keeps the interpreter in ``root/bin`` while
    # Windows' embeddable distribution keeps it at ``root``.  The portable
    # archive must preserve the directory above ``bin`` so its lib/ tree is
    # available at runtime.
    runtime_root = executable.parent.parent if executable.parent.name == "bin" else executable.parent
    for path in runtime_root.rglob("*"):
        if path.is_symlink() or (path.exists() and not path.is_file() and not path.is_dir()):
            raise ValueError(f"portable Python runtime contains an unsupported entry: {path}")
    try:
        completed = subprocess.run(
            [str(executable), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')"],
            check=True,
            capture_output=True,
            text=True,
            cwd=runtime_root,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"portable Python runtime could not be executed: {executable}") from exc
    version = completed.stdout.strip()
    try:
        major, minor, _patch = (int(part) for part in version.split(".", 2))
    except ValueError as exc:
        raise ValueError(f"portable Python runtime reported an invalid version: {version!r}") from exc
    if (major, minor) < (3, 11):
        raise ValueError(f"portable Python runtime must be 3.11 or newer: {version}")
    return runtime_root, executable, version


def _write_portable_runtime(archive: zipfile.ZipFile, runtime: Path) -> dict[str, object]:
    runtime_root, executable, version = _find_portable_runtime(runtime)
    origin = read_runtime_origin(runtime_root / "runtime-origin.json")
    if origin["python_version"] != version:
        raise ValueError(
            "portable Python runtime version does not match runtime-origin.json: "
            f"{version} != {origin['python_version']}"
        )
    license_paths = tuple(origin["license_files"])
    for relative in license_paths:
        license_path = runtime_root.joinpath(*PurePosixPath(relative).parts)
        try:
            license_path.relative_to(runtime_root)
        except ValueError as exc:
            raise ValueError(f"portable runtime license path escapes the runtime root: {relative}") from exc
        if license_path.is_symlink() or not license_path.is_file():
            raise ValueError(f"portable runtime license file is missing or unsafe: {relative}")
    entries: list[str] = []
    total_bytes = 0
    for path in sorted(runtime_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(runtime_root).as_posix()
        content = path.read_bytes()
        # The Windows embeddable distribution uses a ._pth file for an
        # isolated import path.  Point it at the replaceable application
        # source so updating app/ does not require rebuilding Python.
        if path.name.endswith("._pth"):
            try:
                lines = content.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise ValueError(f"portable Python path file is not UTF-8: {path}") from exc
            app_source = "../app/source/src"
            if app_source not in lines:
                lines.append(app_source)
            content = ("\n".join(lines) + "\n").encode("utf-8")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o111:
            mode |= 0o111
        _write_zip_entry(archive, f"connection-map-portable/runtime/{relative}", content, mode=mode or 0o644)
        entries.append(relative)
        total_bytes += len(content)
    license_digests: dict[str, str] = {}
    for relative in license_paths:
        license_path = runtime_root.joinpath(*PurePosixPath(relative).parts)
        content = license_path.read_bytes()
        _write_zip_entry(archive, f"connection-map-portable/licenses/runtime/{relative}", content)
        license_digests[relative] = sha256_bytes(content)
    executable_relative = executable.relative_to(runtime_root).as_posix()
    metadata = {
        "format": "connection-analysis-portable-runtime",
        "schema_version": "1.0",
        "python_version": version,
        "interpreter": executable_relative,
        "files": entries,
        "bytes": total_bytes,
        "origin": origin,
        "license_files": list(license_paths),
        "license_sha256": license_digests,
    }
    _write_zip_entry(
        archive,
        "connection-map-portable/runtime/runtime.json",
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return metadata


def _write_portable_zip(source_archive: Path, output: Path, version: str, *, runtime_dir: Path | None = None) -> Path:
    """Create a portable tree with replaceable app/data and optional runtime."""

    destination = output / f"connection-map-portable-{version}.zip"
    # Keep the extracted directory stable across releases.  The data directory
    # is intentionally not populated with registry.json: extracting a newer
    # archive must never reset the user's repository catalog.
    root_name = "connection-map-portable"
    windows_launcher = """@echo off
setlocal
set "ROOT=%~dp0.."
set "APP=%ROOT%\\app\\source"
set "CONNECTION_MAP_WORKSPACE=%ROOT%\\data"
set "BUNDLED_PYTHON=%ROOT%\\runtime\\python.exe"
if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" -m connection_map %*
  exit /b %ERRORLEVEL%
)
if "%CONNECTION_MAP_PYTHON%"=="" set "CONNECTION_MAP_PYTHON=python"
"%CONNECTION_MAP_PYTHON%" -c "import operator,sys; raise SystemExit(0 if operator.ge(sys.version_info[:2], (3, 11)) else 2)"
if errorlevel 1 (echo Python 3.11以上が必要です。CONNECTION_MAP_PYTHONで指定できます。 1>&2 & exit /b 2)
set "PYTHONPATH=%APP%\\src;%PYTHONPATH%"
"%CONNECTION_MAP_PYTHON%" -m connection_map %*
"""
    posix_launcher = """#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP="$ROOT/app/source"
export CONNECTION_MAP_WORKSPACE="$ROOT/data"
if [ -x "$ROOT/runtime/bin/python3.13" ]; then
  PYTHON_BIN="$ROOT/runtime/bin/python3.13"
elif [ -x "$ROOT/runtime/bin/python3" ]; then
  PYTHON_BIN="$ROOT/runtime/bin/python3"
elif [ -x "$ROOT/runtime/python3" ]; then
  PYTHON_BIN="$ROOT/runtime/python3"
elif [ -x "$ROOT/runtime/python" ]; then
  PYTHON_BIN="$ROOT/runtime/python"
else
  PYTHON_BIN=${CONNECTION_MAP_PYTHON:-python3}
fi
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 2)'
export PYTHONPATH="$APP/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" -m connection_map "$@"
"""
    runtime_note = ""
    if runtime_dir is not None:
        runtime_note = "A private Python runtime is included under runtime; app/source remains replaceable.\n\n"
    readme = (
        f"Connection Analysis Mapping {version} portable package\n\n"
        "Application source is under app/source and persistent workspace data "
        "is under data. Run launcher/connection-map.cmd on Windows or "
        "launcher/connection-map.sh on POSIX systems.\n\n"
        "License information is available at LICENSE.txt and "
        "THIRD_PARTY_NOTICES.md; original license texts are under licenses/.\n\n"
        f"{runtime_note}"
        "If runtime/ is absent, Python 3.11 or newer is required. Optional parser "
        "dependencies are installed in a source or local Python environment and "
        "are not included in the base runtime.\n\n"
        "The data directory is created and populated on first use. Update by "
        "replacing app/, launcher/, and (when present) runtime/ from the same "
        "release while retaining data/. Do not mix runtime/ from different "
        "releases.\n"
    )
    runtime_metadata: dict[str, object] | None = None
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_zip_entry(
            archive,
            f"{root_name}/data/README.txt",
            "This directory contains persistent repository data. Do not replace it during updates.\n",
        )
        _write_zip_entry(archive, f"{root_name}/data/repositories/.gitkeep", "")
        _write_zip_entry(archive, f"{root_name}/launcher/connection-map.cmd", windows_launcher.replace("\n", "\r\n"))
        _write_zip_entry(archive, f"{root_name}/launcher/connection-map.sh", posix_launcher, mode=0o755)
        _write_zip_entry(archive, f"{root_name}/README.txt", readme)
        if runtime_dir is not None:
            runtime_metadata = _write_portable_runtime(archive, runtime_dir)
        with tarfile.open(source_archive, mode="r:*") as tar:
            members = tar.getmembers()
            names = [_archive_member_parts(member.name) for member in members]
            if not members or len(names) != len(set(names)) or len({parts[0] for parts in names}) != 1:
                raise ValueError("source archive must contain one top-level directory with unique members")
            root = names[0][0]
            required = {
                (root, "pyproject.toml"),
                (root, "LICENSE"),
                (root, "src", "connection_map", "__init__.py"),
                (root, "src", "connection_map", "__main__.py"),
                (root, "src", "connection_map", "cli.py"),
                (root, "src", "connection_map", "installer.py"),
                (root, "src", "connection_map", "web", "index.html"),
            }
            missing = required - set(names)
            if missing:
                rendered = ", ".join("/".join(parts) for parts in sorted(missing))
                raise ValueError(f"source archive is missing required files: {rendered}")
            pyproject_member = next(member for member, parts in zip(members, names, strict=True) if parts == (root, "pyproject.toml"))
            pyproject_file = tar.extractfile(pyproject_member)
            if pyproject_file is None:
                raise ValueError("source archive pyproject.toml is not readable")
            try:
                project = tomllib.loads(pyproject_file.read().decode("utf-8")).get("project", {})
            except (UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise ValueError("source archive pyproject.toml is invalid") from exc
            if not isinstance(project, dict) or project.get("name") != "connection-analysis-mapping":
                raise ValueError("source archive project name must be 'connection-analysis-mapping'")
            if project.get("version") != version:
                raise ValueError(f"source archive project version must be {version!r}")
            project_license: bytes | None = None
            for member, parts in zip(members, names, strict=True):
                if not (member.isdir() or member.isreg()):
                    raise ValueError(f"source archive contains unsupported entry: {member.name}")
                relative = parts[1:]
                if not relative or member.isdir():
                    continue
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    raise ValueError(f"source archive member is unreadable: {member.name}")
                content = fileobj.read()
                if relative == ("LICENSE",):
                    project_license = content
                _write_zip_entry(archive, f"{root_name}/app/source/{'/'.join(relative)}", content)
        if project_license is None:
            raise ValueError("source archive project LICENSE is missing or unreadable")
        _write_zip_entry(archive, f"{root_name}/LICENSE.txt", project_license)
        _write_zip_entry(archive, f"{root_name}/licenses/ConnectionAnalysisMapping-MIT.txt", project_license)
        runtime_origin = runtime_metadata.get("origin") if runtime_metadata is not None else None
        runtime_license_paths = (
            tuple(runtime_metadata["license_files"])
            if runtime_metadata is not None
            else ()
        )
        notices = render_third_party_notices(
            version=version,
            runtime_origin=runtime_origin if isinstance(runtime_origin, dict) else None,
            runtime_license_paths=runtime_license_paths,
        )
        _write_zip_entry(archive, f"{root_name}/THIRD_PARTY_NOTICES.md", notices)
        _write_zip_entry(archive, f"{root_name}/app/README.txt", "Bundled source is under app/source. Keep this directory replaceable during updates.\n")
    return destination


def _write_posix_launcher(source_archive: Path, output: Path, version: str) -> Path:
    destination = output / f"connection-map-install-{version}-posix.sh"
    encoded = base64.b64encode(source_archive.read_bytes()).decode("ascii")
    wrapped_payload = "\n".join(textwrap.wrap(encoded, width=76))
    script = f"""#!/bin/sh
set -eu

PYTHON_BIN="${{CONNECTION_MAP_PYTHON:-python3}}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.11以上が必要です。CONNECTION_MAP_PYTHONで実行ファイルを指定できます。" >&2
  exit 2
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  echo "Python 3.11以上が必要です。" >&2
  exit 2
fi

PAYLOAD_LINE="$(awk '/^{PAYLOAD_MARKER}$/ {{ print NR + 1; exit }}' "$0")"
if [ -z "$PAYLOAD_LINE" ]; then
  echo "インストーラーpayloadが見つかりません。" >&2
  exit 2
fi

TMP_DIR="$(mktemp -d "${{TMPDIR:-/tmp}}/connection-map-installer.XXXXXX")"
cleanup() {{ rm -rf "$TMP_DIR"; }}
trap cleanup EXIT HUP INT TERM
PAYLOAD="$TMP_DIR/installer-core.tar.gz"
case "$(uname -s)" in
  Darwin*) tail -n +"$PAYLOAD_LINE" "$0" | base64 -D > "$PAYLOAD" ;;
  *) tail -n +"$PAYLOAD_LINE" "$0" | base64 -d > "$PAYLOAD" ;;
esac
mkdir "$TMP_DIR/extracted"
"$PYTHON_BIN" - "$PAYLOAD" "$TMP_DIR/extracted" "$@" <<'PY'
{_POSIX_BOOTSTRAP}
PY
exit 0

{PAYLOAD_MARKER}
{wrapped_payload}
"""
    destination.write_text(script, encoding="utf-8", newline="\n")
    try:
        destination.chmod(0o755)
    except OSError:
        pass
    return destination


def _windows_architecture() -> str:
    machine = platform.machine().lower()
    return "arm64" if "arm64" in machine or "aarch64" in machine else "x64"


def _build_windows_executables(source_archive: Path, output: Path, version: str, repository_root: Path) -> list[Path]:
    architecture = _windows_architecture()
    entrypoint = repository_root / "scripts" / "run_installer.py"
    work_root = repository_root / ".tmp" / "pyinstaller"
    work_root.mkdir(parents=True, exist_ok=True)
    bundled_archive = work_root / "installer-core.tar.gz"
    shutil.copy2(source_archive, bundled_archive)
    results: list[Path] = []
    for suffix, windowed in (("", True), ("-cli", False)):
        name = f"connection-map-install-{version}-windows-{architecture}{suffix}"
        work_dir = work_root / name
        if work_dir.exists():
            shutil.rmtree(work_dir)
        command = [
            sys.executable,
            str(repository_root / "scripts" / "build_pyinstaller.py"),
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            name,
            "--distpath",
            str(output),
            "--workpath",
            str(work_dir),
            "--specpath",
            str(work_dir),
            "--paths",
            str(repository_root / "src"),
            "--add-data",
            f"{bundled_archive}{os.pathsep}connection_map",
        ]
        command.append("--windowed" if windowed else "--console")
        command.append(str(entrypoint))
        try:
            subprocess.run(command, check=True, cwd=repository_root)
        except FileNotFoundError as exc:
            raise ValueError("PyInstaller is not installed; run uv sync --extra packaging") from exc
        result = output / f"{name}.exe"
        if not result.is_file():
            raise ValueError(f"PyInstaller did not create {result}")
        results.append(result)
    return results


def _clean_output(output: Path) -> None:
    if not output.exists():
        return
    allowed_prefixes = ("connection_analysis_mapping-", "connection-map-install-", "connection-map-portable-")
    for path in output.iterdir():
        if path.name == "SHA256SUMS.txt":
            path.unlink()
            continue
        if not path.is_file() or not path.name.startswith(allowed_prefixes):
            raise ValueError(
                f"refusing to clean an unexpected release output entry: {path}. "
                "Use a dedicated release-artifacts directory."
            )
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build installer media for a GitHub Release.")
    parser.add_argument("--dist", type=Path, default=Path("dist"), help="uv build output directory")
    parser.add_argument("--output", type=Path, default=Path("release-artifacts"))
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument(
        "--python-runtime",
        type=Path,
        help="self-contained Python runtime directory to include in the portable zip",
    )
    parser.add_argument(
        "--allow-system-python",
        action="store_true",
        help="build a developer portable zip without an included Python runtime",
    )
    parser.add_argument(
        "--clean",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="clear the dedicated output directory first (default; use --no-clean to retain compatible artifacts)",
    )
    args = parser.parse_args()

    repository_root = Path(__file__).resolve().parents[1]
    project_file = repository_root / "pyproject.toml"
    version = _project_version(project_file)
    dist = args.dist.resolve()
    output = args.output.resolve()
    source_archive = _find_source_archive(dist, version, args.source_archive)
    if output == dist:
        raise ValueError("output must not be the uv build directory")
    runtime_dir = args.python_runtime or (
        Path(os.environ["CONNECTION_MAP_PYTHON_RUNTIME"])
        if os.environ.get("CONNECTION_MAP_PYTHON_RUNTIME")
        else None
    )
    if runtime_dir is None and not args.allow_system_python:
        raise ValueError(
            "portable builds require --python-runtime (or CONNECTION_MAP_PYTHON_RUNTIME); "
            "use --allow-system-python only for developer smoke tests"
        )
    output.mkdir(parents=True, exist_ok=True)
    if args.clean:
        _clean_output(output)

    _copy_python_distributions(dist, output, source_archive, version)
    generated = [
        _write_posix_launcher(source_archive, output, version),
        _write_portable_zip(source_archive, output, version, runtime_dir=runtime_dir),
    ]
    if os.name == "nt":
        generated.extend(_build_windows_executables(source_archive, output, version, repository_root))
    else:
        print("Windows exe generation skipped on non-Windows host.")
    manifest = output / "SHA256SUMS.txt"
    artifacts = write_manifest(output, manifest, include_all=True)
    print(f"wrote {output} ({len(artifacts)} artifacts + checksum)")
    for path in generated:
        print(f"generated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
