"""Small shared helpers for the Phase 3 Tree-sitter analyzers.

The helpers deliberately stop at syntax-level information.  They never import,
build, execute, or connect to the repository being inspected.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AnalysisConfig, discover_source_files, repository_id
from .model import GraphBuilder


class TreeSitterDependencyError(ValueError):
    """Raised when a Phase 3 grammar optional extra is not installed."""


@dataclass(slots=True)
class TreeFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str


def parser_for(grammar: str, *, extra: str | None = None) -> Any:
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        extra_name = extra or grammar
        raise TreeSitterDependencyError(
            f"Phase 3 grammar {grammar!r} requires tree-sitter-language-pack; "
            f"run 'uv sync --extra {extra_name}' (or the documented language extra) first"
        ) from exc
    try:
        return get_parser(grammar)
    except Exception as exc:  # pragma: no cover - grammar availability is environment-specific
        raise TreeSitterDependencyError(f"tree-sitter grammar {grammar!r} is unavailable") from exc


def parse_tree(parser: Any, source: bytes) -> Any:
    return parser.parse(source)


def load_tree_files(
    root: Path,
    config: AnalysisConfig,
    *,
    language: str,
    grammar: str,
    extra: str | None = None,
) -> tuple[list[TreeFile], list[tuple[str, str]]]:
    files, skipped = discover_source_files(root, config, languages={language})
    parser = parser_for(grammar, extra=extra)
    tree_files: list[TreeFile] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_bytes()
        except (OSError, UnicodeError):
            skipped.append((relative, "read_failed"))
            continue
        try:
            tree = parse_tree(parser, source)
        except Exception:  # pragma: no cover - parser-specific failure
            skipped.append((relative, "parse_failed"))
            continue
        tree_files.append(
            TreeFile(
                path=path,
                relative_path=relative,
                source=source,
                tree=tree,
                module_id=f"{language}:{relative}:module",
            )
        )
    return tree_files, sorted(skipped)


def node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def span_for_tree(node: Any) -> dict[str, int]:
    start_row, start_col = node.start_point
    end_row, end_col = node.end_point
    return {
        "start_line": int(start_row) + 1,
        "start_col": int(start_col),
        "end_line": int(end_row) + 1,
        "end_col": int(end_col),
    }


def walk_with_ancestors(node: Any, ancestors: tuple[Any, ...] = ()) -> Iterator[tuple[Any, tuple[Any, ...]]]:
    yield node, ancestors
    next_ancestors = (*ancestors, node)
    for child in node.children:
        yield from walk_with_ancestors(child, next_ancestors)


def nearest_scope(ancestors: Sequence[Any], scope_ids: dict[int, str], fallback: str) -> str:
    for ancestor in reversed(ancestors):
        scope_id = scope_ids.get(ancestor.id)
        if scope_id:
            return scope_id
    return fallback


def first_descendant(node: Any, types: set[str]) -> Any | None:
    for candidate, _ in walk_with_ancestors(node):
        if candidate is not node and candidate.type in types:
            return candidate
    return None


def descendant_texts(node: Any, source: bytes, types: set[str]) -> list[str]:
    return [node_text(candidate, source).strip() for candidate, _ in walk_with_ancestors(node) if candidate.type in types]


def unique_id(existing: dict[str, Any], base: str, salt: int | str) -> str:
    if base not in existing:
        return base
    candidate = f"{base}~{salt}"
    counter = 2
    while candidate in existing:
        candidate = f"{base}~{salt}-{counter}"
        counter += 1
    return candidate


def add_module(builder: GraphBuilder, tree_file: TreeFile, *, language: str, grammar: str) -> None:
    builder.add_node(
        {
            "id": tree_file.module_id,
            "kind": "module",
            "qualified_name": tree_file.relative_path,
            "display_name": tree_file.relative_path,
            "file": tree_file.relative_path,
            "span": span_for_tree(tree_file.tree.root_node),
            "parent_id": None,
            "visibility": "public",
            "extensions": {"language": language, "grammar": grammar},
        }
    )


def add_relation(
    builder: GraphBuilder,
    *,
    source_id: str,
    target_id: str,
    relation_type: str,
    source_span: dict[str, int] | None,
    detail: dict[str, Any],
    resolution_status: str = "resolved",
    confidence: float = 1.0,
    edge_prefix: str = "phase3",
    provenance: str = "ast",
) -> str:
    identity = {
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        # Two identical calls from different source locations are distinct
        # relationships for navigation purposes.  Keeping the span in the
        # identity prevents the second call from being silently collapsed.
        "source_span": source_span,
        "detail": detail,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8", "replace"
        )
    ).hexdigest()[:20]
    edge_id = f"{edge_prefix}:edge:{digest}"
    builder.add_edge(
        {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation_type": relation_type,
            "resolution_status": resolution_status,
            "provenance": provenance,
            "confidence": confidence,
            "source_span": source_span,
            "detail": detail,
        }
    )
    return edge_id


def external_node(
    builder: GraphBuilder,
    cache: dict[str, str],
    *,
    node_id: str,
    qualified_name: str,
    display_name: str,
    language: str,
    kind: str = "external",
    extensions: dict[str, Any] | None = None,
) -> str:
    existing = cache.get(node_id)
    if existing:
        return existing
    node_extensions: dict[str, Any] = {"language": language, "external": True}
    if extensions:
        node_extensions.update(extensions)
    builder.add_node(
        {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": display_name,
            "file": None,
            "span": None,
            "parent_id": None,
            "visibility": "unknown",
            "extensions": node_extensions,
        }
    )
    cache[node_id] = node_id
    return node_id


def diagnostic(
    builder: GraphBuilder,
    *,
    code: str,
    message: str,
    file: str | None = None,
    span: dict[str, int] | None = None,
    severity: str = "warning",
    details: dict[str, Any] | None = None,
) -> None:
    builder.add_diagnostic(
        {
            "code": code,
            "severity": severity,
            "message": message,
            "file": file,
            "span": span,
            "details": details or {},
        }
    )


def add_skipped_diagnostics(builder: GraphBuilder, skipped: list[tuple[str, str]]) -> None:
    for relative, reason in skipped:
        code = {
            "generated": "generated_file",
            "read_failed": "read_error",
            "parse_failed": "parse_error",
        }.get(reason, "excluded_file")
        severity = "error" if code in {"read_error", "parse_error"} else "info"
        diagnostic(
            builder,
            code=code,
            message=f"file was not analyzed: {reason}",
            file=relative,
            severity=severity,
            details={"reason": reason},
        )


def finish_document(
    builder: GraphBuilder,
    *,
    root: Path,
    language: str,
    languages: list[str],
    analyzer_name: str,
    analyzer_version: str,
    config: AnalysisConfig,
    deterministic: bool,
    commit_sha: str | None,
    grammar: str,
    extra_runtime: dict[str, Any] | None = None,
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        parser_version = importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:
        # The extended analyzer can intentionally use its lexical fallback
        # without the optional Tree-sitter package. Keep that result usable
        # and expose the missing dependency in runtime metadata.
        parser_version = "unavailable"
    runtime: dict[str, Any] = {
        "python_version": platform.python_version(),
        "ast_version": "tree-sitter",
        "parser": "tree-sitter-language-pack",
        "parser_version": parser_version,
        "grammars": [grammar],
    }
    if extra_runtime:
        runtime.update(extra_runtime)
    meta = {
        "analyzer": {"name": analyzer_name, "version": analyzer_version},
        "language": language,
        "languages": languages,
        "target": {
            "repository_id": repository_id(root),
            "relative_root": ".",
            "commit_sha": commit_sha,
        },
        "runtime": runtime,
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": config.to_dict(),
    }
    if extensions:
        meta["extensions"] = extensions
    return builder.document(meta)
