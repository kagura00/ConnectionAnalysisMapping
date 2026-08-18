from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from build_installers import _find_portable_runtime
from fetch_python_runtime import _extract_tar_verified, _safe_member
from third_party_notices import find_license_files, read_runtime_origin, write_runtime_origin


def test_find_portable_runtime_keeps_posix_lib_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    executable = runtime / "bin" / "python3"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder")
    (runtime / "lib" / "python3.13").mkdir(parents=True)

    class Completed:
        stdout = "3.13.15\n"

    monkeypatch.setattr("build_installers.subprocess.run", lambda *args, **kwargs: Completed())
    runtime_root, found_executable, version = _find_portable_runtime(runtime)

    assert runtime_root == runtime.resolve()
    assert found_executable == executable
    assert version == "3.13.15"


def _write_tar(path: Path, members: list[tuple[str, bytes, int]]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for name, content, mode in members:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = mode
            archive.addfile(member, io.BytesIO(content))


def test_extract_tar_verified_preserves_runtime_tree_and_rejects_symlink(tmp_path: Path) -> None:
    archive_path = tmp_path / "runtime.tar.gz"
    _write_tar(
        archive_path,
        [
            ("python-runtime/bin/python3.13", b"python", 0o755),
            ("python-runtime/lib/python3.13/encodings.py", b"encoding", 0o644),
        ],
    )
    destination = tmp_path / "extracted"
    _extract_tar_verified(archive_path, destination)

    assert (destination / "python-runtime/bin/python3.13").read_bytes() == b"python"
    assert (destination / "python-runtime/lib/python3.13/encodings.py").read_bytes() == b"encoding"

    linked_archive = tmp_path / "linked.tar.gz"
    with tarfile.open(linked_archive, mode="w:gz") as archive:
        target_member = tarfile.TarInfo("python-runtime/bin/python3.13")
        target_member.size = len(b"python")
        target_member.mode = 0o755
        archive.addfile(target_member, io.BytesIO(b"python"))
        member = tarfile.TarInfo("python-runtime/bin/python3")
        member.type = tarfile.SYMTYPE
        member.linkname = "python3.13"
        archive.addfile(member)
    linked_destination = tmp_path / "linked-extracted"
    _extract_tar_verified(linked_archive, linked_destination)
    assert (linked_destination / "python-runtime/bin/python3").read_bytes() == b"python"

    unsafe_archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(unsafe_archive, mode="w:gz") as archive:
        member = tarfile.TarInfo("python-runtime/bin/python3")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../../outside"
        archive.addfile(member)
    with pytest.raises(ValueError, match="unsafe|missing|escapes"):
        _extract_tar_verified(unsafe_archive, tmp_path / "unsafe-extracted")


def test_safe_member_rejects_traversal() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        _safe_member("../outside")


def test_runtime_origin_records_provider_and_verified_license_files(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "LICENSE.txt").write_text("Python license\n", encoding="utf-8")
    (runtime / "share" / "doc" / "python" / "copyright").parent.mkdir(parents=True)
    (runtime / "share" / "doc" / "python" / "copyright").write_text("Copyright\n", encoding="utf-8")

    license_files = find_license_files(runtime)
    assert license_files == ("LICENSE.txt", "share/doc/python/copyright")
    origin_path = runtime / "runtime-origin.json"
    write_runtime_origin(
        origin_path,
        {
            "format": "connection-analysis-runtime-origin",
            "schema_version": "1.0",
            "provider": "test-provider",
            "provider_release": "test-release",
            "python_version": "3.13.15",
            "target": "linux-x86_64",
            "source_url": "https://example.test/python.tar.gz",
            "archive_filename": "python.tar.gz",
            "archive_sha256": "a" * 64,
            "license_files": list(license_files),
        },
    )

    loaded = read_runtime_origin(origin_path)
    assert loaded["provider"] == "test-provider"
    assert loaded["license_files"] == list(license_files)


def test_runtime_origin_rejects_unsafe_license_path(tmp_path: Path) -> None:
    origin_path = tmp_path / "runtime-origin.json"
    with pytest.raises(ValueError, match="unsafe path"):
        write_runtime_origin(
            origin_path,
            {
                "format": "connection-analysis-runtime-origin",
                "schema_version": "1.0",
                "provider": "test-provider",
                "provider_release": None,
                "python_version": "3.13.15",
                "target": "windows-amd64",
                "source_url": "https://example.test/python.zip",
                "archive_filename": "python.zip",
                "archive_sha256": "a" * 64,
                "license_files": ["../outside.txt"],
            },
        )
