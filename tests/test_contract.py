from __future__ import annotations

import json
from pathlib import Path

import pytest

from connection_map.contract import ContractError, validate_document
from connection_map.model import GraphBuilder


def _example_document() -> dict:
    path = Path(__file__).parents[1] / "examples" / "analysis-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_contract_checks_target_runtime_and_parent_references() -> None:
    missing_target = _example_document()
    del missing_target["meta"]["target"]["repository_id"]
    with pytest.raises(ContractError, match="repository_id"):
        validate_document(missing_target)

    missing_parent = _example_document()
    missing_parent["nodes"][0]["parent_id"] = "missing-parent"
    with pytest.raises(ContractError, match="node parent does not exist"):
        validate_document(missing_parent)

    missing_runtime = _example_document()
    del missing_runtime["meta"]["runtime"]["ast_version"]
    with pytest.raises(ContractError, match="ast_version"):
        validate_document(missing_runtime)


def test_contract_rejects_schema_incompatible_fields_and_parent_cycles() -> None:
    unknown = _example_document()
    unknown["nodes"][0]["unknown_node_property"] = True
    with pytest.raises(ContractError, match="unknown_node_property"):
        validate_document(unknown)

    bad_diagnostic = _example_document()
    bad_diagnostic["diagnostics"] = [{"code": "bad", "severity": "error", "message": "bad", "file": 42}]
    with pytest.raises(ContractError, match="diagnostic.file"):
        validate_document(bad_diagnostic)

    cycle = _example_document()
    cycle["nodes"][0]["parent_id"] = cycle["nodes"][1]["id"]
    cycle["nodes"][1]["parent_id"] = cycle["nodes"][0]["id"]
    with pytest.raises(ContractError, match="parent cycle"):
        validate_document(cycle)


def test_graph_builder_rejects_conflicting_edge_ids() -> None:
    builder = GraphBuilder()
    edge = {
        "id": "edge:duplicate",
        "source_id": "source",
        "target_id": "target",
        "relation_type": "calls",
        "resolution_status": "resolved",
        "provenance": "ast",
        "confidence": 1.0,
        "source_span": None,
        "detail": {"expression": "run()"},
    }
    assert builder.add_edge(edge) is True
    assert builder.add_edge(dict(edge)) is False
    changed = {**edge, "detail": {"expression": "other()"}}
    with pytest.raises(ValueError, match="edge ID collision"):
        builder.add_edge(changed)
