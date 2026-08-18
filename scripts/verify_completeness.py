"""Verify analyzer completeness against an independent golden manifest.

The manifest is intentionally written independently of analyzer internals.  A
case names the source root, the selected language, every declaration that must
be present, and a finite set of source-level relationships that must be
present.  The verifier reports both missing and unexpected declarations so a
test cannot pass merely because a parser returned a subset of the expected
graph.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = PROJECT_ROOT / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from connection_map.analyzer import analyze_repository  # noqa: E402
from connection_map.config import AnalysisConfig  # noqa: E402
from connection_map.contract import validate_document  # noqa: E402

NodeKey = tuple[str, str, str]
EdgeKey = tuple[str, NodeKey, NodeKey]


def _node_key(node: dict[str, Any]) -> NodeKey:
    return (
        str(node.get("file") or ""),
        str(node.get("kind") or ""),
        str(node.get("qualified_name") or ""),
    )


def _expected_node_key(entry: dict[str, Any] | list[str]) -> NodeKey:
    if isinstance(entry, list):
        if len(entry) != 3:
            raise ValueError("expected node arrays must contain [file, kind, qualified_name]")
        return (str(entry[0]), str(entry[1]), str(entry[2]))
    return (
        str(entry.get("file") or ""),
        str(entry.get("kind") or ""),
        str(entry.get("qualified_name") or ""),
    )


def _node_keys(document: dict[str, Any], kinds: set[str]) -> Counter[NodeKey]:
    return Counter(
        _node_key(node)
        for node in document.get("nodes", [])
        if node.get("file") and node.get("kind") in kinds
    )


def _ref_from_entry(entry: dict[str, Any] | list[str]) -> NodeKey:
    return _expected_node_key(entry)


def _edge_key(
    edge: dict[str, Any],
    refs_by_id: dict[str, NodeKey],
) -> EdgeKey | None:
    source = refs_by_id.get(str(edge.get("source_id")))
    target = refs_by_id.get(str(edge.get("target_id")))
    if source is None or target is None:
        # External and unknown endpoints do not have a stable file-qualified
        # identity.  They are covered by diagnostics and relation-specific
        # required checks when a corpus needs to assert them.
        return None
    return (str(edge.get("relation_type") or ""), source, target)


def _expected_edges(case: dict[str, Any]) -> Counter[EdgeKey]:
    result: Counter[EdgeKey] = Counter()
    for entry in case.get("expected_edges", []):
        if isinstance(entry, list):
            if len(entry) != 3:
                raise ValueError("expected edge arrays must contain [relation_type, source, target]")
            relation_type, source, target = entry
        else:
            relation_type = entry.get("relation_type")
            source = entry["source"]
            target = entry["target"]
        result[
            (
                str(relation_type or ""),
                _ref_from_entry(source),
                _ref_from_entry(target),
            )
        ] += 1
    return result


def _actual_edges(
    document: dict[str, Any],
    relation_types: set[str],
) -> Counter[EdgeKey]:
    refs_by_id = {
        str(node["id"]): _node_key(node)
        for node in document.get("nodes", [])
        if node.get("file")
    }
    result: Counter[EdgeKey] = Counter()
    for edge in document.get("edges", []):
        if edge.get("relation_type") not in relation_types:
            continue
        # Completeness of the golden graph is measured for relationships that
        # the analyzer resolved to an in-repository node.  Unresolved and
        # external relationships remain valuable diagnostics, but they are a
        # separate quality dimension and must not look like an unexpected
        # internal declaration edge.
        if edge.get("resolution_status") != "resolved":
            continue
        key = _edge_key(edge, refs_by_id)
        if key is not None:
            result[key] += 1
    return result


def _format_node_differences(values: Counter[NodeKey]) -> list[list[Any]]:
    return [list(key) + [count] for key, count in sorted(values.items())]


def _format_edge_differences(values: Counter[EdgeKey]) -> list[dict[str, Any]]:
    return [
        {
            "relation_type": relation_type,
            "source": list(source),
            "target": list(target),
            "count": count,
        }
        for (relation_type, source, target), count in sorted(values.items())
    ]


def _case_config(case: dict[str, Any]) -> AnalysisConfig:
    values: dict[str, Any] = {
        "language": case["language"],
        "languages": list(case.get("languages", [])),
        "include_tests": bool(case.get("include_tests", True)),
    }
    for name in ("include", "exclude", "test_patterns", "generated"):
        if name in case:
            values[name] = list(case[name])
    return AnalysisConfig(**values)


def verify_case(project_root: Path, case: dict[str, Any]) -> dict[str, Any]:
    root = (project_root / case["root"]).resolve()
    config = _case_config(case)
    document = analyze_repository(root, config, deterministic=True, commit_sha="completeness-golden")
    validate_document(document)

    checked_kinds = set(case.get("checked_kinds", []))
    expected_nodes = Counter(_expected_node_key(entry) for entry in case.get("expected_nodes", []))
    actual_nodes = _node_keys(document, checked_kinds)
    missing_nodes = expected_nodes - actual_nodes
    extra_nodes = actual_nodes - expected_nodes

    expected_edges = _expected_edges(case)
    checked_relations = set(case.get("checked_relations", []))
    actual_edges = _actual_edges(document, checked_relations)
    missing_edges = expected_edges - actual_edges
    exact_relations = checked_relations & set(case.get("exact_relations", []))
    actual_exact_edges = Counter(
        {
            key: count
            for key, count in actual_edges.items()
            if key[0] in exact_relations
        }
    )
    expected_exact_edges = Counter(
        {
            key: count
            for key, count in expected_edges.items()
            if key[0] in exact_relations
        }
    )
    extra_edges = actual_exact_edges - expected_exact_edges

    selected_files = {
        str(node["file"])
        for node in document.get("nodes", [])
        if node.get("file") and node.get("kind") == "module"
    }
    expected_files = set(case.get("expected_files", []))
    missing_files = sorted(expected_files - selected_files)
    extra_files = sorted(selected_files - expected_files) if expected_files else []

    blocked_diagnostics = [
        diagnostic
        for diagnostic in document.get("diagnostics", [])
        if diagnostic.get("code") in set(case.get("blocked_diagnostics", ["parse_error", "parser_recovery"]))
    ]
    status = not (
        missing_nodes
        or extra_nodes
        or missing_edges
        or extra_edges
        or missing_files
        or extra_files
        or blocked_diagnostics
    )
    return {
        "name": case["name"],
        "language": case["language"],
        "root": str(root),
        "status": "complete" if status else "incomplete",
        "expected_files": sorted(expected_files),
        "actual_files": sorted(selected_files),
        "missing_files": missing_files,
        "extra_files": extra_files,
        "checked_kinds": sorted(checked_kinds),
        "expected_nodes": sum(expected_nodes.values()),
        "actual_nodes": sum(actual_nodes.values()),
        "missing_nodes": _format_node_differences(missing_nodes),
        "extra_nodes": _format_node_differences(extra_nodes),
        "checked_relations": sorted(checked_relations),
        "expected_edges": sum(expected_edges.values()),
        "actual_edges": sum(actual_edges.values()),
        "missing_edges": _format_edge_differences(missing_edges),
        "extra_edges": _format_edge_differences(extra_edges),
        "blocked_diagnostics": blocked_diagnostics,
        "diagnostic_counts": dict(Counter(item.get("code", "") for item in document.get("diagnostics", []))),
        "analysis_counts": document.get("meta", {}).get("counts", {}),
    }


def verify_manifest(manifest_path: Path, project_root: Path | None = None) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    project_root = (project_root or PROJECT_ROOT).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "connection-analysis-completeness":
        raise ValueError("manifest.format must be connection-analysis-completeness")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest.cases must be a non-empty array")
    reports = [verify_case(project_root, case) for case in cases]
    return {
        "format": manifest["format"],
        "version": manifest.get("version"),
        "manifest": str(manifest_path),
        "project_root": str(project_root),
        "cases": reports,
        "complete": all(item["status"] == "complete" for item in reports),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify golden completeness cases.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "tests" / "completeness" / "manifest.json",
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = verify_manifest(args.manifest, args.project_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
