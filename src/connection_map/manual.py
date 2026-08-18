"""Validation and application of manual graph overlays."""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

from .contract import ContractError, canonical_sha256, validate_document


class ManualOverlayError(ValueError):
    """Raised when a manual-v1 overlay cannot be safely applied."""


_STATUSES = {"resolved", "external", "unresolved", "unsupported"}
_MANUAL_KINDS = {"external", "unknown"}
_ANNOTATION_KINDS = {"note", "label", "group"}
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManualOverlayError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManualOverlayError(f"{name} must be a non-empty string")
    return value


def _span(value: Any, name: str) -> None:
    if value is None:
        return
    item = _mapping(value, name)
    for field in ("start_line", "start_col", "end_line", "end_col"):
        number = item.get(field)
        if not isinstance(number, int) or isinstance(number, bool):
            raise ManualOverlayError(f"{name}.{field} must be an integer")
    if item["start_line"] < 1 or item["end_line"] < 1:
        raise ManualOverlayError(f"{name} line numbers must be >= 1")
    if item["start_col"] < 0 or item["end_col"] < 0:
        raise ManualOverlayError(f"{name} column offsets must be >= 0")


def _finite(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ManualOverlayError(f"{name} must be a finite number")


def validate_manual(
    document: Any,
    *,
    node_ids: set[str] | None = None,
    edge_ids: set[str] | None = None,
) -> None:
    """Validate a manual overlay, optionally against a base graph."""

    root = _mapping(document, "manual")
    if root.get("format") != "connection-analysis-manual":
        raise ManualOverlayError("manual.format must be connection-analysis-manual")
    if root.get("schema_version") != "1.0":
        raise ManualOverlayError("manual.schema_version must be 1.0")
    if root.get("analysis_schema_version") != "1.0":
        raise ManualOverlayError("manual.analysis_schema_version must be 1.0")
    analysis_hash = root.get("analysis_sha256")
    if analysis_hash is not None and (not isinstance(analysis_hash, str) or not _HASH_RE.fullmatch(analysis_hash)):
        raise ManualOverlayError("manual.analysis_sha256 must be a 64-character hexadecimal string")

    nodes = root.get("nodes")
    edges = root.get("edges")
    annotations = root.get("annotations")
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(annotations, list):
        raise ManualOverlayError("manual.nodes, manual.edges, and manual.annotations must be arrays")

    manual_node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        item = _mapping(node, f"manual.nodes[{index}]")
        node_id = _string(item.get("id"), f"manual.nodes[{index}].id")
        if not node_id.startswith("manual:"):
            raise ManualOverlayError(f"manual node ID must start with manual:: {node_id}")
        if node_id in manual_node_ids:
            raise ManualOverlayError(f"duplicate manual node ID: {node_id}")
        manual_node_ids.add(node_id)
        _string(item.get("label"), f"manual.nodes[{index}].label")
        if item.get("kind") not in _MANUAL_KINDS:
            raise ManualOverlayError(f"invalid manual node kind: {item.get('kind')!r}")

    known_nodes = set(node_ids or set()) | manual_node_ids
    manual_edge_ids: set[str] = set()
    for index, edge in enumerate(edges):
        item = _mapping(edge, f"manual.edges[{index}]")
        edge_id = _string(item.get("id"), f"manual.edges[{index}].id")
        if not edge_id.startswith("manual:"):
            raise ManualOverlayError(f"manual edge ID must start with manual:: {edge_id}")
        if edge_id in manual_edge_ids:
            raise ManualOverlayError(f"duplicate manual edge ID: {edge_id}")
        manual_edge_ids.add(edge_id)
        source_id = _string(item.get("source_id"), f"manual.edges[{index}].source_id")
        target_id = _string(item.get("target_id"), f"manual.edges[{index}].target_id")
        if node_ids is not None and source_id not in known_nodes:
            raise ManualOverlayError(f"manual edge source does not exist: {source_id}")
        if node_ids is not None and target_id not in known_nodes:
            raise ManualOverlayError(f"manual edge target does not exist: {target_id}")
        _string(item.get("relation_type"), f"manual.edges[{index}].relation_type")
        if item.get("resolution_status") not in _STATUSES:
            raise ManualOverlayError(
                f"invalid manual edge resolution_status: {item.get('resolution_status')!r}"
            )
        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
            raise ManualOverlayError(f"manual.edges[{index}].confidence must be between 0 and 1")
        _span(item.get("source_span"), f"manual.edges[{index}].source_span")
        _mapping(item.get("detail"), f"manual.edges[{index}].detail")

    known_edges = set(edge_ids or set()) | manual_edge_ids
    for index, annotation in enumerate(annotations):
        item = _mapping(annotation, f"manual.annotations[{index}]")
        annotation_id = _string(item.get("id"), f"manual.annotations[{index}].id")
        if not annotation_id.startswith("manual:"):
            raise ManualOverlayError(f"manual annotation ID must start with manual:: {annotation_id}")
        if item.get("kind") not in _ANNOTATION_KINDS:
            raise ManualOverlayError(f"invalid manual annotation kind: {item.get('kind')!r}")
        _string(item.get("text"), f"manual.annotations[{index}].text")
        target_node = item.get("node_id")
        target_edge = item.get("edge_id")
        if target_node is not None and target_edge is not None:
            raise ManualOverlayError("an annotation cannot target both node_id and edge_id")
        if node_ids is not None and target_node is not None and target_node not in known_nodes:
            raise ManualOverlayError(f"manual annotation node does not exist: {target_node}")
        if edge_ids is not None and target_edge is not None and target_edge not in known_edges:
            raise ManualOverlayError(f"manual annotation edge does not exist: {target_edge}")
        position = item.get("position")
        if position is not None:
            position_map = _mapping(position, f"manual.annotations[{index}].position")
            _finite(position_map.get("x"), f"manual.annotations[{index}].position.x")
            _finite(position_map.get("y"), f"manual.annotations[{index}].position.y")

    annotation_ids = [item["id"] for item in annotations]
    if len(annotation_ids) != len(set(annotation_ids)):
        raise ManualOverlayError("duplicate manual annotation ID")


def load_manual(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManualOverlayError(f"invalid manual JSON: {path}") from exc
    validate_manual(document)
    return document


def merge_manual(
    analysis: dict[str, Any],
    manual: dict[str, Any],
    *,
    ignore_analysis_hash: bool = False,
) -> dict[str, Any]:
    """Return a derived Contract v1 graph with a validated manual overlay."""

    try:
        validate_document(analysis)
    except ContractError as exc:
        raise ManualOverlayError(f"base analysis is invalid: {exc}") from exc
    node_ids = {item["id"] for item in analysis["nodes"]}
    edge_ids = {item["id"] for item in analysis["edges"]}
    validate_manual(manual, node_ids=node_ids, edge_ids=edge_ids)

    declared_hash = manual.get("analysis_sha256")
    actual_hash = canonical_sha256(analysis)
    if declared_hash and declared_hash.lower() != actual_hash and not ignore_analysis_hash:
        raise ManualOverlayError(
            "manual.analysis_sha256 does not match the base analysis; "
            "use --ignore-analysis-hash only when reapplying intentionally"
        )

    merged = copy.deepcopy(analysis)
    merged_nodes = list(merged["nodes"])
    merged_edges = list(merged["edges"])
    for item in manual["nodes"]:
        node_id = item["id"]
        if node_id in node_ids:
            raise ManualOverlayError(f"manual node collides with base node: {node_id}")
        label = item["label"]
        merged_nodes.append(
            {
                "id": node_id,
                "kind": item["kind"],
                "qualified_name": label,
                "display_name": label,
                "file": None,
                "span": None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {"manual": True},
            }
        )
    for item in manual["edges"]:
        edge_id = item["id"]
        if edge_id in edge_ids:
            raise ManualOverlayError(f"manual edge collides with base edge: {edge_id}")
        merged_edges.append(
            {
                "id": edge_id,
                "source_id": item["source_id"],
                "target_id": item["target_id"],
                "relation_type": item["relation_type"],
                "resolution_status": item["resolution_status"],
                "provenance": "manual",
                "confidence": item["confidence"],
                "source_span": item.get("source_span"),
                "detail": item["detail"],
            }
        )

    merged["nodes"] = sorted(merged_nodes, key=lambda item: item["id"])
    merged["edges"] = sorted(merged_edges, key=lambda item: item["id"])
    extensions = merged.setdefault("meta", {}).setdefault("extensions", {})
    extensions["manual_overlay"] = {
        "schema_version": manual["schema_version"],
        "analysis_sha256": actual_hash,
        "node_ids": [item["id"] for item in manual["nodes"]],
        "edge_ids": [item["id"] for item in manual["edges"]],
        "annotations": copy.deepcopy(manual["annotations"]),
    }
    merged["meta"]["counts"] = {
        "nodes": len(merged["nodes"]),
        "edges": len(merged["edges"]),
        "diagnostics": len(merged["diagnostics"]),
    }
    validate_document(merged)
    return merged
