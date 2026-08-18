"""Runtime checks for the analyzer contract.

The JSON Schema is the interchange artifact. This lightweight validator keeps
the default CLI dependency-free and catches the invariants most important to
the viewer (unique IDs and valid references).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any


class ContractError(ValueError):
    """Raised when a graph document violates the v1 contract."""


def canonical_sha256(value: Any) -> str:
    """Return a stable SHA-256 for a JSON-compatible value.

    Pretty-printing and object key order are intentionally ignored so the hash
    can be used to bind a manual overlay or a static bundle to an analysis
    snapshot produced by a different JSON writer.
    """

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_NODE_KINDS = {
    "module",
    "namespace",
    "class",
    "function",
    "method",
    "lambda",
    "interface",
    "type",
    "element",
    "style_rule",
    "external",
    "unknown",
}
_RESOLUTION_STATUSES = {"resolved", "external", "unresolved", "unsupported"}
_PROVENANCE = {"ast", "lsp", "runtime", "manual", "unknown"}
_RETURN_BEHAVIORS = {
    "no_explicit_return",
    "returns_value",
    "returns_none",
    "mixed",
    "unknown",
}


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{name} must be a non-empty string")
    return value


def _reject_unknown_fields(value: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"{name} contains unknown fields: {', '.join(unknown)}")


def _validate_span(value: Any, name: str) -> None:
    span = _require_mapping(value, name)
    _reject_unknown_fields(span, {"start_line", "start_col", "end_line", "end_col"}, name)
    for field in ("start_line", "start_col", "end_line", "end_col"):
        number = span.get(field)
        if not isinstance(number, int) or isinstance(number, bool):
            raise ContractError(f"{name}.{field} must be an integer")
    if span["start_line"] < 1 or span["end_line"] < 1:
        raise ContractError(f"{name} line numbers must be >= 1")
    if span["start_col"] < 0 or span["end_col"] < 0:
        raise ContractError(f"{name} column offsets must be >= 0")


def _validate_optional_span(value: Any, name: str) -> None:
    if value is not None:
        _validate_span(value, name)


def _validate_node(node: Any, ids: set[str]) -> None:
    item = _require_mapping(node, "node")
    _reject_unknown_fields(
        item,
        {
            "id", "kind", "qualified_name", "display_name", "file", "span", "parent_id",
            "visibility", "signature", "return_behavior", "execution_kind", "return_sites", "extensions",
        },
        "node",
    )
    for field in ("id", "kind", "qualified_name", "file", "span", "parent_id", "visibility"):
        if field not in item:
            raise ContractError(f"node.{field} is required")
    node_id = _require_string(item.get("id"), "node.id")
    if node_id in ids:
        raise ContractError(f"duplicate node ID: {node_id}")
    ids.add(node_id)
    if item.get("kind") not in _NODE_KINDS:
        raise ContractError(f"invalid node.kind: {item.get('kind')!r}")
    _require_string(item.get("qualified_name"), "node.qualified_name")
    if "display_name" in item and not isinstance(item["display_name"], str):
        raise ContractError("node.display_name must be a string")
    file_value = item.get("file")
    if file_value is not None and not isinstance(file_value, str):
        raise ContractError("node.file must be a string or null")
    _validate_optional_span(item.get("span"), "node.span")
    parent_id = item.get("parent_id")
    if parent_id is not None:
        _require_string(parent_id, "node.parent_id")
    if item.get("visibility") not in {"public", "private", "unknown"}:
        raise ContractError(f"invalid node.visibility: {item.get('visibility')!r}")
    extensions = item.get("extensions")
    if extensions is not None:
        extensions = _require_mapping(extensions, "node.extensions")
        if "language" in extensions:
            _require_string(extensions["language"], "node.extensions.language")
    if "return_behavior" in item and item["return_behavior"] not in _RETURN_BEHAVIORS:
        raise ContractError(f"invalid node.return_behavior: {item['return_behavior']!r}")
    if "execution_kind" in item and item["execution_kind"] not in {"sync", "async", "generator", "async_generator", "suspend", "unknown"}:
        raise ContractError(f"invalid node.execution_kind: {item['execution_kind']!r}")
    if "signature" in item and not isinstance(item["signature"], str):
        raise ContractError("node.signature must be a string")
    if "return_sites" in item:
        if not isinstance(item["return_sites"], list):
            raise ContractError("node.return_sites must be an array")
        for site in item["return_sites"]:
            site_map = _require_mapping(site, "node.return_site")
            _reject_unknown_fields(site_map, {"span", "value_kind"}, "node.return_site")
            if "span" not in site_map or "value_kind" not in site_map:
                raise ContractError("node.return_site.span and value_kind are required")
            _validate_span(site_map.get("span"), "node.return_site.span")
            if site_map.get("value_kind") not in {"value", "none"}:
                raise ContractError("node.return_site.value_kind must be value or none")


def _validate_edge(edge: Any, node_ids: set[str], edge_ids: set[str]) -> None:
    item = _require_mapping(edge, "edge")
    _reject_unknown_fields(
        item,
        {
            "id", "source_id", "target_id", "relation_type", "resolution_status", "provenance",
            "confidence", "source_span", "detail", "extensions",
        },
        "edge",
    )
    for field in (
        "id", "source_id", "target_id", "relation_type", "resolution_status", "provenance",
        "confidence", "source_span", "detail",
    ):
        if field not in item:
            raise ContractError(f"edge.{field} is required")
    edge_id = _require_string(item.get("id"), "edge.id")
    if edge_id in edge_ids:
        raise ContractError(f"duplicate edge ID: {edge_id}")
    edge_ids.add(edge_id)
    source_id = _require_string(item.get("source_id"), "edge.source_id")
    target_id = _require_string(item.get("target_id"), "edge.target_id")
    if source_id not in node_ids:
        raise ContractError(f"edge source does not exist: {source_id}")
    if target_id not in node_ids:
        raise ContractError(f"edge target does not exist: {target_id}")
    relation_type = item.get("relation_type")
    if not isinstance(relation_type, str) or not relation_type.strip():
        raise ContractError("edge.relation_type must be a non-empty string")
    if item.get("resolution_status") not in _RESOLUTION_STATUSES:
        raise ContractError(f"invalid edge.resolution_status: {item.get('resolution_status')!r}")
    if item.get("provenance") not in _PROVENANCE:
        raise ContractError(f"invalid edge.provenance: {item.get('provenance')!r}")
    confidence = item.get("confidence")
    if not isinstance(confidence, int | float) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ContractError("edge.confidence must be a number between 0 and 1")
    _validate_optional_span(item.get("source_span"), "edge.source_span")
    _require_mapping(item.get("detail"), "edge.detail")
    if "extensions" in item:
        _require_mapping(item["extensions"], "edge.extensions")


def _validate_runtime(value: Any) -> None:
    runtime = _require_mapping(value, "meta.runtime")
    _reject_unknown_fields(runtime, {"python_version", "ast_version", "parser", "parser_version", "grammars", "build_context"}, "meta.runtime")
    for field in ("python_version", "ast_version"):
        _require_string(runtime.get(field), f"meta.runtime.{field}")
    for field in ("parser", "parser_version"):
        if field in runtime:
            _require_string(runtime[field], f"meta.runtime.{field}")
    grammars = runtime.get("grammars")
    if grammars is not None and (
        not isinstance(grammars, list)
        or not all(isinstance(grammar, str) and grammar for grammar in grammars)
    ):
        raise ContractError("meta.runtime.grammars must be an array of non-empty strings")
    if "build_context" in runtime:
        _require_mapping(runtime["build_context"], "meta.runtime.build_context")


def validate_document(document: Any) -> None:
    """Validate a graph and raise :class:`ContractError` on the first error."""

    root = _require_mapping(document, "document")
    _reject_unknown_fields(root, {"format", "schema_version", "meta", "nodes", "edges", "diagnostics"}, "document")
    if root.get("format") != "connection-analysis-map":
        raise ContractError("document.format must be connection-analysis-map")
    if root.get("schema_version") != "1.0":
        raise ContractError("document.schema_version must be 1.0")
    meta = _require_mapping(root.get("meta"), "document.meta")
    _reject_unknown_fields(
        meta,
        {"analyzer", "language", "languages", "target", "runtime", "generated_at", "deterministic", "settings", "counts", "extensions"},
        "document.meta",
    )
    for key in ("analyzer", "language", "target", "runtime", "generated_at", "deterministic", "settings"):
        if key not in meta:
            raise ContractError(f"document.meta.{key} is required")
    analyzer = _require_mapping(meta["analyzer"], "meta.analyzer")
    _reject_unknown_fields(analyzer, {"name", "version"}, "meta.analyzer")
    _require_string(analyzer.get("name"), "meta.analyzer.name")
    _require_string(analyzer.get("version"), "meta.analyzer.version")
    _require_string(meta.get("language"), "meta.language")
    if "languages" in meta:
        languages = meta["languages"]
        if not isinstance(languages, list) or not languages or not all(
            isinstance(language, str) and language.strip() for language in languages
        ):
            raise ContractError("meta.languages must be a non-empty array of language names")
    target = _require_mapping(meta["target"], "meta.target")
    _reject_unknown_fields(target, {"repository_id", "relative_root", "commit_sha"}, "meta.target")
    _require_string(target.get("repository_id"), "meta.target.repository_id")
    if not isinstance(target.get("relative_root"), str):
        raise ContractError("meta.target.relative_root must be a string")
    if "commit_sha" not in target:
        raise ContractError("meta.target.commit_sha is required")
    if target.get("commit_sha") is not None and not isinstance(target.get("commit_sha"), str):
        raise ContractError("meta.target.commit_sha must be a string or null")
    _validate_runtime(meta["runtime"])
    _require_mapping(meta["settings"], "meta.settings")
    if "counts" in meta:
        counts = _require_mapping(meta["counts"], "meta.counts")
        for field in ("nodes", "edges", "diagnostics"):
            if field in counts and (not isinstance(counts[field], int) or isinstance(counts[field], bool) or counts[field] < 0):
                raise ContractError(f"meta.counts.{field} must be a non-negative integer")
    if "extensions" in meta:
        _require_mapping(meta["extensions"], "meta.extensions")
    if not isinstance(meta["deterministic"], bool):
        raise ContractError("meta.deterministic must be boolean")
    if meta["generated_at"] is not None and not isinstance(meta["generated_at"], str):
        raise ContractError("meta.generated_at must be a string or null")

    nodes = root.get("nodes")
    edges = root.get("edges")
    diagnostics = root.get("diagnostics")
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(diagnostics, list):
        raise ContractError("nodes, edges, and diagnostics must be arrays")
    node_ids: set[str] = set()
    for node in nodes:
        _validate_node(node, node_ids)
    for node in nodes:
        parent_id = node.get("parent_id")
        if parent_id is not None and parent_id not in node_ids:
            raise ContractError(f"node parent does not exist: {parent_id}")
    parent_by_id = {node["id"]: node.get("parent_id") for node in nodes}
    for node_id in parent_by_id:
        path: set[str] = set()
        current: str | None = node_id
        while current is not None:
            if current in path:
                raise ContractError(f"node parent cycle detected: {current}")
            path.add(current)
            parent = parent_by_id[current]
            current = parent if isinstance(parent, str) else None
    edge_ids: set[str] = set()
    for edge in edges:
        _validate_edge(edge, node_ids, edge_ids)
    for diagnostic in diagnostics:
        item = _require_mapping(diagnostic, "diagnostic")
        _reject_unknown_fields(item, {"code", "severity", "message", "file", "span", "node_id", "edge_id", "details", "extensions"}, "diagnostic")
        _require_string(item.get("code"), "diagnostic.code")
        _require_string(item.get("message"), "diagnostic.message")
        if item.get("severity") not in {"info", "warning", "error"}:
            raise ContractError(f"invalid diagnostic.severity: {item.get('severity')!r}")
        _validate_optional_span(item.get("span"), "diagnostic.span")
        for field in ("file", "node_id", "edge_id"):
            if field in item and item[field] is not None and not isinstance(item[field], str):
                raise ContractError(f"diagnostic.{field} must be a string or null")
        for field in ("details", "extensions"):
            if field in item:
                _require_mapping(item[field], f"diagnostic.{field}")


def validate_many(documents: Iterable[Any]) -> None:
    for document in documents:
        validate_document(document)
