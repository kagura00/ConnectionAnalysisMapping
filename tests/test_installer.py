from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from connection_map.installer import main


def _write_archive(path: Path) -> None:
    root = "connection_analysis_mapping-0.1.0"
    files = {
        "pyproject.toml": '[project]\nname = "connection-analysis-mapping"\nversion = "0.1.0"\n',
        "src/connection_map/__init__.py": "__version__ = '0.1.0'\n",
        "src/connection_map/web/index.html": "<!doctype html>\n",
    }
    with tarfile.open(path, mode="w:gz") as archive:
        for relative_name, content in files.items():
            payload = content.encode("utf-8")
            member = tarfile.TarInfo(f"{root}/{relative_name}")
            member.mode = 0o644
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def test_cli_installer_initializes_target_and_installs_core(tmp_path: Path) -> None:
    target = tmp_path / "repository"
    target.mkdir()
    archive = tmp_path / "connection_analysis_mapping-0.1.0.tar.gz"
    _write_archive(archive)

    result = main(["--cli", "--target", str(target), "--archive", str(archive), "--yes"])

    assert result == 0
    assert (target / ".connection-map" / "analyzer" / "run.py").is_file()
    manifest = json.loads(
        (target / ".connection-map" / "core" / "core-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["core_version"] == "0.1.0"


def test_cli_installer_preserves_target_configuration_on_update(tmp_path: Path) -> None:
    target = tmp_path / "repository"
    target.mkdir()
    archive = tmp_path / "connection_analysis_mapping-0.1.0.tar.gz"
    _write_archive(archive)
    assert main(["--cli", "--target", str(target), "--archive", str(archive), "--yes"]) == 0

    config = target / ".connection-map" / "config.toml"
    config.write_text("repository-specific\n", encoding="utf-8")
    assert main(["--cli", "--target", str(target), "--archive", str(archive), "--yes"]) == 0

    assert config.read_text(encoding="utf-8") == "repository-specific\n"
    assert any(
        path.is_dir() and path.name.startswith("core-")
        for path in (target / ".connection-map" / "backups").iterdir()
    )


def test_invalid_archive_does_not_leave_a_partial_scaffold(tmp_path: Path) -> None:
    target = tmp_path / "repository"
    target.mkdir()
    archive = tmp_path / "invalid.tar.gz"
    archive.write_text("not a tar archive\n", encoding="utf-8")

    result = main(["--cli", "--target", str(target), "--archive", str(archive), "--yes"])

    assert result == 2
    assert not (target / ".connection-map").exists()
