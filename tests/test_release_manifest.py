from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


def test_release_manifest_is_sorted_and_reproducible(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "connection_analysis_mapping-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "connection_analysis_mapping-0.1.0.tar.gz").write_bytes(b"source")
    (dist / "unrelated.txt").write_text("ignore", encoding="utf-8")

    output = dist / "SHA256SUMS.txt"
    script = Path(__file__).parents[1] / "scripts" / "build_release_manifest.py"
    subprocess.run(
        [sys.executable, str(script), "--dist", str(dist), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    first = output.read_text(encoding="utf-8")
    subprocess.run(
        [sys.executable, str(script), "--dist", str(dist), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert first == output.read_text(encoding="utf-8")
    assert "connection_analysis_mapping-0.1.0.tar.gz" in first
    assert "unrelated.txt" not in first
    assert hashlib.sha256(b"source").hexdigest() in first


def test_release_manifest_can_include_installer_media(tmp_path: Path) -> None:
    dist = tmp_path / "release-artifacts"
    dist.mkdir()
    (dist / "connection_analysis_mapping-0.1.0.tar.gz").write_bytes(b"source")
    (dist / "connection-map-install-0.1.0-posix.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (dist / "SHA256SUMS.txt").write_text("old\n", encoding="utf-8")

    output = dist / "SHA256SUMS.txt"
    script = Path(__file__).parents[1] / "scripts" / "build_release_manifest.py"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--dist",
            str(dist),
            "--output",
            str(output),
            "--all-artifacts",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    manifest = output.read_text(encoding="utf-8")
    assert "connection_analysis_mapping-0.1.0.tar.gz" in manifest
    assert "connection-map-install-0.1.0-posix.sh" in manifest
    assert "SHA256SUMS.txt" not in manifest
