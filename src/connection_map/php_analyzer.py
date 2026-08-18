"""Tree-sitter based static analyzer for PHP source files."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import AnalysisConfig, discover_source_files, repository_id
from .contract import validate_document
from .model import GraphBuilder

ANALYZER_NAME = "connection-map-php-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"php"}

_TYPE_KINDS = {"class", "interface", "type"}
_CALLABLE_KINDS = {"function", "method", "lambda"}
_TYPE_DECLARATION_TYPES = {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration", "anonymous_class"}
_CALLABLE_DECLARATION_TYPES = {"function_definition", "method_declaration", "anonymous_function", "arrow_function"}
_PARAMETER_TYPES = {"simple_parameter", "variadic_parameter", "property_promotion_parameter"}
_TYPE_CONTAINER_TYPES = {"named_type", "optional_type", "union_type", "intersection_type", "callable_type", "array_type", "parenthesized_type"}
_BUILTIN_TYPES = {
    "array",
    "bool",
    "callable",
    "false",
    "float",
    "int",
    "iterable",
    "mixed",
    "never",
    "null",
    "object",
    "string",
    "true",
    "void",
    "self",
    "parent",
    "static",
}


class PhpAnalyzerDependencyError(ValueError):
    """Raised when the optional PHP parser dependency is unavailable."""


@dataclass(slots=True)
class PhpFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str
    namespace_nodes: list[tuple[Any, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PhpSymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str | None
    declaration_kind: str
    arity: int | None = None
    min_arity: int | None = None
    owner_q: str | None = None


@dataclass(slots=True)
class PhpImportInfo:
    class_aliases: dict[str, str] = field(default_factory=dict)
    function_aliases: dict[str, str] = field(default_factory=dict)
    const_aliases: dict[str, str] = field(default_factory=dict)

    def aliases_for(self, kind: str) -> dict[str, str]:
        if kind == "function":
            return self.function_aliases
        if kind == "const":
            return self.const_aliases
        return self.class_aliases


@dataclass(slots=True)
class PhpAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    files: list[PhpFile] = field(default_factory=list)
    files_by_path: dict[str, PhpFile] = field(default_factory=dict)
    namespace_by_q: dict[str, str] = field(default_factory=dict)
    namespace_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    definitions: dict[str, PhpSymbol] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    symbols_by_name: dict[str, list[PhpSymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[PhpSymbol]] = field(default_factory=dict)
    imports_by_scope: dict[tuple[str, str], PhpImportInfo] = field(default_factory=dict)
    variable_types: dict[tuple[str, str], str] = field(default_factory=dict)
    property_types: dict[tuple[str, str], str] = field(default_factory=dict)
    return_types: dict[str, str] = field(default_factory=dict)
    parent_types: dict[str, str] = field(default_factory=dict)
    used_types: dict[str, list[str]] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_namespace(
        self,
        qualified_name: str,
        *,
        php_file: PhpFile,
        tree_node: Any | None,
        declaration_form: str,
    ) -> str:
        existing = self.namespace_by_q.get(qualified_name)
        if existing is not None:
            if tree_node is not None:
                self.namespace_by_node[(php_file.relative_path, tree_node.id)] = qualified_name
            return existing

        parent_q = qualified_name.rsplit("\\", 1)[0] if "\\" in qualified_name else None
        parent_id = None
        if parent_q:
            parent_id = self.add_namespace(
                parent_q,
                php_file=php_file,
                tree_node=None,
                declaration_form="inferred_parent",
            )
        node_id = f"php:{qualified_name}:namespace"
        display_name = "\\" if qualified_name == _GLOBAL_NAMESPACE else qualified_name.rsplit("\\", 1)[-1]
        self.builder.add_node(
            {
                "id": node_id,
                "kind": "namespace",
                "qualified_name": qualified_name,
                "display_name": display_name,
                "file": php_file.relative_path,
                "span": _span_for_tree(tree_node) if tree_node is not None else _span_for_tree(php_file.tree.root_node),
                "parent_id": parent_id,
                "visibility": "public",
                "signature": f"namespace {display_name}",
                "extensions": {
                    "language": "php",
                    "grammar": "php",
                    "declaration_form": declaration_form,
                },
            }
        )
        self.namespace_by_q[qualified_name] = node_id
        symbol = PhpSymbol(
            node_id=node_id,
            name=display_name,
            qualified_name=qualified_name,
            kind="namespace",
            file_path=php_file.relative_path,
            declaration_kind=declaration_form,
        )
        self._index_symbol(symbol)
        if tree_node is not None:
            self.namespace_by_node[(php_file.relative_path, tree_node.id)] = qualified_name
        return node_id

    def add_module(self, php_file: PhpFile) -> None:
        namespace_names = sorted({name for _, name in php_file.namespace_nodes})
        if not namespace_names:
            namespace_names = [_GLOBAL_NAMESPACE]
        self.builder.add_node(
            {
                "id": php_file.module_id,
                "kind": "module",
                "qualified_name": php_file.relative_path,
                "display_name": php_file.relative_path,
                "file": php_file.relative_path,
                "span": _span_for_tree(php_file.tree.root_node),
                "parent_id": None,
                "visibility": "public",
                "signature": f"file {php_file.relative_path}",
                "extensions": {
                    "language": "php",
                    "grammar": "php",
                    "namespace_names": [name for name in namespace_names if name != _GLOBAL_NAMESPACE],
                },
            }
        )
        for namespace_name in namespace_names:
            namespace_id = self.namespace_by_q[namespace_name]
            _add_relation(
                self,
                php_file.module_id,
                namespace_id,
                "contains",
                resolution_status="resolved",
                confidence=1.0,
                source_span=_span_for_tree(php_file.tree.root_node),
                detail={"kind": "file_namespace"},
            )

    def add_definition(
        self,
        php_file: PhpFile,
        tree_node: Any,
        *,
        kind: str,
        name: str,
        qualified_name: str,
        declaration_kind: str,
        parent_id: str,
        arity: int | None = None,
        min_arity: int | None = None,
        signature: str | None = None,
        return_behavior: str | None = None,
        return_sites: list[dict[str, Any]] | None = None,
        extensions: dict[str, Any] | None = None,
        owner_q: str | None = None,
    ) -> str:
        base_id = f"php:{php_file.relative_path}:{qualified_name}:{kind}"
        node_id = _unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": "php",
            "grammar": "php",
            "declaration_kind": declaration_kind,
        }
        if extensions:
            node_extensions.update(extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": php_file.relative_path,
            "span": _span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": _visibility_for_node(tree_node, php_file.source),
            "signature": signature or _signature_for_node(tree_node, php_file.source),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        if kind in _CALLABLE_KINDS:
            node["execution_kind"] = "sync"
        self.builder.add_node(node)
        parent_symbol = self.definitions.get(parent_id)
        resolved_owner = owner_q or _owner_qualified_name(parent_symbol)
        symbol = PhpSymbol(
            node_id=node_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=php_file.relative_path,
            declaration_kind=declaration_kind,
            arity=arity,
            min_arity=min_arity,
            owner_q=resolved_owner,
        )
        self._index_symbol(symbol)
        self.definition_by_node[(php_file.relative_path, tree_node.id)] = node_id
        _add_relation(
            self,
            parent_id,
            node_id,
            "contains",
            resolution_status="resolved",
            confidence=1.0,
            source_span=_span_for_tree(tree_node),
            detail={"kind": "lexical_definition", "declaration_kind": declaration_kind},
        )
        return node_id

    def _index_symbol(self, symbol: PhpSymbol) -> None:
        self.definitions[symbol.node_id] = symbol
        self.symbols_by_name.setdefault(symbol.name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(symbol.qualified_name, []).append(symbol)

    def symbols_for_name(self, name: str, *, kinds: set[str] | None = None) -> list[PhpSymbol]:
        candidates = list(self.symbols_by_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def symbols_for_qualified_name(self, name: str, *, kinds: set[str] | None = None) -> list[PhpSymbol]:
        candidates = list(self.symbols_by_qualified_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def external_node(
        self,
        label: str,
        *,
        unknown: bool = False,
        php_file: PhpFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (kind, label)
        existing = self._external_node_ids.get(key)
        if existing is not None:
            return existing
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"php:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": php_file.relative_path if unknown and php_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": "php",
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
        php_file: PhpFile | None = None,
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
                "file": php_file.relative_path if php_file else file_path,
                "span": span or (_span_for_tree(tree_node) if tree_node else None),
                "node_id": node_id,
                "details": details or {},
            }
        )


_GLOBAL_NAMESPACE = "<global>"


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Analyze PHP source without importing, building, or executing target code."""

    active_config = config or AnalysisConfig(language="php")
    active_config.validate()
    if active_config.language != "php":
        raise ValueError("PHP analyzer requires language = 'php'")
    if set(active_config.active_languages()) != SUPPORTED_LANGUAGES:
        raise ValueError("PHP analyzer supports only php")

    root = root.resolve()
    builder = GraphBuilder()
    context = PhpAnalysisContext(root, active_config, builder)
    files, skipped = discover_source_files(root, active_config, languages={"php"})
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped PHP source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            php_file = _parse_file(path, root)
        except (OSError, UnicodeError) as exc:
            builder.add_diagnostic(
                {
                    "code": "read_error",
                    "severity": "error",
                    "message": str(exc),
                    "file": relative_path,
                    "span": None,
                    "details": {},
                }
            )
            continue
        context.files.append(php_file)
        context.files_by_path[relative_path] = php_file

    _index_namespaces(context)
    for php_file in context.files:
        context.add_module(php_file)
        if php_file.tree.root_node.has_error:
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {php_file.relative_path}",
                php_file=php_file,
                tree_node=php_file.tree.root_node,
                details={"grammar": "php"},
            )

    for php_file in context.files:
        _index_imports(context, php_file)
    for php_file in context.files:
        _collect_type_definitions(context, php_file)
    for php_file in context.files:
        _collect_callable_definitions(context, php_file)
    for php_file in context.files:
        _collect_scope_relations(context, php_file)
    for php_file in context.files:
        _collect_declared_metadata(context, php_file)
    for php_file in context.files:
        _collect_assignment_relations(context, php_file)
    for php_file in context.files:
        _collect_call_relations(context, php_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "php",
        "languages": ["php"],
        "target": {
            "repository_id": repository_id(root),
            "relative_root": ".",
            "commit_sha": commit_sha if commit_sha is not None else _git_commit(root),
        },
        "runtime": {
            "python_version": platform.python_version(),
            "ast_version": "tree-sitter",
            "parser": "tree-sitter-language-pack",
            "parser_version": _parser_package_version(),
            "grammars": ["php"],
        },
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": active_config.to_dict(),
        "extensions": {
            "namespace_count": len(context.namespace_by_q),
            "dynamic_resolution": "unresolved",
            "composer_resolution": "not_attempted",
        },
    }
    document = builder.document(meta)
    validate_document(document)
    return document


@lru_cache(maxsize=2)
def _parser_for() -> Any:
    try:
        from tree_sitter_language_pack import get_parser
    except ModuleNotFoundError as exc:
        raise PhpAnalyzerDependencyError(
            "PHP解析には任意依存が必要です。'uv sync --extra php' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser("php")
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise PhpAnalyzerDependencyError(f"Tree-sitter grammar 'php'を読み込めません: {exc}") from exc


def _parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _parse_file(path: Path, root: Path) -> PhpFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    return PhpFile(
        path=path,
        relative_path=relative_path,
        source=source,
        tree=_parser_for().parse(source),
        module_id=f"php:{relative_path}:module",
    )


def _index_namespaces(context: PhpAnalysisContext) -> None:
    for php_file in context.files:
        nodes = [node for node in _walk_tree(php_file.tree.root_node) if node.type == "namespace_definition"]
        for node in nodes:
            name_node = node.child_by_field_name("name")
            qualified_name = _normalize_name(_node_text(name_node, php_file.source)) if name_node is not None else _GLOBAL_NAMESPACE
            qualified_name = qualified_name or _GLOBAL_NAMESPACE
            php_file.namespace_nodes.append((node, qualified_name))
            declaration_form = "bracketed" if node.child_by_field_name("body") is not None else "unbracketed"
            context.add_namespace(
                qualified_name,
                php_file=php_file,
                tree_node=node,
                declaration_form=declaration_form,
            )
        if not php_file.namespace_nodes:
            context.add_namespace(
                _GLOBAL_NAMESPACE,
                php_file=php_file,
                tree_node=None,
                declaration_form="inferred_global",
            )


def _index_imports(context: PhpAnalysisContext, php_file: PhpFile) -> None:
    for node in _walk_tree(php_file.tree.root_node):
        if node.type != "namespace_use_declaration":
            continue
        namespace_name = _namespace_for_node(context, php_file, node)
        info = context.imports_by_scope.setdefault((php_file.relative_path, namespace_name), PhpImportInfo())
        entries = _parse_namespace_use(_node_text(node, php_file.source))
        for kind, path, alias in entries:
            info.aliases_for(kind)[alias] = path


def _collect_type_definitions(context: PhpAnalysisContext, php_file: PhpFile) -> None:
    for node in _walk_tree(php_file.tree.root_node):
        if node.type not in _TYPE_DECLARATION_TYPES:
            continue
        namespace_name = _namespace_for_node(context, php_file, node)
        namespace_id = context.namespace_by_q[namespace_name]
        if node.type == "anonymous_class":
            name = f"class@anonymous:{node.start_point[0] + 1}:{node.start_point[1]}"
        else:
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, php_file.source).strip() if name_node is not None else "<anonymous>"
        if not name:
            continue
        if node.type == "interface_declaration":
            kind = "interface"
        elif node.type in {"trait_declaration", "enum_declaration"}:
            kind = "type"
        else:
            kind = "class"
        qualified_name = _join_namespace(namespace_name, name)
        parent_id = _scope_id_for_node(context, php_file, node, include_current=False) or namespace_id
        context.add_definition(
            php_file,
            node,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            declaration_kind=node.type,
            parent_id=parent_id,
            extensions={
                "anonymous": node.type == "anonymous_class",
                "php_type": _php_type_form(node.type),
            },
        )


def _collect_callable_definitions(context: PhpAnalysisContext, php_file: PhpFile) -> None:
    for node in _walk_tree(php_file.tree.root_node):
        if node.type not in _CALLABLE_DECLARATION_TYPES:
            continue
        parent_id = _scope_id_for_node(context, php_file, node, include_current=False)
        if parent_id is None:
            parent_id = context.namespace_by_q[_namespace_for_node(context, php_file, node)]
        parent_symbol = context.definitions.get(parent_id)
        parameters = node.child_by_field_name("parameters")
        arity, min_arity = _parameter_arity(parameters)
        if node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, php_file.source).strip() if name_node is not None else "<method>"
            if parent_symbol is None or parent_symbol.kind not in _TYPE_KINDS:
                continue
            qualified_name = f"{parent_symbol.qualified_name}::{name}({arity})"
            kind = "method"
            declaration_form = node.type
        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, php_file.source).strip() if name_node is not None else "<function>"
            prefix = parent_symbol.qualified_name if parent_symbol and parent_symbol.kind == "namespace" else _namespace_for_node(context, php_file, node)
            qualified_name = f"{_join_namespace(prefix, name)}({arity})" if prefix != _GLOBAL_NAMESPACE else f"{name}({arity})"
            kind = "function"
            declaration_form = node.type
        else:
            line = node.start_point[0] + 1
            col = node.start_point[1]
            name = "<closure>" if node.type == "anonymous_function" else "<arrow>"
            parent_q = parent_symbol.qualified_name if parent_symbol else _namespace_for_node(context, php_file, node)
            qualified_name = f"{parent_q}::{name}@{line}:{col}({arity})"
            kind = "lambda"
            declaration_form = node.type
        return_behavior, return_sites = _return_info(node, include_arrow=node.type == "arrow_function")
        context.add_definition(
            php_file,
            node,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            declaration_kind=declaration_form,
            parent_id=parent_id,
            arity=arity,
            min_arity=min_arity,
            return_behavior=return_behavior,
            return_sites=return_sites,
            extensions={"parameter_count": arity, "minimum_parameter_count": min_arity},
            owner_q=parent_symbol.qualified_name if parent_symbol and parent_symbol.kind in _TYPE_KINDS else None,
        )


def _collect_scope_relations(context: PhpAnalysisContext, php_file: PhpFile) -> None:
    for node in _walk_tree(php_file.tree.root_node):
        if node.type == "namespace_use_declaration":
            _collect_import(context, php_file, node)
        elif node.type in _TYPE_DECLARATION_TYPES:
            _collect_inheritance(context, php_file, node)
        elif node.type == "use_declaration" and _enclosing_type_node(node) is not None:
            _collect_trait_use(context, php_file, node)


def _collect_declared_metadata(context: PhpAnalysisContext, php_file: PhpFile) -> None:
    for node in _walk_tree(php_file.tree.root_node):
        if node.type in _PARAMETER_TYPES | {"property_declaration", "function_definition", "method_declaration"}:
            _collect_declared_type_uses(context, php_file, node)


def _collect_assignment_relations(context: PhpAnalysisContext, php_file: PhpFile) -> None:
    for node in _walk_tree(php_file.tree.root_node):
        if node.type == "assignment_expression":
            _collect_assignment_type(context, php_file, node)


def _collect_call_relations(context: PhpAnalysisContext, php_file: PhpFile) -> None:
    for node in _walk_tree(php_file.tree.root_node):
        if node.type == "function_call_expression":
            _collect_function_call(context, php_file, node)
        elif node.type == "member_call_expression":
            _collect_member_call(context, php_file, node)
        elif node.type == "scoped_call_expression":
            _collect_scoped_call(context, php_file, node)
        elif node.type == "object_creation_expression":
            _collect_object_creation(context, php_file, node)


def _collect_import(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    raw = _node_text(node, php_file.source).strip()
    entries = _parse_namespace_use(raw)
    if not entries:
        target = context.external_node(f"import:{raw}", unknown=True, php_file=php_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, php_file, node, "unresolved_import", f"PHP useを解釈できません: {raw}")
        _add_relation(
            context,
            php_file.module_id,
            target,
            "imports",
            resolution_status="unresolved",
            confidence=0.2,
            source_span=_span_for_tree(node),
            detail={"import": raw},
        )
        return
    for kind, path, alias in entries:
        candidates = _symbols_for_reference(context, php_file, path, kind=kind, node=node)
        if len(candidates) == 1:
            target, status, confidence = candidates[0].node_id, "resolved", 1.0
        elif len(candidates) > 1:
            target = context.external_node(f"import:{path}", unknown=True, php_file=php_file, span=_span_for_tree(node))
            status, confidence = "unresolved", 0.2
            _diagnose_unresolved(context, php_file, node, "unresolved_import", f"PHP useを一意に解決できません: {path}")
        else:
            target = context.external_node(f"import:{path}")
            status, confidence = "external", 0.75
        _add_relation(
            context,
            php_file.module_id,
            target,
            "imports",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(node),
            detail={"import": raw, "kind": kind, "path": path, "alias": alias},
        )


def _collect_inheritance(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    owner_id = context.definition_by_node.get((php_file.relative_path, node.id))
    if owner_id is None:
        return
    owner_symbol = context.definitions.get(owner_id)
    if owner_symbol is None:
        return
    for child in node.named_children:
        if child.type == "base_clause":
            role = "extends"
        elif child.type == "class_interface_clause":
            role = "implements" if node.type in {"class_declaration", "enum_declaration", "anonymous_class"} else "extends"
        else:
            continue
        for reference_node in _reference_children(child):
            reference = _normalize_name(_node_text(reference_node, php_file.source))
            if not reference:
                continue
            target, status, confidence = _resolve_type(context, php_file, reference, reference_node)
            _add_relation(
                context,
                owner_id,
                target,
                "inherits",
                resolution_status=status,
                confidence=confidence,
                source_span=_span_for_tree(reference_node),
                detail={"reference": reference, "role": role},
            )
            if role == "extends" and status == "resolved":
                target_symbol = context.definitions.get(target)
                if target_symbol is not None and target_symbol.kind in _TYPE_KINDS:
                    context.parent_types[owner_symbol.qualified_name] = target_symbol.qualified_name


def _collect_trait_use(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    owner_node = _enclosing_type_node(node)
    owner_id = context.definition_by_node.get((php_file.relative_path, owner_node.id)) if owner_node is not None else None
    if owner_id is None:
        return
    raw = _node_text(node, php_file.source).strip()
    body = re.sub(r"^use\s+", "", raw.removesuffix(";").strip(), flags=re.IGNORECASE)
    body = body.split("{", 1)[0].strip()
    for reference in _split_top_level(body, ","):
        reference = _normalize_name(reference)
        if not reference:
            continue
        target, status, confidence = _resolve_type(context, php_file, reference, node)
        _add_relation(
            context,
            owner_id,
            target,
            "uses",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(node),
            detail={"reference": reference, "role": "trait", "declaration": raw},
        )
        if status == "resolved":
            target_symbol = context.definitions.get(target)
            owner_symbol = context.definitions.get(owner_id)
            if target_symbol is not None and owner_symbol is not None:
                context.used_types.setdefault(owner_symbol.qualified_name, []).append(target_symbol.qualified_name)


def _collect_declared_type_uses(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    type_node = node.child_by_field_name("type")
    if node.type in {"function_definition", "method_declaration"}:
        type_node = node.child_by_field_name("return_type")
    if type_node is not None:
        source_id = _enclosing_definition(context, php_file, node) or php_file.module_id
        _add_type_references(context, php_file, source_id, type_node)
        if node.type in {"function_definition", "method_declaration"} and source_id in context.definitions:
            primary = _first_type_reference(type_node, php_file.source)
            if primary:
                context.return_types[source_id] = primary
    if node.type in _PARAMETER_TYPES:
        source_id = _enclosing_definition(context, php_file, node) or php_file.module_id
        names = _variable_names(node, php_file.source)
        primary = _first_type_reference(type_node, php_file.source) if type_node is not None else ""
        for name in names:
            if primary:
                context.variable_types[(source_id, name)] = primary
        owner_q = _owner_type_q(context, source_id)
        if owner_q and primary and node.type == "property_promotion_parameter":
            for name in names:
                context.property_types[(owner_q, name)] = primary
    elif node.type == "property_declaration" and type_node is not None:
        owner_q = _owner_type_q(context, _enclosing_definition(context, php_file, node) or php_file.module_id)
        primary = _first_type_reference(type_node, php_file.source)
        if owner_q and primary:
            for name in _variable_names(node, php_file.source):
                context.property_types[(owner_q, name)] = primary


def _add_type_references(context: PhpAnalysisContext, php_file: PhpFile, source_id: str, type_node: Any) -> None:
    seen: set[tuple[str, int]] = set()
    for reference, reference_node in _type_references(type_node, php_file.source):
        key = (reference, int(reference_node.start_byte))
        if key in seen or reference.lower() in _BUILTIN_TYPES:
            continue
        seen.add(key)
        target, status, confidence = _resolve_type(context, php_file, reference, reference_node)
        _add_relation(
            context,
            source_id,
            target,
            "uses",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(reference_node),
            detail={"reference": reference, "kind": "type_reference"},
        )


def _collect_function_call(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    caller = _enclosing_definition(context, php_file, node) or php_file.module_id
    function_node = node.child_by_field_name("function")
    expression = _node_text(function_node, php_file.source).strip() if function_node is not None else ""
    arguments = node.child_by_field_name("arguments")
    arity = _argument_arity(arguments)
    if not expression or expression.startswith("$"):
        _record_unresolved_call(context, php_file, node, caller, expression, arity, receiver="")
        return
    candidates = _callable_candidates(context, php_file, expression, arity, node, kind="function")
    _record_call(context, php_file, node, caller, candidates, expression, arity, receiver="", call_kind="function")


def _collect_member_call(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    caller = _enclosing_definition(context, php_file, node) or php_file.module_id
    object_node = node.child_by_field_name("object")
    name_node = node.child_by_field_name("name")
    name = _node_text(name_node, php_file.source).strip() if name_node is not None else ""
    receiver = _node_text(object_node, php_file.source).strip() if object_node is not None else ""
    arity = _argument_arity(node.child_by_field_name("arguments"))
    owner_q = _receiver_type_reference(context, php_file, caller, object_node)
    candidates = _method_candidates(context, owner_q, name, arity)
    expression = _node_text(node, php_file.source).strip()
    _record_call(context, php_file, node, caller, candidates, expression, arity, receiver=receiver, call_kind="member")


def _collect_scoped_call(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    caller = _enclosing_definition(context, php_file, node) or php_file.module_id
    scope_node = node.child_by_field_name("scope")
    name_node = node.child_by_field_name("name")
    scope = _node_text(scope_node, php_file.source).strip() if scope_node is not None else ""
    name = _node_text(name_node, php_file.source).strip() if name_node is not None else ""
    arity = _argument_arity(node.child_by_field_name("arguments"))
    owner_q = _scoped_owner_q(context, php_file, caller, scope, node)
    candidates = _method_candidates(context, owner_q, name, arity)
    expression = _node_text(node, php_file.source).strip()
    _record_call(context, php_file, node, caller, candidates, expression, arity, receiver=scope, call_kind="scoped")


def _collect_object_creation(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    caller = _enclosing_definition(context, php_file, node) or php_file.module_id
    class_node = _object_class_node(node)
    if class_node is None or class_node.type == "anonymous_class":
        return
    reference = _normalize_name(_node_text(class_node, php_file.source))
    if not reference:
        return
    target, status, confidence = _resolve_type(context, php_file, reference, class_node)
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"expression": _node_text(node, php_file.source).strip(), "callee": reference, "kind": "object_creation"},
    )


def _collect_assignment_type(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> None:
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None:
        return
    names = _variable_names(left, php_file.source)
    source_id = _enclosing_definition(context, php_file, node) or php_file.module_id
    type_reference = _receiver_type_reference(context, php_file, source_id, right)
    if not type_reference:
        type_reference = _expression_type_reference(right, php_file.source)
    if not type_reference:
        return
    for name in names:
        context.variable_types[(source_id, name)] = type_reference


def _record_call(
    context: PhpAnalysisContext,
    php_file: PhpFile,
    node: Any,
    caller: str,
    candidates: list[PhpSymbol],
    expression: str,
    arity: int,
    *,
    receiver: str,
    call_kind: str,
) -> None:
    if len(candidates) == 1:
        target, status, confidence = candidates[0].node_id, "resolved", 1.0
    elif len(candidates) > 1:
        target = context.external_node(f"call:{expression}", unknown=True, php_file=php_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
        _diagnose_unresolved(
            context,
            php_file,
            node,
            "unresolved_call",
            f"PHP呼び出しを一意に解決できません: {expression}",
            node_id=caller,
            details={"expression": expression, "callee": expression, "arity": arity, "receiver": receiver},
        )
    else:
        target = context.external_node(f"call:{expression or '<unknown>'}")
        status, confidence = "external", 0.7
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"expression": expression, "callee": expression, "arity": arity, "receiver": receiver, "kind": call_kind},
    )


def _record_unresolved_call(
    context: PhpAnalysisContext,
    php_file: PhpFile,
    node: Any,
    caller: str,
    expression: str,
    arity: int,
    *,
    receiver: str,
) -> None:
    target = context.external_node(f"call:{expression or '<dynamic>'}", unknown=True, php_file=php_file, span=_span_for_tree(node))
    _diagnose_unresolved(
        context,
        php_file,
        node,
        "unresolved_call",
        f"PHP呼び出しを静的に解決できません: {expression or '<dynamic>'}",
        node_id=caller,
        details={"expression": expression, "arity": arity, "receiver": receiver},
    )
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status="unresolved",
        confidence=0.2,
        source_span=_span_for_tree(node),
        detail={"expression": expression, "callee": expression, "arity": arity, "receiver": receiver, "kind": "dynamic"},
    )


def _resolve_type(context: PhpAnalysisContext, php_file: PhpFile, reference: str, node: Any) -> tuple[str, str, float]:
    candidates = _symbols_for_reference(context, php_file, reference, kind="class", node=node)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(f"type:{reference}", unknown=True, php_file=php_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, php_file, node, "unresolved_type", f"PHP型を一意に解決できません: {reference}")
        return target, "unresolved", 0.2
    if _looks_local_reference(context, php_file, reference, node):
        target = context.external_node(f"type:{reference}", unknown=True, php_file=php_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, php_file, node, "unresolved_type", f"対象内PHP型を解決できません: {reference}")
        return target, "unresolved", 0.2
    return context.external_node(f"type:{reference}"), "external", 0.7


def _symbols_for_reference(
    context: PhpAnalysisContext,
    php_file: PhpFile,
    reference: str,
    *,
    kind: str,
    node: Any,
) -> list[PhpSymbol]:
    raw = _normalize_name(reference)
    if not raw:
        return []
    if kind == "class":
        special = _special_type_reference(context, php_file, raw, node)
        if special:
            return special
    if raw.lower() in _BUILTIN_TYPES:
        return []
    info = context.imports_by_scope.get((php_file.relative_path, _namespace_for_node(context, php_file, node)), PhpImportInfo())
    aliases = info.aliases_for(kind)
    names: list[str] = []
    if raw.startswith("namespace\\"):
        names.append(_join_namespace(_namespace_for_node(context, php_file, node), raw.removeprefix("namespace\\")))
    else:
        segments = raw.split("\\")
        imported = aliases.get(segments[0])
        if imported:
            names.append("\\".join([imported, *segments[1:]]))
        current_namespace = _namespace_for_node(context, php_file, node)
        if current_namespace != _GLOBAL_NAMESPACE:
            names.append(_join_namespace(current_namespace, raw))
        names.append(raw)
    candidates: list[PhpSymbol] = []
    symbol_kinds = _TYPE_KINDS if kind == "class" else {"function"}
    for name in names:
        candidates.extend(context.symbols_for_qualified_name(name, kinds=symbol_kinds))
        if kind == "function":
            candidates.extend(
                symbol
                for qualified_name, symbols in context.symbols_by_qualified_name.items()
                if qualified_name.startswith(f"{name}(")
                for symbol in symbols
                if symbol.kind == "function"
            )
    if not candidates and "\\" not in raw:
        candidates.extend(context.symbols_for_name(raw, kinds=symbol_kinds))
    return _unique_symbols(candidates)


def _callable_candidates(
    context: PhpAnalysisContext,
    php_file: PhpFile,
    reference: str,
    arity: int,
    node: Any,
    *,
    kind: str,
) -> list[PhpSymbol]:
    candidates = _symbols_for_reference(context, php_file, reference, kind=kind, node=node)
    return _filter_arity(candidates, arity)


def _method_candidates(context: PhpAnalysisContext, owner_q: str | None, name: str, arity: int) -> list[PhpSymbol]:
    if not name:
        return []
    owners = _owner_chain(context, owner_q)
    candidates = [symbol for symbol in context.symbols_for_name(name, kinds={"method"}) if symbol.owner_q in owners]
    return _filter_arity(candidates, arity)


def _special_type_reference(context: PhpAnalysisContext, php_file: PhpFile, reference: str, node: Any) -> list[PhpSymbol]:
    caller = _enclosing_definition(context, php_file, node)
    owner_q = _owner_type_q(context, caller or php_file.module_id)
    if reference in {"self", "static"} and owner_q:
        return context.symbols_for_qualified_name(owner_q, kinds=_TYPE_KINDS)
    if reference == "parent" and owner_q:
        parent_q = context.parent_types.get(owner_q)
        return context.symbols_for_qualified_name(parent_q, kinds=_TYPE_KINDS) if parent_q else []
    return []


def _receiver_type_reference(context: PhpAnalysisContext, php_file: PhpFile, caller: str, node: Any | None) -> str | None:
    if node is None:
        return None
    raw = _node_text(node, php_file.source).strip()
    if raw == "$this":
        return _owner_type_q(context, caller)
    if node.type == "variable_name":
        return _lookup_variable_type(context, php_file, caller, _variable_name(raw), node)
    if node.type == "member_access_expression":
        object_node = node.child_by_field_name("object")
        name_node = node.child_by_field_name("name")
        owner_q = _receiver_type_reference(context, php_file, caller, object_node)
        field = _node_text(name_node, php_file.source).strip() if name_node is not None else ""
        reference = context.property_types.get((owner_q, field)) if owner_q else None
        return _resolved_type_q(context, php_file, reference, node) if reference else None
    if node.type == "object_creation_expression":
        class_node = _object_class_node(node)
        if class_node is not None:
            reference = _normalize_name(_node_text(class_node, php_file.source))
            candidates = _symbols_for_reference(context, php_file, reference, kind="class", node=class_node)
            if len(candidates) == 1:
                return candidates[0].qualified_name
    if node.type == "function_call_expression":
        function_node = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        expression = _node_text(function_node, php_file.source).strip() if function_node is not None else ""
        candidates = _callable_candidates(
            context,
            php_file,
            expression,
            _argument_arity(arguments),
            function_node if function_node is not None else node,
            kind="function",
        )
        if len(candidates) == 1:
            return _resolved_type_q(context, php_file, context.return_types.get(candidates[0].node_id, ""), node)
    if raw in {"self", "static", "parent"}:
        candidates = _special_type_reference(context, php_file, raw, node)
        return candidates[0].qualified_name if len(candidates) == 1 else None
    return None


def _scoped_owner_q(context: PhpAnalysisContext, php_file: PhpFile, caller: str, scope: str, node: Any) -> str | None:
    if scope in {"self", "static"}:
        return _owner_type_q(context, caller)
    if scope == "parent":
        owner_q = _owner_type_q(context, caller)
        return context.parent_types.get(owner_q) if owner_q else None
    candidates = _symbols_for_reference(context, php_file, scope, kind="class", node=node)
    return candidates[0].qualified_name if len(candidates) == 1 else None


def _lookup_variable_type(
    context: PhpAnalysisContext,
    php_file: PhpFile,
    caller: str,
    name: str,
    node: Any,
) -> str | None:
    reference = context.variable_types.get((caller, name))
    if reference:
        return _resolved_type_q(context, php_file, reference, node)
    owner_q = _owner_type_q(context, caller)
    if owner_q:
        reference = context.property_types.get((owner_q, name))
        if reference:
            return _resolved_type_q(context, php_file, reference, node)
    return None


def _resolved_type_q(context: PhpAnalysisContext, php_file: PhpFile, reference: str, node: Any) -> str | None:
    if not reference:
        return None
    candidates = _symbols_for_reference(context, php_file, reference, kind="class", node=node)
    if len(candidates) == 1:
        return candidates[0].qualified_name
    return reference


def _owner_type_q(context: PhpAnalysisContext, node_id: str) -> str | None:
    symbol = context.definitions.get(node_id)
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS:
        return symbol.qualified_name
    return symbol.owner_q


def _owner_chain(context: PhpAnalysisContext, owner_q: str | None) -> set[str]:
    if not owner_q:
        return set()
    result: set[str] = set()
    pending = [owner_q]
    while pending:
        current = pending.pop()
        if not current or current in result:
            continue
        result.add(current)
        pending.extend(context.used_types.get(current, []))
        parent = context.parent_types.get(current)
        if parent:
            pending.append(parent)
    return result


def _return_info(node: Any, *, include_arrow: bool = False) -> tuple[str, list[dict[str, Any]]]:
    if include_arrow and node.type == "arrow_function":
        return "returns_value", []
    sites: list[dict[str, Any]] = []
    has_value = False
    has_none = False

    def visit(current: Any) -> None:
        nonlocal has_value, has_none
        if current is not node and current.type in _CALLABLE_DECLARATION_TYPES:
            return
        if current.type == "return_statement":
            value = bool(current.named_children)
            has_value = has_value or value
            has_none = has_none or not value
            sites.append({"span": _span_for_tree(current), "value_kind": "value" if value else "none"})
            return
        for child in current.named_children:
            visit(child)

    visit(node)
    if not sites:
        return "no_explicit_return", []
    if has_value and has_none:
        return "mixed", sites
    return ("returns_value" if has_value else "returns_none"), sites


def _namespace_for_node(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> str:
    current = node
    while current is not None:
        if current.type == "namespace_definition":
            value = context.namespace_by_node.get((php_file.relative_path, current.id))
            if value is not None:
                return value
        current = current.parent
    for namespace_node, namespace_name in reversed(php_file.namespace_nodes):
        if namespace_node.child_by_field_name("body") is None and node.start_byte >= namespace_node.end_byte:
            return namespace_name
    return _GLOBAL_NAMESPACE


def _scope_id_for_node(context: PhpAnalysisContext, php_file: PhpFile, node: Any, *, include_current: bool) -> str | None:
    current = node if include_current else node.parent
    while current is not None:
        node_id = context.definition_by_node.get((php_file.relative_path, current.id))
        if node_id is not None:
            return node_id
        current = current.parent
    return context.namespace_by_q.get(_namespace_for_node(context, php_file, node))


def _enclosing_definition(context: PhpAnalysisContext, php_file: PhpFile, node: Any) -> str | None:
    current = node
    while current is not None:
        node_id = context.definition_by_node.get((php_file.relative_path, current.id))
        if node_id is not None:
            return node_id
        current = current.parent
    return None


def _enclosing_type_node(node: Any | None) -> Any | None:
    current = node.parent if node is not None else None
    while current is not None:
        if current.type in _TYPE_DECLARATION_TYPES:
            return current
        current = current.parent
    return None


def _type_references(node: Any | None, source: bytes) -> list[tuple[str, Any]]:
    if node is None:
        return []
    result: list[tuple[str, Any]] = []

    def visit(current: Any) -> None:
        if current.type in {"primitive_type", "variadic_type"}:
            return
        if current.type in {"named_type", "qualified_name"}:
            reference = _normalize_name(_node_text(current, source))
            if reference and reference.lower() not in _BUILTIN_TYPES:
                result.append((reference, current))
            return
        for child in current.named_children:
            visit(child)

    visit(node)
    seen: set[tuple[str, int]] = set()
    deduped: list[tuple[str, Any]] = []
    for reference, reference_node in result:
        key = (reference, int(reference_node.start_byte))
        if key not in seen:
            seen.add(key)
            deduped.append((reference, reference_node))
    return deduped


def _first_type_reference(node: Any | None, source: bytes) -> str:
    references = _type_references(node, source)
    return references[0][0] if references else ""


def _reference_children(node: Any) -> list[Any]:
    return [child for child in node.named_children if child.type in {"name", "qualified_name", "namespace_name", "scoped_identifier"}]


def _object_class_node(node: Any) -> Any | None:
    for child in node.named_children:
        if child.type in {"arguments", "anonymous_class"}:
            continue
        if child.type in {"name", "qualified_name", "namespace_name", "scoped_identifier"}:
            return child
    return None


def _expression_type_reference(node: Any, source: bytes) -> str:
    if node.type == "object_creation_expression":
        class_node = _object_class_node(node)
        return _normalize_name(_node_text(class_node, source)) if class_node is not None else ""
    if node.type == "parenthesized_expression" and node.named_children:
        return _expression_type_reference(node.named_children[-1], source)
    return ""


def _variable_names(node: Any, source: bytes) -> list[str]:
    names: list[str] = []
    for current in _walk_tree(node):
        if current.type == "variable_name":
            name = _variable_name(_node_text(current, source))
            if name and name not in names:
                names.append(name)
    return names


def _variable_name(raw: str) -> str:
    return raw.strip().lstrip("$")


def _parameter_arity(node: Any | None) -> tuple[int, int]:
    if node is None:
        return 0, 0
    parameters = [child for child in node.named_children if child.type in _PARAMETER_TYPES]
    optional = 0
    for parameter in parameters:
        if parameter.child_by_field_name("default_value") is not None:
            optional += 1
    return len(parameters), len(parameters) - optional


def _argument_arity(node: Any | None) -> int:
    return len(node.named_children) if node is not None else 0


def _filter_arity(candidates: list[PhpSymbol], arity: int) -> list[PhpSymbol]:
    return [
        symbol
        for symbol in candidates
        if symbol.arity is None or (symbol.min_arity or 0) <= arity <= symbol.arity
    ]


def _parse_namespace_use(raw: str) -> list[tuple[str, str, str]]:
    text = raw.strip().removesuffix(";").strip()
    match = re.match(r"^use\s+(?:(function|const)\s+)?(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return []
    kind = match.group(1).lower() if match.group(1) else "class"
    body = match.group(2).strip()
    entries: list[tuple[str, str, str]] = []
    for part in _split_top_level(body, ","):
        part = part.strip()
        if not part:
            continue
        group_match = re.match(r"^(.+?)\\\{(.+)\}$", part, flags=re.DOTALL)
        if group_match:
            prefix = _normalize_name(group_match.group(1).rstrip("\\"))
            for member in _split_top_level(group_match.group(2), ","):
                path, alias = _path_and_alias(member, prefix)
                if path:
                    entries.append((kind, path, alias))
            continue
        path, alias = _path_and_alias(part, "")
        if path:
            entries.append((kind, path, alias))
    return entries


def _path_and_alias(value: str, prefix: str) -> tuple[str, str]:
    text = value.strip()
    alias_match = re.match(r"^(.+?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$", text, flags=re.IGNORECASE | re.DOTALL)
    if alias_match:
        path = _normalize_name(alias_match.group(1))
        alias = alias_match.group(2)
    else:
        path = _normalize_name(text)
        alias = path.rsplit("\\", 1)[-1] if path else ""
    if prefix:
        path = f"{prefix}\\{path}" if path else prefix
    return path, alias


def _split_top_level(value: str, delimiter: str) -> list[str]:
    result: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char in "{([":
            depth += 1
        elif char in "})]" and depth:
            depth -= 1
        elif char == delimiter and depth == 0:
            result.append(value[start:index])
            start = index + 1
    result.append(value[start:])
    return result


def _normalize_name(value: str) -> str:
    raw = value.strip().strip("`").strip()
    raw = re.sub(r"\s+", "", raw)
    raw = raw.removeprefix("\\")
    return raw


def _join_namespace(namespace_name: str, name: str) -> str:
    if namespace_name == _GLOBAL_NAMESPACE or not namespace_name:
        return name
    return f"{namespace_name}\\{name}" if name else namespace_name


def _looks_local_reference(context: PhpAnalysisContext, php_file: PhpFile, reference: str, node: Any) -> bool:
    current = _namespace_for_node(context, php_file, node)
    if current != _GLOBAL_NAMESPACE and reference.startswith(f"{current}\\"):
        return True
    return any(
        symbol.qualified_name.startswith(f"{reference}\\") or symbol.qualified_name == reference
        for symbol in context.definitions.values()
        if symbol.kind in _TYPE_KINDS | {"function"}
    )


def _php_type_form(node_type: str) -> str:
    return {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "trait",
        "enum_declaration": "enum",
        "anonymous_class": "anonymous_class",
    }.get(node_type, node_type)


def _owner_qualified_name(symbol: PhpSymbol | None) -> str | None:
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS:
        return symbol.qualified_name
    return symbol.owner_q


def _unique_symbols(symbols: Iterable[PhpSymbol]) -> list[PhpSymbol]:
    result: list[PhpSymbol] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol.node_id not in seen:
            seen.add(symbol.node_id)
            result.append(symbol)
    return result


def _node_text(node: Any | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _signature_for_node(node: Any, source: bytes, limit: int = 240) -> str:
    for line in _node_text(node, source).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return _node_text(node, source).strip()[:limit]


def _visibility_for_node(node: Any, source: bytes) -> str:
    prefix = _node_text(node, source).split("{", 1)[0]
    if re.search(r"\b(?:private|protected)\b", prefix):
        return "private"
    return "public"


def _walk_tree(node: Any) -> Iterable[Any]:
    yield node
    for child in node.children:
        yield from _walk_tree(child)


def _span_for_tree(node: Any | None) -> dict[str, int] | None:
    if node is None or not hasattr(node, "start_point"):
        return None
    return {
        "start_line": int(node.start_point[0]) + 1,
        "start_col": int(node.start_point[1]),
        "end_line": int(node.end_point[0]) + 1,
        "end_col": int(node.end_point[1]),
    }


def _unique_id(existing: dict[str, Any], base: str, salt: int) -> str:
    if base not in existing:
        return base
    return f"{base}~{salt}"


def _add_relation(
    context: PhpAnalysisContext,
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
        [source_id, target_id, relation_type, source_span, detail],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    edge_id = f"php-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
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


def _diagnose_unresolved(
    context: PhpAnalysisContext,
    php_file: PhpFile,
    node: Any,
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    context.diagnostic(
        code,
        "warning",
        message,
        php_file=php_file,
        tree_node=node,
        node_id=node_id,
        details=details,
    )
