"""Fetch a pinned minimal Python runtime for portable release builds.

The runtime is a build input, not a tracked repository asset.  The archive is
verified before extraction and is never imported or executed by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

RUNTIMES = {
    ("3.13.15", "amd64"): {
        "url": "https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip",
        "sha256": "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf",
    },
}

STANDALONE_RUNTIMES = {
    "linux-x86_64": {
        "filename": "cpython-3.13.15+20260814-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "sha256": "aaca2af2ab4d7b68a712660d1334c0cfd5ec13c0312ccd30c29122d8d0342320",
    },
    "macos-arm64": {
        "filename": "cpython-3.13.15+20260814-aarch64-apple-darwin-install_only_stripped.tar.gz",
        "sha256": "6d472fc49a4d95e58214a992c4c92aa73fe2a935837a01a9a36bab0bec6d72f3",
    },
    "macos-x86_64": {
        "filename": "cpython-3.13.15+20260814-x86_64-apple-darwin-install_only_stripped.tar.gz",
        "sha256": "bf87354efcd9ae517da606fcda4e3a3f0d73a6f05ca7cba3c6d3c5270074bfc8",
    },
}
STANDALONE_RELEASE = "20260814"


def _safe_member(name: str) -> PurePosixPath:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        raise ValueError(f"unsafe runtime archive member: {name!r}")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or PureWindowsPath(name).drive
        or any(":" in part or part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe runtime archive member: {name!r}")
    return path


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "connection-analysis-mapping-runtime-fetch/1"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _extract_verified(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        names = [_safe_member(member.filename) for member in members]
        if len(names) != len(set(names)):
            raise ValueError("runtime archive contains duplicate members")
        for member, relative in zip(members, names, strict=True):
            if member.is_dir():
                continue
            unix_mode = (member.external_attr >> 16) & 0o170000
            if not member.filename or (unix_mode and unix_mode != 0o100000):
                raise ValueError(f"runtime archive contains an unsupported entry: {member.filename}")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _extract_tar_verified(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [_safe_member(member.name) for member in members]
        if len(names) != len(set(names)):
            raise ValueError("runtime archive contains duplicate members")
        member_by_name = {relative: member for member, relative in zip(members, names, strict=True)}
        link_targets: dict[PurePosixPath, PurePosixPath] = {}
        for member, relative in zip(members, names, strict=True):
            if member.isdir():
                target = destination.joinpath(*relative.parts)
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym():
                linkname = member.linkname
                if not linkname or linkname.startswith(("/", "\\")) or "\\" in linkname:
                    raise ValueError(f"runtime archive contains an unsafe symlink: {member.name}")
                combined = relative.parent / PurePosixPath(linkname)
                parts: list[str] = []
                for part in combined.parts:
                    if part in {"", "."}:
                        continue
                    if part == "..":
                        if not parts:
                            raise ValueError(f"runtime archive symlink escapes root: {member.name}")
                        parts.pop()
                    else:
                        parts.append(part)
                if not parts:
                    raise ValueError(f"runtime archive symlink has an empty target: {member.name}")
                link_target = PurePosixPath(*parts)
                target_member = member_by_name.get(link_target)
                if target_member is None or not (target_member.isreg() or target_member.issym()):
                    raise ValueError(f"runtime archive symlink target is missing: {member.name}")
                link_targets[relative] = link_target
                continue
            if not member.isreg():
                raise ValueError(f"runtime archive contains an unsupported entry: {member.name}")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"runtime archive member is unreadable: {member.name}")
            with extracted, target.open("wb") as output:
                shutil.copyfileobj(extracted, output)
            target.chmod(member.mode & 0o777)

        resolved_links: dict[PurePosixPath, PurePosixPath] = {}

        def resolve_link(relative: PurePosixPath, active: set[PurePosixPath]) -> PurePosixPath:
            if relative in resolved_links:
                return resolved_links[relative]
            if relative in active:
                raise ValueError(f"runtime archive contains a symlink cycle: {relative}")
            active.add(relative)
            target = link_targets[relative]
            resolved = resolve_link(target, active) if target in link_targets else target
            active.remove(relative)
            resolved_links[relative] = resolved
            return resolved

        for relative in link_targets:
            resolved = resolve_link(relative, set())
            source = destination.joinpath(*resolved.parts)
            target = destination.joinpath(*relative.parts)
            if not source.is_file():
                raise ValueError(f"runtime archive symlink target is not a regular file: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            # A case-sensitive archive can contain links such as ``N/foo``
            # -> ``n/foo``.  On Windows both names resolve to the same file;
            # the materialized link is already present in that case.
            if os.path.normcase(str(source.resolve())) == os.path.normcase(str(target.resolve())):
                continue
            shutil.copyfile(source, target)
            target.chmod(source.stat().st_mode & 0o777)


def _prune_standalone_runtime(root: Path) -> None:
    """Remove build/developer assets while retaining the complete stdlib."""

    removable = (
        root / "include",
        root / "share",
        root / "lib" / "pkgconfig",
        root / "lib" / "itcl4.3.8",
        root / "lib" / "tcl9",
        root / "lib" / "tcl9.0",
        root / "lib" / "tk9.0",
        root / "lib" / "thread3.0.6",
        root / "lib" / "libpython3.so",
        root / "lib" / "libpython3.13.so",
        root / "lib" / "libtcl9.0.so",
        root / "lib" / "libtcl9tk9.0.so",
        root / "lib" / "python3.13" / "site-packages",
        root / "lib" / "python3.13" / "ensurepip",
        root / "lib" / "python3.13" / "idlelib",
        root / "lib" / "python3.13" / "tkinter",
        root / "lib" / "python3.13" / "turtledemo",
        root / "lib" / "python3.13" / "pydoc.py",
        root / "lib" / "python3.13" / "pydoc_data",
        root / "bin" / "idle3",
        root / "bin" / "idle3.13",
        root / "bin" / "pydoc3",
        root / "bin" / "pydoc3.13",
        root / "bin" / "pip",
        root / "bin" / "pip3",
        root / "bin" / "pip3.13",
        root / "bin" / "python",
        root / "bin" / "python3",
        root / "bin" / "python3-config",
        root / "bin" / "python3.13-config",
    )
    for path in removable:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a pinned, minimal Python runtime for a portable build.")
    parser.add_argument("--output", type=Path, required=True, help="directory to receive the extracted runtime")
    parser.add_argument("--provider", choices=("official-windows", "python-build-standalone"), default="official-windows")
    parser.add_argument("--version", default="3.13.15")
    parser.add_argument(
        "--target",
        choices=("windows-amd64", "linux-x86_64", "macos-arm64", "macos-x86_64"),
        default="windows-amd64",
    )
    parser.add_argument("--architecture", choices=("amd64",), default="amd64", help=argparse.SUPPRESS)
    parser.add_argument("--force", action="store_true", help="replace an existing output directory")
    args = parser.parse_args()

    if args.provider == "official-windows":
        if args.target != "windows-amd64":
            raise ValueError("official-windows currently supports only --target windows-amd64")
        metadata = RUNTIMES.get((args.version, args.architecture))
        if metadata is None:
            supported = ", ".join(f"{version}/{architecture}" for version, architecture in RUNTIMES)
            raise ValueError(f"unsupported official runtime; supported pinned inputs: {supported}")
        url = metadata["url"]
        expected_sha256 = metadata["sha256"]
        archive_kind = "zip"
    else:
        if args.version != "3.13.15":
            raise ValueError("python-build-standalone is pinned to Python 3.13.15 in this build")
        if args.target == "windows-amd64":
            raise ValueError("use official-windows for the smaller Windows embeddable runtime")
        metadata = STANDALONE_RUNTIMES[args.target]
        url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{STANDALONE_RELEASE}/{metadata['filename']}"
        expected_sha256 = metadata["sha256"]
        if expected_sha256 == "TODO":
            raise ValueError(f"runtime checksum is not pinned yet for {args.target}")
        archive_kind = "tar"
    output = args.output.resolve()
    if output.exists():
        if not output.is_dir():
            raise ValueError(f"runtime output is not a directory: {output}")
        if not args.force:
            raise ValueError(f"runtime output already exists: {output}; use --force to replace it")
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="connection-map-python-") as temporary:
        archive = Path(temporary) / "python-embed.zip"
        _download(url, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise ValueError(f"Python runtime SHA-256 mismatch: expected {expected_sha256}, got {digest}")
        staging = Path(temporary) / "runtime"
        staging.mkdir()
        if archive_kind == "zip":
            _extract_verified(archive, staging)
            source = staging
        else:
            _extract_tar_verified(archive, staging)
            children = list(staging.iterdir())
            if len(children) != 1 or not children[0].is_dir():
                raise ValueError("python-build-standalone archive must contain one runtime directory")
            source = children[0]
            _prune_standalone_runtime(source)
        source.rename(output)
    print(f"extracted Python {args.version} {args.target} runtime to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
