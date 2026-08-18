"""Generate deterministic Contract v1 graphs for Web performance testing."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

from connection_map.contract import validate_document

RELATIONS = ("calls", "imports", "dynamic_imports", "inherits", "uses")


def build_graph(node_count: int, edge_factor: int = 3, seed: int = 20260816) -> dict[str, Any]:
    if node_count < 1:
        raise ValueError("node_count must be positive")
    if edge_factor < 0:
        raise ValueError("edge_factor must not be negative")
    rng = random.Random(seed)
    module_count = max(1, (node_count + 24) // 25)
    module_count = min(module_count, node_count)
    nodes: list[dict[str, Any]] = []
    module_ids: list[str] = []
    function_ids: list[str] = []
    for module_index in range(module_count):
        module_id = f"python:synthetic/module_{module_index}.py:module"
        module_ids.append(module_id)
        nodes.append(_node(module_id, "module", f"synthetic.module_{module_index}", "module_{module_index}.py", None))
    for index in range(node_count - module_count):
        module_index = index % module_count
        module_id = module_ids[module_index]
        function_id = f"python:synthetic/module_{module_index}.py:function_{index}:function"
        function_ids.append(function_id)
        behavior = ("returns_value", "returns_none", "mixed", "no_explicit_return")[index % 4]
        nodes.append(
            _node(
                function_id,
                "function",
                f"function_{index}",
                f"synthetic/module_{module_index}.py",
                module_id,
                return_behavior=behavior,
            )
        )

    edges: list[dict[str, Any]] = []
    for index, function_id in enumerate(function_ids):
        module_id = module_ids[index % module_count]
        edges.append(_edge(f"contains-{index}", module_id, function_id, "contains", 1.0, "resolved"))
    total_relations = min(len(function_ids) * edge_factor, max(0, len(function_ids) * (len(function_ids) - 1)))
    seen: set[tuple[str, str, str]] = set()
    function_index = {function_id: index for index, function_id in enumerate(function_ids)}
    for index in range(total_relations):
        source = function_ids[index % len(function_ids)]
        target = function_ids[rng.randrange(len(function_ids))]
        if source == target:
            target = function_ids[(function_index[source] + 1) % len(function_ids)]
        relation = RELATIONS[index % len(RELATIONS)]
        key = (source, target, relation)
        if key in seen:
            continue
        seen.add(key)
        status = "unresolved" if relation == "dynamic_imports" and index % 7 == 0 else "resolved"
        edges.append(_edge(f"relation-{index}", source, target, relation, 0.2 if status == "unresolved" else 1.0, status))

    document = {
        "format": "connection-analysis-map",
        "schema_version": "1.0",
        "meta": {
            "analyzer": {"name": "synthetic-performance-fixture", "version": "0.1.0"},
            "language": "python",
            "target": {"repository_id": "synthetic", "relative_root": ".", "commit_sha": None},
            "runtime": {"python_version": "3.11", "ast_version": "python-3.11"},
            "generated_at": None,
            "deterministic": True,
            "settings": {"node_count": node_count, "edge_factor": edge_factor, "seed": seed},
            "counts": {"nodes": len(nodes), "edges": len(edges), "diagnostics": 0},
        },
        "nodes": nodes,
        "edges": edges,
        "diagnostics": [],
    }
    validate_document(document)
    return document


def _node(
    node_id: str,
    kind: str,
    qualified_name: str,
    file: str,
    parent_id: str | None,
    *,
    return_behavior: str | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "kind": kind,
        "qualified_name": qualified_name,
        "display_name": qualified_name.rsplit(".", 1)[-1],
        "file": file,
        "span": {"start_line": 1, "start_col": 0, "end_line": 1, "end_col": 1},
        "parent_id": parent_id,
        "visibility": "public",
    }
    if return_behavior:
        node["return_behavior"] = return_behavior
        node["execution_kind"] = "sync"
        node["return_sites"] = []
    return node


def _edge(edge_id: str, source_id: str, target_id: str, relation: str, confidence: float, status: str) -> dict[str, Any]:
    return {
        "id": f"edge:synthetic-{edge_id}",
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation,
        "resolution_status": status,
        "provenance": "ast",
        "confidence": confidence,
        "source_span": None,
        "detail": {"synthetic": True},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, required=True)
    parser.add_argument("--edges-per-node", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260816)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = build_graph(args.nodes, args.edges_per_node, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(document['nodes'])} nodes, {len(document['edges'])} edges)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
