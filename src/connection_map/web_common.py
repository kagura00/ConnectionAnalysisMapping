"""Shared helpers and state for the language-specific Web analyzers."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import posixpath
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import AnalysisConfig
from .model import GraphBuilder


class WebAnalyzerDependencyError(ValueError):
    """Raised when the optional Web parser dependency is not installed."""


@dataclass(slots=True)
class WebFile:
    path: Path
    relative_path: str
    language: str
    grammar: str
    source: bytes
    tree: Any
    module_id: str


@dataclass(frozen=True, slots=True)
class Symbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str


@dataclass(frozen=True, slots=True)
class HtmlElementInfo:
    node_id: str
    file_path: str
    tag: str
    element_id: str | None
    classes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CssRuleInfo:
    node_id: str
    file_path: str
    selectors: tuple[str, ...]
    span: dict[str, int] | None


@dataclass(frozen=True, slots=True)
class DomReference:
    source_id: str
    selector: str
    relation_type: str
    file_path: str
    span: dict[str, int] | None
    detail: dict[str, Any]


@dataclass(slots=True)
class WebAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    files: list[WebFile] = field(default_factory=list)
    files_by_path: dict[str, WebFile] = field(default_factory=dict)
    symbols_by_name: dict[str, list[Symbol]] = field(default_factory=dict)
    symbols_by_file_name: dict[str, dict[str, Symbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[Symbol]] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    definition_qualified_name: dict[str, str] = field(default_factory=dict)
    definition_kind: dict[str, str] = field(default_factory=dict)
    import_bindings: dict[str, dict[str, str]] = field(default_factory=dict)
    html_elements: list[HtmlElementInfo] = field(default_factory=list)
    css_rules: list[CssRuleInfo] = field(default_factory=list)
    dom_references: list[DomReference] = field(default_factory=list)
    _external_node_ids: dict[str, str] = field(default_factory=dict)

    def add_module(self, web_file: WebFile) -> None:
        lines = web_file.source.splitlines() or [b""]
        if web_file.tree.root_node.end_point:
            end_point = web_file.tree.root_node.end_point
            end_line = int(end_point[0]) + 1
            end_col = int(end_point[1])
        else:  # pragma: no cover - defensive fallback for parser API changes
            end_line = len(lines)
            end_col = len(lines[-1])
        self.builder.add_node(
            {
                "id": web_file.module_id,
                "kind": "module",
                "qualified_name": web_file.relative_path,
                "display_name": web_file.relative_path,
                "file": web_file.relative_path,
                "span": {
                    "start_line": 1,
                    "start_col": 0,
                    "end_line": max(1, end_line),
                    "end_col": max(0, end_col),
                },
                "parent_id": None,
                "visibility": "public",
                "extensions": {"language": web_file.language, "grammar": web_file.grammar},
            }
        )

    def add_definition(
        self,
        web_file: WebFile,
        tree_node: Any,
        *,
        kind: str,
        name: str | None,
        return_behavior: str | None = None,
        return_sites: list[dict[str, Any]] | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> str:
        actual_name = name or "<lambda>"
        parent_id = self.enclosing_definition(web_file, tree_node.parent) or web_file.module_id
        parent_qualified = self.definition_qualified_name.get(parent_id, web_file.relative_path)
        if parent_id == web_file.module_id:
            qualified_name = actual_name
        elif self.definition_kind.get(parent_id) in {"function", "method", "lambda"}:
            qualified_name = f"{parent_qualified}.<locals>.{actual_name}"
        else:
            qualified_name = f"{parent_qualified}.{actual_name}"
        base_id = f"{web_file.language}:{web_file.relative_path}:{qualified_name}:{kind}"
        node_id = unique_id(self.builder.nodes, base_id, tree_node.start_byte)
        node_extensions = {"language": web_file.language, "grammar": web_file.grammar}
        if extensions:
            node_extensions.update(extensions)
        node = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": actual_name,
            "file": web_file.relative_path,
            "span": span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": visibility_for_name(actual_name),
            "signature": first_line(node_text(tree_node, web_file.source)),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        self.builder.add_node(node)
        self.definition_by_node[(web_file.relative_path, tree_node.id)] = node_id
        self.definition_qualified_name[node_id] = qualified_name
        self.definition_kind[node_id] = kind
        symbol = Symbol(node_id, actual_name, qualified_name, kind, web_file.relative_path)
        self.symbols_by_name.setdefault(actual_name, []).append(symbol)
        self.symbols_by_file_name.setdefault(web_file.relative_path, {}).setdefault(actual_name, symbol)
        self.symbols_by_qualified_name.setdefault(qualified_name, []).append(symbol)
        add_relation(
            self,
            parent_id,
            node_id,
            "contains",
            resolution_status="resolved",
            confidence=1.0,
            source_span=span_for_tree(tree_node),
            detail={"kind": "lexical_definition"},
        )
        return node_id

    def enclosing_definition(self, web_file: WebFile, tree_node: Any | None) -> str | None:
        current = tree_node
        while current is not None:
            node_id = self.definition_by_node.get((web_file.relative_path, current.id))
            if node_id:
                return node_id
            current = current.parent
        return None

    def resolve_symbol(
        self,
        name: str,
        *,
        file_path: str | None = None,
        qualified_name: str | None = None,
    ) -> str | None:
        if file_path and name in self.import_bindings.get(file_path, {}):
            return self.import_bindings[file_path][name]
        if qualified_name:
            qualified_candidates = self.symbols_by_qualified_name.get(qualified_name, [])
            if len(qualified_candidates) == 1:
                return qualified_candidates[0].node_id
        if file_path:
            local = self.symbols_by_file_name.get(file_path, {}).get(name)
            if local:
                return local.node_id
        candidates = self.symbols_by_name.get(name, [])
        if len(candidates) == 1:
            return candidates[0].node_id
        return None

    def external_node(self, label: str, *, unknown: bool = False) -> str:
        key = ("unknown:" if unknown else "external:") + label
        existing = self._external_node_ids.get(key)
        if existing:
            return existing
        kind = "unknown" if unknown else "external"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        node_id = f"web:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label,
                "display_name": label,
                "file": None,
                "span": None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {"external": True},
            }
        )
        self._external_node_ids[key] = node_id
        return node_id

    def diagnostic(
        self,
        code: str,
        severity: str,
        message: str,
        *,
        web_file: WebFile | None = None,
        tree_node: Any | None = None,
        node_id: str | None = None,
        file_path: str | None = None,
        span: dict[str, int] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.builder.add_diagnostic(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "file": web_file.relative_path if web_file else file_path,
                "span": span or (span_for_tree(tree_node) if tree_node else None),
                "node_id": node_id,
                "details": details or {},
            }
        )


@lru_cache(maxsize=8)
def parser_for(grammar: str) -> Any:
    try:
        from tree_sitter_language_pack import get_parser
    except ModuleNotFoundError as exc:
        raise WebAnalyzerDependencyError(
            "Web解析には任意依存が必要です。'uv sync --extra web' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser(grammar)
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise WebAnalyzerDependencyError(f"Tree-sitter grammar '{grammar}' を読み込めません: {exc}") from exc


def parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - dependency check normally catches this
        return "unknown"


def grammar_for_file(language: str, relative_path: str) -> str:
    if language == "typescript" and relative_path.lower().endswith(".tsx"):
        return "tsx"
    return language


def parse_file(path: Path, root: Path, language: str) -> WebFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    grammar = grammar_for_file(language, relative_path)
    tree = parser_for(grammar).parse(source)
    return WebFile(
        path=path,
        relative_path=relative_path,
        language=language,
        grammar=grammar,
        source=source,
        tree=tree,
        module_id=f"{language}:{relative_path}:module",
    )


def span_for_tree(node: Any) -> dict[str, int] | None:
    if node is None or not hasattr(node, "start_point"):
        return None
    return {
        "start_line": int(node.start_point[0]) + 1,
        "start_col": int(node.start_point[1]),
        "end_line": int(node.end_point[0]) + 1,
        "end_col": int(node.end_point[1]),
    }


def node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def first_line(value: str, limit: int = 240) -> str:
    line = value.splitlines()[0].strip() if value.splitlines() else value.strip()
    return line[:limit]


def walk_tree(node: Any) -> Iterable[Any]:
    yield node
    for child in node.children:
        yield from walk_tree(child)


def direct_child(node: Any, *types: str) -> Any | None:
    wanted = set(types)
    return next((child for child in node.children if child.type in wanted), None)


def descendant(node: Any, *types: str) -> Any | None:
    wanted = set(types)
    for child in walk_tree(node):
        if child is not node and child.type in wanted:
            return child
    return None


def children_of_type(node: Any, *types: str) -> list[Any]:
    wanted = set(types)
    return [child for child in node.children if child.type in wanted]


def field_or_child(node: Any, field_name: str, *types: str) -> Any | None:
    value = node.child_by_field_name(field_name)
    if value is not None:
        return value
    if types:
        return direct_child(node, *types)
    return None


def node_name(node: Any, source: bytes, *field_names: str) -> str | None:
    for field_name in field_names:
        child = node.child_by_field_name(field_name)
        if child is not None:
            return node_text(child, source).strip()
    return None


def string_value(node: Any | None, source: bytes) -> str | None:
    if node is None:
        return None
    raw = node_text(node, source).strip()
    if not raw:
        return ""
    if raw[0] in {"'", '"'} and raw[-1:] == raw[0]:
        try:
            value = ast.literal_eval(raw)
            return value if isinstance(value, str) else None
        except (SyntaxError, ValueError):
            return raw[1:-1]
    if raw[0] == "`" and raw[-1:] == "`":
        if "${" in raw:
            return None
        return raw[1:-1]
    return raw


def visibility_for_name(name: str) -> str:
    return "private" if name.startswith("_") else "public"


def unique_id(existing: dict[str, Any], base: str, salt: int) -> str:
    if base not in existing:
        return base
    return f"{base}~{salt}"


def add_relation(
    context: WebAnalysisContext,
    source_id: str,
    target_id: str,
    relation_type: str,
    *,
    resolution_status: str,
    confidence: float,
    source_span: dict[str, int] | None,
    detail: dict[str, Any],
) -> str:
    identity = json.dumps(
        {
            "source": source_id,
            "target": target_id,
            "relation": relation_type,
            "span": source_span,
            "detail": detail,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    edge_id = f"web-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
    context.builder.add_edge(
        {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation_type": relation_type,
            "resolution_status": resolution_status,
            "provenance": "ast",
            "confidence": max(0.0, min(1.0, confidence)),
            "source_span": source_span,
            "detail": detail,
        }
    )
    return edge_id


def resolve_reference(
    context: WebAnalysisContext,
    current: WebFile,
    reference: str,
    *,
    allow_bare: bool = False,
) -> WebFile | None:
    """Resolve a source reference against the files selected for this run.

    Resolution is intentionally limited to ``files_by_path`` rather than the
    filesystem, so an import cannot introduce an unselected dependency. The
    candidate order preserves an explicit suffix before trying the common
    TypeScript/JavaScript extension fallbacks.
    """

    clean = reference.split("#", 1)[0].split("?", 1)[0]
    if not clean or "://" in clean or clean.startswith("data:"):
        return None
    if clean.startswith("/"):
        candidate = posixpath.normpath(clean.lstrip("/"))
    elif clean.startswith(".") or allow_bare:
        candidate = posixpath.normpath(posixpath.join(posixpath.dirname(current.relative_path), clean))
    else:
        return None
    if candidate == ".." or candidate.startswith("../"):
        return None
    candidates = [candidate]
    suffix = Path(candidate).suffix.lower()
    web_extensions = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".css", ".html")
    if suffix in {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}:
        stem = str(Path(candidate).with_suffix(""))
        candidates.extend(stem + extension for extension in web_extensions[:8])
    elif not suffix:
        candidates.extend(candidate + extension for extension in web_extensions)
        candidates.extend(posixpath.join(candidate, "index" + extension) for extension in web_extensions)
    for item in candidates:
        normalized = posixpath.normpath(item)
        if normalized == ".." or normalized.startswith("../"):
            continue
        target = context.files_by_path.get(normalized)
        if target:
            return target
    return None


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None
