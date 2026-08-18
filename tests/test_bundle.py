from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from connection_map.bundle import (
    BundleError,
    quick_validate_bundle,
    search_bundle,
    split_analysis_file,
    validate_bundle,
)
from connection_map.contract import canonical_sha256

PROJECT_ROOT = Path(__file__).parents[1]
ANALYSIS_PATH = PROJECT_ROOT / "examples" / "analysis-v1.json"


def test_split_bundle_validates_and_searches(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    index = split_analysis_file(
        ANALYSIS_PATH,
        bundle,
        node_chunk_size=1,
        edge_chunk_size=1,
        diagnostic_chunk_size=1,
        search_chunk_size=1,
    )
    assert index["counts"]["nodes"] == 2
    assert index["overview"]["path"] == "overview/index.json"
    assert (bundle / "overview" / "index.json").is_file()
    assert index["search"]["record_format"] == "node_records"
    assert (bundle / index["search"]["record_chunks"][0]["path"]).is_file()
    validated = validate_bundle(bundle)
    assert validated["analysis_sha256"] == canonical_sha256(json.loads(ANALYSIS_PATH.read_text(encoding="utf-8")))
    matches = search_bundle(bundle, "main")
    assert [item["id"] for item in matches] == ["python:app.py:main:function"]


def test_split_refuses_non_empty_output_without_force(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(BundleError, match="not empty"):
        split_analysis_file(ANALYSIS_PATH, bundle)


def test_bundle_integrity_detects_chunk_tampering(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    split_analysis_file(ANALYSIS_PATH, bundle)
    node_chunk = next((bundle / "nodes").glob("*.json"))
    node_chunk.write_text("[]\n", encoding="utf-8")
    with pytest.raises(BundleError, match="SHA-256 mismatch"):
        validate_bundle(bundle)


def test_quick_bundle_validation_rejects_malformed_search_shapes(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    index = split_analysis_file(ANALYSIS_PATH, bundle)
    index["search"]["shards"] = {"not": "an array"}
    (bundle / "index.json").write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(BundleError, match="search.shards must be an array"):
        quick_validate_bundle(bundle)


def test_bundle_search_record_order_is_integrity_checked(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    index = split_analysis_file(ANALYSIS_PATH, bundle, node_chunk_size=1)
    entries = index["search"]["record_chunks"]
    first = bundle / entries[0]["path"]
    second = bundle / entries[1]["path"]
    first_payload, second_payload = first.read_bytes(), second.read_bytes()
    first.write_bytes(second_payload)
    second.write_bytes(first_payload)
    for entry in entries[:2]:
        entry_path = bundle / entry["path"]
        entry["sha256"] = hashlib.sha256(entry_path.read_bytes()).hexdigest()
    (bundle / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(BundleError, match="ordinal"):
        validate_bundle(bundle)


def test_bundle_overview_rejects_wrong_representative_chunk(tmp_path: Path) -> None:
    document = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    external = {
        "id": "python:external:library",
        "kind": "external",
        "qualified_name": "library.helper",
        "display_name": "library.helper",
        "file": None,
        "span": None,
        "parent_id": None,
        "visibility": "unknown",
        "extensions": {"language": "python", "external": True},
    }
    document["nodes"].append(external)
    document["edges"].append({
        **document["edges"][0],
        "id": "edge:overview-representative",
        "source_id": "python:app.py:main:function",
        "target_id": external["id"],
        "relation_type": "references",
        "confidence": 0.5,
    })
    document["meta"]["counts"] = {"nodes": len(document["nodes"]), "edges": len(document["edges"]), "diagnostics": 0}
    analysis_path = tmp_path / "analysis.json"
    analysis_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    bundle = tmp_path / "bundle"
    index = split_analysis_file(analysis_path, bundle, edge_chunk_size=1)
    overview_path = bundle / index["overview"]["path"]
    overview = json.loads(overview_path.read_text(encoding="utf-8"))
    group_path = bundle / overview["edge_group_chunks"][0]["path"]
    groups = json.loads(group_path.read_text(encoding="utf-8"))
    assert groups
    groups[0]["representative_edge_chunk"] = (
        groups[0]["representative_edge_chunk"] + 1
    ) % len(index["chunks"]["edges"])
    group_path.write_text(json.dumps(groups, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    overview["edge_group_chunks"][0]["sha256"] = hashlib.sha256(group_path.read_bytes()).hexdigest()
    overview_path.write_text(json.dumps(overview, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    index["overview"]["sha256"] = hashlib.sha256(overview_path.read_bytes()).hexdigest()
    (bundle / "index.json").write_text(json.dumps(index, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(BundleError, match="representative edge chunk"):
        validate_bundle(bundle)


def test_bundle_search_uses_client_consistent_unicode_normalization(tmp_path: Path) -> None:
    document = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    document["nodes"][1]["qualified_name"] = "ẞervice"
    document["nodes"][1]["display_name"] = "ẞervice"
    document_path = tmp_path / "unicode-analysis.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    bundle = tmp_path / "bundle"
    split_analysis_file(document_path, bundle)

    matches = search_bundle(bundle, "ẞ")

    assert [item["id"] for item in matches] == ["python:app.py:main:function"]


def test_bundle_search_records_map_logical_namespaces_to_their_file_module(tmp_path: Path) -> None:
    document = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    document["meta"]["language"] = "ruby"
    document["meta"]["languages"] = ["ruby"]
    document["nodes"] = [
        {
            "id": "ruby:lib/example.rb:module",
            "kind": "module",
            "qualified_name": "lib/example.rb",
            "display_name": "lib/example.rb",
            "file": "lib/example.rb",
            "span": None,
            "parent_id": None,
            "visibility": "public",
            "extensions": {"language": "ruby"},
        },
        {
            "id": "ruby:Demo:namespace",
            "kind": "namespace",
            "qualified_name": "Demo",
            "display_name": "Demo",
            "file": "lib/example.rb",
            "span": None,
            "parent_id": None,
            "visibility": "public",
            "extensions": {"language": "ruby"},
        },
        {
            "id": "ruby:Demo::Worker:class",
            "kind": "class",
            "qualified_name": "Demo::Worker",
            "display_name": "Worker",
            "file": "lib/example.rb",
            "span": None,
            "parent_id": "ruby:Demo:namespace",
            "visibility": "public",
            "extensions": {"language": "ruby"},
        },
    ]
    document["edges"] = [
        {
            "id": "edge:ruby-file-namespace",
            "source_id": "ruby:lib/example.rb:module",
            "target_id": "ruby:Demo:namespace",
            "relation_type": "contains",
            "resolution_status": "resolved",
            "provenance": "ast",
            "confidence": 1.0,
            "source_span": None,
            "detail": {"kind": "file_scope"},
        },
        {
            "id": "edge:ruby-namespace-class",
            "source_id": "ruby:Demo:namespace",
            "target_id": "ruby:Demo::Worker:class",
            "relation_type": "contains",
            "resolution_status": "resolved",
            "provenance": "ast",
            "confidence": 1.0,
            "source_span": None,
            "detail": {"kind": "lexical_definition"},
        },
    ]
    document["meta"]["counts"] = {"nodes": 3, "edges": 2, "diagnostics": 0}
    document["diagnostics"] = []

    bundle = tmp_path / "bundle"
    document_path = tmp_path / "ruby-analysis.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    index = split_analysis_file(document_path, bundle)
    assert index["counts"]["nodes"] == 3
    records = []
    for entry in index["search"]["record_chunks"]:
        records.extend(json.loads((bundle / entry["path"]).read_text(encoding="utf-8")))
    worker = next(record for record in records if record["id"] == "ruby:Demo::Worker:class")
    assert worker["module"] == 0
    validate_bundle(bundle)


def test_bundle_keeps_external_nodes_and_edges_in_virtual_modules(tmp_path: Path) -> None:
    document = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    external = {
        "id": "python:external:library",
        "kind": "external",
        "qualified_name": "library:helper",
        "display_name": "library:helper",
        "file": None,
        "span": None,
        "parent_id": None,
        "visibility": "unknown",
        "extensions": {"language": "python", "external": True},
    }
    unknown = {
        "id": "python:unknown:missing",
        "kind": "unknown",
        "qualified_name": "missing:helper",
        "display_name": "missing:helper",
        "file": None,
        "span": None,
        "parent_id": None,
        "visibility": "unknown",
        "extensions": {"language": "python", "unresolved": True},
    }
    document["nodes"].append(external)
    document["nodes"].append(unknown)
    document["edges"].append({
        "id": "edge:example-external",
        "source_id": "python:app.py:main:function",
        "target_id": external["id"],
        "relation_type": "calls",
        "resolution_status": "external",
        "provenance": "ast",
        "confidence": 0.7,
        "source_span": None,
        "detail": {"expression": "library.helper()"},
    })
    document["edges"].extend([
        {
            "id": "edge:external-unknown",
            "source_id": external["id"],
            "target_id": unknown["id"],
            "relation_type": "references",
            "resolution_status": "unresolved",
            "provenance": "ast",
            "confidence": 0.4,
            "source_span": None,
            "detail": {"expression": "missing.helper"},
        },
        {
            "id": "edge:unknown-module",
            "source_id": unknown["id"],
            "target_id": "python:app.py:module",
            "relation_type": "references",
            "resolution_status": "unresolved",
            "provenance": "ast",
            "confidence": 0.2,
            "source_span": None,
            "detail": {"expression": "missing"},
        },
    ])
    document["meta"]["counts"] = {
        "nodes": len(document["nodes"]),
        "edges": len(document["edges"]),
        "diagnostics": 0,
    }
    bundle = tmp_path / "bundle"
    # Rebuild from the modified document rather than the checked-in example.
    document_path = tmp_path / "external-analysis.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    index = split_analysis_file(document_path, bundle, node_chunk_size=1, edge_chunk_size=1)
    overview = json.loads((bundle / index["overview"]["path"]).read_text(encoding="utf-8"))

    virtual = [node for node in overview["modules"] if node.get("extensions", {}).get("virtual")]
    assert {node["extensions"]["virtual_scope"] for node in virtual} == {"external", "unresolved"}
    external_ordinal = next(str(index) for index, node in enumerate(document["nodes"]) if node["id"] == external["id"])
    unknown_ordinal = next(str(index) for index, node in enumerate(document["nodes"]) if node["id"] == unknown["id"])
    virtual_index = overview["module_by_node"][external_ordinal]
    assert overview["modules"][virtual_index]["extensions"]["virtual_scope"] == "external"
    assert overview["edge_chunks_by_node"][external_ordinal]
    assert overview["edge_chunks_by_node"][unknown_ordinal]
    overview_groups = [
        group
        for entry in overview["edge_group_chunks"]
        for group in json.loads((bundle / entry["path"]).read_text(encoding="utf-8"))
    ]
    assert overview_groups
    assert all(group["representative_edge_id"] for group in overview_groups)
    assert all(isinstance(group["representative_edge_chunk"], int) for group in overview_groups)
    validate_bundle(bundle)


def test_bundle_aggregate_keeps_confidence_range_separate_from_representative(tmp_path: Path) -> None:
    document = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    external = {
        "id": "python:external:library",
        "kind": "external",
        "qualified_name": "library.helper",
        "display_name": "library.helper",
        "file": None,
        "span": None,
        "parent_id": None,
        "visibility": "unknown",
        "extensions": {"language": "python", "external": True},
    }
    document["nodes"].append(external)
    document["edges"] = [
        {
            **document["edges"][0],
            "id": "edge:aggregate-high",
            "source_id": "python:app.py:main:function",
            "target_id": external["id"],
            "relation_type": "references",
            "confidence": 0.9,
        },
        {
            **document["edges"][0],
            "id": "edge:aggregate-low",
            "source_id": "python:app.py:main:function",
            "target_id": external["id"],
            "relation_type": "references",
            "confidence": 0.2,
        },
    ]
    document["meta"]["counts"] = {"nodes": len(document["nodes"]), "edges": 2, "diagnostics": 0}
    document_path = tmp_path / "aggregate-analysis.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    bundle = tmp_path / "bundle"
    index = split_analysis_file(document_path, bundle, edge_chunk_size=1)
    overview = json.loads((bundle / index["overview"]["path"]).read_text(encoding="utf-8"))
    groups = [
        group
        for entry in overview["edge_group_chunks"]
        for group in json.loads((bundle / entry["path"]).read_text(encoding="utf-8"))
    ]

    assert len(groups) == 1
    assert groups[0]["confidence_min"] == 0.2
    assert groups[0]["confidence_max"] == 0.9
    assert groups[0]["representative_edge_id"] == "edge:aggregate-high"
    validate_bundle(bundle)
