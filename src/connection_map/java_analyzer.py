"""Tree-sitter based static analyzer for Java source files."""

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

ANALYZER_NAME = "connection-map-java-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"java"}

_TYPE_KINDS = {"class", "interface", "type"}
_SCOPE_KINDS = {"namespace", "class", "interface", "type", "method", "lambda"}
_PRIMITIVE_TYPES = {
    "byte",
    "short",
    "int",
    "long",
    "float",
    "double",
    "boolean",
    "char",
    "void",
    "var",
}
_TYPE_NODE_TYPES = {
    "annotated_type",
    "array_type",
    "generic_type",
    "intersection_type",
    "scoped_type_identifier",
    "type_identifier",
    "type_parameter",
    "type_bound",
    "union_type",
    "wildcard",
}
_DECLARATION_TYPES = {
    "field_declaration",
    "formal_parameter",
    "local_variable_declaration",
    "resource",
    "catch_formal_parameter",
    "method_declaration",
}
_TYPE_WRAPPER_TYPES = {
    "array_type",
    "annotated_type",
    "intersection_type",
    "type_arguments",
    "type_bound",
    "union_type",
    "wildcard",
}


class JavaAnalyzerDependencyError(ValueError):
    """Raised when the optional Java parser dependency is unavailable."""


@dataclass(slots=True)
class JavaFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str


@dataclass(frozen=True, slots=True)
class JavaSymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str
    declaration_kind: str
    arity: int | None = None
    owner_q: str | None = None


@dataclass(slots=True)
class JavaImportInfo:
    direct: dict[str, str] = field(default_factory=dict)
    wildcards: list[str] = field(default_factory=list)


@dataclass(slots=True)
class JavaAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    analysis_context: AnalysisContext = field(default_factory=AnalysisContext)
    files: list[JavaFile] = field(default_factory=list)
    files_by_path: dict[str, JavaFile] = field(default_factory=dict)
    definitions: dict[str, JavaSymbol] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    symbols_by_name: dict[str, list[JavaSymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[JavaSymbol]] = field(default_factory=dict)
    package_by_file: dict[str, str | None] = field(default_factory=dict)
    imports_by_file: dict[str, JavaImportInfo] = field(default_factory=dict)
    package_nodes: dict[str, list[str]] = field(default_factory=dict)
    package_node_by_file: dict[str, str] = field(default_factory=dict)
    variable_types: dict[tuple[str, str], str] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_module(self, java_file: JavaFile) -> None:
        root = java_file.tree.root_node
        self.builder.add_node(
            {
                "id": java_file.module_id,
                "kind": "module",
                "qualified_name": java_file.relative_path,
                "display_name": java_file.relative_path,
                "file": java_file.relative_path,
                "span": _span_for_tree(root),
                "parent_id": None,
                "visibility": "public",
                "extensions": {"language": "java", "grammar": "java"},
            }
        )

    def add_definition(
        self,
        java_file: JavaFile,
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
        base_id = f"java:{java_file.relative_path}:{qualified_name}:{kind}"
        node_id = _unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": "java",
            "grammar": "java",
            "declaration_kind": declaration_kind,
        }
        if extensions:
            node_extensions.update(extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": java_file.relative_path,
            "span": _span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": _visibility_for_node(tree_node, java_file.source, kind),
            "signature": signature or _signature_for_node(tree_node, java_file.source),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        if kind in {"method", "lambda"}:
            node.setdefault("execution_kind", "sync")
        self.builder.add_node(node)
        symbol = JavaSymbol(
            node_id=node_id,
            name="<init>" if extensions and extensions.get("constructor") else name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=java_file.relative_path,
            declaration_kind=declaration_kind,
            arity=arity,
            owner_q=_owner_qualified_name(self.definitions.get(parent_id)),
        )
        self.definitions[node_id] = symbol
        self.definition_by_node[(java_file.relative_path, tree_node.id)] = node_id
        self.symbols_by_name.setdefault(symbol.name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(qualified_name, []).append(symbol)
        if kind == "namespace":
            self.package_nodes.setdefault(qualified_name, []).append(node_id)
            self.package_node_by_file[java_file.relative_path] = node_id
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

    def symbols_for_name(self, name: str, *, kinds: set[str] | None = None) -> list[JavaSymbol]:
        candidates = list(self.symbols_by_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def symbols_for_qualified_name(self, name: str, *, kinds: set[str] | None = None) -> list[JavaSymbol]:
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
        java_file: JavaFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (kind, label)
        existing = self._external_node_ids.get(key)
        if existing:
            return existing
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"java:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": java_file.relative_path if unknown and java_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": "java",
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
        java_file: JavaFile | None = None,
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
                "file": java_file.relative_path if java_file else file_path,
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
    """Analyze Java source without compiling or executing the target code."""

    active_config = config or AnalysisConfig(language="java")
    active_config.validate()
    if active_config.language != "java":
        raise ValueError("Java analyzer requires language = 'java'")
    if set(active_config.active_languages()) != SUPPORTED_LANGUAGES:
        raise ValueError("Java analyzer supports only java")

    root = root.resolve()
    builder = GraphBuilder()
    analysis_context = load_analysis_context(root, active_config)
    for diagnostic in analysis_context.diagnostics:
        builder.add_diagnostic({"code": diagnostic["code"], "severity": diagnostic["severity"], "message": diagnostic["message"], "file": None, "span": None, "details": {}})
    context = JavaAnalysisContext(root, active_config, builder, analysis_context)
    files, skipped = discover_source_files(root, active_config, languages={"java"})
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped Java source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            java_file = _parse_file(path, root)
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
        context.files.append(java_file)
        context.files_by_path[java_file.relative_path] = java_file

    for java_file in context.files:
        context.add_module(java_file)
        _index_file_metadata(context, java_file)
        if java_file.tree.root_node.has_error:
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {java_file.relative_path}",
                java_file=java_file,
                tree_node=java_file.tree.root_node,
                details={"grammar": "java"},
            )

    for java_file in context.files:
        _collect_definitions(context, java_file)
    for java_file in context.files:
        _collect_relations(context, java_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "java",
        "languages": ["java"],
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
            "grammars": ["java"],
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
        raise JavaAnalyzerDependencyError(
            "Java解析には任意依存が必要です。'uv sync --extra jvm' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser("java")
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise JavaAnalyzerDependencyError(f"Tree-sitter grammar 'java' を読み込めません: {exc}") from exc


def _parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _parse_file(path: Path, root: Path) -> JavaFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    tree = _parser_for().parse(source)
    return JavaFile(
        path=path,
        relative_path=relative_path,
        source=source,
        tree=tree,
        module_id=f"java:{relative_path}:module",
    )


def _index_file_metadata(context: JavaAnalysisContext, java_file: JavaFile) -> None:
    package_name: str | None = None
    imports = JavaImportInfo()
    for node in java_file.tree.root_node.named_children:
        if node.type == "package_declaration":
            package_name = _package_or_import_name(node, java_file.source)
        elif node.type == "import_declaration":
            import_name = _package_or_import_name(node, java_file.source)
            if not import_name:
                continue
            if import_name.endswith(".*"):
                imports.wildcards.append(import_name[:-2])
            else:
                imports.direct[import_name.rsplit(".", 1)[-1]] = import_name
    context.package_by_file[java_file.relative_path] = package_name
    context.imports_by_file[java_file.relative_path] = imports


def _collect_definitions(context: JavaAnalysisContext, java_file: JavaFile) -> None:
    for node in _walk_tree(java_file.tree.root_node):
        candidate = _definition_candidate(java_file, node)
        if candidate is None:
            continue
        kind, display_name, identity_name, declaration_kind, extensions, arity = candidate
        parent_id = _scope_parent(context, java_file, node)
        parent_id, qualified_name = _qualified_scope(context, java_file, node, identity_name, parent_id, kind)
        return_behavior = None
        return_sites = None
        if kind == "method" and not extensions.get("constructor"):
            if declaration_kind == "definition":
                return_behavior, return_sites = _return_info(node)
            else:
                return_behavior = "unknown"
        context.add_definition(
            java_file,
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
    java_file: JavaFile,
    node: Any,
) -> tuple[str, str, str, str, dict[str, Any], int | None] | None:
    node_type = node.type
    if node_type == "package_declaration":
        name = _package_or_import_name(node, java_file.source)
        return ("namespace", name or "<default>", name or "<default>", "package", {}, None)
    if node_type in {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }:
        name = _named_field(node, java_file.source, "name")
        if not name:
            return None
        if node_type == "class_declaration":
            kind = "class"
            declaration_kind = "class"
            form = "class"
        elif node_type == "interface_declaration":
            kind = "interface"
            declaration_kind = "interface"
            form = "interface"
        elif node_type == "annotation_type_declaration":
            kind = "interface"
            declaration_kind = "annotation"
            form = "annotation"
        elif node_type == "record_declaration":
            kind = "type"
            declaration_kind = "record"
            form = "record"
        else:
            kind = "type"
            declaration_kind = "enum"
            form = "enum"
        return (kind, name, name, declaration_kind, {"declaration_form": form}, None)
    if node_type in {"method_declaration", "constructor_declaration", "compact_constructor_declaration"}:
        name = _named_field(node, java_file.source, "name") or "<constructor>"
        arity = _parameter_arity(node)
        if node_type == "method_declaration":
            declaration_kind = "definition" if node.child_by_field_name("body") is not None else "declaration"
            return (
                "method",
                name,
                f"{name}({arity})",
                declaration_kind,
                {},
                arity,
            )
        return (
            "method",
            name,
            f"<init>({arity})",
            "definition" if node.child_by_field_name("body") is not None else "declaration",
            {"declaration_form": "constructor", "constructor": True},
            arity,
        )
    if node_type == "lambda_expression":
        start_line = int(node.start_point[0]) + 1
        start_col = int(node.start_point[1])
        name = f"<lambda@{start_line}:{start_col}>"
        return ("lambda", name, name, "lambda", {"declaration_form": "lambda"}, None)
    return None


def _scope_parent(context: JavaAnalysisContext, java_file: JavaFile, node: Any) -> str:
    current = node.parent
    while current is not None:
        node_id = context.definition_by_node.get((java_file.relative_path, current.id))
        if node_id is not None:
            symbol = context.definitions[node_id]
            if symbol.kind in _SCOPE_KINDS:
                return node_id
        current = current.parent
    return java_file.module_id


def _qualified_scope(
    context: JavaAnalysisContext,
    java_file: JavaFile,
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
    package_name = context.package_by_file.get(java_file.relative_path)
    if package_name:
        package_id = context.package_node_by_file.get(java_file.relative_path)
        if package_id is not None:
            package_symbol = context.definitions[package_id]
            return package_id, f"{package_symbol.qualified_name}.{name}"
        return parent_id, f"{package_name}.{name}"
    return parent_id, name


def _collect_relations(context: JavaAnalysisContext, java_file: JavaFile) -> None:
    for node in _walk_tree(java_file.tree.root_node):
        if node.type == "import_declaration":
            _collect_import(context, java_file, node)
        elif node.type in {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
        }:
            _collect_inheritance(context, java_file, node)
        elif node.type in _DECLARATION_TYPES:
            _collect_declared_type_uses(context, java_file, node)
        elif node.type == "method_invocation":
            _collect_method_invocation(context, java_file, node)
        elif node.type == "object_creation_expression":
            _collect_object_creation(context, java_file, node)
        elif node.type == "explicit_constructor_invocation":
            _collect_explicit_constructor_invocation(context, java_file, node)


def _collect_import(context: JavaAnalysisContext, java_file: JavaFile, node: Any) -> None:
    raw = _node_text(node, java_file.source).strip()
    name = _package_or_import_name(node, java_file.source)
    if not name:
        target = context.external_node(f"import:{raw}", unknown=True, java_file=java_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, java_file, node, "unresolved_import", f"Java importを解釈できません: {raw}")
        status = "unresolved"
        confidence = 0.2
    else:
        target, status, confidence = _resolve_import(context, java_file, name, node)
    _add_relation(
        context,
        java_file.module_id,
        target,
        "imports",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"import": raw, "name": name, "static": bool(re.search(r"\bstatic\b", raw))},
    )


def _resolve_import(
    context: JavaAnalysisContext,
    java_file: JavaFile,
    name: str,
    node: Any,
) -> tuple[str, str, float]:
    wildcard = name.endswith(".*")
    base_name = name[:-2] if wildcard else name
    if wildcard:
        package_nodes = context.package_nodes.get(base_name, [])
        if package_nodes:
            target = min(package_nodes)
            return target, "resolved", 1.0
        return context.external_node(f"import:{name}"), "external", 0.75

    type_candidates = context.symbols_for_qualified_name(base_name, kinds=_TYPE_KINDS)
    method_candidates = [
        symbol
        for qualified, symbols in context.symbols_by_qualified_name.items()
        if qualified.startswith(f"{base_name}(")
        for symbol in symbols
        if symbol.kind == "method"
    ]
    candidates = type_candidates + method_candidates
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(f"import:{name}", unknown=True, java_file=java_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, java_file, node, "unresolved_import", f"Java importを一意に解決できません: {name}")
        return target, "unresolved", 0.2

    package_name = base_name.rsplit(".", 1)[0] if "." in base_name else ""
    if package_name in context.package_nodes:
        target = context.external_node(f"import:{name}", unknown=True, java_file=java_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, java_file, node, "unresolved_import", f"ローカルJava importを解決できません: {name}")
        return target, "unresolved", 0.2
    return context.external_node(f"import:{name}"), "external", 0.75


def _collect_inheritance(context: JavaAnalysisContext, java_file: JavaFile, node: Any) -> None:
    owner_id = context.definition_by_node.get((java_file.relative_path, node.id))
    if owner_id is None:
        return
    fields = ("superclass", "interfaces")
    if node.type == "interface_declaration":
        fields = ("interfaces",)
    for field_name in fields:
        wrapper = node.child_by_field_name(field_name)
        if wrapper is None:
            continue
        for child in _inheritance_type_nodes(wrapper):
            reference = _primary_type_name(child, java_file.source)
            if not reference:
                continue
            target, status, confidence = _resolve_type(context, java_file, reference, child)
            _add_relation(
                context,
                owner_id,
                target,
                "inherits",
                resolution_status=status,
                confidence=confidence,
                source_span=_span_for_tree(child),
                detail={"reference": reference, "role": field_name},
            )


def _collect_declared_type_uses(context: JavaAnalysisContext, java_file: JavaFile, node: Any) -> None:
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return
    source_id = _enclosing_definition(context, java_file, node) or java_file.module_id
    for reference, reference_node in _type_references(type_node, java_file.source):
        if reference in _PRIMITIVE_TYPES:
            continue
        target, status, confidence = _resolve_type(context, java_file, reference, reference_node)
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
    primary_reference = _primary_type_name(type_node, java_file.source)
    if primary_reference:
        for variable_name in _declarator_names(node, java_file.source):
            context.variable_types[(source_id, variable_name)] = primary_reference


def _resolve_type(
    context: JavaAnalysisContext,
    java_file: JavaFile,
    reference: str,
    node: Any,
) -> tuple[str, str, float]:
    candidates = _type_candidates(context, java_file, reference)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(f"type:{reference}", unknown=True, java_file=java_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, java_file, node, "unresolved_type", f"Java型を一意に解決できません: {reference}")
        return target, "unresolved", 0.2
    if context.analysis_context.type_index.has_type(reference):
        return context.external_node(f"type:{reference}", context_source="classpath"), "external", 0.8
    return context.external_node(f"type:{reference}"), "external", 0.7


def _type_candidates(context: JavaAnalysisContext, java_file: JavaFile, reference: str) -> list[JavaSymbol]:
    possible_names: list[str] = []
    if "." in reference:
        possible_names.append(reference)
    else:
        imports = context.imports_by_file.get(java_file.relative_path, JavaImportInfo())
        imported = imports.direct.get(reference)
        if imported:
            possible_names.append(imported)
        package_name = context.package_by_file.get(java_file.relative_path)
        if package_name:
            possible_names.append(f"{package_name}.{reference}")
        possible_names.extend(f"{package}.{reference}" for package in imports.wildcards)
    candidates: list[JavaSymbol] = []
    for name in possible_names:
        candidates.extend(context.symbols_for_qualified_name(name, kinds=_TYPE_KINDS))
    if not candidates:
        candidates = context.symbols_for_name(reference.rsplit(".", 1)[-1], kinds=_TYPE_KINDS)
    return _unique_symbols(candidates)


def _collect_method_invocation(context: JavaAnalysisContext, java_file: JavaFile, node: Any) -> None:
    caller = _enclosing_definition(context, java_file, node) or java_file.module_id
    name_node = node.child_by_field_name("name")
    name = _node_text(name_node, java_file.source).strip() if name_node is not None else ""
    arguments = node.child_by_field_name("arguments")
    arity = _argument_arity(arguments)
    object_node = node.child_by_field_name("object")
    object_text = _node_text(object_node, java_file.source).strip() if object_node is not None else ""
    expression = _node_text(node, java_file.source).strip()
    target, status, confidence = _resolve_method_call(
        context,
        java_file,
        caller,
        name,
        arity,
        object_text,
        node,
    )
    if status == "unresolved":
        _diagnose_unresolved(
            context,
            java_file,
            node,
            "unresolved_call",
            f"Java呼び出しを静的に解決できません: {expression}",
            node_id=caller,
            details={"expression": expression, "callee": name, "arity": arity, "receiver": object_text},
        )
    _add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"expression": expression, "callee": name, "arity": arity, "receiver": object_text},
    )


def _resolve_method_call(
    context: JavaAnalysisContext,
    java_file: JavaFile,
    caller: str,
    name: str,
    arity: int,
    receiver: str,
    node: Any,
) -> tuple[str, str, float]:
    if not name:
        return (
            context.external_node("call:<unknown>", unknown=True, java_file=java_file, span=_span_for_tree(node)),
            "unresolved",
            0.1,
        )
    caller_symbol = context.definitions.get(caller)
    owner_q = caller_symbol.owner_q if caller_symbol else None
    if caller_symbol and caller_symbol.kind in _TYPE_KINDS:
        owner_q = caller_symbol.qualified_name
    if receiver in {"", "this"}:
        candidates = _methods_for_owner(context, owner_q, name, arity)
        if not candidates:
            candidates = _methods_for_name(context, name, arity)
    elif receiver == "super":
        candidates = _methods_for_name(context, name, arity)
    else:
        owner_reference = _receiver_type_reference(context, java_file, caller, receiver)
        if owner_reference is None:
            return (
                context.external_node(
                    f"call:{receiver}.{name}/{arity}",
                    unknown=True,
                    java_file=java_file,
                    span=_span_for_tree(node),
                ),
                "unresolved",
                0.2,
            )
        type_candidates = _type_candidates(context, java_file, owner_reference)
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
                context.external_node(
                    f"call:{receiver}.{name}/{arity}",
                    unknown=True,
                    java_file=java_file,
                    span=_span_for_tree(node),
                ),
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
            context.external_node(f"call:{receiver + '.' if receiver else ''}{name}/{arity}", unknown=True, java_file=java_file, span=_span_for_tree(node)),
            "unresolved",
            0.2,
        )
    return context.external_node(f"call:{receiver + '.' if receiver else ''}{name}/{arity}"), "external", 0.7


def _collect_object_creation(context: JavaAnalysisContext, java_file: JavaFile, node: Any) -> None:
    caller = _enclosing_definition(context, java_file, node) or java_file.module_id
    type_node = node.child_by_field_name("type")
    arguments = node.child_by_field_name("arguments")
    reference = _primary_type_name(type_node, java_file.source) if type_node is not None else ""
    arity = _argument_arity(arguments)
    expression = _node_text(node, java_file.source).strip()
    candidates = _type_candidates(context, java_file, reference) if reference else []
    constructors: list[JavaSymbol] = []
    for type_symbol in candidates:
        constructors.extend(_methods_for_owner(context, type_symbol.qualified_name, "<init>", arity))
    constructors = _unique_symbols(constructors)
    if len(constructors) == 1:
        target, status, confidence = constructors[0].node_id, "resolved", 1.0
    elif len(constructors) > 1:
        target = context.external_node(f"new:{reference}/{arity}", unknown=True, java_file=java_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
        _diagnose_unresolved(context, java_file, node, "unresolved_call", f"Javaコンストラクタを一意に解決できません: {expression}", node_id=caller)
    elif reference:
        target = context.external_node(
            f"new:{reference}/{arity}",
            context_source="classpath" if context.analysis_context.type_index.has_type(reference) else None,
        )
        status, confidence = "external", 0.7
    else:
        target = context.external_node(f"new:{reference or '<unknown>'}/{arity}", unknown=True, java_file=java_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
        _diagnose_unresolved(context, java_file, node, "unresolved_call", f"Javaコンストラクタを解決できません: {expression}", node_id=caller)
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


def _collect_explicit_constructor_invocation(context: JavaAnalysisContext, java_file: JavaFile, node: Any) -> None:
    caller = _enclosing_definition(context, java_file, node) or java_file.module_id
    raw = _node_text(node, java_file.source).strip()
    callee = "super" if raw.startswith("super") else "this"
    arity = _argument_arity(node.child_by_field_name("arguments"))
    caller_symbol = context.definitions.get(caller)
    owner_q = caller_symbol.owner_q if caller_symbol else None
    if callee == "this" and owner_q:
        candidates = _methods_for_owner(context, owner_q, "<init>", arity)
    else:
        candidates = []
    candidates = _unique_symbols(candidates)
    if len(candidates) == 1:
        target, status, confidence = candidates[0].node_id, "resolved", 1.0
    elif len(candidates) > 1:
        target = context.external_node(f"call:{callee}/{arity}", unknown=True, java_file=java_file, span=_span_for_tree(node))
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


def _methods_for_name(context: JavaAnalysisContext, name: str, arity: int) -> list[JavaSymbol]:
    return [symbol for symbol in context.symbols_for_name(name, kinds={"method"}) if symbol.arity == arity]


def _methods_for_owner(context: JavaAnalysisContext, owner_q: str | None, name: str, arity: int) -> list[JavaSymbol]:
    if not owner_q:
        return []
    return [
        symbol
        for symbol in _methods_for_name(context, name, arity)
        if symbol.owner_q == owner_q
    ]


def _receiver_type_reference(
    context: JavaAnalysisContext,
    java_file: JavaFile,
    caller: str,
    receiver: str,
) -> str | None:
    cleaned = receiver.strip()
    if not cleaned:
        return None
    new_match = re.match(r"new\s+([A-Za-z_$][\w$.]*)", cleaned)
    if new_match:
        return new_match.group(1)
    simple_name = cleaned.rsplit(".", 1)[-1]
    if cleaned.startswith("this.") or "." not in cleaned:
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
                if type_symbols:
                    reference = context.variable_types.get((type_symbols[0].node_id, simple_name))
                    if reference:
                        return reference
            current = None
        return simple_name if re.fullmatch(r"[A-Za-z_$][\w$]*", simple_name) else None
    return cleaned.rsplit(".", 1)[0]


def _enclosing_definition(context: JavaAnalysisContext, java_file: JavaFile, node: Any) -> str | None:
    current = node
    while current is not None:
        node_id = context.definition_by_node.get((java_file.relative_path, current.id))
        if node_id is not None:
            return node_id
        current = current.parent
    return None


def _return_info(node: Any) -> tuple[str, list[dict[str, Any]]]:
    sites: list[dict[str, Any]] = []
    has_value = False
    has_none = False
    for child in _walk_tree(node):
        if child.type != "return_statement":
            continue
        value = bool(child.named_children)
        has_value = has_value or value
        has_none = has_none or not value
        sites.append({"span": _span_for_tree(child), "value_kind": "value" if value else "none"})
    if not sites:
        return "no_explicit_return", []
    if has_value and has_none:
        return "mixed", sites
    return ("returns_value" if has_value else "returns_none"), sites


def _inheritance_type_nodes(node: Any) -> Iterable[Any]:
    if node.type == "type_list":
        yield from node.named_children
        return
    for child in node.named_children:
        if child.type == "type_list":
            yield from child.named_children
        else:
            yield child


def _declarator_names(node: Any, source: bytes) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for child in _walk_tree(node):
        if child.type not in {"variable_declarator", "formal_parameter", "spread_parameter", "receiver_parameter"}:
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text(name_node, source).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _type_references(node: Any, source: bytes) -> list[tuple[str, Any]]:
    references: list[tuple[str, Any]] = []

    def visit(current: Any) -> None:
        if current.type == "generic_type":
            name_node = current.child_by_field_name("name")
            if name_node is None and current.named_children:
                name_node = current.named_children[0]
            reference = _primary_type_name(name_node, source) if name_node is not None else ""
            if reference:
                references.append((reference, name_node or current))
            arguments = current.child_by_field_name("type_arguments")
            if arguments is not None:
                for child in arguments.named_children:
                    visit(child)
            return
        if current.type in {"type_identifier", "scoped_type_identifier"}:
            reference = _primary_type_name(current, source)
            if reference:
                references.append((reference, current))
            return
        if current.type in {"identifier", "integral_type", "floating_point_type", "boolean_type", "void_type"}:
            if current.type == "identifier":
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


def _primary_type_name(node: Any | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "generic_type":
        name_node = node.child_by_field_name("name")
        if name_node is None and node.named_children:
            name_node = node.named_children[0]
        return _primary_type_name(name_node, source)
    if node.type in _TYPE_WRAPPER_TYPES:
        for child in node.named_children:
            result = _primary_type_name(child, source)
            if result:
                return result
        return ""
    raw = _node_text(node, source).strip()
    if not raw:
        return ""
    raw = re.sub(r"^@[A-Za-z_][\w.]*\s*", "", raw)
    raw = re.sub(r"\s+", "", raw)
    raw = raw.replace("...", "").replace("[]", "")
    raw = _strip_generic_arguments(raw).removeprefix("?")
    raw = raw.removeprefix("extends").removeprefix("super")
    if raw in _PRIMITIVE_TYPES:
        return raw
    if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", raw):
        return raw
    return ""


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


def _package_or_import_name(node: Any, source: bytes) -> str:
    raw = _node_text(node, source).strip()
    raw = re.sub(r"^(?:package|import)\s+", "", raw)
    raw = re.sub(r"^static\s+", "", raw)
    return raw.rstrip(";").strip()


def _named_field(node: Any, source: bytes, field_name: str) -> str | None:
    child = node.child_by_field_name(field_name)
    if child is None:
        return None
    return _node_text(child, source).strip() or None


def _parameter_arity(node: Any) -> int:
    parameters = node.child_by_field_name("parameters")
    if parameters is None:
        parameters = next((child for child in node.named_children if child.type == "formal_parameters"), None)
    if parameters is None:
        return 0
    return sum(
        child.type in {"formal_parameter", "spread_parameter", "receiver_parameter"}
        for child in parameters.named_children
    )


def _argument_arity(node: Any | None) -> int:
    if node is None:
        return 0
    return len(node.named_children)


def _visibility_for_node(node: Any, source: bytes, kind: str) -> str:
    modifiers = node.child_by_field_name("modifiers")
    if modifiers is None:
        modifiers = next((child for child in node.named_children if child.type == "modifiers"), None)
    raw = _node_text(modifiers, source) if modifiers is not None else ""
    if re.search(r"\bpublic\b", raw):
        return "public"
    if re.search(r"\bprivate\b|\bprotected\b", raw):
        return "private"
    if kind in {"namespace", "module"}:
        return "public"
    return "unknown"


def _owner_qualified_name(symbol: JavaSymbol | None) -> str | None:
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS:
        return symbol.qualified_name
    return symbol.owner_q


def _unique_symbols(symbols: Iterable[JavaSymbol]) -> list[JavaSymbol]:
    result: list[JavaSymbol] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol.node_id not in seen:
            seen.add(symbol.node_id)
            result.append(symbol)
    return result


def _node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _first_line(value: str, limit: int = 240) -> str:
    lines = value.splitlines()
    return (lines[0].strip() if lines else value.strip())[:limit]


def _signature_for_node(node: Any, source: bytes, limit: int = 240) -> str:
    for line in _node_text(node, source).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("@"):
            return stripped[:limit]
    return _first_line(_node_text(node, source), limit)


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
    context: JavaAnalysisContext,
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
        [
            source_id,
            target_id,
            relation_type,
            repr(source_span),
            repr(sorted(detail.items())),
        ]
    )
    edge_id = f"java-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
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
    context: JavaAnalysisContext,
    java_file: JavaFile,
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
        java_file=java_file,
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
