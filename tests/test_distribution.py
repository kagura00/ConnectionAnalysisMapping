from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from connection_map.distribution import DistributionError, install_core, rollback_core
from connection_map.scaffold import initialize_target


def _write_archive(path: Path, version: str, marker: str) -> None:
    root = f"connection_analysis_mapping-{version}"
    files = {
        "pyproject.toml": f'[project]\nname = "connection-analysis-mapping"\nversion = "{version}"\n',
        "src/connection_map/__init__.py": f'__version__ = "{version}"\n',
        "src/connection_map/web/index.html": "<!doctype html>\n",
        "marker.txt": marker,
    }
    with tarfile.open(path, mode="w:gz") as archive:
        for relative_name, content in files.items():
            payload = content.encode("utf-8")
            member = tarfile.TarInfo(f"{root}/{relative_name}")
            member.mode = 0o644
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def test_install_core_preserves_target_specific_files(tmp_path: Path) -> None:
    initialize_target(tmp_path)
    config_path = tmp_path / ".connection-map" / "config.toml"
    analyzer_path = tmp_path / ".connection-map" / "analyzer" / "run.py"
    config_path.write_text("user-config\n", encoding="utf-8")
    analyzer_path.write_text("user-analyzer\n", encoding="utf-8")
    archive = tmp_path / "release-1.tar.gz"
    _write_archive(archive, "1.0.0", "one")

    result = install_core(tmp_path, archive)

    assert result.backup_path is None
    assert (tmp_path / ".connection-map" / "core" / "marker.txt").read_text(encoding="utf-8") == "one"
    manifest = json.loads(
        (tmp_path / ".connection-map" / "core" / "core-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["core_version"] == "1.0.0"
    assert len(manifest["core_sha256"]) == 64
    assert config_path.read_text(encoding="utf-8") == "user-config\n"
    assert analyzer_path.read_text(encoding="utf-8") == "user-analyzer\n"


def test_update_and_rollback_keep_both_core_versions(tmp_path: Path) -> None:
    initialize_target(tmp_path)
    archive_one = tmp_path / "release-1.tar.gz"
    archive_two = tmp_path / "release-2.tar.gz"
    _write_archive(archive_one, "1.0.0", "one")
    _write_archive(archive_two, "2.0.0", "two")
    config_path = tmp_path / ".connection-map" / "config.toml"
    config_path.write_text("user-config\n", encoding="utf-8")
    install_core(tmp_path, archive_one)

    update = install_core(tmp_path, archive_two)

    assert update.backup_path is not None
    assert update.backup_path.is_dir()
    assert (update.backup_path / "marker.txt").read_text(encoding="utf-8") == "one"
    assert (tmp_path / ".connection-map" / "core" / "marker.txt").read_text(encoding="utf-8") == "two"
    assert config_path.read_text(encoding="utf-8") == "user-config\n"

    rollback = rollback_core(tmp_path)

    assert rollback.restored_backup_name == update.backup_path.name
    assert rollback.saved_current_path is not None
    assert rollback.saved_current_path.is_dir()
    assert (tmp_path / ".connection-map" / "core" / "marker.txt").read_text(encoding="utf-8") == "one"
    assert (rollback.saved_current_path / "marker.txt").read_text(encoding="utf-8") == "two"


def test_install_core_rejects_path_traversal_before_touching_target(tmp_path: Path) -> None:
    initialize_target(tmp_path)
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, mode="w:gz") as handle:
        payload = b"escape"
        member = tarfile.TarInfo("release/../escape.txt")
        member.size = len(payload)
        handle.addfile(member, io.BytesIO(payload))

    with pytest.raises(DistributionError, match="unsafe archive member path"):
        install_core(tmp_path, archive)

    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / ".connection-map" / "core").exists()


def test_install_core_rejects_target_root_as_install_directory(tmp_path: Path) -> None:
    archive = tmp_path / "release.tar.gz"
    _write_archive(archive, "1.0.0", "one")

    with pytest.raises(DistributionError, match="child directory"):
        install_core(tmp_path, archive, install_dir=".")


def test_install_core_rejects_duplicate_archive_members(tmp_path: Path) -> None:
    initialize_target(tmp_path)
    archive = tmp_path / "duplicate.tar.gz"
    with tarfile.open(archive, mode="w:gz") as handle:
        root = "connection_analysis_mapping-1.0.0"
        files = [
            ("pyproject.toml", b'[project]\nname = "connection-analysis-mapping"\nversion = "1.0.0"\n'),
            ("pyproject.toml", b'[project]\nname = "connection-analysis-mapping"\nversion = "9.9.9"\n'),
            ("src/connection_map/__init__.py", b"__version__ = '1.0.0'\n"),
            ("src/connection_map/web/index.html", b"<!doctype html>\n"),
        ]
        for relative_name, payload in files:
            member = tarfile.TarInfo(f"{root}/{relative_name}")
            member.mode = 0o644
            member.size = len(payload)
            handle.addfile(member, io.BytesIO(payload))

    with pytest.raises(DistributionError, match="duplicate member names"):
        install_core(tmp_path, archive)

    assert not (tmp_path / ".connection-map" / "core").exists()


def test_rollback_rejects_a_modified_backup(tmp_path: Path) -> None:
    initialize_target(tmp_path)
    archive_one = tmp_path / "release-1.tar.gz"
    archive_two = tmp_path / "release-2.tar.gz"
    _write_archive(archive_one, "1.0.0", "one")
    _write_archive(archive_two, "2.0.0", "two")
    install_core(tmp_path, archive_one)
    update = install_core(tmp_path, archive_two)
    assert update.backup_path is not None
    (update.backup_path / "marker.txt").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(DistributionError, match="content hash"):
        rollback_core(tmp_path)
