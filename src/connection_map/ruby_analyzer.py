"""Tree-sitter based static analyzer for Ruby source files."""

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

ANALYZER_NAME = "connection-map-ruby-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"ruby"}

_GLOBAL_NAMESPACE = "<global>"
_TYPE_KINDS = {"class", "namespace"}
_CALLABLE_KINDS = {"function", "method", "lambda"}
_TYPE_DECLARATION_TYPES = {"class", "module"}
_CALLABLE_DECLARATION_TYPES = {"method", "singleton_method", "lambda", "block", "do_block"}
_STRUCTURAL_CALLS = {"require", "require_relative", "load", "autoload", "include", "prepend", "extend"}
_REQUIRE_CALLS = {"require", "require_relative", "load", "autoload"}
_MIXIN_CALLS = {"include", "prepend", "extend"}
_RUBY_SUFFIXES = {".rb", ".rake", ".gemspec", ".ru"}


class RubyAnalyzerDependencyError(ValueError):
    """Raised when the optional Ruby parser dependency is unavailable."""


@dataclass(slots=True)
class RubyFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str
    scope_nodes: list[tuple[Any, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RubySymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str | None
    declaration_kind: str
    arity: int | None = None
    min_arity: int = 0
    owner_q: str | None = None
    singleton: bool = False


@dataclass(slots=True)
class RubyAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    files: list[RubyFile] = field(default_factory=list)
    files_by_path: dict[str, RubyFile] = field(default_factory=dict)
    namespace_by_q: dict[str, str] = field(default_factory=dict)
    type_by_q: dict[str, str] = field(default_factory=dict)
    type_q_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    definitions: dict[str, RubySymbol] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    definition_nodes: dict[str, Any] = field(default_factory=dict)
    symbols_by_name: dict[str, list[RubySymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[RubySymbol]] = field(default_factory=dict)
    parent_types: dict[str, str] = field(default_factory=dict)
    mixins: dict[str, list[str]] = field(default_factory=dict)
    variable_types: dict[tuple[str, str], str] = field(default_factory=dict)
    property_types: dict[tuple[str, str], str] = field(default_factory=dict)
    constant_types: dict[tuple[str, str], str] = field(default_factory=dict)
    return_types: dict[str, str] = field(default_factory=dict)
    reopen_counts: dict[str, int] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def ensure_global_namespace(self, ruby_file: RubyFile) -> str:
        return self.add_namespace(
            _GLOBAL_NAMESPACE,
            ruby_file=ruby_file,
            tree_node=None,
            declaration_form="inferred_global",
        )

    def add_namespace(
        self,
        qualified_name: str,
        *,
        ruby_file: RubyFile,
        tree_node: Any | None,
        declaration_form: str,
    ) -> str:
        existing = self.namespace_by_q.get(qualified_name)
        if existing is not None:
            if tree_node is not None:
                self.reopen_counts[qualified_name] = self.reopen_counts.get(qualified_name, 0) + 1
                self.type_q_by_node[(ruby_file.relative_path, tree_node.id)] = qualified_name
                self.definition_by_node[(ruby_file.relative_path, tree_node.id)] = existing
            return existing

        parent_q = _parent_constant_name(qualified_name)
        parent_id = None
        if parent_q is not None:
            parent_id = self.type_by_q.get(parent_q) or self.namespace_by_q.get(parent_q)
            if parent_id is None:
                parent_id = self.add_namespace(
                    parent_q,
                    ruby_file=ruby_file,
                    tree_node=None,
                    declaration_form="inferred_parent",
                )
        node_id = f"ruby:{qualified_name}:namespace"
        display_name = "::" if qualified_name == _GLOBAL_NAMESPACE else qualified_name.rsplit("::", 1)[-1]
        self.builder.add_node(
            {
                "id": node_id,
                "kind": "namespace",
                "qualified_name": qualified_name,
                "display_name": display_name,
                "file": ruby_file.relative_path,
                "span": _span_for_tree(tree_node) if tree_node is not None else _span_for_tree(ruby_file.tree.root_node),
                "parent_id": parent_id,
                "visibility": "public",
                "signature": f"module {display_name}",
                "extensions": {
                    "language": "ruby",
                    "grammar": "ruby",
                    "ruby_type": "module",
                    "mix_in": True,
                    "declaration_form": declaration_form,
                },
            }
        )
        self.namespace_by_q[qualified_name] = node_id
        symbol = RubySymbol(
            node_id=node_id,
            name=display_name,
            qualified_name=qualified_name,
            kind="namespace",
            file_path=ruby_file.relative_path,
            declaration_kind=declaration_form,
            owner_q=parent_q,
        )
        self._index_symbol(symbol)
        if tree_node is not None:
            self.type_q_by_node[(ruby_file.relative_path, tree_node.id)] = qualified_name
            self.definition_by_node[(ruby_file.relative_path, tree_node.id)] = node_id
        return node_id

    def add_class(self, ruby_file: RubyFile, tree_node: Any, qualified_name: str) -> str:
        existing = self.type_by_q.get(qualified_name)
        if existing is not None:
            self.reopen_counts[qualified_name] = self.reopen_counts.get(qualified_name, 0) + 1
            self.type_q_by_node[(ruby_file.relative_path, tree_node.id)] = qualified_name
            self.definition_by_node[(ruby_file.relative_path, tree_node.id)] = existing
            return existing

        parent_q = _parent_constant_name(qualified_name)
        parent_id = None
        if parent_q is not None:
            parent_id = self.type_by_q.get(parent_q) or self.namespace_by_q.get(parent_q)
            if parent_id is None:
                parent_id = self.add_namespace(
                    parent_q,
                    ruby_file=ruby_file,
                    tree_node=None,
                    declaration_form="inferred_parent",
                )
        name = qualified_name.rsplit("::", 1)[-1]
        node_id = f"ruby:{qualified_name}:class"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": "class",
                "qualified_name": qualified_name,
                "display_name": name,
                "file": ruby_file.relative_path,
                "span": _span_for_tree(tree_node),
                "parent_id": parent_id,
                "visibility": _visibility_for_node(tree_node, ruby_file.source),
                "signature": _signature_for_node(tree_node, ruby_file.source),
                "extensions": {
                    "language": "ruby",
                    "grammar": "ruby",
                    "ruby_type": "class",
                    "reopened": False,
                },
            }
        )
        self.type_by_q[qualified_name] = node_id
        symbol = RubySymbol(
            node_id=node_id,
            name=name,
            qualified_name=qualified_name,
            kind="class",
            file_path=ruby_file.relative_path,
            declaration_kind="class",
            owner_q=parent_q,
        )
        self._index_symbol(symbol)
        self.type_q_by_node[(ruby_file.relative_path, tree_node.id)] = qualified_name
        self.definition_by_node[(ruby_file.relative_path, tree_node.id)] = node_id
        _add_relation(
            self,
            parent_id or self.namespace_by_q[_GLOBAL_NAMESPACE],
            node_id,
            "contains",
            resolution_status="resolved",
            confidence=1.0,
            source_span=_span_for_tree(tree_node),
            detail={"kind": "lexical_definition", "declaration_kind": "class"},
        )
        return node_id

    def add_module_file(self, ruby_file: RubyFile) -> None:
        self.builder.add_node(
            {
                "id": ruby_file.module_id,
                "kind": "module",
                "qualified_name": ruby_file.relative_path,
                "display_name": ruby_file.relative_path,
                "file": ruby_file.relative_path,
                "span": _span_for_tree(ruby_file.tree.root_node),
                "parent_id": None,
                "visibility": "public",
                "signature": f"file {ruby_file.relative_path}",
                "extensions": {
                    "language": "ruby",
                    "grammar": "ruby",
                    "scope_names": sorted({q for _, q in ruby_file.scope_nodes if q != _GLOBAL_NAMESPACE}),
                },
            }
        )
        scope_names = sorted({q for _, q in ruby_file.scope_nodes}) or [_GLOBAL_NAMESPACE]
        for scope_name in scope_names:
            target = self.type_by_q.get(scope_name) or self.namespace_by_q.get(scope_name)
            if target is None:
                continue
            _add_relation(
                self,
                ruby_file.module_id,
                target,
                "contains",
                resolution_status="resolved",
                confidence=1.0,
                source_span=_span_for_tree(ruby_file.tree.root_node),
                detail={"kind": "file_scope"},
            )

    def add_callable(
        self,
        ruby_file: RubyFile,
        tree_node: Any,
        *,
        name: str,
        qualified_name: str,
        kind: str,
        parent_id: str,
        owner_q: str | None,
        declaration_kind: str,
        arity: int | None,
        min_arity: int,
        singleton: bool = False,
        extensions: dict[str, Any] | None = None,
    ) -> str:
        base_id = f"ruby:{ruby_file.relative_path}:{qualified_name}:{kind}"
        node_id = _unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": "ruby",
            "grammar": "ruby",
            "declaration_kind": declaration_kind,
            "parameter_count": arity,
            "minimum_parameter_count": min_arity,
        }
        if singleton:
            node_extensions["singleton"] = True
        if extensions:
            node_extensions.update(extensions)
        return_behavior, return_sites, return_extensions = _return_info(tree_node)
        node_extensions.update(return_extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": ruby_file.relative_path,
            "span": _span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": _visibility_for_node(tree_node, ruby_file.source),
            "signature": _signature_for_node(tree_node, ruby_file.source),
            "extensions": node_extensions,
            "return_behavior": return_behavior,
            "return_sites": return_sites,
            "execution_kind": "sync",
        }
        self.builder.add_node(node)
        symbol = RubySymbol(
            node_id=node_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=ruby_file.relative_path,
            declaration_kind=declaration_kind,
            arity=arity,
            min_arity=min_arity,
            owner_q=owner_q,
            singleton=singleton,
        )
        self._index_symbol(symbol)
        self.definition_by_node[(ruby_file.relative_path, tree_node.id)] = node_id
        self.definition_nodes[node_id] = tree_node
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

    def _index_symbol(self, symbol: RubySymbol) -> None:
        self.definitions[symbol.node_id] = symbol
        self.symbols_by_name.setdefault(symbol.name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(symbol.qualified_name, []).append(symbol)

    def external_node(
        self,
        label: str,
        *,
        unknown: bool = False,
        ruby_file: RubyFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (kind, label)
        existing = self._external_node_ids.get(key)
        if existing is not None:
            return existing
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"ruby:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": ruby_file.relative_path if unknown and ruby_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": "ruby",
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
        ruby_file: RubyFile | None = None,
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
                "file": ruby_file.relative_path if ruby_file else file_path,
                "span": span or (_span_for_tree(tree_node) if tree_node else None),
                "node_id": node_id,
                "details": details or {},
            }
        )


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Analyze Ruby source without importing, building, or executing target code."""

    active_config = config or AnalysisConfig(language="ruby")
    active_config.validate()
    if active_config.language != "ruby":
        raise ValueError("Ruby analyzer requires language = 'ruby'")
    if set(active_config.active_languages()) != SUPPORTED_LANGUAGES:
        raise ValueError("Ruby analyzer supports only ruby")

    root = root.resolve()
    builder = GraphBuilder()
    context = RubyAnalysisContext(root, active_config, builder)
    files, skipped = discover_source_files(root, active_config, languages={"ruby"})
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped Ruby source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            ruby_file = _parse_file(path, root)
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
        context.files.append(ruby_file)
        context.files_by_path[relative_path] = ruby_file

    _index_scopes(context)
    for ruby_file in context.files:
        context.add_module_file(ruby_file)
        if ruby_file.tree.root_node.has_error:
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {ruby_file.relative_path}",
                ruby_file=ruby_file,
                tree_node=ruby_file.tree.root_node,
                details={"grammar": "ruby"},
            )

    for ruby_file in context.files:
        _collect_callable_definitions(context, ruby_file)
    for ruby_file in context.files:
        _collect_scope_relations(context, ruby_file)
    for ruby_file in context.files:
        _collect_assignment_relations(context, ruby_file)
    _collect_return_types(context)
    for ruby_file in context.files:
        _collect_call_relations(context, ruby_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "ruby",
        "languages": ["ruby"],
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
            "grammars": ["ruby"],
        },
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": active_config.to_dict(),
        "extensions": {
            "module_reopen_count": sum(context.reopen_counts.values()),
            "dynamic_resolution": "unresolved",
            "bundler_resolution": "not_attempted",
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
        raise RubyAnalyzerDependencyError(
            "Ruby解析には任意依存が必要です。'uv sync --extra ruby' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser("ruby")
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise RubyAnalyzerDependencyError(f"Tree-sitter grammar 'ruby'を読み込めません: {exc}") from exc


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


def _parse_file(path: Path, root: Path) -> RubyFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    return RubyFile(
        path=path,
        relative_path=relative_path,
        source=source,
        tree=_parser_for().parse(source),
        module_id=f"ruby:{relative_path}:module",
    )


def _index_scopes(context: RubyAnalysisContext) -> None:
    for ruby_file in context.files:
        context.ensure_global_namespace(ruby_file)
        for node in _walk_tree(ruby_file.tree.root_node):
            if not node.is_named or node.type not in _TYPE_DECLARATION_TYPES:
                continue
            qualified_name = _declaration_qualified_name(context, ruby_file, node)
            if not qualified_name:
                context.diagnostic(
                    "unsupported_construct",
                    "warning",
                    "Rubyのclass/module名を静的に取得できません",
                    ruby_file=ruby_file,
                    tree_node=node,
                )
                continue
            ruby_file.scope_nodes.append((node, qualified_name))
            if node.type == "module":
                context.add_namespace(
                    qualified_name,
                    ruby_file=ruby_file,
                    tree_node=node,
                    declaration_form="module",
                )
            else:
                context.add_class(ruby_file, node, qualified_name)


def _collect_callable_definitions(context: RubyAnalysisContext, ruby_file: RubyFile) -> None:
    for node in _walk_tree(ruby_file.tree.root_node):
        if node.type not in _CALLABLE_DECLARATION_TYPES:
            continue
        if node.type in {"method", "singleton_method"}:
            _collect_method_definition(context, ruby_file, node)
        elif node.type == "lambda":
            _collect_lambda_definition(context, ruby_file, node, declaration_kind="lambda")
        else:
            _collect_lambda_definition(context, ruby_file, node, declaration_kind=node.type)


def _collect_method_definition(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> None:
    lexical_owner = _enclosing_type_q(context, ruby_file, node)
    object_node = node.child_by_field_name("object") if node.type == "singleton_method" else None
    singleton = node.type == "singleton_method"
    owner_q = lexical_owner
    dynamic_receiver = False
    if object_node is not None and _node_text(object_node, ruby_file.source).strip() != "self":
        resolved = _resolve_type_q(context, ruby_file, _node_text(object_node, ruby_file.source), object_node)
        if resolved:
            owner_q = resolved
        else:
            dynamic_receiver = True
    name_node = node.child_by_field_name("name")
    name = _node_text(name_node, ruby_file.source).strip() if name_node is not None else "<method>"
    name = name or "<method>"
    parameters = node.child_by_field_name("parameters")
    arity, min_arity = _parameter_arity(parameters)
    if owner_q:
        separator = "." if singleton else "#"
        qualified_name = f"{owner_q}{separator}{name}{_arity_suffix(arity)}"
        kind = "method"
        parent_id = context.type_by_q.get(owner_q) or context.namespace_by_q.get(owner_q)
    else:
        qualified_name = f"{name}{_arity_suffix(arity)}"
        kind = "function"
        parent_id = context.namespace_by_q[_GLOBAL_NAMESPACE]
    if parent_id is None:
        parent_id = context.namespace_by_q[_GLOBAL_NAMESPACE]
    extensions: dict[str, Any] = {}
    if dynamic_receiver:
        extensions["dynamic_receiver"] = True
    context.add_callable(
        ruby_file,
        node,
        name=name,
        qualified_name=qualified_name,
        kind=kind,
        parent_id=parent_id,
        owner_q=owner_q,
        declaration_kind=node.type,
        arity=arity,
        min_arity=min_arity,
        singleton=singleton,
        extensions=extensions,
    )


def _collect_lambda_definition(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    node: Any,
    *,
    declaration_kind: str,
) -> None:
    parent_id = _parent_definition_id(context, ruby_file, node)
    parent_symbol = context.definitions.get(parent_id)
    parameters = node.child_by_field_name("parameters")
    if parameters is None and node.type in {"block", "do_block"}:
        parameters = node.child_by_field_name("parameters")
    arity, min_arity = _parameter_arity(parameters, block=node.type in {"block", "do_block"})
    line = node.start_point[0] + 1
    col = node.start_point[1]
    parent_name = parent_symbol.qualified_name if parent_symbol else _GLOBAL_NAMESPACE
    name = "<lambda>" if node.type == "lambda" else "<block>"
    qualified_name = f"{parent_name}::{name}@{line}:{col}{_arity_suffix(arity)}"
    owner_q = parent_symbol.owner_q if parent_symbol and parent_symbol.kind in _CALLABLE_KINDS else (
        parent_symbol.qualified_name if parent_symbol and parent_symbol.kind in _TYPE_KINDS else None
    )
    context.add_callable(
        ruby_file,
        node,
        name=name,
        qualified_name=qualified_name,
        kind="lambda",
        parent_id=parent_id,
        owner_q=owner_q,
        declaration_kind=declaration_kind,
        arity=arity,
        min_arity=min_arity,
        extensions={"block": node.type in {"block", "do_block"}},
    )


def _collect_scope_relations(context: RubyAnalysisContext, ruby_file: RubyFile) -> None:
    for node in _walk_tree(ruby_file.tree.root_node):
        if not node.is_named:
            continue
        if node.type == "class":
            _collect_inheritance(context, ruby_file, node)
        elif node.type == "singleton_class":
            context.diagnostic(
                "unsupported_construct",
                "info",
                "Rubyのsingleton classは完全なowner解決を行いません",
                ruby_file=ruby_file,
                tree_node=node,
                details={"construct": "singleton_class"},
            )
        elif node.type == "call":
            method_name = _call_method_name(node, ruby_file.source)
            if method_name in _REQUIRE_CALLS:
                _collect_require(context, ruby_file, node, method_name)
            elif method_name in _MIXIN_CALLS:
                _collect_mixin(context, ruby_file, node, method_name)


def _collect_inheritance(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> None:
    owner_id = context.definition_by_node.get((ruby_file.relative_path, node.id))
    if owner_id is None:
        return
    superclass = node.child_by_field_name("superclass")
    if superclass is None:
        return
    reference_node = superclass
    if superclass.type == "superclass" and superclass.named_children:
        reference_node = superclass.named_children[-1]
    reference = _node_text(reference_node, ruby_file.source).strip()
    if not reference:
        return
    target, status, confidence, target_q = _resolve_type_edge(context, ruby_file, reference, reference_node)
    _add_relation(
        context,
        owner_id,
        target,
        "inherits",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(reference_node),
        detail={"reference": reference, "role": "extends"},
    )
    if target_q and status == "resolved":
        owner_symbol = context.definitions.get(owner_id)
        if owner_symbol:
            context.parent_types[owner_symbol.qualified_name] = target_q


def _collect_mixin(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any, method_name: str) -> None:
    receiver = node.child_by_field_name("receiver")
    if receiver is not None and _node_text(receiver, ruby_file.source).strip() != "self":
        context.diagnostic(
            "unresolved_type",
            "warning",
            f"Rubyの{method_name} receiverを静的に解決できません",
            ruby_file=ruby_file,
            tree_node=node,
            details={"role": method_name},
        )
        return
    owner_q = _enclosing_type_q(context, ruby_file, node)
    owner_id = context.type_by_q.get(owner_q or "") or context.namespace_by_q.get(owner_q or "")
    if owner_id is None:
        return
    arguments = _call_arguments(node)
    if not arguments:
        context.diagnostic(
            "unresolved_type",
            "warning",
            f"Rubyの{method_name}対象がありません",
            ruby_file=ruby_file,
            tree_node=node,
        )
        return
    for argument in arguments:
        reference = _node_text(argument, ruby_file.source).strip()
        target, status, confidence, target_q = _resolve_type_edge(context, ruby_file, reference, argument)
        _add_relation(
            context,
            owner_id,
            target,
            "uses",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(argument),
            detail={"reference": reference, "role": method_name},
        )
        if target_q and status == "resolved":
            context.mixins.setdefault(owner_q or "", []).append(target_q)


def _collect_require(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any, method_name: str) -> None:
    arguments = _call_arguments(node)
    path_node = arguments[1] if method_name == "autoload" and len(arguments) > 1 else (arguments[0] if arguments else None)
    path = _static_string(path_node, ruby_file.source) if path_node is not None else None
    source_id = _caller_or_module(context, ruby_file, node)
    if not path:
        target = context.external_node(
            f"import:<dynamic:{method_name}>",
            unknown=True,
            ruby_file=ruby_file,
            span=_span_for_tree(node),
        )
        _diagnose_unresolved(
            context,
            ruby_file,
            node,
            "unresolved_import",
            f"Rubyの{method_name}対象を静的に取得できません",
        )
        _add_relation(
            context,
            source_id,
            target,
            "imports",
            resolution_status="unresolved",
            confidence=0.2,
            source_span=_span_for_tree(node),
            detail={"require_kind": method_name, "dynamic": True},
        )
        return
    target_file = _find_required_file(context, ruby_file, path, relative=method_name == "require_relative")
    if target_file is not None:
        target = target_file.module_id
        status, confidence = "resolved", 1.0
    else:
        target = context.external_node(f"require:{path}")
        status, confidence = "external", 0.75
    _add_relation(
        context,
        source_id,
        target,
        "imports",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"require_kind": method_name, "path": path},
    )


def _collect_assignment_relations(context: RubyAnalysisContext, ruby_file: RubyFile) -> None:
    for node in _walk_tree(ruby_file.tree.root_node):
        if node.type != "assignment":
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            continue
        reference = _infer_expression_type(context, ruby_file, right, _caller_or_module(context, ruby_file, node))
        if not reference:
            continue
        caller_id = _caller_or_module(context, ruby_file, node)
        name = _variable_name(_node_text(left, ruby_file.source))
        if not name:
            continue
        if left.type == "instance_variable":
            owner_q = _owner_q_for_definition(context, caller_id)
            if owner_q:
                context.property_types[(owner_q, name)] = reference
        elif left.type == "constant":
            scope_q = _owner_q_for_definition(context, caller_id) or _GLOBAL_NAMESPACE
            context.constant_types[(scope_q, name)] = reference
        else:
            context.variable_types[(caller_id, name)] = reference


def _collect_return_types(context: RubyAnalysisContext) -> None:
    for node_id, node in context.definition_nodes.items():
        symbol = context.definitions.get(node_id)
        if symbol is None or symbol.kind not in _CALLABLE_KINDS:
            continue
        ruby_file = context.files_by_path.get(symbol.file_path or "")
        if ruby_file is None:
            continue
        reference = _infer_callable_return_type(context, ruby_file, node, node_id)
        if reference:
            context.return_types[node_id] = reference


def _collect_call_relations(context: RubyAnalysisContext, ruby_file: RubyFile) -> None:
    for node in _walk_tree(ruby_file.tree.root_node):
        if node.type == "call":
            method_name = _call_method_name(node, ruby_file.source)
            if method_name in _STRUCTURAL_CALLS:
                continue
            _collect_call(context, ruby_file, node)
        elif node.is_named and node.type in {"super", "zsuper"}:
            _collect_super(context, ruby_file, node)
        elif node.is_named and node.type == "yield":
            _collect_yield(context, ruby_file, node)


def _collect_call(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> None:
    method_name = _call_method_name(node, ruby_file.source)
    if not method_name:
        return
    caller_id = _caller_or_module(context, ruby_file, node)
    arguments = _call_arguments(node)
    arity = _argument_arity(arguments)
    receiver = node.child_by_field_name("receiver")
    receiver_q = _receiver_type_q(context, ruby_file, caller_id, receiver)
    receiver_text = _node_text(receiver, ruby_file.source).strip() if receiver is not None else ""
    singleton = _receiver_is_singleton(receiver, receiver_text, context, caller_id)

    caller = context.definitions.get(caller_id)
    if method_name == "new" and receiver is None and caller and caller.singleton and caller.owner_q:
        target_id = context.type_by_q.get(caller.owner_q)
        if target_id is not None:
            _record_call(
                context,
                ruby_file,
                caller_id,
                target_id,
                node,
                method_name,
                arity,
                resolution_status="resolved",
                confidence=1.0,
                detail={"callee": method_name, "receiver": "self", "role": "object_creation"},
            )
            return

    if receiver_q and method_name == "new":
        target_id = context.type_by_q.get(receiver_q) or context.namespace_by_q.get(receiver_q)
        if target_id is not None:
            _record_call(
                context,
                ruby_file,
                caller_id,
                target_id,
                node,
                method_name,
                arity,
                resolution_status="resolved",
                confidence=1.0,
                detail={"callee": method_name, "receiver": receiver_text, "role": "object_creation"},
            )
            return

    candidates = _call_candidates(context, ruby_file, caller_id, method_name, arity, receiver_q, singleton)
    if len(candidates) == 1:
        _record_call(
            context,
            ruby_file,
            caller_id,
            candidates[0].node_id,
            node,
            method_name,
            arity,
            resolution_status="resolved",
            confidence=1.0,
            detail={"callee": method_name, **({"receiver": receiver_text} if receiver_text else {})},
        )
    elif len(candidates) > 1:
        _record_unresolved_call(
            context,
            ruby_file,
            caller_id,
            node,
            method_name,
            arity,
            reason="ambiguous_method",
        )
    else:
        _record_external_or_unresolved_call(
            context,
            ruby_file,
            caller_id,
            node,
            method_name,
            arity,
            receiver_text,
            receiver_q,
        )


def _collect_super(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> None:
    caller_id = _enclosing_callable_id(context, ruby_file, node)
    if caller_id is None:
        return
    caller = context.definitions.get(caller_id)
    if caller is None or not caller.owner_q:
        return
    parent_q = context.parent_types.get(caller.owner_q)
    candidates = _method_candidates(
        context,
        parent_q,
        caller.name,
        _argument_arity(node.child_by_field_name("arguments")),
        singleton=caller.singleton,
    )
    if len(candidates) == 1:
        _record_call(
            context,
            ruby_file,
            caller_id,
            candidates[0].node_id,
            node,
            "super",
            _argument_arity(node.child_by_field_name("arguments")),
            resolution_status="resolved",
            confidence=1.0,
            detail={"callee": "super", "role": "super"},
        )
    else:
        _record_unresolved_call(context, ruby_file, caller_id, node, "super", 0, reason="parent_method")


def _collect_yield(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> None:
    caller_id = _caller_or_module(context, ruby_file, node)
    target = context.external_node("call:yield", unknown=True, ruby_file=ruby_file, span=_span_for_tree(node))
    _diagnose_unresolved(
        context,
        ruby_file,
        node,
        "unresolved_call",
        "Rubyのyield先blockは静的に一意化できません",
        node_id=target,
    )
    _add_relation(
        context,
        caller_id,
        target,
        "calls",
        resolution_status="unresolved",
        confidence=0.2,
        source_span=_span_for_tree(node),
        detail={"callee": "yield", "role": "yield"},
    )


def _call_candidates(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    caller_id: str,
    method_name: str,
    arity: int | None,
    receiver_q: str | None,
    singleton: bool,
) -> list[RubySymbol]:
    caller = context.definitions.get(caller_id)
    owner_q = _owner_q_for_definition(context, caller_id) or _enclosing_type_q(context, ruby_file, context.definition_nodes.get(caller_id))
    if receiver_q:
        candidates = _method_candidates(context, receiver_q, method_name, arity, singleton=singleton)
        return candidates
    candidates = _method_candidates(context, owner_q, method_name, arity, singleton=singleton)
    if candidates:
        return candidates
    if caller is None or caller.kind == "function" or owner_q == _GLOBAL_NAMESPACE:
        return _filter_arity(
            [symbol for symbol in context.symbols_by_name.get(method_name, []) if symbol.kind == "function"],
            arity,
        )
    return []


def _method_candidates(
    context: RubyAnalysisContext,
    owner_q: str | None,
    name: str,
    arity: int | None,
    *,
    singleton: bool,
) -> list[RubySymbol]:
    if not owner_q:
        return []
    owners = _owner_chain(context, owner_q)
    for owner in owners:
        candidates = [
            symbol
            for symbol in context.symbols_by_name.get(name, [])
            if symbol.kind == "method" and symbol.owner_q == owner and symbol.singleton == singleton
        ]
        filtered = _filter_arity(candidates, arity)
        if filtered:
            return filtered
    return []


def _record_external_or_unresolved_call(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    caller_id: str,
    node: Any,
    method_name: str,
    arity: int | None,
    receiver_text: str,
    receiver_q: str | None,
) -> None:
    label = f"call:{receiver_text + '.' if receiver_text else ''}{method_name}{_arity_suffix(arity)}"
    local_receiver = receiver_q is not None and receiver_q in context.type_by_q
    target = context.external_node(label, unknown=local_receiver, ruby_file=ruby_file, span=_span_for_tree(node) if local_receiver else None)
    status = "unresolved" if local_receiver else "external"
    confidence = 0.2 if local_receiver else 0.65
    if local_receiver:
        _diagnose_unresolved(
            context,
            ruby_file,
            node,
            "unresolved_call",
            f"Rubyのmethodを一意に解決できません: {method_name}",
            node_id=target,
            details={"receiver": receiver_text, "owner": receiver_q},
        )
    _add_relation(
        context,
        caller_id,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"callee": method_name, **({"receiver": receiver_text} if receiver_text else {})},
    )


def _record_unresolved_call(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    caller_id: str,
    node: Any,
    method_name: str,
    arity: int | None,
    *,
    reason: str,
) -> None:
    target = context.external_node(
        f"call:{method_name}{_arity_suffix(arity)}:{reason}",
        unknown=True,
        ruby_file=ruby_file,
        span=_span_for_tree(node),
    )
    _diagnose_unresolved(
        context,
        ruby_file,
        node,
        "unresolved_call",
        f"Rubyのmethodを一意に解決できません: {method_name}",
        node_id=target,
        details={"reason": reason},
    )
    _add_relation(
        context,
        caller_id,
        target,
        "calls",
        resolution_status="unresolved",
        confidence=0.2,
        source_span=_span_for_tree(node),
        detail={"callee": method_name, "reason": reason},
    )


def _record_call(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    caller_id: str,
    target_id: str,
    node: Any,
    method_name: str,
    arity: int | None,
    *,
    resolution_status: str,
    confidence: float,
    detail: dict[str, Any],
) -> None:
    edge_detail = {"arity": arity, **detail}
    _add_relation(
        context,
        caller_id,
        target_id,
        "calls",
        resolution_status=resolution_status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail=edge_detail,
    )


def _resolve_type_edge(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    reference: str,
    node: Any,
) -> tuple[str, str, float, str | None]:
    target_q = _resolve_type_q(context, ruby_file, reference, node)
    if target_q:
        return context.type_by_q.get(target_q) or context.namespace_by_q[target_q], "resolved", 1.0, target_q
    normalized = _normalize_constant(reference)
    if not normalized:
        target = context.external_node("type:<dynamic>", unknown=True, ruby_file=ruby_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, ruby_file, node, "unresolved_type", "Rubyの型参照を解決できません", node_id=target)
        return target, "unresolved", 0.2, None
    target = context.external_node(f"type:{normalized}")
    return target, "external", 0.75, None


def _resolve_type_q(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    reference: str,
    node: Any | None,
) -> str | None:
    normalized = _normalize_constant(reference)
    if not normalized:
        return None
    if normalized.startswith("::"):
        normalized = normalized.removeprefix("::")
    candidates: list[str] = []
    if node is not None:
        scope_q = _enclosing_type_q(context, ruby_file, node)
        current = scope_q
        while current:
            candidate = f"{current}::{normalized}"
            if candidate not in candidates:
                candidates.append(candidate)
            current = _parent_constant_name(current)
    candidates.append(normalized)
    for candidate in candidates:
        if candidate in context.type_by_q or candidate in context.namespace_by_q:
            return candidate
    return None


def _receiver_type_q(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    caller_id: str,
    receiver: Any | None,
) -> str | None:
    if receiver is None:
        return None
    raw = _node_text(receiver, ruby_file.source).strip()
    if raw == "self":
        return _owner_q_for_definition(context, caller_id) or _enclosing_type_q(context, ruby_file, receiver)
    if receiver.type in {"constant", "scope_resolution"}:
        return _resolve_type_q(context, ruby_file, raw, receiver)
    if receiver.type in {"identifier", "instance_variable"}:
        return _lookup_variable_type(context, ruby_file, caller_id, _variable_name(raw))
    if receiver.type == "call":
        return _infer_expression_type(context, ruby_file, receiver, caller_id)
    if receiver.type == "parenthesized_expression" and receiver.named_children:
        return _infer_expression_type(context, ruby_file, receiver.named_children[-1], caller_id)
    return None


def _receiver_is_singleton(
    receiver: Any | None,
    receiver_text: str,
    context: RubyAnalysisContext,
    caller_id: str,
) -> bool:
    if receiver is None:
        symbol = context.definitions.get(caller_id)
        return bool(symbol and symbol.singleton)
    if receiver_text == "self":
        symbol = context.definitions.get(caller_id)
        return bool(symbol is None or symbol.singleton)
    return receiver.type in {"constant", "scope_resolution"}


def _lookup_variable_type(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    caller_id: str,
    name: str,
) -> str | None:
    reference = context.variable_types.get((caller_id, name))
    if reference:
        return _resolve_type_q(context, ruby_file, reference, context.definition_nodes.get(caller_id)) or reference
    owner_q = _owner_q_for_definition(context, caller_id)
    if owner_q:
        reference = context.property_types.get((owner_q, name))
        if reference:
            return _resolve_type_q(context, ruby_file, reference, context.definition_nodes.get(caller_id)) or reference
    return None


def _infer_expression_type(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    node: Any,
    caller_id: str,
) -> str | None:
    if node.type in {"constant", "scope_resolution"}:
        return _resolve_type_q(context, ruby_file, _node_text(node, ruby_file.source), node)
    if node.type in {"identifier", "instance_variable"}:
        return _lookup_variable_type(context, ruby_file, caller_id, _variable_name(_node_text(node, ruby_file.source)))
    if node.type == "parenthesized_expression" and node.named_children:
        return _infer_expression_type(context, ruby_file, node.named_children[-1], caller_id)
    if node.type != "call":
        return None
    method_name = _call_method_name(node, ruby_file.source)
    receiver = node.child_by_field_name("receiver")
    receiver_q = _receiver_type_q(context, ruby_file, caller_id, receiver)
    if method_name == "new" and receiver_q:
        return receiver_q
    candidates = _call_candidates(
        context,
        ruby_file,
        caller_id,
        method_name,
        _argument_arity(_call_arguments(node)),
        receiver_q,
        _receiver_is_singleton(receiver, _node_text(receiver, ruby_file.source).strip() if receiver else "", context, caller_id),
    )
    if len(candidates) == 1:
        return context.return_types.get(candidates[0].node_id)
    return None


def _infer_callable_return_type(
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
    node: Any,
    node_id: str,
) -> str | None:
    return_nodes = [current for current in _walk_callable_body(node) if current.type == "return"]
    for return_node in return_nodes:
        if return_node.named_children:
            reference = _infer_expression_type(context, ruby_file, return_node.named_children[-1], node_id)
            if reference:
                return reference
    body = node.child_by_field_name("body")
    if body is None:
        return None
    if body.type in {"body_statement", "block_body"} and body.named_children:
        last = body.named_children[-1]
    else:
        last = body
    return _infer_expression_type(context, ruby_file, last, node_id)


def _walk_callable_body(node: Any) -> Iterable[Any]:
    body = node.child_by_field_name("body")
    if body is None:
        return ()
    return _walk_tree(body)


def _find_required_file(context: RubyAnalysisContext, ruby_file: RubyFile, path: str, *, relative: bool) -> RubyFile | None:
    normalized = path.replace("\\", "/").lstrip("./")
    candidates: list[str] = []
    if relative:
        base = Path(ruby_file.relative_path).parent / normalized
        candidates.extend(_ruby_candidate_paths(base.as_posix()))
    else:
        candidates.extend(_ruby_candidate_paths(normalized))
        candidates.extend(_ruby_candidate_paths((Path("lib") / normalized).as_posix()))
        candidates.extend(_ruby_candidate_paths((Path(ruby_file.relative_path).parent / normalized).as_posix()))
    for candidate in candidates:
        ruby_file_candidate = context.files_by_path.get(Path(candidate).as_posix())
        if ruby_file_candidate is not None:
            return ruby_file_candidate
    return None


def _ruby_candidate_paths(value: str) -> list[str]:
    normalized = Path(value).as_posix()
    result = [normalized]
    if Path(normalized).suffix.lower() not in _RUBY_SUFFIXES:
        result.extend(f"{normalized}{suffix}" for suffix in (".rb", ".rake", ".gemspec", ".ru"))
    return result


def _parent_definition_id(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> str:
    current = node.parent
    while current is not None:
        definition_id = context.definition_by_node.get((ruby_file.relative_path, current.id))
        if definition_id is not None:
            return definition_id
        type_q = context.type_q_by_node.get((ruby_file.relative_path, current.id))
        if type_q is not None:
            return context.type_by_q.get(type_q) or context.namespace_by_q[type_q]
        current = current.parent
    return context.namespace_by_q[_GLOBAL_NAMESPACE]


def _caller_or_module(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> str:
    return _enclosing_callable_id(context, ruby_file, node) or ruby_file.module_id


def _enclosing_callable_id(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> str | None:
    current = node
    while current is not None:
        definition_id = context.definition_by_node.get((ruby_file.relative_path, current.id))
        if definition_id is not None:
            symbol = context.definitions.get(definition_id)
            if symbol and symbol.kind in _CALLABLE_KINDS:
                return definition_id
        current = current.parent
    return None


def _owner_q_for_definition(context: RubyAnalysisContext, definition_id: str) -> str | None:
    symbol = context.definitions.get(definition_id)
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS:
        return symbol.qualified_name
    return symbol.owner_q


def _enclosing_type_q(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any | None) -> str | None:
    current = node.parent if node is not None else None
    while current is not None:
        value = context.type_q_by_node.get((ruby_file.relative_path, current.id))
        if value is not None:
            return value
        current = current.parent
    return None


def _declaration_qualified_name(context: RubyAnalysisContext, ruby_file: RubyFile, node: Any) -> str:
    name_node = node.child_by_field_name("name")
    raw = _normalize_constant(_node_text(name_node, ruby_file.source)) if name_node is not None else ""
    if not raw:
        return ""
    parent_q = _enclosing_type_q(context, ruby_file, node)
    if raw.startswith("::"):
        return raw.removeprefix("::")
    if parent_q is None:
        return raw
    if raw == parent_q or raw.startswith(f"{parent_q}::"):
        return raw
    if raw.split("::", 1)[0] in parent_q.split("::"):
        return raw
    return f"{parent_q}::{raw}"


def _call_method_name(node: Any, source: bytes) -> str:
    name_node = node.child_by_field_name("method")
    if name_node is None:
        return ""
    return _node_text(name_node, source).strip()


def _call_arguments(node: Any) -> list[Any]:
    arguments = node.child_by_field_name("arguments")
    return list(arguments.named_children) if arguments is not None else []


def _static_string(node: Any | None, source: bytes) -> str | None:
    if node is None:
        return None
    raw = _node_text(node, source).strip()
    if node.type not in {"string", "string_content"} or "#{" in raw:
        return None
    if len(raw) >= 2 and raw[0] in {"'", '"'} and raw[-1] == raw[0]:
        value = raw[1:-1]
        return value.replace("\\\\", "\\").replace("\\'", "'").replace('\\"', '"')
    return None


def _variable_name(raw: str) -> str:
    return raw.strip().lstrip("@$").strip()


def _parameter_arity(node: Any | None, *, block: bool = False) -> tuple[int | None, int]:
    if node is None:
        return 0, 0
    if node.type == "block_parameters":
        block = False
    required = 0
    total = 0
    variadic = False
    for child in node.named_children:
        if child.type in {"block_parameter", "block_parameters"} and block:
            continue
        if child.type in {"splat_parameter", "hash_splat_parameter"}:
            variadic = True
            continue
        if child.type in {"block_parameter", "block_parameters"}:
            continue
        total += 1
        if child.type not in {"optional_parameter", "keyword_parameter"}:
            required += 1
    return (None if variadic else total), required


def _argument_arity(arguments: Any | list[Any] | None) -> int | None:
    if arguments is None:
        return 0
    children = list(arguments.named_children) if hasattr(arguments, "named_children") else list(arguments)
    variadic = any(child.type in {"splat", "hash_splat_argument"} for child in children)
    return None if variadic else len(children)


def _filter_arity(symbols: list[RubySymbol], arity: int | None) -> list[RubySymbol]:
    if arity is None:
        return [symbol for symbol in symbols if symbol.arity is None]
    return [
        symbol
        for symbol in symbols
        if symbol.arity is None or symbol.min_arity <= arity <= symbol.arity
    ]


def _return_info(node: Any) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    sites: list[dict[str, Any]] = []
    has_value = False
    has_none = False
    for current in _walk_callable_body(node):
        if current is not node and current.type in _CALLABLE_DECLARATION_TYPES:
            continue
        if current.type != "return":
            continue
        value = bool(current.named_children)
        has_value = has_value or value
        has_none = has_none or not value
        sites.append({"span": _span_for_tree(current), "value_kind": "value" if value else "none"})
    if sites:
        if has_value and has_none:
            return "mixed", sites, {}
        return ("returns_value" if has_value else "returns_none"), sites, {}
    body = node.child_by_field_name("body")
    if body is None or (body.type in {"body_statement", "block_body"} and not body.named_children):
        return "returns_none", [], {"return_inference": "empty_body"}
    return "returns_value", [], {"return_inference": "last_expression"}


def _owner_chain(context: RubyAnalysisContext, owner_q: str | None) -> list[str]:
    if not owner_q:
        return []
    result: list[str] = []
    pending = [owner_q]
    while pending:
        current = pending.pop(0)
        if not current or current in result:
            continue
        result.append(current)
        pending.extend(context.mixins.get(current, []))
        parent = context.parent_types.get(current)
        if parent:
            pending.append(parent)
    return result


def _normalize_constant(value: str) -> str:
    raw = value.strip().strip("`")
    raw = re.sub(r"\s+", "", raw)
    if raw.startswith("::"):
        return "::" + raw[2:].replace("/", "::")
    return raw.replace("/", "::")


def _parent_constant_name(value: str) -> str | None:
    if value == _GLOBAL_NAMESPACE:
        return None
    if "::" not in value:
        return _GLOBAL_NAMESPACE
    return value.rsplit("::", 1)[0]


def _arity_suffix(arity: int | None) -> str:
    return "(*)" if arity is None else f"({arity})"


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
    prefix = _node_text(node, source).split("\n", 1)[0]
    if re.search(r"\bprivate\b", prefix):
        return "private"
    if re.search(r"\bprotected\b", prefix):
        return "private"
    return "public"


def _span_for_tree(node: Any | None) -> dict[str, int] | None:
    if node is None or not hasattr(node, "start_point"):
        return None
    return {
        "start_line": int(node.start_point[0]) + 1,
        "start_col": int(node.start_point[1]),
        "end_line": int(node.end_point[0]) + 1,
        "end_col": int(node.end_point[1]),
    }


def _walk_tree(node: Any) -> Iterable[Any]:
    yield node
    for child in node.children:
        yield from _walk_tree(child)


def _walk_tree_without_nested_callables(node: Any) -> Iterable[Any]:
    yield node
    for child in node.children:
        if child is not node and child.type in _CALLABLE_DECLARATION_TYPES:
            continue
        yield from _walk_tree_without_nested_callables(child)


def _unique_id(existing: dict[str, Any], base: str, salt: int) -> str:
    if base not in existing:
        return base
    return f"{base}~{salt}"


def _add_relation(
    context: RubyAnalysisContext,
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
    edge_id = f"ruby-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
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
    context: RubyAnalysisContext,
    ruby_file: RubyFile,
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
        ruby_file=ruby_file,
        tree_node=node,
        node_id=node_id,
        details=details,
    )
