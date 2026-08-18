"""Compact quality and coverage summaries for Contract v1 graphs."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .contract import ContractError, canonical_sha256, validate_document


def summarize_document(document: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_document(document)
    except ContractError as exc:
        raise ValueError(f"invalid analysis JSON: {exc}") from exc

    nodes = document["nodes"]
    edges = document["edges"]
    diagnostics = document["diagnostics"]
    resolution_counts = Counter(edge["resolution_status"] for edge in edges)
    relation_counts = Counter(edge["relation_type"] for edge in edges)
    provenance_counts = Counter(edge["provenance"] for edge in edges)
    diagnostic_codes = Counter(item["code"] for item in diagnostics)
    diagnostic_severity = Counter(item["severity"] for item in diagnostics)
    parse_error_files = {
        item.get("file")
        for item in diagnostics
        if item.get("code") == "parse_error" and item.get("file")
    }
    unresolved_edges = sum(resolution_counts.get(status, 0) for status in ("unresolved", "unsupported"))
    return {
        "format": "connection-analysis-report",
        "schema_version": "1.0",
        "analysis_schema_version": document["schema_version"],
        "analysis_sha256": canonical_sha256(document),
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "diagnostics": len(diagnostics),
            "nodes_by_kind": dict(sorted(Counter(node["kind"] for node in nodes).items())),
            "edges_by_relation": dict(sorted(relation_counts.items())),
            "edges_by_resolution": dict(sorted(resolution_counts.items())),
            "edges_by_provenance": dict(sorted(provenance_counts.items())),
            "diagnostics_by_code": dict(sorted(diagnostic_codes.items())),
            "diagnostics_by_severity": dict(sorted(diagnostic_severity.items())),
        },
        "rates": {
            "unresolved_or_unsupported_edge_rate": unresolved_edges / len(edges) if edges else 0.0,
            "diagnostics_per_node": len(diagnostics) / len(nodes) if nodes else 0.0,
            "parse_error_file_count": len(parse_error_files),
        },
        "meta": {
            "analyzer": document["meta"]["analyzer"],
            "target": document["meta"]["target"],
            "deterministic": document["meta"]["deterministic"],
        },
    }
