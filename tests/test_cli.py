from __future__ import annotations

from pathlib import Path

from connection_map.cli import main


def test_analyze_rejects_a_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert main(["analyze", "--root", str(missing), "--output", str(tmp_path / "out.json")]) == 2


def test_analyze_rejects_an_empty_result_by_default(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<main></main>\n", encoding="utf-8")
    output = tmp_path / "out.json"

    assert main(["analyze", "--root", str(tmp_path), "--output", str(output), "--deterministic"]) == 2
    assert not output.exists()


def test_analyze_can_explicitly_allow_an_empty_result(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("no source files\n", encoding="utf-8")
    output = tmp_path / "out.json"

    assert main([
        "analyze",
        "--root",
        str(tmp_path),
        "--output",
        str(output),
        "--deterministic",
        "--allow-empty",
    ]) == 0
    assert output.is_file()
