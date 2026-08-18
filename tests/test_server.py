from __future__ import annotations

from pathlib import Path

import pytest

from connection_map.cli import main
from connection_map.server import _quick_workspace_state, serve_analysis, serve_workspace
from connection_map.workspace import Workspace


def test_quick_workspace_state_does_not_read_full_analysis(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    workspace = Workspace(tmp_path / "data")
    assert main(["analyze", "--root", str(repository), "--workspace", str(workspace.root), "--deterministic"]) == 0

    record = workspace.records()[0]
    analysis_path = workspace.path_for(record, record.analysis_path).resolve()
    original_read_text = Path.read_text

    def reject_full_analysis_read(path: Path, *args, **kwargs) -> str:
        if path.resolve() == analysis_path:
            raise AssertionError("startup validation must not read analysis.json")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_full_analysis_read)
    state, allowed_files, allowed_hashes = _quick_workspace_state(workspace, record)

    assert state["phase"] == "startup"
    assert state["status"] in {"pending", "invalid"}
    assert "index.json" in allowed_files
    assert allowed_hashes["index.json"]


def test_central_workspace_rejects_non_loopback_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        serve_workspace(tmp_path / "data", host="0.0.0.0")


def test_local_analysis_rejects_non_loopback_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        serve_analysis(tmp_path / "analysis.json", host="0.0.0.0")
