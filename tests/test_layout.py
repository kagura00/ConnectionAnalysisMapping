from __future__ import annotations

import json
from pathlib import Path

import pytest

from connection_map.layout import LayoutError, load_layout, validate_layout
from connection_map.server import serve_analysis


def _layout() -> dict:
    return {
        "format": "connection-analysis-layout",
        "schema_version": "1.0",
        "analysis_schema_version": "1.0",
        "camera": {"x": 10, "y": -2, "zoom": 1.25},
        "nodes": {"python:example.py:module": {"x": 20, "y": 30}},
        "annotations": [],
    }


def test_layout_contract_accepts_partial_positions() -> None:
    validate_layout(_layout())


def test_layout_contract_rejects_wrong_analysis_version() -> None:
    document = _layout()
    document["analysis_schema_version"] = "2.0"
    with pytest.raises(LayoutError, match="analysis_schema_version"):
        validate_layout(document)


def test_layout_loader_reads_json(tmp_path: Path) -> None:
    path = tmp_path / "layout-v1.json"
    path.write_text(json.dumps(_layout()), encoding="utf-8")
    assert load_layout(path)["schema_version"] == "1.0"


def test_serve_rejects_invalid_layout_before_starting(tmp_path: Path) -> None:
    analysis = Path(__file__).parents[1] / "examples" / "analysis-v1.json"
    layout = tmp_path / "invalid-layout.json"
    layout.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid layout JSON"):
        serve_analysis(analysis, layout_path=layout, port=0)
