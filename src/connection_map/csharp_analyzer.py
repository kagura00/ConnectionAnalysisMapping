"""Tree-sitter based static analyzer for C# source files."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .analysis_context import AnalysisContext, load_analysis_context
from .config import AnalysisConfig, discover_source_files, repository_id
from .contract import validate_document
from .model import GraphBuilder

ANALYZER_NAME = "connection-map-csharp-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"csharp"}

_TYPE_KINDS = {"class", "interface", "type"}
_SCOPE_KINDS = {"namespace", "class", "interface", "type", "method", "function", "lambda"}
_PRIMITIVE_TYPES = {
    "bool",
    "byte",
    "char",
    "decimal",
    "double",
    "float",
    "int",
    "long",
    "nint",
    "nuint",
    "object",
    "sbyte",
    "short",
    "string",
    "uint",
    "ulong",
    "ushort",
    "void",
    "var",
    "dynamic",
}
_TYPE_WRAPPER_TYPES = {
    "array_type",
    "function_pointer_type",
    "generic_name",
    "nullable_type",
    "pointer_type",
    "tuple_type",
    "type_parameter_list",
    "type_parameter_constraints_clause",
}
_DECLARATION_TYPES = {
    "delegate_declaration",
    "event_declaration",
    "field_declaration",
    "local_declaration_statement",
    "method_declaration",
    "parameter",
    "property_declaration",
}
_TYPE_DECLARATION_TYPES = {
    "class_declaration",
    "interface_declaration",
    "struct_declaration",
    "enum_declaration",
    "record_declaration",
}


class CSharpAnalyzerDependencyError(ValueError):
    """Raised when the optional C# parser dependency is unavailable."""


@dataclass(slots=True)
class CSharpFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str


@dataclass(frozen=True, slots=True)
class CSharpSymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str
    declaration_kind: str
    arity: int | None = None
    owner_q: str | None = None


@dataclass(slots=True)
class CSharpUsingInfo:
    aliases: dict[str, str] = field(default_factory=dict)
    direct: dict[str, str] = field(default_factory=dict)
    namespaces: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CSharpAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    analysis_context: AnalysisContext = field(default_factory=AnalysisContext)
    files: list[CSharpFile] = field(default_factory=list)
    files_by_path: dict[str, CSharpFile] = field(default_factory=dict)
    definitions: dict[str, CSharpSymbol] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    symbols_by_name: dict[str, list[CSharpSymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[CSharpSymbol]] = field(default_factory=dict)
    namespace_by_file: dict[str, str | None] = field(default_factory=dict)
    usings_by_file: dict[str, CSharpUsingInfo] = field(default_factory=dict)
    namespace_nodes: dict[str, list[str]] = field(default_factory=dict)
    namespace_node_by_file: dict[str, str] = field(default_factory=dict)
    variable_types: dict[tuple[str, str], str] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_module(self, csharp_file: CSharpFile) -> None:
        self.builder.add_node(
            {
                "id": csharp_file.module_id,
                "kind": "module",
                "qualified_name": csharp_file.relative_path,
                "display_name": csharp_file.relative_path,
                "file": csharp_file.relative_path,
                "span": _span_for_tree(csharp_file.tree.root_node),
                "parent_id": None,
                "visibility": "public",
                "extensions": {"language": "csharp", "grammar": "c_sharp"},
            }
        )

    def add_definition(
        self,
        csharp_file: CSharpFile,
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
    ) -> str:
        base_id = f"csharp:{csharp_file.relative_path}:{qualified_name}:{kind}"
        node_id = _unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": "csharp",
            "grammar": "c_sharp",
            "declaration_kind": declaration_kind,
        }
        if extensions:
            node_extensions.update(extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": csharp_file.relative_path,
            "span": _span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": _visibility_for_node(tree_node, csharp_file.source, kind),
            "signature": signature or _signature_for_node(tree_node, csharp_file.source),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        if kind in {"method", "function", "lambda"}:
            node["execution_kind"] = _execution_kind_for_node(tree_node, csharp_file.source)
        self.builder.add_node(node)
        symbol = CSharpSymbol(
            node_id=node_id,
            name="<init>" if extensions and extensions.get("constructor") else name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=csharp_file.relative_path,
            declaration_kind=declaration_kind,
            arity=arity,
            owner_q=_owner_qualified_name(self.definitions.get(parent_id)),
        )
        self.definitions[node_id] = symbol
        self.definition_by_node[(csharp_file.relative_path, tree_node.id)] = node_id
        self.symbols_by_name.setdefault(symbol.name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(qualified_name, []).append(symbol)
        if kind == "namespace":
            self.namespace_nodes.setdefault(qualified_name, []).append(node_id)
            self.namespace_node_by_file.setdefault(csharp_file.relative_path, node_id)
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

    def symbols_for_name(self, name: str, *, kinds: set[str] | None = None) -> list[CSharpSymbol]:
        candidates = list(self.symbols_by_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def symbols_for_qualified_name(self, name: str, *, kinds: set[str] | None = None) -> list[CSharpSymbol]:
        candidates = list(self.symbols_by_qualified_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def external_node(
        self,
        label: str,
        *,
        unknown: bool = False,
        context_source: str | None = None,
        csharp_file: CSharpFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (kind, label)
        if key in self._external_node_ids:
            return self._external_node_ids[key]
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"csharp:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": csharp_file.relative_path if unknown and csharp_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": "csharp",
                    "external_label": label,
                    **({"context_source": context_source} if context_source else {}),
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
        csharp_file: CSharpFile | None = None,
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
                "file": csharp_file.relative_path if csharp_file else file_path,
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
    """Analyze C# source without compiling or executing the target code."""

    active_config = config or AnalysisConfig(language="csharp")
    active_config.validate()
    if active_config.language != "csharp":
        raise ValueError("C# analyzer requires language = 'csharp'")
    if set(active_config.active_languages()) != SUPPORTED_LANGUAGES:
        raise ValueError("C# analyzer supports only csharp")

    root = root.resolve()
    builder = GraphBuilder()
    analysis_context = load_analysis_context(root, active_config)
    for diagnostic in analysis_context.diagnostics:
        builder.add_diagnostic({"code": diagnostic["code"], "severity": diagnostic["severity"], "message": diagnostic["message"], "file": None, "span": None, "details": {}})
    context = CSharpAnalysisContext(root, active_config, builder, analysis_context)
    files, skipped = discover_source_files(root, active_config, languages={"csharp"})
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped C# source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            csharp_file = _parse_file(path, root)
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
        context.files.append(csharp_file)
        context.files_by_path[csharp_file.relative_path] = csharp_file

    for csharp_file in context.files:
        context.add_module(csharp_file)
        _index_file_metadata(context, csharp_file)
        if csharp_file.tree.root_node.has_error:
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {csharp_file.relative_path}",
                csharp_file=csharp_file,
                tree_node=csharp_file.tree.root_node,
                details={"grammar": "c_sharp"},
            )

    for csharp_file in context.files:
        _collect_definitions(context, csharp_file)
    for csharp_file in context.files:
        _collect_relations(context, csharp_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "csharp",
        "languages": ["csharp"],
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
            "grammars": ["c_sharp"],
            "build_context": analysis_context.summary(),
        },
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": active_config.to_dict(),
    }
    document = builder.document(meta)
    validate_document(document)
    return document


@lru_cache(maxsize=2)
def _parser_for() -> Any:
    try:
        from tree_sitter_language_pack import get_parser
    except ModuleNotFoundError as exc:
        raise CSharpAnalyzerDependencyError(
            "C#解析には任意依存が必要です。'uv sync --extra dotnet' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser("csharp")
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise CSharpAnalyzerDependencyError(f"Tree-sitter grammar 'c_sharp'を読み込めません: {exc}") from exc


def _parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _parse_file(path: Path, root: Path) -> CSharpFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    return CSharpFile(
        path=path,
        relative_path=relative_path,
        source=source,
        tree=_parser_for().parse(source),
        module_id=f"csharp:{relative_path}:module",
    )


def _index_file_metadata(context: CSharpAnalysisContext, csharp_file: CSharpFile) -> None:
    namespace_name: str | None = None
    usings = CSharpUsingInfo()
    for node in _walk_tree(csharp_file.tree.root_node):
        if node.type in {"namespace_declaration", "file_scoped_namespace_declaration"}:
            name = _named_field(node, csharp_file.source, "name")
            if name and namespace_name is None:
                namespace_name = _clean_qualified_name(name)
        elif node.type == "using_directive":
            alias, name, is_namespace = _using_parts(node, csharp_file.source)
            if not name:
                continue
            if alias:
                usings.aliases[alias] = name
            elif is_namespace:
                usings.namespaces.append(name)
            else:
                usings.direct[name.rsplit(".", 1)[-1]] = name
    context.namespace_by_file[csharp_file.relative_path] = namespace_name
    context.usings_by_file[csharp_file.relative_path] = usings


def _collect_definitions(context: CSharpAnalysisContext, csharp_file: CSharpFile) -> None:
    for node in _walk_tree(csharp_file.tree.root_node):
        candidate = _definition_candidate(csharp_file, node)
        if candidate is None:
            continue
        kind, display_name, identity_name, declaration_kind, extensions, arity = candidate
        parent_id = _scope_parent(context, csharp_file, node)
        parent_id, qualified_name = _qualified_scope(context, csharp_file, node, identity_name, parent_id, kind)
        return_behavior = None
        return_sites = None
        if kind in {"method", "function"} and not extensions.get("constructor"):
            if declaration_kind == "definition":
                return_behavior, return_sites = _return_info(node)
            else:
                return_behavior = "unknown"
        context.add_definition(
            csharp_file,
            node,
            kind=kind,
            name=display_name,
            qualified_name=qualified_name,
            declaration_kind=declaration_kind,
            parent_id=parent_id,
            arity=arity,
            return_behavior=return_behavior,
            return_sites=return_sites,
            extensions=extensions,
        )


def _definition_candidate(
    csharp_file: CSharpFile,
    node: Any,
) -> tuple[str, str, str, str, dict[str, Any], int | None] | None:
    node_type = node.type
    if node_type in {"namespace_declaration", "file_scoped_namespace_declaration"}:
        name = _named_field(node, csharp_file.source, "name")
        if not name:
            return None
        name = _clean_qualified_name(name)
        return "namespace", name, name, "namespace", {}, None
    if node_type in _TYPE_DECLARATION_TYPES or node_type == "delegate_declaration":
        name = _named_field(node, csharp_file.source, "name")
        if not name:
            return None
        if node_type == "class_declaration":
            kind, declaration_kind, form = "class", "class", "class"
        elif node_type == "interface_declaration":
            kind, declaration_kind, form = "interface", "interface", "interface"
        elif node_type == "record_declaration":
            raw = _node_text(node, csharp_file.source)
            form = "record_struct" if re.search(r"\brecord\s+struct\b", raw) else "record"
            kind, declaration_kind = "type", "record"
        elif node_type == "struct_declaration":
            kind, declaration_kind, form = "type", "struct", "struct"
        elif node_type == "enum_declaration":
            kind, declaration_kind, form = "type", "enum", "enum"
        else:
            kind, declaration_kind, form = "type", "delegate", "delegate"
        return kind, name, name, declaration_kind, {"declaration_form": form}, None
    if node_type in {"method_declaration", "local_function_statement"}:
        name = _named_field(node, csharp_file.source, "name")
        if not name:
            return None
        arity = _parameter_arity(node)
        declaration_kind = "definition" if node.child_by_field_name("body") is not None else "declaration"
        kind = "function" if node_type == "local_function_statement" else "method"
        return kind, name, f"{name}({arity})", declaration_kind, {}, arity
    if node_type == "constructor_declaration":
        name = _named_field(node, csharp_file.source, "name") or "<constructor>"
        arity = _parameter_arity(node)
        declaration_kind = "definition" if node.child_by_field_name("body") is not None else "declaration"
        return (
            "method",
            name,
            f"<init>({arity})",
            declaration_kind,
            {"declaration_form": "constructor", "constructor": True},
            arity,
        )
    if node.type in {"operator_declaration", "conversion_operator_declaration"}:
        raw = _node_text(node, csharp_file.source)
        operator = _named_field(node, csharp_file.source, "operator") or _operator_name(raw)
        arity = _parameter_arity(node)
        if not operator:
            operator = "operator"
        declaration_kind = "definition" if node.child_by_field_name("body") is not None else "declaration"
        return "method", operator, f"{operator}({arity})", declaration_kind, {"declaration_form": "operator"}, arity
    if node_type == "lambda_expression":
        start_line = int(node.start_point[0]) + 1
        start_col = int(node.start_point[1])
        name = f"<lambda@{start_line}:{start_col}>"
        return "lambda", name, name, "lambda", {"declaration_form": "lambda"}, None
    return None


def _scope_parent(context: CSharpAnalysisContext, csharp_file: CSharpFile, node: Any) -> str:
    current = node.parent
    while current is not None:
        node_id = context.definition_by_node.get((csharp_file.relative_path, current.id))
        if node_id is not None:
            symbol = context.definitions[node_id]
            if symbol.kind in _SCOPE_KINDS:
                return node_id
        current = current.parent
    return csharp_file.module_id


def _qualified_scope(
    context: CSharpAnalysisContext,
    csharp_file: CSharpFile,
    node: Any,
    name: str,
    parent_id: str,
    kind: str,
) -> tuple[str, str]:
    parent_symbol = context.definitions.get(parent_id)
    if parent_symbol is not None and parent_symbol.kind != "module":
        return parent_id, f"{parent_symbol.qualified_name}.{name}"
    if kind == "namespace":
        return parent_id, name
    namespace_id = context.namespace_node_by_file.get(csharp_file.relative_path)
    namespace_name = context.namespace_by_file.get(csharp_file.relative_path)
    if namespace_id and namespace_name:
        namespace_symbol = context.definitions.get(namespace_id)
        if namespace_symbol:
            return namespace_id, f"{namespace_symbol.qualified_name}.{name}"
    return parent_id, name


def _collect_relations(context: CSharpAnalysisContext, csharp_file: CSharpFile) -> None:
    for node in _walk_tree(csharp_file.tree.root_node):
        if node.type == "using_directive":
            _collect_import(context, csharp_file, node)
        elif node.type in _TYPE_DECLARATION_TYPES:
            _collect_inheritance(context, csharp_file, node)
        elif node.type in _DECLARATION_TYPES:
            _collect_declared_type_uses(context, csharp_file, node)
        elif node.type == "invocation_expression":
            _collect_invocation(context, csharp_file, node)
        elif node.type == "object_creation_expression":
            _collect_object_creation(context, csharp_file, node)
        elif node.type == "constructor_initializer":
            _collect_constructor_initializer(context, csharp_file, node)


def _collect_import(context: CSharpAnalysisContext, csharp_file: CSharpFile, node: Any) -> None:
    raw = _node_text(node, csharp_file.source).strip()
    alias, name, is_namespace = _using_parts(node, csharp_file.source)
    if not name:
        target = context.external_node(f"import:{raw}", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, csharp_file, node, "unresolved_import", f"C# usingを解釈できません: {raw}")
        status, confidence = "unresolved", 0.2
    else:
        target, status, confidence = _resolve_import(context, csharp_file, name, node, is_namespace=is_namespace)
    _add_relation(
        context,
        csharp_file.module_id,
        target,
        "imports",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={
            "import": raw,
            "name": name,
            "alias": alias,
            "static": bool(re.search(r"\bstatic\b", raw)),
            "global": bool(re.search(r"\bglobal\s+using\b", raw)),
        },
    )


def _resolve_import(
    context: CSharpAnalysisContext,
    csharp_file: CSharpFile,
    name: str,
    node: Any,
    *,
    is_namespace: bool,
) -> tuple[str, str, float]:
    if is_namespace:
        namespace_candidates = context.namespace_nodes.get(name, [])
        if namespace_candidates:
            return min(namespace_candidates), "resolved", 1.0
        type_candidates = _type_candidates(context, csharp_file, name)
        if len(type_candidates) == 1:
            return type_candidates[0].node_id, "resolved", 1.0
        if len(type_candidates) > 1:
            target = context.external_node(
                f"import:{name}", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node)
            )
            _diagnose_unresolved(context, csharp_file, node, "unresolved_import", f"C# usingを一意に解決できません: {name}")
            return target, "unresolved", 0.2
        return context.external_node(f"import:{name}"), "external", 0.75
    candidates = _type_candidates(context, csharp_file, name)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(f"import:{name}", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, csharp_file, node, "unresolved_import", f"C# usingを一意に解決できません: {name}")
        return target, "unresolved", 0.2
    return context.external_node(f"import:{name}"), "external", 0.75


def _collect_inheritance(context: CSharpAnalysisContext, csharp_file: CSharpFile, node: Any) -> None:
    owner_id = context.definition_by_node.get((csharp_file.relative_path, node.id))
    if owner_id is None:
        return
    base_list = next((child for child in node.named_children if child.type == "base_list"), None)
    if base_list is None:
        return
    for child in base_list.named_children:
        reference = _primary_type_name(child, csharp_file.source)
        if not reference:
            continue
        target, status, confidence = _resolve_type(context, csharp_file, reference, child)
        _add_relation(
            context,
            owner_id,
            target,
            "inherits",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(child),
            detail={"reference": reference, "role": "base_list"},
        )


def _collect_declared_type_uses(context: CSharpAnalysisContext, csharp_file: CSharpFile, node: Any) -> None:
    type_node = _declared_type_node(node)
    if type_node is None:
        return
    source_id = _enclosing_definition(context, csharp_file, node) or csharp_file.module_id
    for reference, reference_node in _type_references(type_node, csharp_file.source):
        if reference in _PRIMITIVE_TYPES:
            continue
        target, status, confidence = _resolve_type(context, csharp_file, reference, reference_node)
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
    primary_reference = _primary_type_name(type_node, csharp_file.source)
    if primary_reference:
        for variable_name in _declarator_names(node, csharp_file.source):
            context.variable_types[(source_id, variable_name)] = primary_reference


def _declared_type_node(node: Any) -> Any | None:
    if node.type == "method_declaration":
        return node.child_by_field_name("returns")
    if node.type == "delegate_declaration":
        return node.child_by_field_name("type")
    if node.type in {"field_declaration", "local_declaration_statement"}:
        declaration = next((child for child in node.named_children if child.type == "variable_declaration"), None)
        return declaration.child_by_field_name("type") if declaration is not None else None
    return node.child_by_field_name("type")


def _resolve_type(
    context: CSharpAnalysisContext,
    csharp_file: CSharpFile,
    reference: str,
    node: Any,
) -> tuple[str, str, float]:
    candidates = _type_candidates(context, csharp_file, reference)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(f"type:{reference}", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, csharp_file, node, "unresolved_type", f"C#型を一意に解決できません: {reference}")
        return target, "unresolved", 0.2
    if context.analysis_context.type_index.has_type(reference):
        return context.external_node(f"type:{reference}", context_source="classpath"), "external", 0.8
    return context.external_node(f"type:{reference}"), "external", 0.7


def _type_candidates(
    context: CSharpAnalysisContext,
    csharp_file: CSharpFile,
    reference: str,
) -> list[CSharpSymbol]:
    reference = _clean_type_reference(reference)
    if not reference or reference in _PRIMITIVE_TYPES:
        return []
    imports = context.usings_by_file.get(csharp_file.relative_path, CSharpUsingInfo())
    possible_names: list[str] = []
    alias = imports.aliases.get(reference)
    if alias:
        possible_names.append(alias)
    direct = imports.direct.get(reference)
    if direct:
        possible_names.append(direct)
    if "." in reference:
        possible_names.append(reference)
        namespace_name = context.namespace_by_file.get(csharp_file.relative_path)
        if namespace_name:
            possible_names.append(f"{namespace_name}.{reference}")
    else:
        namespace_name = context.namespace_by_file.get(csharp_file.relative_path)
        if namespace_name:
            possible_names.append(f"{namespace_name}.{reference}")
        possible_names.extend(f"{namespace}.{reference}" for namespace in imports.namespaces)
    candidates: list[CSharpSymbol] = []
    for name in possible_names:
        candidates.extend(context.symbols_for_qualified_name(name, kinds=_TYPE_KINDS))
    if not candidates:
        candidates = context.symbols_for_name(reference.rsplit(".", 1)[-1], kinds=_TYPE_KINDS)
    return _unique_symbols(candidates)


def _collect_invocation(context: CSharpAnalysisContext, csharp_file: CSharpFile, node: Any) -> None:
    caller = _enclosing_definition(context, csharp_file, node) or csharp_file.module_id
    function_node = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    name, receiver = _invocation_parts(function_node, csharp_file.source)
    arity = _argument_arity(arguments)
    expression = _node_text(node, csharp_file.source).strip()
    target, status, confidence = _resolve_method_call(
        context,
        csharp_file,
        caller,
        name,
        arity,
        receiver,
        node,
    )
    if status == "unresolved":
        _diagnose_unresolved(
            context,
            csharp_file,
            node,
            "unresolved_call",
            f"C#呼び出しを静的に解決できません: {expression}",
            node_id=caller,
            details={"expression": expression, "callee": name, "arity": arity, "receiver": receiver},
        )
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"expression": expression, "callee": name, "arity": arity, "receiver": receiver},
    )


def _resolve_method_call(
    context: CSharpAnalysisContext,
    csharp_file: CSharpFile,
    caller: str,
    name: str,
    arity: int,
    receiver: str,
    node: Any,
) -> tuple[str, str, float]:
    if not name:
        return context.external_node("call:<unknown>", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node)), "unresolved", 0.1
    caller_symbol = context.definitions.get(caller)
    owner_q = caller_symbol.owner_q if caller_symbol else None
    if caller_symbol and caller_symbol.kind in _TYPE_KINDS:
        owner_q = caller_symbol.qualified_name
    if receiver in {"", "this"}:
        candidates = _methods_for_owner(context, owner_q, name, arity)
        if not candidates:
            candidates = _methods_for_name(context, name, arity)
    elif receiver == "base":
        candidates = _methods_for_name(context, name, arity)
    else:
        owner_reference = _receiver_type_reference(context, csharp_file, caller, receiver)
        if owner_reference is None:
            return (
                context.external_node(f"call:{receiver}.{name}/{arity}", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node)),
                "unresolved",
                0.2,
            )
        type_candidates = _type_candidates(context, csharp_file, owner_reference)
        if not type_candidates:
            if context.analysis_context.type_index.has_method(owner_reference, name):
                return (
                    context.external_node(
                        f"call:{receiver}.{name}/{arity}",
                        context_source="classpath",
                    ),
                    "external",
                    0.8,
                )
            return (
                context.external_node(f"call:{receiver}.{name}/{arity}", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node)),
                "unresolved",
                0.2,
            )
        candidates = []
        for type_symbol in type_candidates:
            candidates.extend(_methods_for_owner(context, type_symbol.qualified_name, name, arity))
    candidates = _unique_symbols(candidates)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        return (
            context.external_node(
                f"call:{receiver + '.' if receiver else ''}{name}/{arity}",
                unknown=True,
                csharp_file=csharp_file,
                span=_span_for_tree(node),
            ),
            "unresolved",
            0.2,
        )
    return context.external_node(f"call:{receiver + '.' if receiver else ''}{name}/{arity}"), "external", 0.7


def _collect_object_creation(context: CSharpAnalysisContext, csharp_file: CSharpFile, node: Any) -> None:
    caller = _enclosing_definition(context, csharp_file, node) or csharp_file.module_id
    type_node = node.child_by_field_name("type")
    reference = _primary_type_name(type_node, csharp_file.source) if type_node is not None else ""
    arity = _argument_arity(node.child_by_field_name("arguments"))
    expression = _node_text(node, csharp_file.source).strip()
    candidates = _type_candidates(context, csharp_file, reference) if reference else []
    constructors: list[CSharpSymbol] = []
    for type_symbol in candidates:
        constructors.extend(_methods_for_owner(context, type_symbol.qualified_name, "<init>", arity))
    constructors = _unique_symbols(constructors)
    if len(constructors) == 1:
        target, status, confidence = constructors[0].node_id, "resolved", 1.0
    elif len(constructors) > 1:
        target = context.external_node(f"new:{reference}/{arity}", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
        _diagnose_unresolved(context, csharp_file, node, "unresolved_call", f"C#コンストラクタを一意に解決できません: {expression}", node_id=caller)
    elif reference:
        target = context.external_node(
            f"new:{reference}/{arity}",
            context_source="classpath" if context.analysis_context.type_index.has_type(reference) else None,
        )
        status, confidence = "external", 0.7
    else:
        target = context.external_node("new:<unknown>", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
        _diagnose_unresolved(context, csharp_file, node, "unresolved_call", f"C#コンストラクタを解決できません: {expression}", node_id=caller)
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"expression": expression, "callee": f"{reference}.<init>", "arity": arity},
    )


def _collect_constructor_initializer(context: CSharpAnalysisContext, csharp_file: CSharpFile, node: Any) -> None:
    caller = _enclosing_definition(context, csharp_file, node) or csharp_file.module_id
    raw = _node_text(node, csharp_file.source).strip()
    callee = "base" if re.search(r"\bbase\s*\(", raw) else "this"
    arity = _argument_arity(next((child for child in node.named_children if child.type == "argument_list"), None))
    owner_q = context.definitions.get(caller).owner_q if context.definitions.get(caller) else None
    candidates = _methods_for_owner(context, owner_q, "<init>", arity) if callee == "this" else []
    candidates = _unique_symbols(candidates)
    if len(candidates) == 1:
        target, status, confidence = candidates[0].node_id, "resolved", 1.0
    elif len(candidates) > 1:
        target = context.external_node(f"call:{callee}/{arity}", unknown=True, csharp_file=csharp_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
    else:
        target = context.external_node(f"call:{callee}/{arity}")
        status, confidence = "external", 0.7
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"expression": raw, "callee": callee, "arity": arity},
    )


def _methods_for_name(context: CSharpAnalysisContext, name: str, arity: int) -> list[CSharpSymbol]:
    return [symbol for symbol in context.symbols_for_name(name, kinds={"method", "function"}) if symbol.arity == arity]


def _methods_for_owner(context: CSharpAnalysisContext, owner_q: str | None, name: str, arity: int) -> list[CSharpSymbol]:
    if not owner_q:
        return []
    return [symbol for symbol in _methods_for_name(context, name, arity) if symbol.owner_q == owner_q]


def _receiver_type_reference(
    context: CSharpAnalysisContext,
    csharp_file: CSharpFile,
    caller: str,
    receiver: str,
) -> str | None:
    cleaned = receiver.strip()
    if not cleaned:
        return None
    new_match = re.match(r"new\s+([A-Za-z_][\w.]*)", cleaned)
    if new_match:
        return new_match.group(1)
    cleaned = cleaned.removeprefix("this.")
    simple_name = cleaned.rsplit(".", 1)[-1]
    current = caller
    while current:
        reference = context.variable_types.get((current, simple_name))
        if reference:
            return reference
        symbol = context.definitions.get(current)
        if symbol is None:
            break
        owner_q = symbol.owner_q
        if owner_q:
            type_symbols = context.symbols_for_qualified_name(owner_q, kinds=_TYPE_KINDS)
            for type_symbol in type_symbols:
                reference = context.variable_types.get((type_symbol.node_id, simple_name))
                if reference:
                    return reference
        current = None
    if re.fullmatch(r"[A-Za-z_][\w.]*", cleaned):
        return cleaned
    return None


def _enclosing_definition(context: CSharpAnalysisContext, csharp_file: CSharpFile, node: Any) -> str | None:
    current = node
    while current is not None:
        node_id = context.definition_by_node.get((csharp_file.relative_path, current.id))
        if node_id is not None:
            return node_id
        current = current.parent
    return None


def _return_info(node: Any) -> tuple[str, list[dict[str, Any]]]:
    sites: list[dict[str, Any]] = []
    has_value = False
    has_none = False
    for child in _walk_tree(node):
        if child.type == "return_statement":
            value = bool(child.named_children)
            has_value = has_value or value
            has_none = has_none or not value
            sites.append({"span": _span_for_tree(child), "value_kind": "value" if value else "none"})
        elif child.type == "yield_return_statement":
            has_value = True
            sites.append({"span": _span_for_tree(child), "value_kind": "yield_value"})
    if not sites and any(child.type == "arrow_expression_clause" for child in _walk_tree(node)):
        return "returns_value", []
    if not sites:
        return "no_explicit_return", []
    if has_value and has_none:
        return "mixed", sites
    return ("returns_value" if has_value else "returns_none"), sites


def _type_references(node: Any, source: bytes) -> list[tuple[str, Any]]:
    references: list[tuple[str, Any]] = []

    def visit(current: Any) -> None:
        if current.type in {"generic_name", "qualified_name"}:
            reference = _primary_type_name(current, source)
            if reference:
                references.append((reference, current))
            if current.type == "generic_name":
                for child in current.named_children:
                    if child.type not in {"identifier", "type_argument_list"}:
                        visit(child)
                arguments = next((child for child in current.named_children if child.type == "type_argument_list"), None)
                if arguments is not None:
                    for child in arguments.named_children:
                        visit(child)
            return
        if current.type in {"identifier", "predefined_type"}:
            reference = _primary_type_name(current, source)
            if reference:
                references.append((reference, current))
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


def _declarator_names(node: Any, source: bytes) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for child in _walk_tree(node):
        if child.type not in {"variable_declarator", "parameter"}:
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text(name_node, source).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _primary_type_name(node: Any | None, source: bytes) -> str:
    if node is None:
        return ""
    raw = _node_text(node, source).strip()
    if not raw:
        return ""
    raw = re.sub(r"\s+", "", raw)
    raw = raw.removeprefix("global::")
    raw = raw.replace("?", "").replace("[]", "").replace("*", "")
    raw = _strip_generic_arguments(raw)
    if raw in _PRIMITIVE_TYPES:
        return raw
    if re.fullmatch(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*", raw):
        return raw
    return ""


def _clean_type_reference(value: str) -> str:
    return _primary_type_name_from_text(value)


def _primary_type_name_from_text(value: str) -> str:
    raw = value.strip().replace("global::", "")
    raw = re.sub(r"\s+", "", raw).replace("?", "").replace("[]", "").replace("*", "")
    raw = _strip_generic_arguments(raw)
    return raw


def _clean_qualified_name(value: str) -> str:
    return value.strip().replace("global::", "").replace(" ", "")


def _strip_generic_arguments(value: str) -> str:
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


def _using_parts(node: Any, source: bytes) -> tuple[str | None, str, bool]:
    raw = _node_text(node, source).strip().rstrip(";").strip()
    raw = re.sub(r"^global\s+", "", raw)
    raw = re.sub(r"^using\s+", "", raw)
    is_static = False
    if raw.startswith("static "):
        is_static = True
        raw = raw[7:].strip()
    alias: str | None = None
    if "=" in raw:
        alias, raw = (part.strip() for part in raw.split("=", 1))
    name = _clean_qualified_name(raw)
    is_namespace = bool(name) and not is_static and alias is None
    return alias, name, is_namespace


def _named_field(node: Any, source: bytes, field_name: str) -> str | None:
    child = node.child_by_field_name(field_name)
    if child is None:
        return None
    return _node_text(child, source).strip() or None


def _parameter_arity(node: Any) -> int:
    parameters = node.child_by_field_name("parameters")
    if parameters is None:
        parameters = next((child for child in node.named_children if child.type == "parameter_list"), None)
    if parameters is None:
        return 0
    return sum(child.type == "parameter" for child in parameters.named_children)


def _argument_arity(node: Any | None) -> int:
    if node is None:
        return 0
    return len(node.named_children)


def _invocation_parts(node: Any | None, source: bytes) -> tuple[str, str]:
    if node is None:
        return "", ""
    if node.type == "member_access_expression":
        name_node = node.child_by_field_name("name")
        expression_node = node.child_by_field_name("expression")
        return (
            _node_text(name_node, source).strip() if name_node is not None else "",
            _node_text(expression_node, source).strip() if expression_node is not None else "",
        )
    return _node_text(node, source).strip(), ""


def _operator_name(raw: str) -> str | None:
    match = re.search(r"\boperator\s+([^\s(]+)", raw)
    return f"operator {match.group(1)}" if match else None


def _visibility_for_node(node: Any, source: bytes, kind: str) -> str:
    raw = _node_text(node, source)
    if re.search(r"\bpublic\b", raw):
        return "public"
    if re.search(r"\bprivate\b|\bprotected\b", raw):
        return "private"
    if kind in {"namespace", "module"}:
        return "public"
    return "unknown"


def _execution_kind_for_node(node: Any, source: bytes) -> str:
    raw = _node_text(node, source)
    has_async = bool(re.search(r"\basync\b", raw))
    has_yield = any(child.type == "yield_return_statement" for child in _walk_tree(node))
    if has_async and has_yield:
        return "async_generator"
    if has_yield:
        return "generator"
    return "async" if has_async else "sync"


def _owner_qualified_name(symbol: CSharpSymbol | None) -> str | None:
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS:
        return symbol.qualified_name
    return symbol.owner_q


def _unique_symbols(symbols: Iterable[CSharpSymbol]) -> list[CSharpSymbol]:
    result: list[CSharpSymbol] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol.node_id not in seen:
            seen.add(symbol.node_id)
            result.append(symbol)
    return result


def _node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _signature_for_node(node: Any, source: bytes, limit: int = 240) -> str:
    for line in _node_text(node, source).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("["):
            return stripped[:limit]
    return _node_text(node, source).strip()[:limit]


def _walk_tree(node: Any) -> Iterable[Any]:
    yield node
    for child in node.children:
        yield from _walk_tree(child)


def _span_for_tree(node: Any) -> dict[str, int] | None:
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
    context: CSharpAnalysisContext,
    source_id: str,
    target_id: str,
    relation_type: str,
    *,
    resolution_status: str,
    confidence: float,
    source_span: dict[str, int] | None,
    detail: dict[str, Any],
) -> str:
    identity = "\x1f".join(
        [source_id, target_id, relation_type, repr(source_span), repr(sorted(detail.items()))]
    )
    edge_id = f"csharp-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
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
    context: CSharpAnalysisContext,
    csharp_file: CSharpFile,
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
        csharp_file=csharp_file,
        tree_node=node,
        node_id=node_id,
        details=details,
    )


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
