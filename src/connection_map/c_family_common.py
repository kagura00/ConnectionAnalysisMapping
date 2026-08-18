"""Shared Tree-sitter state for the C and C++ analyzers."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .analysis_context import AnalysisContext
from .config import AnalysisConfig
from .model import GraphBuilder


class CFamilyAnalyzerDependencyError(ValueError):
    """Raised when the optional C/C++ parser dependency is unavailable."""


@dataclass(slots=True)
class CFile:
    path: Path
    relative_path: str
    language: str
    grammar: str
    source: bytes
    tree: Any
    module_id: str


@dataclass(frozen=True, slots=True)
class CSymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str
    declaration_kind: str


@dataclass(slots=True)
class CFamilyAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    analysis_context: AnalysisContext = field(default_factory=AnalysisContext)
    files: list[CFile] = field(default_factory=list)
    files_by_path: dict[str, CFile] = field(default_factory=dict)
    symbols_by_name: dict[str, list[CSymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[CSymbol]] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    definitions: dict[str, CSymbol] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def add_module(self, c_file: CFile) -> None:
        lines = c_file.source.splitlines() or [b""]
        root = c_file.tree.root_node
        end_line = int(root.end_point[0]) + 1
        end_col = int(root.end_point[1])
        self.builder.add_node(
            {
                "id": c_file.module_id,
                "kind": "module",
                "qualified_name": c_file.relative_path,
                "display_name": c_file.relative_path,
                "file": c_file.relative_path,
                "span": {
                    "start_line": 1,
                    "start_col": 0,
                    "end_line": max(1, end_line if root.end_point else len(lines)),
                    "end_col": max(0, end_col if root.end_point else len(lines[-1])),
                },
                "parent_id": None,
                "visibility": "public",
                "extensions": {"language": c_file.language, "grammar": c_file.grammar},
            }
        )

    def enclosing_definition(self, c_file: CFile, tree_node: Any | None) -> str | None:
        current = tree_node
        while current is not None:
            node_id = self.definition_by_node.get((c_file.relative_path, current.id))
            if node_id:
                return node_id
            current = current.parent
        return None

    def find_qualified_parent(self, qualified_name: str, *, kinds: set[str] | None = None) -> str | None:
        candidates = self.symbols_by_qualified_name.get(qualified_name, [])
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        if len(candidates) == 1:
            return candidates[0].node_id
        return None

    def add_definition(
        self,
        c_file: CFile,
        tree_node: Any,
        *,
        kind: str,
        name: str,
        qualified_name: str,
        declaration_kind: str,
        parent_id: str,
        signature: str | None = None,
        return_behavior: str | None = None,
        return_sites: list[dict[str, Any]] | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> str:
        base_id = f"{c_file.language}:{c_file.relative_path}:{qualified_name}:{kind}"
        node_id = unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": c_file.language,
            "grammar": c_file.grammar,
            "declaration_kind": declaration_kind,
        }
        if extensions:
            node_extensions.update(extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": c_file.relative_path,
            "span": span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": visibility_for_name(name),
            "signature": signature or first_line(node_text(tree_node, c_file.source)),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        if kind in {"function", "method"}:
            node.setdefault("execution_kind", "sync")
        self.builder.add_node(node)
        symbol = CSymbol(node_id, name, qualified_name, kind, c_file.relative_path, declaration_kind)
        self.definitions[node_id] = symbol
        self.definition_by_node[(c_file.relative_path, tree_node.id)] = node_id
        self.symbols_by_name.setdefault(name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(qualified_name, []).append(symbol)
        add_relation(
            self,
            parent_id,
            node_id,
            "contains",
            resolution_status="resolved",
            confidence=1.0,
            source_span=span_for_tree(tree_node),
            detail={"kind": "lexical_definition", "declaration_kind": declaration_kind},
        )
        return node_id

    def symbols_for_name(self, name: str, *, kinds: set[str] | None = None) -> list[CSymbol]:
        candidates = list(self.symbols_by_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        definitions = [
            item
            for item in candidates
            if item.declaration_kind in {"definition", "class", "struct", "union", "enum", "namespace", "alias"}
        ]
        return definitions or candidates

    def symbols_for_qualified_name(self, name: str, *, kinds: set[str] | None = None) -> list[CSymbol]:
        candidates = list(self.symbols_by_qualified_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        definitions = [item for item in candidates if item.declaration_kind == "definition"]
        return definitions or candidates

    def external_node(
        self,
        label: str,
        *,
        language: str,
        unknown: bool = False,
        c_file: CFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (language, kind, label)
        existing = self._external_node_ids.get(key)
        if existing:
            return existing
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"{language}:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": c_file.relative_path if unknown and c_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": language,
                    "external_label": label,
                    **({"unresolved": True} if unknown else {}),
                },
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
        c_file: CFile | None = None,
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
                "file": c_file.relative_path if c_file else file_path,
                "span": span or (span_for_tree(tree_node) if tree_node else None),
                "node_id": node_id,
                "details": details or {},
            }
        )


@lru_cache(maxsize=4)
def parser_for(grammar: str) -> Any:
    try:
        from tree_sitter_language_pack import get_parser
    except ModuleNotFoundError as exc:
        raise CFamilyAnalyzerDependencyError(
            "C/C++解析には任意依存が必要です。'uv sync --extra native' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser(grammar)
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise CFamilyAnalyzerDependencyError(f"Tree-sitter grammar '{grammar}' を読み込めません: {exc}") from exc


def parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def parse_file(path: Path, root: Path, language: str) -> CFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    grammar = language
    tree = parser_for(grammar).parse(source)
    return CFile(
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


def span_sort_key(span: dict[str, int] | None) -> tuple[int, int]:
    return ((span or {}).get("start_line", 0), (span or {}).get("start_col", 0))


def visibility_for_name(name: str) -> str:
    return "private" if name.startswith("_") else "public"


def unique_id(existing: dict[str, Any], base: str, salt: int) -> str:
    if base not in existing:
        return base
    return f"{base}~{salt}"


def add_relation(
    context: CFamilyAnalysisContext,
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
    edge_id = f"c-family-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
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


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None
