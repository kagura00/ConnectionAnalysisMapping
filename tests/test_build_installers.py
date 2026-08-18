from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import build_installers
from build_installers import _clean_output, _write_portable_zip, _write_posix_launcher


def test_clean_output_removes_only_release_artifacts(tmp_path: Path) -> None:
    (tmp_path / "connection_analysis_mapping-0.1.0.tar.gz").write_bytes(b"archive")
    (tmp_path / "connection-map-install-0.1.0-posix.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "SHA256SUMS.txt").write_text("checksum\n", encoding="utf-8")

    _clean_output(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_clean_output_refuses_unexpected_entries(tmp_path: Path) -> None:
    unexpected = tmp_path / "keep-me.txt"
    unexpected.write_text("do not remove\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected release output entry"):
        _clean_output(tmp_path)

    assert unexpected.read_text(encoding="utf-8") == "do not remove\n"


def test_posix_launcher_validates_before_loading_embedded_package(tmp_path: Path) -> None:
    archive = tmp_path / "core.tar.gz"
    archive.write_bytes(b"archive")

    launcher = _write_posix_launcher(archive, tmp_path, "1.0.0")
    source = launcher.read_text(encoding="utf-8")

    assert "tar -xzf" not in source
    assert "runpy.run_module(\"connection_map.installer\"" in source
    assert "source archive contains duplicate member names" in source
    assert "export PYTHONPATH" not in source
    assert "\nexit 0\n\n__CONNECTION_MAP_PAYLOAD__\n" in source


def test_portable_zip_separates_app_data_and_launchers(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    (source_root / "src" / "connection_map" / "web").mkdir(parents=True)
    (source_root / "src" / "connection_map" / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "src" / "connection_map" / "__main__.py").write_text("", encoding="utf-8")
    (source_root / "src" / "connection_map" / "cli.py").write_text("", encoding="utf-8")
    (source_root / "src" / "connection_map" / "installer.py").write_text("", encoding="utf-8")
    (source_root / "src" / "connection_map" / "web" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (source_root / "pyproject.toml").write_text(
        "[project]\nname = 'connection-analysis-mapping'\nversion = '1.0.0'\n",
        encoding="utf-8",
    )
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source_root, arcname="connection_analysis_mapping-1.0.0")

    portable = _write_portable_zip(archive, tmp_path, "1.0.0")
    with zipfile.ZipFile(portable) as package:
        names = set(package.namelist())
        assert "connection-map-portable/data/registry.json" not in names
        assert "connection-map-portable/launcher/connection-map.sh" in names
        assert "connection-map-portable/app/source/pyproject.toml" in names
        assert not any(name.startswith("connection-map-portable/data/") and "/app/" in name for name in names)
        launcher_info = package.getinfo("connection-map-portable/launcher/connection-map.sh")
        assert launcher_info.external_attr >> 16 & 0o111
        windows_launcher_bytes = package.read("connection-map-portable/launcher/connection-map.cmd")
        assert b"\r\n" in windows_launcher_bytes
        windows_launcher = windows_launcher_bytes.decode("utf-8")
        assert "operator.ge" in windows_launcher
        assert "version_info >=" not in windows_launcher
        assert "-m connection_map %*" in windows_launcher
        assert "-m connection_map.cli" not in windows_launcher


def test_portable_zip_rejects_incomplete_source_archive(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    (source_root / "src" / "connection_map").mkdir(parents=True)
    (source_root / "src" / "connection_map" / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "src" / "connection_map" / "__main__.py").write_text("", encoding="utf-8")
    (source_root / "src" / "connection_map" / "cli.py").write_text("", encoding="utf-8")
    (source_root / "src" / "connection_map" / "installer.py").write_text("", encoding="utf-8")
    (source_root / "pyproject.toml").write_text(
        "[project]\nname = 'wrong-name'\nversion = '1.0.0'\n",
        encoding="utf-8",
    )
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source_root, arcname="connection_analysis_mapping-1.0.0")

    with pytest.raises(ValueError, match="source archive is missing required files"):
        _write_portable_zip(archive, tmp_path, "1.0.0")


def test_portable_zip_embeds_runtime_and_keeps_app_replaceable(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "source"
    (source_root / "src" / "connection_map" / "web").mkdir(parents=True)
    for name in ("__init__.py", "__main__.py", "cli.py", "installer.py"):
        (source_root / "src" / "connection_map" / name).write_text("", encoding="utf-8")
    (source_root / "src" / "connection_map" / "web" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (source_root / "pyproject.toml").write_text(
        "[project]\nname = 'connection-analysis-mapping'\nversion = '1.0.0'\n",
        encoding="utf-8",
    )
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source_root, arcname="connection_analysis_mapping-1.0.0")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"runtime")
    (runtime / "python313._pth").write_text("python313.zip\n", encoding="utf-8")
    monkeypatch.setattr(
        build_installers,
        "_find_portable_runtime",
        lambda _runtime: (runtime, runtime / "python.exe", "3.13.15"),
    )

    portable = _write_portable_zip(archive, tmp_path, "1.0.0", runtime_dir=runtime)
    with zipfile.ZipFile(portable) as package:
        names = set(package.namelist())
        assert "connection-map-portable/runtime/python.exe" in names
        assert "connection-map-portable/runtime/runtime.json" in names
        pth = package.read("connection-map-portable/runtime/python313._pth").decode("utf-8")
        assert "../app/source/src" in pth
        assert "connection-map-portable/app/source/pyproject.toml" in names
