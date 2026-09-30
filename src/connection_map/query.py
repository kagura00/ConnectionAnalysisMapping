"""Bounded relationship context shared by the CLI and local viewer."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .contract import canonical_sha256, validate_document
from .evidence import check_freshness, coverage_summary


class GraphQuery:
    def __init__(self, document: dict[str, Any]):
        validate_document(document)
        self.document = document
        self.sha256 = canonical_sha256(document)
        self.nodes = {node["id"]: node for node in document["nodes"]}
        self.outgoing: dict[str, list[dict]] = defaultdict(list)
        self.incoming: dict[str, list[dict]] = defaultdict(list)
        for edge in sorted(document["edges"], key=lambda item: item["id"]):
            self.outgoing[edge["source_id"]].append(edge)
            self.incoming[edge["target_id"]].append(edge)

    def diagnostics_page(self, *, severity: str = "all", file: str = "", offset: int = 0, limit: int = 200) -> dict:
        if severity not in {"all", "error", "warning", "info"}:
            raise ValueError("invalid diagnostic severity")
        if not isinstance(file, str) or len(file) > 500:
            raise ValueError("diagnostic file filter must be at most 500 characters")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100_000_000:
            raise ValueError("invalid diagnostic offset")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("diagnostic limit must be between 1 and 200")
        ranks = {"error": 0, "warning": 1, "info": 2}
        all_items = self.document["diagnostics"]
        items = [item for item in all_items if (severity == "all" or item["severity"] == severity)
                 and file.casefold() in (item.get("file") or "").casefold()]
        items.sort(key=lambda item: (ranks[item["severity"]], item.get("file") or "",
                                    (item.get("span") or {}).get("start_line", 0), item.get("code", "")))
        return {"analysis_sha256": self.sha256, "diagnostics": items[offset:offset + limit],
                "total": len(items), "total_all": len(all_items), "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < len(items) else None,
                "severity_counts": dict(Counter(item["severity"] for item in all_items))}

    def neighborhood(
        self, node_id: str, *, direction: str = "both", relations: list[str] | None = None,
        depth: int = 1, max_nodes: int = 60, max_edges: int = 120,
        resolution: str = "all", root: Path | None = None,
    ) -> dict[str, Any]:
        if node_id not in self.nodes:
            raise ValueError(f"unknown node ID: {node_id}")
        if direction not in {"in", "out", "both"}:
            raise ValueError("direction must be in, out, or both")
        if resolution not in {"all", "resolved", "external", "unresolved", "unsupported"}:
            raise ValueError("invalid resolution filter")
        for name, value, low, high in (("depth", depth, 0, 5), ("max_nodes", max_nodes, 1, 500),
                                       ("max_edges", max_edges, 1, 1000)):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        allowed = set(relations) if relations else None
        if allowed and any(not isinstance(value, str) or not value.strip() for value in allowed):
            raise ValueError("relation types must be non-empty strings")

        def incident(current: str) -> list[dict]:
            edges = []
            if direction != "in":
                edges.extend(self.outgoing[current])
            if direction != "out":
                edges.extend(self.incoming[current])
            return sorted({edge["id"]: edge for edge in edges if (
                (edge["relation_type"] in allowed if allowed is not None else edge["relation_type"] != "contains")
                and (resolution == "all" or edge["resolution_status"] == resolution)
            )}.values(), key=lambda item: item["id"])

        selected = {node_id}
        frontier = {node_id}
        selected_edges: dict[str, dict] = {}
        budget_limited = False
        for _ in range(depth):
            next_frontier = set()
            for current in sorted(frontier):
                for edge in incident(current):
                    if edge["id"] in selected_edges:
                        continue
                    new_ids = {edge["source_id"], edge["target_id"]} - selected
                    if len(selected) + len(new_ids) > max_nodes or len(selected_edges) >= max_edges:
                        budget_limited = True
                        continue
                    selected_edges[edge["id"]] = edge
                    selected.update(new_ids)
                    next_frontier.update(new_ids)
            frontier = next_frontier
            if not frontier:
                break
        depth_limited = any(edge["id"] not in selected_edges for current in frontier for edge in incident(current))
        files = {self.nodes[key].get("file") for key in selected} - {None}
        def relevant_diagnostic(item: dict) -> bool:
            # Prefer explicit owners over whole-file matches: an unrelated
            # function in the same file must not consume the 50-item budget.
            if item.get("edge_id"):
                return item["edge_id"] in selected_edges
            if item.get("node_id"):
                return item["node_id"] in selected
            return not item.get("file") or item["file"] in files

        diagnostics = [item for item in self.document["diagnostics"] if relevant_diagnostic(item)]
        edges = [{**edge, "source_file": self.nodes[edge["source_id"]].get("file")}
                 for edge in sorted(selected_edges.values(), key=lambda item: item["id"])]
        annotations = self.document["meta"].get("extensions", {}).get("manual_overlay", {}).get("annotations", [])
        annotations = [item for item in annotations if item.get("node_id") in selected
                       or item.get("edge_id") in selected_edges
                       or (not item.get("node_id") and not item.get("edge_id"))]
        return {
            "format": "connection-analysis-context", "schema_version": "1.0",
            "analysis_sha256": self.sha256, "focus_id": node_id,
            "query": {"direction": direction, "relations": sorted(allowed) if allowed else None,
                      "resolution": resolution, "depth": depth, "max_nodes": max_nodes, "max_edges": max_edges},
            "nodes": [self.nodes[key] for key in sorted(selected)], "edges": edges,
            "edge_counts_by_resolution": dict(sorted(Counter(edge["resolution_status"] for edge in edges).items())),
            "diagnostics": diagnostics[:50], "annotations": annotations[:50],
            "truncation": {"budget_limited": budget_limited, "depth_limited": depth_limited,
                           "diagnostics": len(diagnostics) > 50, "annotations": len(annotations) > 50},
            "coverage": coverage_summary(self.document), "freshness": check_freshness(self.document, root),
        }
