from __future__ import annotations

import json
from pathlib import Path

import pytest

from connection_map.contract import canonical_sha256, validate_document
from connection_map.manual import ManualOverlayError, load_manual, merge_manual, validate_manual

PROJECT_ROOT = Path(__file__).parents[1]
ANALYSIS_PATH = PROJECT_ROOT / "examples" / "analysis-v1.json"
MANUAL_PATH = PROJECT_ROOT / "examples" / "manual-v1.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_manual_example_validates_against_example_graph() -> None:
    analysis = _load(ANALYSIS_PATH)
    manual = load_manual(MANUAL_PATH)
    validate_manual(
        manual,
        node_ids={node["id"] for node in analysis["nodes"]},
        edge_ids={edge["id"] for edge in analysis["edges"]},
    )
    merged = merge_manual(analysis, manual)
    validate_document(merged)
    assert any(node["id"] == "manual:external:redis" for node in merged["nodes"])
    assert any(edge["provenance"] == "manual" for edge in merged["edges"])
    assert merged["meta"]["extensions"]["manual_overlay"]["annotations"]


def test_manual_hash_binds_overlay_to_base_graph() -> None:
    analysis = _load(ANALYSIS_PATH)
    manual = _load(MANUAL_PATH)
    manual["analysis_sha256"] = canonical_sha256(analysis)
    assert merge_manual(analysis, manual)["meta"]["counts"]["edges"] == 2

    changed = json.loads(json.dumps(analysis))
    changed["meta"]["target"]["repository_id"] = "other"
    with pytest.raises(ManualOverlayError, match="analysis_sha256"):
        merge_manual(changed, manual)


def test_manual_missing_reference_is_rejected() -> None:
    analysis = _load(ANALYSIS_PATH)
    manual = _load(MANUAL_PATH)
    manual["edges"][0]["source_id"] = "python:missing.py:missing:function"
    with pytest.raises(ManualOverlayError, match="source does not exist"):
        merge_manual(analysis, manual)
