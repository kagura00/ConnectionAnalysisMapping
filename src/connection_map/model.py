"""Small in-memory model for the analyzer contract.

The model intentionally uses plain JSON-compatible dictionaries at the boundary.
This keeps the contract usable by analyzers written in other languages while
giving the Python implementation a few helpers for deterministic output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Span:
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def to_dict(self) -> dict[str, int]:
        return {
            "start_line": self.start_line,
            "start_col": self.start_col,
            "end_line": self.end_line,
            "end_col": self.end_col,
        }


def span_for(node: Any) -> dict[str, int] | None:
    """Return a contract span for an AST node, if it has a source position."""

    if not hasattr(node, "lineno"):
        return None
    start_line = int(node.lineno)
    start_col = int(getattr(node, "col_offset", 0))
    end_line = int(getattr(node, "end_lineno", start_line))
    end_col = int(getattr(node, "end_col_offset", start_col))
    return Span(start_line, start_col, end_line, end_col).to_dict()


class GraphBuilder:
    """Collect nodes, edges, and diagnostics while rejecting duplicate IDs."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.diagnostics: list[dict[str, Any]] = []

    def add_node(self, node: dict[str, Any]) -> None:
        node_id = node["id"]
        existing = self.nodes.get(node_id)
        if existing is not None and existing != node:
            raise ValueError(f"node ID collision: {node_id}")
        self.nodes[node_id] = node

    def add_edge(self, edge: dict[str, Any]) -> bool:
        edge_id = edge["id"]
        existing = self.edges.get(edge_id)
        if existing is not None:
            if existing != edge:
                raise ValueError(f"edge ID collision: {edge_id}")
            # Identical rediscovery is harmless and is reported as a duplicate.
            return False
        self.edges[edge_id] = edge
        return True

    def add_diagnostic(self, diagnostic: dict[str, Any]) -> None:
        self.diagnostics.append(diagnostic)

    def document(self, meta: dict[str, Any]) -> dict[str, Any]:
        # Sorting at the boundary keeps filesystem traversal order out of the
        # serialized graph and makes deterministic re-analysis meaningful.
        counts = {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "diagnostics": len(self.diagnostics),
        }
        meta = {**meta, "counts": counts}
        diagnostics = sorted(
            self.diagnostics,
            key=lambda item: (
                item.get("file") or "",
                (item.get("span") or {}).get("start_line", 0),
                item.get("code", ""),
                item.get("message", ""),
            ),
        )
        return {
            "format": "connection-analysis-map",
            "schema_version": "1.0",
            "meta": meta,
            "nodes": [self.nodes[key] for key in sorted(self.nodes)],
            "edges": [self.edges[key] for key in sorted(self.edges)],
            "diagnostics": diagnostics,
        }
