"""Tree-sitter based static analyzer for Rust source files."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .analysis_context import RustBuildProfile, load_analysis_context, rust_cfg_expression_matches
from .config import AnalysisConfig, discover_source_files, repository_id
from .contract import validate_document
from .model import GraphBuilder

ANALYZER_NAME = "connection-map-rust-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"rust"}

_TYPE_KINDS = {"type", "interface", "class"}
_CALLABLE_KINDS = {"function", "method", "lambda"}
_PRIMITIVE_TYPES = {
    "bool",
    "char",
    "f32",
    "f64",
    "i8",
    "i16",
    "i32",
    "i64",
    "i128",
    "isize",
    "str",
    "u8",
    "u16",
    "u32",
    "u64",
    "u128",
    "usize",
    "unit",
    "()",
}
_TYPE_DECLARATION_TYPES = {
    "struct_item",
    "enum_item",
    "union_item",
    "trait_item",
    "type_item",
    "associated_type",
    "const_item",
    "static_item",
}


class RustAnalyzerDependencyError(ValueError):
    """Raised when the optional Rust parser dependency is unavailable."""


@dataclass(slots=True)
class RustFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str
    module_path: str
    cfg_attributes: tuple[str, ...] = ()
    namespace_id: str | None = None


@dataclass(frozen=True, slots=True)
class RustSymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str | None
    declaration_kind: str
    arity: int | None = None
    owner_q: str | None = None


@dataclass(slots=True)
class RustUseInfo:
    aliases: dict[str, str] = field(default_factory=dict)
    wildcards: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RustImplInfo:
    file_path: str
    node_id: int
    self_reference: str
    trait_reference: str | None
    owner_id: str | None = None
    owner_q: str | None = None


@dataclass(slots=True)
class RustAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    crate_name: str
    cfg_profile: RustBuildProfile = field(default_factory=RustBuildProfile)
    files: list[RustFile] = field(default_factory=list)
    files_by_path: dict[str, RustFile] = field(default_factory=dict)
    namespace_by_q: dict[str, str] = field(default_factory=dict)
    namespace_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    definitions: dict[str, RustSymbol] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    symbols_by_name: dict[str, list[RustSymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[RustSymbol]] = field(default_factory=dict)
    imports_by_file: dict[str, RustUseInfo] = field(default_factory=dict)
    impls_by_node: dict[tuple[str, int], RustImplInfo] = field(default_factory=dict)
    variable_types: dict[tuple[str, str], str] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_namespace(
        self,
        qualified_name: str,
        *,
        rust_file: RustFile | None,
        tree_node: Any | None,
        declaration_form: str,
    ) -> str:
        existing = self.namespace_by_q.get(qualified_name)
        if existing is not None:
            if tree_node is not None and rust_file is not None:
                self.namespace_by_node[(rust_file.relative_path, tree_node.id)] = existing
            return existing
        node_id = f"rust:{qualified_name}:namespace"
        parent_q = qualified_name.rsplit("::", 1)[0] if "::" in qualified_name else None
        parent_id = self.namespace_by_q.get(parent_q) if parent_q else None
        file_path = rust_file.relative_path if rust_file is not None else None
        span = _span_for_tree(tree_node) if tree_node is not None else (
            _span_for_tree(rust_file.tree.root_node) if rust_file is not None else None
        )
        display_name = qualified_name.rsplit("::", 1)[-1] or self.crate_name
        self.builder.add_node(
            {
                "id": node_id,
                "kind": "namespace",
                "qualified_name": qualified_name,
                "display_name": display_name,
                "file": file_path,
                "span": span,
                "parent_id": parent_id,
                "visibility": "public",
                "signature": f"mod {display_name}",
                "extensions": {
                    "language": "rust",
                    "grammar": "rust",
                    "declaration_form": declaration_form,
                    "crate_name": self.crate_name,
                },
            }
        )
        self.namespace_by_q[qualified_name] = node_id
        symbol = RustSymbol(
            node_id=node_id,
            name=display_name,
            qualified_name=qualified_name,
            kind="namespace",
            file_path=file_path,
            declaration_kind=declaration_form,
        )
        self._index_symbol(symbol)
        if tree_node is not None and rust_file is not None:
            self.namespace_by_node[(rust_file.relative_path, tree_node.id)] = node_id
        return node_id

    def add_module(self, rust_file: RustFile) -> None:
        parent_id = rust_file.namespace_id
        extensions: dict[str, Any] = {
            "language": "rust",
            "grammar": "rust",
            "module_path": rust_file.module_path,
            "module_path_inferred": True,
        }
        if rust_file.cfg_attributes:
            extensions["cfg_attributes"] = list(rust_file.cfg_attributes)
        self.builder.add_node(
            {
                "id": rust_file.module_id,
                "kind": "module",
                "qualified_name": rust_file.module_path,
                "display_name": rust_file.relative_path,
                "file": rust_file.relative_path,
                "span": _span_for_tree(rust_file.tree.root_node),
                "parent_id": parent_id,
                "visibility": "public",
                "signature": f"mod {rust_file.module_path}",
                "extensions": extensions,
            }
        )
        if parent_id is not None:
            _add_relation(
                self,
                parent_id,
                rust_file.module_id,
                "contains",
                resolution_status="resolved",
                confidence=1.0,
                source_span=_span_for_tree(rust_file.tree.root_node),
                detail={"kind": "module_file"},
            )

    def add_definition(
        self,
        rust_file: RustFile,
        tree_node: Any,
        *,
        kind: str,
        name: str,
        qualified_name: str,
        declaration_kind: str,
        parent_id: str,
        arity: int | None = None,
        signature: str | None = None,
        return_behavior: str | None = None,
        return_sites: list[dict[str, Any]] | None = None,
        extensions: dict[str, Any] | None = None,
        owner_q: str | None = None,
    ) -> str:
        base_id = f"rust:{rust_file.relative_path}:{qualified_name}:{kind}"
        node_id = _unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": "rust",
            "grammar": "rust",
            "declaration_kind": declaration_kind,
        }
        if extensions:
            node_extensions.update(extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": rust_file.relative_path,
            "span": _span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": _visibility_for_node(tree_node, rust_file.source),
            "signature": signature or _signature_for_node(tree_node, rust_file.source),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        if kind in _CALLABLE_KINDS:
            node["execution_kind"] = _execution_kind_for_node(tree_node, rust_file.source)
        self.builder.add_node(node)
        parent_symbol = self.definitions.get(parent_id)
        resolved_owner = owner_q or _owner_qualified_name(parent_symbol)
        symbol = RustSymbol(
            node_id=node_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=rust_file.relative_path,
            declaration_kind=declaration_kind,
            arity=arity,
            owner_q=resolved_owner,
        )
        self.definitions[node_id] = symbol
        self.definition_by_node[(rust_file.relative_path, tree_node.id)] = node_id
        self._index_symbol(symbol)
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

    def _index_symbol(self, symbol: RustSymbol) -> None:
        self.definitions[symbol.node_id] = symbol
        self.symbols_by_name.setdefault(symbol.name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(symbol.qualified_name, []).append(symbol)

    def symbols_for_name(self, name: str, *, kinds: set[str] | None = None) -> list[RustSymbol]:
        candidates = list(self.symbols_by_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def symbols_for_qualified_name(
        self,
        name: str,
        *,
        kinds: set[str] | None = None,
    ) -> list[RustSymbol]:
        candidates = list(self.symbols_by_qualified_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def external_node(
        self,
        label: str,
        *,
        unknown: bool = False,
        rust_file: RustFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (kind, label)
        if key in self._external_node_ids:
            return self._external_node_ids[key]
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"rust:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": rust_file.relative_path if unknown and rust_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": "rust",
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
        rust_file: RustFile | None = None,
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
                "file": rust_file.relative_path if rust_file else file_path,
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
    """Analyze Rust source without building, importing, or executing target code."""

    active_config = config or AnalysisConfig(language="rust")
    active_config.validate()
    if active_config.language != "rust":
        raise ValueError("Rust analyzer requires language = 'rust'")
    if set(active_config.active_languages()) != SUPPORTED_LANGUAGES:
        raise ValueError("Rust analyzer supports only rust")

    root = root.resolve()
    builder = GraphBuilder()
    context = RustAnalysisContext(root, active_config, builder, _read_crate_name(root))
    analysis_context = load_analysis_context(root, active_config)
    context.cfg_profile = analysis_context.rust
    for diagnostic in analysis_context.diagnostics:
        builder.add_diagnostic({"code": diagnostic["code"], "severity": diagnostic["severity"], "message": diagnostic["message"], "file": None, "span": None, "details": {}})
    files, skipped = discover_source_files(root, active_config, languages={"rust"})
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped Rust source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            rust_file = _parse_file(path, root)
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
        if not _rust_file_matches_build(rust_file, context.cfg_profile):
            builder.add_diagnostic(
                {
                    "code": "build_condition_excluded",
                    "severity": "info",
                    "message": f"Skipped Rust source file by cfg condition: {relative_path}",
                    "file": relative_path,
                    "span": None,
                    "details": {"cfg_attributes": list(rust_file.cfg_attributes)},
                }
            )
            continue
        context.files.append(rust_file)
        context.files_by_path[rust_file.relative_path] = rust_file

    _index_namespaces(context)
    for rust_file in context.files:
        rust_file.namespace_id = context.namespace_by_q.get(rust_file.module_path)
        context.add_module(rust_file)
        if rust_file.tree.root_node.has_error:
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {rust_file.relative_path}",
                rust_file=rust_file,
                tree_node=rust_file.tree.root_node,
                details={"grammar": "rust"},
            )

    for rust_file in context.files:
        _index_file_metadata(context, rust_file)
    for rust_file in context.files:
        _index_impls(context, rust_file)
    for rust_file in context.files:
        _collect_type_definitions(context, rust_file)
    _resolve_impls(context)
    for rust_file in context.files:
        _collect_associated_type_definitions(context, rust_file)
    for rust_file in context.files:
        _collect_callable_definitions(context, rust_file)
    for rust_file in context.files:
        _collect_import_relations(context, rust_file)
        _collect_inheritance_relations(context, rust_file)
        _collect_type_relations(context, rust_file)
    for rust_file in context.files:
        _collect_call_relations(context, rust_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "rust",
        "languages": ["rust"],
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
            "grammars": ["rust"],
            "build_context": analysis_context.summary(),
        },
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": active_config.to_dict(),
        "extensions": {
            "crate_name": context.crate_name,
            "module_count": len(context.files),
            "namespace_count": len(context.namespace_by_q),
            "cfg_attributes_recorded": sum(bool(item.cfg_attributes) for item in context.files),
            "cargo_metadata_read": (root / "Cargo.toml").exists(),
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
        raise RustAnalyzerDependencyError(
            "Rust解析には任意依存が必要です。'uv sync --extra rust' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser("rust")
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise RustAnalyzerDependencyError(f"Tree-sitter grammar 'rust'を読み込めません: {exc}") from exc


def _parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _parse_file(path: Path, root: Path) -> RustFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    tree = _parser_for().parse(source)
    return RustFile(
        path=path,
        relative_path=relative_path,
        source=source,
        tree=tree,
        module_id=f"rust:{relative_path}:module",
        module_path=_module_path_for_file(relative_path),
        cfg_attributes=_cfg_attributes(source),
    )


def _index_namespaces(context: RustAnalysisContext) -> None:
    sources: dict[str, tuple[RustFile, Any | None, str]] = {}
    for rust_file in context.files:
        for qualified_name in _namespace_prefixes(rust_file.module_path):
            sources.setdefault(qualified_name, (rust_file, None, "file_module"))
        for node in _walk_active_tree(context, rust_file):
            if node.type != "mod_item" or node.child_by_field_name("body") is None:
                continue
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, rust_file.source).strip() if name_node is not None else ""
            if not name:
                continue
            qualified_name = f"{_module_path_for_node(rust_file, node)}::{name}"
            sources[qualified_name] = (rust_file, node, "inline_module")
    if context.files:
        sources.setdefault("crate", (context.files[0], None, "crate"))
    for qualified_name in sorted(sources, key=lambda value: (value.count("::"), value)):
        rust_file, tree_node, declaration_form = sources[qualified_name]
        context.add_namespace(
            qualified_name,
            rust_file=rust_file,
            tree_node=tree_node,
            declaration_form=declaration_form,
        )


def _index_file_metadata(context: RustAnalysisContext, rust_file: RustFile) -> None:
    info = RustUseInfo()
    for node in _walk_active_tree(context, rust_file):
        if node.type == "use_declaration":
            raw = _node_text(node, rust_file.source).strip()
            for path, local_name, wildcard in _expand_use_paths(raw):
                if wildcard:
                    info.wildcards.append(path)
                elif local_name:
                    info.aliases[local_name] = path
        elif node.type == "extern_crate_declaration":
            raw = _node_text(node, rust_file.source).strip().removesuffix(";")
            raw = re.sub(r"^extern\s+crate\s+", "", raw).strip()
            if raw:
                parts = re.split(r"\s+as\s+", raw, maxsplit=1)
                info.aliases[parts[-1].strip()] = parts[0].strip()
    context.imports_by_file[rust_file.relative_path] = info


def _index_impls(context: RustAnalysisContext, rust_file: RustFile) -> None:
    for node in _walk_active_tree(context, rust_file):
        if node.type != "impl_item":
            continue
        type_node = node.child_by_field_name("type")
        self_reference = _primary_type_name(type_node, rust_file.source)
        if not self_reference:
            self_reference = "<unknown>"
        trait_node = node.child_by_field_name("trait")
        trait_reference = _primary_type_name(trait_node, rust_file.source) if trait_node is not None else None
        context.impls_by_node[(rust_file.relative_path, node.id)] = RustImplInfo(
            file_path=rust_file.relative_path,
            node_id=node.id,
            self_reference=self_reference,
            trait_reference=trait_reference,
        )


def _resolve_impls(context: RustAnalysisContext) -> None:
    for info in context.impls_by_node.values():
        rust_file = context.files_by_path[info.file_path]
        candidates = _type_candidates(context, rust_file, info.self_reference, node=None)
        if len(candidates) == 1:
            info.owner_id = candidates[0].node_id
            info.owner_q = candidates[0].qualified_name
        elif info.self_reference and info.self_reference != "<unknown>":
            info.owner_q = _resolve_path_reference(context, rust_file, info.self_reference, node=None)


def _collect_type_definitions(context: RustAnalysisContext, rust_file: RustFile) -> None:
    for node in _walk_active_tree(context, rust_file):
        if node.type not in _TYPE_DECLARATION_TYPES or node.type == "associated_type":
            continue
        if node.type == "type_item" and _enclosing_impl(context, rust_file, node) is not None:
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text(name_node, rust_file.source).strip()
        if not name:
            continue
        parent_id = _scope_parent_id(context, rust_file, node)
        if parent_id is None:
            continue
        parent_symbol = context.definitions.get(parent_id)
        prefix = parent_symbol.qualified_name if parent_symbol else _module_path_for_node(rust_file, node)
        kind, declaration_kind, declaration_form = _type_declaration_form(node)
        if node.type in {"const_item", "static_item"} and parent_symbol and parent_symbol.kind in _CALLABLE_KINDS:
            continue
        qualified_name = f"{prefix}::{name}"
        context.add_definition(
            rust_file,
            node,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            declaration_kind=declaration_kind,
            parent_id=parent_id,
            extensions={"declaration_form": declaration_form},
            owner_q=prefix if parent_symbol and parent_symbol.kind in _TYPE_KINDS else None,
        )


def _collect_associated_type_definitions(context: RustAnalysisContext, rust_file: RustFile) -> None:
    """Add associated types after impl owners have been resolved."""

    for node in _walk_active_tree(context, rust_file):
        if node.type not in {"associated_type", "type_item"}:
            continue
        if node.type == "type_item" and _enclosing_impl(context, rust_file, node) is None:
            continue
        if (rust_file.relative_path, node.id) in context.definition_by_node:
            continue
        name_node = node.child_by_field_name("name")
        parent_id = _scope_parent_id(context, rust_file, node)
        if name_node is None or parent_id is None:
            continue
        name = _node_text(name_node, rust_file.source).strip()
        parent_symbol = context.definitions.get(parent_id)
        prefix = parent_symbol.qualified_name if parent_symbol else _module_path_for_node(rust_file, node)
        context.add_definition(
            rust_file,
            node,
            kind="type",
            name=name,
            qualified_name=f"{prefix}::{name}",
            declaration_kind="associated_type",
            parent_id=parent_id,
            extensions={"declaration_form": "associated_type"},
            owner_q=prefix if parent_symbol and parent_symbol.kind in _TYPE_KINDS else None,
        )


def _collect_callable_definitions(context: RustAnalysisContext, rust_file: RustFile) -> None:
    for node in _walk_active_tree(context, rust_file):
        if node.type not in {"function_item", "function_signature_item", "closure_expression", "macro_definition"}:
            continue
        if node.type == "closure_expression":
            _add_closure_definition(context, rust_file, node)
            continue
        if node.type == "macro_definition":
            _add_macro_definition(context, rust_file, node)
            continue
        _add_function_definition(context, rust_file, node)


def _add_function_definition(context: RustAnalysisContext, rust_file: RustFile, node: Any) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _node_text(name_node, rust_file.source).strip()
    if not name:
        return
    parent_id = _scope_parent_id(context, rust_file, node)
    if parent_id is None:
        return
    parent_symbol = context.definitions.get(parent_id)
    is_method = bool(parent_symbol and parent_symbol.kind in _TYPE_KINDS)
    kind = "method" if is_method else "function"
    prefix = parent_symbol.qualified_name if parent_symbol else _module_path_for_node(rust_file, node)
    arity = _parameter_arity(node.child_by_field_name("parameters"))
    qualified_name = f"{prefix}::{name}({arity})"
    has_body = node.child_by_field_name("body") is not None
    return_behavior, return_sites = _return_info(node) if has_body else ("unknown", [])
    extensions: dict[str, Any] = {}
    if is_method:
        extensions["associated_item"] = True
    impl_info = _enclosing_impl(context, rust_file, node)
    if impl_info is not None and impl_info.trait_reference:
        extensions["impl_trait"] = impl_info.trait_reference
    context.add_definition(
        rust_file,
        node,
        kind=kind,
        name=name,
        qualified_name=qualified_name,
        declaration_kind="definition" if has_body else "declaration",
        parent_id=parent_id,
        arity=arity,
        return_behavior=return_behavior,
        return_sites=return_sites,
        extensions=extensions,
        owner_q=parent_symbol.qualified_name if is_method and parent_symbol else None,
    )


def _add_closure_definition(context: RustAnalysisContext, rust_file: RustFile, node: Any) -> None:
    parent_id = _enclosing_definition(context, rust_file, node.parent) or _scope_parent_id(context, rust_file, node)
    if parent_id is None:
        return
    parent_symbol = context.definitions.get(parent_id)
    line = int(node.start_point[0]) + 1
    column = int(node.start_point[1])
    name = f"<closure@{line}:{column}>"
    prefix = parent_symbol.qualified_name if parent_symbol else _module_path_for_node(rust_file, node)
    qualified_name = f"{prefix}::{name}"
    return_behavior, return_sites = _return_info(node)
    context.add_definition(
        rust_file,
        node,
        kind="lambda",
        name=name,
        qualified_name=qualified_name,
        declaration_kind="closure",
        parent_id=parent_id,
        arity=_closure_arity(node),
        return_behavior=return_behavior,
        return_sites=return_sites,
        extensions={"declaration_form": "closure_expression"},
        owner_q=parent_symbol.owner_q if parent_symbol else None,
    )


def _add_macro_definition(context: RustAnalysisContext, rust_file: RustFile, node: Any) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _node_text(name_node, rust_file.source).strip()
    parent_id = _scope_parent_id(context, rust_file, node)
    if not name or parent_id is None:
        return
    parent_symbol = context.definitions.get(parent_id)
    prefix = parent_symbol.qualified_name if parent_symbol else _module_path_for_node(rust_file, node)
    context.add_definition(
        rust_file,
        node,
        kind="function",
        name=name,
        qualified_name=f"{prefix}::{name}",
        declaration_kind="macro",
        parent_id=parent_id,
        extensions={"declaration_form": "macro"},
        owner_q=None,
    )


def _collect_import_relations(context: RustAnalysisContext, rust_file: RustFile) -> None:
    source_id = rust_file.module_id
    for node in _walk_active_tree(context, rust_file):
        if node.type == "use_declaration":
            raw = _node_text(node, rust_file.source).strip()
            paths = _expand_use_paths(raw)
            for path, local_name, wildcard in paths:
                target, status, confidence = _resolve_import(context, rust_file, path, node)
                _add_relation(
                    context,
                    source_id,
                    target,
                    "imports",
                    resolution_status=status,
                    confidence=confidence,
                    source_span=_span_for_tree(node),
                    detail={
                        "path": path,
                        "local_name": local_name,
                        "wildcard": wildcard,
                        "reexport": bool(re.search(r"\bpub\s+use\b", raw)),
                    },
                )
        elif node.type == "extern_crate_declaration":
            raw = _node_text(node, rust_file.source).strip().removesuffix(";")
            path = re.sub(r"^extern\s+crate\s+", "", raw).strip()
            if path:
                target = context.external_node(f"crate:{path}")
                _add_relation(
                    context,
                    source_id,
                    target,
                    "imports",
                    resolution_status="external",
                    confidence=0.75,
                    source_span=_span_for_tree(node),
                    detail={"path": path, "kind": "extern_crate"},
                )


def _resolve_import(
    context: RustAnalysisContext,
    rust_file: RustFile,
    path: str,
    node: Any,
) -> tuple[str, str, float]:
    candidates = _path_symbols(context, rust_file, path, kinds=None)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(f"use:{path}", unknown=True, rust_file=rust_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, rust_file, node, "unresolved_import", f"Rust useを一意に解決できません: {path}")
        return target, "unresolved", 0.2
    if _looks_local_path(path):
        target = context.external_node(f"use:{path}", unknown=True, rust_file=rust_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, rust_file, node, "unresolved_import", f"対象内Rust pathを解決できません: {path}")
        return target, "unresolved", 0.2
    return context.external_node(f"use:{path}"), "external", 0.75


def _collect_inheritance_relations(context: RustAnalysisContext, rust_file: RustFile) -> None:
    for node in _walk_active_tree(context, rust_file):
        if node.type == "trait_item":
            source_id = context.definition_by_node.get((rust_file.relative_path, node.id))
            bounds = node.child_by_field_name("bounds")
            if source_id is None or bounds is None:
                continue
            for reference, reference_node in _type_references(bounds, rust_file.source):
                target, status, confidence = _resolve_type(context, rust_file, reference, reference_node)
                _add_relation(
                    context,
                    source_id,
                    target,
                    "inherits",
                    resolution_status=status,
                    confidence=confidence,
                    source_span=_span_for_tree(reference_node),
                    detail={"reference": reference, "role": "supertrait"},
                )
        elif node.type == "impl_item":
            info = context.impls_by_node.get((rust_file.relative_path, node.id))
            trait_node = node.child_by_field_name("trait")
            if info is None or trait_node is None:
                continue
            source_id = info.owner_id or rust_file.namespace_id or rust_file.module_id
            reference = _primary_type_name(trait_node, rust_file.source)
            if not reference:
                continue
            target, status, confidence = _resolve_type(context, rust_file, reference, trait_node)
            _add_relation(
                context,
                source_id,
                target,
                "inherits",
                resolution_status=status,
                confidence=confidence,
                source_span=_span_for_tree(trait_node),
                detail={"reference": reference, "role": "implements", "self_type": info.self_reference},
            )


def _collect_type_relations(context: RustAnalysisContext, rust_file: RustFile) -> None:
    for node in _walk_active_tree(context, rust_file):
        if node.type == "field_declaration":
            type_node = node.child_by_field_name("type")
            source_id = _enclosing_definition(context, rust_file, node) or rust_file.namespace_id or rust_file.module_id
            _add_type_references(context, rust_file, type_node, source_id, "field_type")
            field_name = node.child_by_field_name("name")
            owner_id = _nearest_type_definition(context, rust_file, node)
            reference = _primary_type_name(type_node, rust_file.source)
            if owner_id and field_name is not None and reference:
                context.variable_types[(owner_id, _node_text(field_name, rust_file.source).strip())] = reference
        elif node.type == "parameter":
            type_node = node.child_by_field_name("type")
            source_id = _enclosing_definition(context, rust_file, node) or rust_file.namespace_id or rust_file.module_id
            _add_type_references(context, rust_file, type_node, source_id, "parameter_type")
            reference = _primary_type_name(type_node, rust_file.source)
            if reference:
                for name in _pattern_names(node.child_by_field_name("pattern"), rust_file.source):
                    context.variable_types[(source_id, name)] = reference
        elif node.type in {"function_item", "function_signature_item"}:
            source_id = context.definition_by_node.get((rust_file.relative_path, node.id))
            if source_id is None:
                source_id = _enclosing_definition(context, rust_file, node) or rust_file.namespace_id or rust_file.module_id
            _add_type_references(context, rust_file, node.child_by_field_name("return_type"), source_id, "return_type")
            _add_type_references(context, rust_file, node.child_by_field_name("type_parameters"), source_id, "generic_bound")
        elif node.type in {"type_item", "associated_type"}:
            source_id = context.definition_by_node.get((rust_file.relative_path, node.id))
            if source_id:
                _add_type_references(context, rust_file, node.child_by_field_name("type"), source_id, "type_alias")
                _add_type_references(context, rust_file, node.child_by_field_name("bounds"), source_id, "associated_bound")
        elif node.type == "let_declaration":
            source_id = _enclosing_definition(context, rust_file, node) or rust_file.namespace_id or rust_file.module_id
            pattern = node.child_by_field_name("pattern")
            type_node = pattern.child_by_field_name("type") if pattern is not None else None
            reference = _primary_type_name(type_node, rust_file.source)
            if type_node is not None:
                _add_type_references(context, rust_file, type_node, source_id, "local_type")
            if not reference:
                value = node.child_by_field_name("value")
                reference = _expression_type_reference(context, rust_file, value)
            if reference:
                for name in _pattern_names(pattern, rust_file.source):
                    context.variable_types[(source_id, name)] = reference
        elif node.type in {"where_predicate", "type_parameters", "trait_bounds", "bounded_type"}:
            source_id = _enclosing_definition(context, rust_file, node) or rust_file.namespace_id or rust_file.module_id
            _add_type_references(context, rust_file, node, source_id, "generic_bound")
        elif node.type == "struct_expression":
            source_id = _enclosing_definition(context, rust_file, node) or rust_file.namespace_id or rust_file.module_id
            type_node = node.child_by_field_name("name")
            reference = _primary_type_name(type_node, rust_file.source)
            if reference:
                target, status, confidence = _resolve_type(context, rust_file, reference, type_node)
                _add_relation(
                    context,
                    source_id,
                    target,
                    "uses",
                    resolution_status=status,
                    confidence=confidence,
                    source_span=_span_for_tree(type_node),
                    detail={"reference": reference, "kind": "struct_expression"},
                )


def _collect_call_relations(context: RustAnalysisContext, rust_file: RustFile) -> None:
    for node in _walk_active_tree(context, rust_file):
        if node.type == "call_expression":
            _collect_call(context, rust_file, node)
        elif node.type == "macro_invocation":
            _collect_macro_call(context, rust_file, node)


def _collect_call(context: RustAnalysisContext, rust_file: RustFile, node: Any) -> None:
    caller = _enclosing_definition(context, rust_file, node) or rust_file.namespace_id or rust_file.module_id
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    name, receiver, path = _invocation_parts(function, rust_file.source)
    arity = _argument_arity(arguments)
    expression = _node_text(node, rust_file.source).strip()
    target, status, confidence = _resolve_call(context, rust_file, caller, name, receiver, path, arity, node)
    if status == "unresolved":
        _diagnose_unresolved(
            context,
            rust_file,
            node,
            "unresolved_call",
            f"Rust呼び出しを静的に解決できません: {expression}",
            node_id=caller,
            details={"expression": expression, "callee": name, "arity": arity, "receiver": receiver, "path": path},
        )
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={
            "expression": expression,
            "callee": name,
            "receiver": receiver,
            "path": path,
            "arity": arity,
            "call_kind": "method" if receiver else "direct",
        },
    )


def _collect_macro_call(context: RustAnalysisContext, rust_file: RustFile, node: Any) -> None:
    caller = _enclosing_definition(context, rust_file, node) or rust_file.namespace_id or rust_file.module_id
    macro_node = node.child_by_field_name("macro")
    path = _node_text(macro_node, rust_file.source).strip() if macro_node is not None else ""
    name = path.rsplit("::", 1)[-1]
    candidates = [
        symbol
        for symbol in context.symbols_for_name(name, kinds={"function"})
        if symbol.declaration_kind == "macro"
    ]
    if len(candidates) == 1:
        target, status, confidence = candidates[0].node_id, "resolved", 1.0
    elif len(candidates) > 1:
        target = context.external_node(f"macro:{path}", unknown=True, rust_file=rust_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
    else:
        target = context.external_node(f"macro:{path}")
        status, confidence = "external", 0.75
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"expression": _node_text(node, rust_file.source).strip(), "macro": path, "call_kind": "macro"},
    )


def _resolve_call(
    context: RustAnalysisContext,
    rust_file: RustFile,
    caller: str,
    name: str,
    receiver: str,
    path: str,
    arity: int,
    node: Any,
) -> tuple[str, str, float]:
    if not name:
        return (
            context.external_node("call:<unknown>", unknown=True, rust_file=rust_file, span=_span_for_tree(node)),
            "unresolved",
            0.1,
        )
    candidates: list[RustSymbol] = []
    if path:
        candidates = _functions_for_path(context, rust_file, path, name, arity)
    if not candidates and receiver:
        receiver_type = _receiver_type_reference(context, rust_file, caller, receiver)
        if receiver_type:
            type_candidates = _type_candidates(context, rust_file, receiver_type, node)
            for type_symbol in type_candidates:
                candidates.extend(_methods_for_owner(context, type_symbol.qualified_name, name, arity))
        elif _path_symbols(context, rust_file, receiver, kinds={"namespace"}):
            candidates = _functions_for_path(context, rust_file, f"{receiver}::{name}", name, arity)
        else:
            return (
                context.external_node(
                    f"call:{receiver}.{name}/{arity}",
                    unknown=True,
                    rust_file=rust_file,
                    span=_span_for_tree(node),
                ),
                "unresolved",
                0.2,
            )
    if not candidates and not receiver:
        caller_symbol = context.definitions.get(caller)
        owner_q = caller_symbol.owner_q if caller_symbol else None
        if owner_q:
            candidates.extend(_methods_for_owner(context, owner_q, name, arity))
        module_path = _module_path_for_node(rust_file, node)
        candidates.extend(_functions_for_module(context, module_path, name, arity))
        import_path = context.imports_by_file.get(rust_file.relative_path, RustUseInfo()).aliases.get(name)
        if import_path:
            candidates.extend(_functions_for_import(context, rust_file, import_path, arity))
        if not candidates:
            candidates.extend(
                symbol
                for symbol in context.symbols_for_name(name, kinds={"function", "method"})
                if symbol.arity == arity
            )
    candidates = _unique_symbols(candidates)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    label = f"call:{path or receiver + '.' if receiver else ''}{name}/{arity}"
    if len(candidates) > 1:
        return (
            context.external_node(label, unknown=True, rust_file=rust_file, span=_span_for_tree(node)),
            "unresolved",
            0.2,
        )
    return context.external_node(label), "external", 0.7


def _functions_for_path(
    context: RustAnalysisContext,
    rust_file: RustFile,
    path: str,
    name: str,
    arity: int,
) -> list[RustSymbol]:
    raw = path
    if raw.endswith(f"::{name}"):
        raw = raw[: -(len(name) + 2)]
    prefix = _resolve_path_reference(context, rust_file, raw, node=None)
    full = f"{prefix}::{name}({arity})" if prefix else f"{raw}::{name}({arity})"
    candidates = context.symbols_for_qualified_name(full, kinds={"function", "method"})
    if not candidates:
        candidates = [
            symbol
            for symbol in context.symbols_for_name(name, kinds={"function", "method"})
            if symbol.arity == arity and (not prefix or symbol.qualified_name.startswith(f"{prefix}::"))
        ]
    return candidates


def _functions_for_import(
    context: RustAnalysisContext,
    rust_file: RustFile,
    import_path: str,
    arity: int,
) -> list[RustSymbol]:
    normalized = _resolve_path_reference(context, rust_file, import_path, node=None)
    return [
        symbol
        for symbol in context.symbols_for_qualified_name(f"{normalized}({arity})", kinds={"function", "method"})
    ]


def _functions_for_module(context: RustAnalysisContext, module_path: str, name: str, arity: int) -> list[RustSymbol]:
    prefix = f"{module_path}::{name}({arity})"
    return context.symbols_for_qualified_name(prefix, kinds={"function"})


def _methods_for_owner(context: RustAnalysisContext, owner_q: str | None, name: str, arity: int) -> list[RustSymbol]:
    if not owner_q:
        return []
    return [
        symbol
        for symbol in context.symbols_for_name(name, kinds={"method"})
        if symbol.arity == arity and symbol.owner_q == owner_q
    ]


def _collect_type_relation(
    context: RustAnalysisContext,
    rust_file: RustFile,
    type_node: Any | None,
    source_id: str,
    detail_kind: str,
) -> None:
    _add_type_references(context, rust_file, type_node, source_id, detail_kind)


def _add_type_references(
    context: RustAnalysisContext,
    rust_file: RustFile,
    type_node: Any | None,
    source_id: str,
    detail_kind: str,
) -> None:
    if type_node is None:
        return
    for reference, reference_node in _type_references(type_node, rust_file.source):
        if _clean_type_reference(reference) in _PRIMITIVE_TYPES:
            continue
        target, status, confidence = _resolve_type(context, rust_file, reference, reference_node)
        _add_relation(
            context,
            source_id,
            target,
            "uses",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(reference_node),
            detail={"reference": reference, "kind": detail_kind},
        )


def _resolve_type(
    context: RustAnalysisContext,
    rust_file: RustFile,
    reference: str,
    node: Any | None,
) -> tuple[str, str, float]:
    candidates = _type_candidates(context, rust_file, reference, node)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(
            f"type:{reference}",
            unknown=True,
            rust_file=rust_file,
            span=_span_for_tree(node),
        )
        _diagnose_unresolved(context, rust_file, node, "unresolved_type", f"Rust型を一意に解決できません: {reference}")
        return target, "unresolved", 0.2
    if _looks_local_path(reference):
        target = context.external_node(
            f"type:{reference}",
            unknown=True,
            rust_file=rust_file,
            span=_span_for_tree(node),
        )
        return target, "unresolved", 0.3
    return context.external_node(f"type:{reference}"), "external", 0.7


def _type_candidates(
    context: RustAnalysisContext,
    rust_file: RustFile,
    reference: str,
    node: Any | None,
) -> list[RustSymbol]:
    cleaned = _clean_type_reference(reference)
    if not cleaned or cleaned in _PRIMITIVE_TYPES:
        return []
    if cleaned == "Self":
        owner = _nearest_type_definition(context, rust_file, node) if node is not None else None
        symbol = context.definitions.get(owner) if owner else None
        return [symbol] if symbol is not None else []
    names = [_resolve_path_reference(context, rust_file, cleaned, node=node)]
    if "::" not in cleaned:
        names.extend(
            [
                f"{_module_path_for_node(rust_file, node)}::{cleaned}",
                f"crate::{cleaned}",
            ]
        )
    candidates: list[RustSymbol] = []
    for name in names:
        if name:
            candidates.extend(context.symbols_for_qualified_name(name, kinds=_TYPE_KINDS))
    if not candidates:
        candidates = context.symbols_for_name(cleaned.rsplit("::", 1)[-1], kinds=_TYPE_KINDS)
    return _unique_symbols(candidates)


def _path_symbols(
    context: RustAnalysisContext,
    rust_file: RustFile,
    path: str,
    *,
    kinds: set[str] | None,
) -> list[RustSymbol]:
    normalized = _resolve_path_reference(context, rust_file, path, node=None)
    candidates = context.symbols_for_qualified_name(normalized, kinds=kinds)
    if not candidates and path.rsplit("::", 1)[-1] in context.imports_by_file.get(rust_file.relative_path, RustUseInfo()).aliases:
        alias_path = context.imports_by_file[rust_file.relative_path].aliases[path.rsplit("::", 1)[-1]]
        candidates = context.symbols_for_qualified_name(
            _resolve_path_reference(context, rust_file, alias_path, node=None),
            kinds=kinds,
        )
    return _unique_symbols(candidates)


def _resolve_path_reference(
    context: RustAnalysisContext,
    rust_file: RustFile,
    reference: str,
    *,
    node: Any | None,
    _seen: set[str] | None = None,
) -> str:
    cleaned = _clean_path_reference(reference)
    if not cleaned:
        return ""
    seen = _seen if _seen is not None else set()
    if cleaned in seen:
        return cleaned
    seen.add(cleaned)
    current_module = _module_path_for_node(rust_file, node)
    if cleaned == "Self":
        owner = _nearest_type_definition(context, rust_file, node) if node is not None else None
        symbol = context.definitions.get(owner) if owner else None
        return symbol.qualified_name if symbol else cleaned
    if cleaned == "crate" or cleaned.startswith("crate::"):
        return cleaned
    if cleaned == "self" or cleaned.startswith("self::"):
        suffix = cleaned.removeprefix("self").removeprefix("::")
        return f"{current_module}::{suffix}" if suffix else current_module
    if cleaned == "super" or cleaned.startswith("super::"):
        suffix = cleaned.removeprefix("super").removeprefix("::")
        parent = current_module.rsplit("::", 1)[0] if "::" in current_module else current_module
        return f"{parent}::{suffix}" if suffix else parent
    first, _, suffix = cleaned.partition("::")
    alias = context.imports_by_file.get(rust_file.relative_path, RustUseInfo()).aliases.get(first)
    if alias:
        if alias == cleaned or alias == first:
            return cleaned
        base = _resolve_path_reference(context, rust_file, alias, node=node, _seen=seen)
        return f"{base}::{suffix}" if suffix else base
    return cleaned


def _receiver_type_reference(
    context: RustAnalysisContext,
    rust_file: RustFile,
    caller: str,
    receiver: str,
) -> str | None:
    cleaned = receiver.strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"^&(?:mut\s+)?", "", cleaned).strip()
    if cleaned in {"self", "Self"}:
        symbol = context.definitions.get(caller)
        return symbol.owner_q if symbol else None
    segments = [part for part in re.split(r"\.", cleaned) if part]
    if not segments:
        return None
    first = segments[0]
    symbol = context.definitions.get(caller)
    owner_q = symbol.owner_q if symbol else None
    current_type = owner_q if first == "self" else _lookup_variable_type(context, caller, first)
    if current_type is None:
        candidates = _type_candidates(context, rust_file, first, node=None)
        if len(candidates) == 1:
            current_type = candidates[0].qualified_name
    if current_type is None:
        return None
    for field_name in segments[1:]:
        field_type = _field_type_reference(context, rust_file, current_type, field_name)
        if field_type is None:
            return None
        current_type = field_type
    return current_type


def _lookup_variable_type(context: RustAnalysisContext, caller: str, name: str) -> str | None:
    reference = context.variable_types.get((caller, name))
    if reference:
        return reference
    symbol = context.definitions.get(caller)
    if symbol is not None and symbol.owner_q:
        for owner in context.symbols_for_qualified_name(symbol.owner_q, kinds=_TYPE_KINDS):
            reference = context.variable_types.get((owner.node_id, name))
            if reference:
                return reference
    return None


def _field_type_reference(
    context: RustAnalysisContext,
    rust_file: RustFile,
    owner_reference: str,
    field_name: str,
) -> str | None:
    for symbol in _type_candidates(context, rust_file, owner_reference, node=None):
        reference = context.variable_types.get((symbol.node_id, field_name))
        if reference:
            return reference
    return None


def _enclosing_impl(context: RustAnalysisContext, rust_file: RustFile, node: Any) -> RustImplInfo | None:
    current = node.parent
    while current is not None:
        info = context.impls_by_node.get((rust_file.relative_path, current.id))
        if info is not None:
            return info
        current = current.parent
    return None


def _scope_parent_id(context: RustAnalysisContext, rust_file: RustFile, node: Any) -> str | None:
    current = node.parent
    while current is not None:
        definition_id = context.definition_by_node.get((rust_file.relative_path, current.id))
        if definition_id is not None:
            return definition_id
        if current.type == "impl_item":
            info = context.impls_by_node.get((rust_file.relative_path, current.id))
            if info is not None and info.owner_id is not None:
                return info.owner_id
        if current.type == "mod_item":
            namespace_id = context.namespace_by_node.get((rust_file.relative_path, current.id))
            if namespace_id is not None:
                return namespace_id
        current = current.parent
    return context.namespace_by_q.get(_module_path_for_node(rust_file, node)) or rust_file.namespace_id


def _enclosing_definition(context: RustAnalysisContext, rust_file: RustFile, node: Any | None) -> str | None:
    current = node
    while current is not None:
        definition_id = context.definition_by_node.get((rust_file.relative_path, current.id))
        if definition_id is not None:
            return definition_id
        current = current.parent
    return None


def _nearest_type_definition(context: RustAnalysisContext, rust_file: RustFile, node: Any | None) -> str | None:
    current = node
    while current is not None:
        definition_id = context.definition_by_node.get((rust_file.relative_path, current.id))
        if definition_id is not None:
            symbol = context.definitions.get(definition_id)
            if symbol and symbol.kind in _TYPE_KINDS:
                return definition_id
        current = current.parent
    return None


def _return_info(node: Any) -> tuple[str, list[dict[str, Any]]]:
    sites: list[dict[str, Any]] = []
    has_value = False
    has_none = False

    def visit(current: Any) -> None:
        nonlocal has_value, has_none
        if current is not node and current.type in {"function_item", "closure_expression", "macro_definition"}:
            return
        if current.type == "return_expression":
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


def _type_references(node: Any | None, source: bytes) -> list[tuple[str, Any]]:
    references: list[tuple[str, Any]] = []
    type_nodes = {
        "type_identifier",
        "scoped_type_identifier",
        "generic_type",
        "trait_object_type",
        "impl_type",
        "dynamic_type",
    }

    def visit(current: Any) -> None:
        if current is None:
            return
        if current.type in type_nodes:
            reference = _primary_type_name(current, source)
            if reference:
                references.append((reference, current))
            if current.type == "generic_type":
                arguments = current.child_by_field_name("type_arguments")
                if arguments is not None:
                    for child in arguments.named_children:
                        visit(child)
            return
        if current.type in {"primitive_type", "lifetime", "type_parameter"}:
            if current.type == "type_parameter":
                bounds = current.child_by_field_name("bounds")
                if bounds is not None:
                    visit(bounds)
            return
        for child in current.named_children:
            visit(child)

    visit(node)
    result: list[tuple[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for reference, reference_node in references:
        key = (reference, int(reference_node.start_byte))
        if key not in seen:
            seen.add(key)
            result.append((reference, reference_node))
    return result


def _type_declaration_form(node: Any) -> tuple[str, str, str]:
    mapping = {
        "struct_item": ("type", "definition", "struct"),
        "enum_item": ("type", "definition", "enum"),
        "union_item": ("type", "definition", "union"),
        "trait_item": ("interface", "definition", "trait"),
        "type_item": ("type", "alias", "alias"),
        "associated_type": ("type", "associated_type", "associated_type"),
        "const_item": ("type", "const", "const"),
        "static_item": ("type", "static", "static"),
    }
    return mapping[node.type]


def _pattern_names(node: Any | None, source: bytes) -> list[str]:
    if node is None:
        return []
    if node.type in {"identifier", "field_identifier"}:
        value = _node_text(node, source).strip()
        return [value] if value and value != "_" else []
    names: list[str] = []
    for child in node.named_children:
        for name in _pattern_names(child, source):
            if name not in names:
                names.append(name)
    return names


def _expression_type_reference(
    context: RustAnalysisContext,
    rust_file: RustFile,
    node: Any | None,
) -> str:
    if node is None:
        return ""
    if node.type == "struct_expression":
        return _primary_type_name(node.child_by_field_name("name"), rust_file.source)
    if node.type == "call_expression":
        function = node.child_by_field_name("function")
        name, receiver, path = _invocation_parts(function, rust_file.source)
        if receiver:
            candidates = _type_candidates(context, rust_file, receiver, node)
            if len(candidates) == 1:
                return candidates[0].qualified_name
        if path:
            candidates = _functions_for_path(context, rust_file, path, name, _argument_arity(node.child_by_field_name("arguments")))
            if len(candidates) == 1 and candidates[0].owner_q:
                return candidates[0].owner_q
    if node.type in {"parenthesized_expression", "reference_expression", "unary_expression"}:
        children = node.named_children
        if children:
            return _expression_type_reference(context, rust_file, children[-1])
    return ""


def _invocation_parts(node: Any | None, source: bytes) -> tuple[str, str, str]:
    if node is None:
        return "", "", ""
    if node.type == "generic_function":
        function = node.child_by_field_name("function")
        return _invocation_parts(function, source)
    raw = _node_text(node, source).strip()
    if node.type == "field_expression":
        field = node.child_by_field_name("field")
        value = node.child_by_field_name("value")
        name = _node_text(field, source).strip() if field is not None else ""
        receiver = _node_text(value, source).strip() if value is not None else ""
        return name, receiver, ""
    if node.type in {"scoped_identifier", "scoped_type_identifier"}:
        parts = [part for part in raw.split("::") if part]
        if len(parts) > 1:
            return parts[-1], "", raw
    return raw, "", ""


def _parameter_arity(node: Any | None) -> int:
    if node is None:
        return 0
    # `self` is an implicit receiver for method calls and is not part of the
    # call-site argument count used in the stable qualified name.
    return sum(1 for child in node.named_children if child.type in {"parameter", "variadic_parameter"})


def _closure_arity(node: Any) -> int:
    parameters = node.child_by_field_name("parameters")
    return _parameter_arity(parameters)


def _argument_arity(node: Any | None) -> int:
    return len(node.named_children) if node is not None else 0


def _visibility_for_node(node: Any, source: bytes) -> str:
    raw = _node_text(node, source)
    return "public" if re.search(r"\bpub(?:\s*\([^)]*\))?\b", raw) else "private"


def _execution_kind_for_node(node: Any, source: bytes) -> str:
    raw = _node_text(node, source)
    return "async" if re.search(r"\basync\b", raw.split("{", 1)[0]) else "sync"


def _owner_qualified_name(symbol: RustSymbol | None) -> str | None:
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS:
        return symbol.qualified_name
    return symbol.owner_q


def _unique_symbols(symbols: Iterable[RustSymbol]) -> list[RustSymbol]:
    result: list[RustSymbol] = []
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


def _walk_tree(node: Any) -> Iterable[Any]:
    yield node
    for child in node.children:
        yield from _walk_tree(child)


def _cfg_expressions_for_node(rust_file: RustFile, node: Any) -> list[str]:
    """Return cfg expressions attached to a syntax node.

    Tree-sitter versions differ slightly in whether an attribute is included
    in an item's span.  Checking both child attributes and immediately
    preceding source lines keeps filtering stable across parser versions.
    """

    expressions: list[str] = []
    for child in getattr(node, "children", ()):
        if getattr(child, "type", "") != "attribute_item":
            continue
        raw = _node_text(child, rust_file.source)
        match = re.search(r"#\s*!?\s*\[\s*cfg\s*\((.*?)\)\s*\]", raw, flags=re.DOTALL)
        if match:
            expressions.append(match.group(1))
    if expressions:
        return expressions

    lines = rust_file.source.decode("utf-8", errors="replace").splitlines()
    line_number = int(getattr(node, "start_point", (0, 0))[0])
    while line_number > 0:
        stripped = lines[line_number - 1].strip()
        if not stripped:
            line_number -= 1
            continue
        if not stripped.startswith("#"):
            break
        match = re.search(r"#\s*!?\s*\[\s*cfg\s*\((.*?)\)\s*\]", stripped, flags=re.DOTALL)
        if match:
            expressions.insert(0, match.group(1))
        line_number -= 1
    return expressions


def _walk_active_tree(context: RustAnalysisContext, rust_file: RustFile, node: Any | None = None) -> Iterable[Any]:
    is_root = node is None
    current = rust_file.tree.root_node if is_root else node
    if not is_root:
        if any(not rust_cfg_expression_matches(expression, context.cfg_profile) for expression in _cfg_expressions_for_node(rust_file, current)):
            return
    yield current
    for child in current.children:
        yield from _walk_active_tree(context, rust_file, child)


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
    context: RustAnalysisContext,
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
    edge_id = f"rust-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
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
    context: RustAnalysisContext,
    rust_file: RustFile,
    node: Any | None,
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
        rust_file=rust_file,
        tree_node=node,
        node_id=node_id,
        details=details,
    )


def _module_path_for_file(relative_path: str) -> str:
    parts = relative_path.replace("\\", "/").split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return "crate"
    filename = parts.pop()
    stem = filename.removesuffix(".rs")
    if stem not in {"lib", "main", "mod"}:
        parts.append(stem)
    return "crate" + ("::" + "::".join(part for part in parts if part) if parts else "")


def _module_path_for_node(rust_file: RustFile, node: Any | None) -> str:
    base = rust_file.module_path
    if node is None:
        return base
    names: list[str] = []
    current = node.parent
    while current is not None:
        if current.type == "mod_item" and current.child_by_field_name("body") is not None:
            name_node = current.child_by_field_name("name")
            name = _node_text(name_node, rust_file.source).strip() if name_node is not None else ""
            if name:
                names.append(name)
        current = current.parent
    if not names:
        return base
    return f"{base}::{'::'.join(reversed(names))}"


def _namespace_prefixes(qualified_name: str) -> list[str]:
    parts = qualified_name.split("::")
    return ["::".join(parts[:index]) for index in range(1, len(parts) + 1)]


def _read_crate_name(root: Path) -> str:
    cargo = root / "Cargo.toml"
    try:
        with cargo.open("rb") as handle:
            raw = tomllib.load(handle)
        package_name = raw.get("package", {}).get("name")
        if isinstance(package_name, str) and package_name.strip():
            return package_name.strip().replace("-", "_")
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        pass
    return root.name.replace("-", "_") or "crate"


def _cfg_attributes(source: bytes) -> tuple[str, ...]:
    values: list[str] = []
    for line in source.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if re.match(r"#\!?\s*\[\s*cfg(?:_attr)?\b", stripped):
            values.append(stripped)
    return tuple(values)


def _rust_file_matches_build(rust_file: RustFile, profile: RustBuildProfile) -> bool:
    for attribute in rust_file.cfg_attributes:
        match = re.search(r"#\s*!\s*\[\s*cfg\s*\((.*?)\)\s*\]", attribute, flags=re.DOTALL)
        if match and not rust_cfg_expression_matches(match.group(1), profile):
            return False
    return True


def _clean_path_reference(value: str) -> str:
    raw = value.strip().replace(" ", "")
    raw = raw.removeprefix("::")
    return raw


def _clean_type_reference(value: str) -> str:
    raw = _clean_path_reference(value)
    raw = re.sub(r"^&(?:'[_A-Za-z][_A-Za-z0-9]*\s*)?(?:mut\s+)?", "", raw)
    raw = re.sub(r"^\*(?:const|mut)\s*", "", raw)
    raw = re.sub(r"^(?:dyn|impl)\s+", "", raw)
    raw = re.sub(r"'[_A-Za-z][_A-Za-z0-9]*", "", raw)
    raw = raw.replace("?", "")
    raw = _strip_generic_arguments(raw)
    raw = raw.removesuffix("::Output") if raw.endswith("::Output") else raw
    if raw.startswith("for<"):
        closing = raw.find(">")
        raw = raw[closing + 1 :] if closing >= 0 else raw
    return raw


def _primary_type_name(node: Any | None, source: bytes) -> str:
    if node is None:
        return ""
    raw = _node_text(node, source).strip()
    cleaned = _clean_type_reference(raw)
    if re.fullmatch(r"[A-Za-z_][\w]*(?:::[A-Za-z_][\w]*)*", cleaned):
        return cleaned
    return ""


def _strip_generic_arguments(value: str) -> str:
    if "<" not in value:
        return value
    result: list[str] = []
    depth = 0
    for char in value:
        if char == "<":
            depth += 1
        elif char == ">" and depth:
            depth -= 1
        elif depth == 0:
            result.append(char)
    return "".join(result)


def _expand_use_paths(raw: str) -> list[tuple[str, str, bool]]:
    text = raw.strip().removesuffix(";").strip()
    text = re.sub(r"^(?:pub(?:\s*\([^)]*\))?\s+)?use\s+", "", text).strip()
    if not text:
        return []

    def split_top_level(value: str) -> list[str]:
        parts: list[str] = []
        start = 0
        depth = 0
        for index, char in enumerate(value):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(value[start:index].strip())
                start = index + 1
        parts.append(value[start:].strip())
        return [part for part in parts if part]

    def expand(value: str, prefix: str = "") -> list[tuple[str, str, bool]]:
        value = value.strip()
        opening = value.find("{")
        if opening >= 0:
            depth = 0
            closing = -1
            for index in range(opening, len(value)):
                if value[index] == "{":
                    depth += 1
                elif value[index] == "}":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
            if closing >= 0:
                before = value[:opening].rstrip(":")
                base = "::".join(part for part in (prefix, before) if part)
                result: list[tuple[str, str, bool]] = []
                for item in split_top_level(value[opening + 1 : closing]):
                    result.extend(expand(item, base))
                return result
        if not value:
            return []
        if value == "self":
            path = prefix
            local = path.rsplit("::", 1)[-1] if path else "self"
            return [(path, local, False)] if path else []
        alias_match = re.match(r"^(.*?)\s+as\s+([A-Za-z_][\w]*)$", value)
        if alias_match:
            path = "::".join(part for part in (prefix, alias_match.group(1).strip()) if part)
            return [(path, alias_match.group(2), False)]
        path = "::".join(part for part in (prefix, value) if part)
        if path.endswith("::*"):
            return [(path.removesuffix("::*"), "", True)]
        local = path.rsplit("::", 1)[-1]
        return [(path, local, False)]

    return expand(text)


def _looks_local_path(path: str) -> bool:
    return path.startswith(("crate", "self", "super", "root"))


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
