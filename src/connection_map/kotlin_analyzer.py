"""Tree-sitter based static analyzer for Kotlin source files."""

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

ANALYZER_NAME = "connection-map-kotlin-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"kotlin"}

_TYPE_KINDS = {"class", "interface", "type"}
_SCOPE_KINDS = {"namespace", "class", "interface", "type", "method", "function", "lambda"}
_PRIMITIVE_TYPES = {
    "Any",
    "Boolean",
    "Byte",
    "Char",
    "Double",
    "Float",
    "Int",
    "Long",
    "Nothing",
    "Short",
    "String",
    "Unit",
    "dynamic",
}
_DEFINITION_TYPES = {
    "class_declaration",
    "object_declaration",
    "companion_object",
    "type_alias",
    "function_declaration",
    "primary_constructor",
    "secondary_constructor",
    "lambda_literal",
    "anonymous_function",
}
_TYPE_CONTAINER_TYPES = {
    "class_parameter",
    "function_value_parameters",
    "parameter",
    "property_declaration",
    "function_declaration",
    "primary_constructor",
    "secondary_constructor",
    "type_alias",
}


class KotlinAnalyzerDependencyError(ValueError):
    """Raised when the optional Kotlin parser dependency is unavailable."""


@dataclass(slots=True)
class KotlinFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str


@dataclass(frozen=True, slots=True)
class KotlinSymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str
    declaration_kind: str
    arity: int | None = None
    owner_q: str | None = None
    receiver_type: str | None = None


@dataclass(slots=True)
class KotlinImportInfo:
    direct: dict[str, str] = field(default_factory=dict)
    wildcards: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KotlinAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    analysis_context: AnalysisContext = field(default_factory=AnalysisContext)
    files: list[KotlinFile] = field(default_factory=list)
    files_by_path: dict[str, KotlinFile] = field(default_factory=dict)
    definitions: dict[str, KotlinSymbol] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    symbols_by_name: dict[str, list[KotlinSymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[KotlinSymbol]] = field(default_factory=dict)
    package_by_file: dict[str, str | None] = field(default_factory=dict)
    imports_by_file: dict[str, KotlinImportInfo] = field(default_factory=dict)
    package_nodes: dict[str, list[str]] = field(default_factory=dict)
    package_node_by_file: dict[str, str] = field(default_factory=dict)
    variable_types: dict[tuple[str, str], str] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_module(self, kotlin_file: KotlinFile) -> None:
        root = kotlin_file.tree.root_node
        self.builder.add_node(
            {
                "id": kotlin_file.module_id,
                "kind": "module",
                "qualified_name": kotlin_file.relative_path,
                "display_name": kotlin_file.relative_path,
                "file": kotlin_file.relative_path,
                "span": _span_for_tree(root),
                "parent_id": None,
                "visibility": "public",
                "extensions": {"language": "kotlin", "grammar": "kotlin"},
            }
        )

    def add_definition(
        self,
        kotlin_file: KotlinFile,
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
        base_id = f"kotlin:{kotlin_file.relative_path}:{qualified_name}:{kind}"
        node_id = _unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": "kotlin",
            "grammar": "kotlin",
            "declaration_kind": declaration_kind,
        }
        if extensions:
            node_extensions.update(extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": kotlin_file.relative_path,
            "span": _span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": _visibility_for_node(tree_node, kotlin_file.source, kind),
            "signature": signature or _signature_for_node(tree_node, kotlin_file.source),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        if kind in {"method", "function", "lambda"}:
            node.setdefault("execution_kind", "suspend" if _is_suspend(tree_node, kotlin_file.source) else "sync")
        self.builder.add_node(node)
        symbol = KotlinSymbol(
            node_id=node_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=kotlin_file.relative_path,
            declaration_kind=declaration_kind,
            arity=arity,
            owner_q=_owner_qualified_name(self.definitions.get(parent_id)),
            receiver_type=(extensions or {}).get("receiver_type"),
        )
        self.definitions[node_id] = symbol
        self.definition_by_node[(kotlin_file.relative_path, tree_node.id)] = node_id
        self.symbols_by_name.setdefault(symbol.name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(qualified_name, []).append(symbol)
        if kind == "namespace":
            self.package_nodes.setdefault(qualified_name, []).append(node_id)
            self.package_node_by_file[kotlin_file.relative_path] = node_id
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

    def symbols_for_name(self, name: str, *, kinds: set[str] | None = None) -> list[KotlinSymbol]:
        candidates = list(self.symbols_by_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def symbols_for_qualified_name(
        self,
        name: str,
        *,
        kinds: set[str] | None = None,
    ) -> list[KotlinSymbol]:
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
        kotlin_file: KotlinFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (kind, label)
        existing = self._external_node_ids.get(key)
        if existing:
            return existing
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"kotlin:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": kotlin_file.relative_path if unknown and kotlin_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": "kotlin",
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
        kotlin_file: KotlinFile | None = None,
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
                "file": kotlin_file.relative_path if kotlin_file else file_path,
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
    """Analyze Kotlin source without compiling or executing the target code."""

    active_config = config or AnalysisConfig(language="kotlin")
    active_config.validate()
    if active_config.language != "kotlin":
        raise ValueError("Kotlin analyzer requires language = 'kotlin'")
    if set(active_config.active_languages()) != SUPPORTED_LANGUAGES:
        raise ValueError("Kotlin analyzer supports only kotlin")

    root = root.resolve()
    builder = GraphBuilder()
    analysis_context = load_analysis_context(root, active_config)
    for diagnostic in analysis_context.diagnostics:
        builder.add_diagnostic({"code": diagnostic["code"], "severity": diagnostic["severity"], "message": diagnostic["message"], "file": None, "span": None, "details": {}})
    context = KotlinAnalysisContext(root, active_config, builder, analysis_context)
    files, skipped = discover_source_files(root, active_config, languages={"kotlin"})
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped Kotlin source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            kotlin_file = _parse_file(path, root)
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
        context.files.append(kotlin_file)
        context.files_by_path[kotlin_file.relative_path] = kotlin_file

    for kotlin_file in context.files:
        context.add_module(kotlin_file)
        _index_file_metadata(context, kotlin_file)
        if kotlin_file.tree.root_node.has_error:
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {kotlin_file.relative_path}",
                kotlin_file=kotlin_file,
                tree_node=kotlin_file.tree.root_node,
                details={"grammar": "kotlin"},
            )

    for kotlin_file in context.files:
        _collect_definitions(context, kotlin_file)
    for kotlin_file in context.files:
        _collect_relations(context, kotlin_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "kotlin",
        "languages": ["kotlin"],
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
            "grammars": ["kotlin"],
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
        raise KotlinAnalyzerDependencyError(
            "Kotlin解析には任意依存が必要です。'uv sync --extra jvm' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser("kotlin")
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise KotlinAnalyzerDependencyError(f"Tree-sitter grammar 'kotlin' を読み込めません: {exc}") from exc


def _parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _parse_file(path: Path, root: Path) -> KotlinFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    tree = _parser_for().parse(source)
    return KotlinFile(
        path=path,
        relative_path=relative_path,
        source=source,
        tree=tree,
        module_id=f"kotlin:{relative_path}:module",
    )


def _index_file_metadata(context: KotlinAnalysisContext, kotlin_file: KotlinFile) -> None:
    package_name: str | None = None
    imports = KotlinImportInfo()
    for node in kotlin_file.tree.root_node.named_children:
        if node.type == "package_header":
            package_name = _package_name(node, kotlin_file.source)
    for node in _walk_tree(kotlin_file.tree.root_node):
        if node.type != "import_header":
            continue
        name, alias = _import_name_and_alias(node, kotlin_file.source)
        if not name:
            continue
        if name.endswith(".*"):
            imports.wildcards.append(name[:-2])
        else:
            imports.direct[alias or name.rsplit(".", 1)[-1]] = name
    context.package_by_file[kotlin_file.relative_path] = package_name
    context.imports_by_file[kotlin_file.relative_path] = imports


def _collect_definitions(context: KotlinAnalysisContext, kotlin_file: KotlinFile) -> None:
    for node in _walk_tree(kotlin_file.tree.root_node):
        candidate = _definition_candidate(kotlin_file, node)
        if candidate is None:
            continue
        kind, display_name, identity_name, declaration_kind, extensions, arity = candidate
        parent_id = _scope_parent(context, kotlin_file, node)
        parent_id, qualified_name = _qualified_scope(
            context,
            kotlin_file,
            node,
            identity_name,
            parent_id,
            kind,
        )
        parent_symbol = context.definitions.get(parent_id)
        if kind == "method" and (
            parent_id == kotlin_file.module_id
            or (parent_symbol is not None and parent_symbol.kind in {"module", "namespace"})
        ):
            kind = "function"
        return_behavior = None
        return_sites = None
        if kind in {"method", "function", "lambda"}:
            return_behavior, return_sites = _return_info_for_source(node, kotlin_file.source)
        context.add_definition(
            kotlin_file,
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
    kotlin_file: KotlinFile,
    node: Any,
) -> tuple[str, str, str, str, dict[str, Any], int | None] | None:
    node_type = node.type
    if node_type == "package_header":
        name = _package_name(node, kotlin_file.source)
        return ("namespace", name or "<root>", name or "<root>", "package", {}, None)
    if node_type == "class_declaration":
        name = _first_direct_type_identifier(node, kotlin_file.source)
        if not name:
            return None
        modifiers = _modifiers_text(node, kotlin_file.source)
        name_node = next((item for item in node.named_children if item.type == "type_identifier"), None)
        header = (
            kotlin_file.source[node.start_byte : name_node.start_byte].decode("utf-8", errors="replace")
            if name_node is not None
            else ""
        )
        modifiers = f"{header} {modifiers}"
        if re.search(r"\binterface\b", modifiers):
            kind, form, declaration_kind = "interface", "interface", "interface"
        elif re.search(r"\benum\b", modifiers):
            kind, form, declaration_kind = "type", "enum", "enum"
        elif re.search(r"\bannotation\b", modifiers):
            kind, form, declaration_kind = "class", "annotation", "annotation"
        else:
            kind, form, declaration_kind = "class", "class", "class"
        extensions: dict[str, Any] = {"declaration_form": form}
        for modifier in ("data", "sealed", "open", "inner", "value", "expect", "actual"):
            if re.search(rf"\b{modifier}\b", modifiers):
                extensions.setdefault("modifiers", []).append(modifier)
        return (kind, name, name, declaration_kind, extensions, None)
    if node_type == "object_declaration":
        name = _first_direct_type_identifier(node, kotlin_file.source)
        if not name:
            return None
        return ("class", name, name, "object", {"declaration_form": "object"}, None)
    if node_type == "companion_object":
        return ("class", "Companion", "Companion", "companion", {"declaration_form": "companion"}, None)
    if node_type == "type_alias":
        name = _first_direct_type_identifier(node, kotlin_file.source)
        if not name:
            return None
        return ("type", name, name, "typealias", {"declaration_form": "typealias"}, None)
    if node_type == "function_declaration":
        name = _first_direct_simple_identifier(node, kotlin_file.source)
        if not name:
            return None
        arity = _parameter_arity(node)
        receiver = _receiver_type(node, kotlin_file.source)
        piece = f"{receiver}.{name}({arity})" if receiver else f"{name}({arity})"
        extensions: dict[str, Any] = {}
        if receiver:
            extensions["receiver_type"] = receiver
            extensions["declaration_form"] = "extension_function"
        else:
            extensions["declaration_form"] = _function_form(node, kotlin_file.source)
        return ("method", name, piece, "definition", extensions, arity)
    if node_type in {"primary_constructor", "secondary_constructor"}:
        arity = _parameter_arity(node)
        return (
            "method",
            "<init>",
            f"<init>({arity})",
            "constructor",
            {"declaration_form": "primary_constructor" if node_type == "primary_constructor" else "secondary_constructor", "constructor": True},
            arity,
        )
    if node_type in {"lambda_literal", "anonymous_function"}:
        start_line = int(node.start_point[0]) + 1
        start_col = int(node.start_point[1])
        name = f"<lambda@{start_line}:{start_col}>"
        form = "lambda" if node_type == "lambda_literal" else "anonymous_function"
        return ("lambda", name, name, form, {"declaration_form": form}, _lambda_arity(node))
    return None


def _scope_parent(context: KotlinAnalysisContext, kotlin_file: KotlinFile, node: Any) -> str:
    current = node.parent
    while current is not None:
        node_id = context.definition_by_node.get((kotlin_file.relative_path, current.id))
        if node_id is not None:
            symbol = context.definitions[node_id]
            if symbol.kind in _SCOPE_KINDS:
                return node_id
        current = current.parent
    return kotlin_file.module_id


def _qualified_scope(
    context: KotlinAnalysisContext,
    kotlin_file: KotlinFile,
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
    package_name = context.package_by_file.get(kotlin_file.relative_path)
    if package_name:
        package_id = context.package_node_by_file.get(kotlin_file.relative_path)
        if package_id is not None:
            package_symbol = context.definitions[package_id]
            return package_id, f"{package_symbol.qualified_name}.{name}"
        return parent_id, f"{package_name}.{name}"
    return parent_id, name


def _collect_relations(context: KotlinAnalysisContext, kotlin_file: KotlinFile) -> None:
    for node in _walk_tree(kotlin_file.tree.root_node):
        if node.type == "import_header":
            _collect_import(context, kotlin_file, node)
        elif node.type in {"class_declaration", "object_declaration"}:
            _collect_inheritance(context, kotlin_file, node)
        elif node.type in _TYPE_CONTAINER_TYPES:
            _collect_declared_type_uses(context, kotlin_file, node)
        elif node.type == "call_expression":
            _collect_method_call(context, kotlin_file, node)
        elif node.type == "constructor_delegation_call":
            _collect_constructor_delegation(context, kotlin_file, node)
        elif node.type == "callable_reference":
            _collect_callable_reference(context, kotlin_file, node)


def _collect_import(context: KotlinAnalysisContext, kotlin_file: KotlinFile, node: Any) -> None:
    raw = _node_text(node, kotlin_file.source).strip()
    name, alias = _import_name_and_alias(node, kotlin_file.source)
    if not name:
        target = context.external_node(
            f"import:{raw}",
            unknown=True,
            kotlin_file=kotlin_file,
            span=_span_for_tree(node),
        )
        _diagnose_unresolved(context, kotlin_file, node, "unresolved_import", f"Kotlin importを解釈できません: {raw}")
        status, confidence = "unresolved", 0.2
    else:
        target, status, confidence = _resolve_import(context, kotlin_file, name, node)
    _add_relation(
        context,
        kotlin_file.module_id,
        target,
        "imports",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"import": raw, "name": name, "alias": alias},
    )


def _resolve_import(
    context: KotlinAnalysisContext,
    kotlin_file: KotlinFile,
    name: str,
    node: Any,
) -> tuple[str, str, float]:
    if name.endswith(".*"):
        package_name = name[:-2]
        package_nodes = context.package_nodes.get(package_name, [])
        if package_nodes:
            return min(package_nodes), "resolved", 1.0
        return context.external_node(f"import:{name}"), "external", 0.75

    candidates = _symbols_for_import(context, name)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(
            f"import:{name}",
            unknown=True,
            kotlin_file=kotlin_file,
            span=_span_for_tree(node),
        )
        _diagnose_unresolved(context, kotlin_file, node, "unresolved_import", f"Kotlin importを一意に解決できません: {name}")
        return target, "unresolved", 0.2

    package_name = name.rsplit(".", 1)[0] if "." in name else ""
    if package_name in context.package_nodes:
        target = context.external_node(
            f"import:{name}",
            unknown=True,
            kotlin_file=kotlin_file,
            span=_span_for_tree(node),
        )
        _diagnose_unresolved(context, kotlin_file, node, "unresolved_import", f"ローカルKotlin importを解決できません: {name}")
        return target, "unresolved", 0.2
    return context.external_node(f"import:{name}"), "external", 0.75


def _symbols_for_import(context: KotlinAnalysisContext, name: str) -> list[KotlinSymbol]:
    candidates: list[KotlinSymbol] = []
    candidates.extend(context.symbols_for_qualified_name(name))
    package_name, member_name = name.rsplit(".", 1) if "." in name else ("", name)
    for qualified_name, symbols in context.symbols_by_qualified_name.items():
        if qualified_name.startswith(f"{name}(") or (
            package_name
            and qualified_name.startswith(f"{package_name}.")
            and qualified_name.endswith(f".{member_name}({symbols[0].arity})")
        ):
            candidates.extend(symbols)
    return _unique_symbols(candidates)


def _collect_inheritance(context: KotlinAnalysisContext, kotlin_file: KotlinFile, node: Any) -> None:
    owner_id = context.definition_by_node.get((kotlin_file.relative_path, node.id))
    if owner_id is None:
        return
    for child in node.named_children:
        if child.type != "delegation_specifier":
            continue
        reference_node = next(
            (
                item
                for item in child.named_children
                if item.type in {"user_type", "constructor_invocation"}
            ),
            None,
        )
        if reference_node is None:
            continue
        if reference_node.type == "constructor_invocation":
            type_node = next(
                (item for item in reference_node.named_children if item.type == "user_type"),
                None,
            )
        else:
            type_node = reference_node
        reference = _primary_type_name(type_node, kotlin_file.source)
        if not reference:
            continue
        target, status, confidence = _resolve_type(context, kotlin_file, reference, type_node)
        _add_relation(
            context,
            owner_id,
            target,
            "inherits",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(type_node),
            detail={"reference": reference, "role": "supertype"},
        )


def _collect_declared_type_uses(context: KotlinAnalysisContext, kotlin_file: KotlinFile, node: Any) -> None:
    source_id = _enclosing_definition(context, kotlin_file, node) or kotlin_file.module_id
    containers = _declared_type_containers(node)
    for container in containers:
        variable_names = _declared_names(container, kotlin_file.source)
        if not variable_names and container.type == "variable_declaration":
            name = _first_direct_simple_identifier(container, kotlin_file.source)
            variable_names = [name] if name else []
        if not variable_names and node.type in {"class_parameter", "parameter"}:
            variable_names = _declared_names(node, kotlin_file.source)
        for reference, reference_node in _type_references(container, kotlin_file.source):
            if reference not in _PRIMITIVE_TYPES:
                target, status, confidence = _resolve_type(context, kotlin_file, reference, reference_node)
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
            for variable_name in variable_names:
                _record_variable_type(
                    context,
                    source_id,
                    variable_name,
                    reference,
                    include_owner=node.type in {"class_parameter", "property_declaration"},
                )


def _declared_type_containers(node: Any) -> list[Any]:
    if node.type == "function_declaration":
        return [
            child
            for child in node.named_children
            if child.type in {"function_value_parameters", "user_type", "receiver_type", "type_parameters"}
        ]
    if node.type in {"primary_constructor", "secondary_constructor"}:
        return [child for child in node.named_children if child.type == "function_value_parameters"] + [
            child for child in node.named_children if child.type == "class_parameter"
        ]
    if node.type == "class_parameter":
        return [child for child in node.named_children if child.type == "user_type"]
    if node.type == "parameter":
        return [child for child in node.named_children if child.type in {"user_type", "nullable_type", "function_type"}]
    if node.type == "property_declaration":
        return [child for child in node.named_children if child.type == "variable_declaration"]
    if node.type == "type_alias":
        return [child for child in node.named_children if child.type in {"user_type", "nullable_type", "function_type"}]
    return []


def _record_variable_type(
    context: KotlinAnalysisContext,
    source_id: str,
    name: str,
    reference: str,
    *,
    include_owner: bool = False,
) -> None:
    if name:
        context.variable_types[(source_id, name)] = reference
        if include_owner:
            symbol = context.definitions.get(source_id)
            if symbol and symbol.owner_q:
                for owner in context.symbols_for_qualified_name(symbol.owner_q, kinds=_TYPE_KINDS):
                    context.variable_types[(owner.node_id, name)] = reference


def _resolve_type(
    context: KotlinAnalysisContext,
    kotlin_file: KotlinFile,
    reference: str,
    node: Any,
) -> tuple[str, str, float]:
    candidates = _type_candidates(context, kotlin_file, reference)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(
            f"type:{reference}",
            unknown=True,
            kotlin_file=kotlin_file,
            span=_span_for_tree(node),
        )
        _diagnose_unresolved(context, kotlin_file, node, "unresolved_type", f"Kotlin型を一意に解決できません: {reference}")
        return target, "unresolved", 0.2
    if context.analysis_context.type_index.has_type(reference):
        return context.external_node(f"type:{reference}", context_source="classpath"), "external", 0.8
    return context.external_node(f"type:{reference}"), "external", 0.7


def _type_candidates(
    context: KotlinAnalysisContext,
    kotlin_file: KotlinFile,
    reference: str,
) -> list[KotlinSymbol]:
    reference = _strip_type_reference(reference)
    possible_names: list[str] = []
    if "." in reference:
        possible_names.append(reference)
        package_name = context.package_by_file.get(kotlin_file.relative_path)
        if package_name and not reference.startswith(f"{package_name}."):
            possible_names.append(f"{package_name}.{reference}")
    else:
        imports = context.imports_by_file.get(kotlin_file.relative_path, KotlinImportInfo())
        imported = imports.direct.get(reference)
        if imported:
            possible_names.append(imported)
        package_name = context.package_by_file.get(kotlin_file.relative_path)
        if package_name:
            possible_names.append(f"{package_name}.{reference}")
        possible_names.extend(f"{package}.{reference}" for package in imports.wildcards)
    candidates: list[KotlinSymbol] = []
    for name in possible_names:
        candidates.extend(context.symbols_for_qualified_name(name, kinds=_TYPE_KINDS))
    if not candidates:
        candidates = context.symbols_for_name(reference.rsplit(".", 1)[-1], kinds=_TYPE_KINDS)
    return _unique_symbols(candidates)


def _collect_method_call(context: KotlinAnalysisContext, kotlin_file: KotlinFile, node: Any) -> None:
    caller = _enclosing_definition(context, kotlin_file, node) or kotlin_file.module_id
    name, receiver, arguments = _call_parts(node, kotlin_file.source)
    arity = _argument_arity(arguments)
    expression = _node_text(node, kotlin_file.source).strip()
    target, status, confidence = _resolve_method_call(
        context,
        kotlin_file,
        caller,
        name,
        arity,
        receiver,
        node,
    )
    if status == "unresolved":
        _diagnose_unresolved(
            context,
            kotlin_file,
            node,
            "unresolved_call",
            f"Kotlin呼び出しを静的に解決できません: {expression}",
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
        detail={
            "expression": expression,
            "callee": name,
            "arity": arity,
            "receiver": receiver,
            "call_kind": "attribute" if receiver else "direct",
        },
    )


def _resolve_method_call(
    context: KotlinAnalysisContext,
    kotlin_file: KotlinFile,
    caller: str,
    name: str,
    arity: int,
    receiver: str,
    node: Any,
) -> tuple[str, str, float]:
    if not name:
        return (
            context.external_node(
                "call:<unknown>",
                unknown=True,
                kotlin_file=kotlin_file,
                span=_span_for_tree(node),
            ),
            "unresolved",
            0.1,
        )

    if not receiver:
        type_candidates = _type_candidates(context, kotlin_file, name)
        constructors = _constructors_for_types(context, type_candidates, arity)
        if len(constructors) == 1:
            return constructors[0].node_id, "resolved", 1.0
        candidates = _unqualified_call_candidates(context, caller, name, arity)
    elif receiver == "super":
        candidates = _methods_for_name(context, name, arity)
    else:
        owner_reference = _receiver_type_reference(context, kotlin_file, caller, receiver)
        candidates = []
        if owner_reference:
            type_candidates = _type_candidates(context, kotlin_file, owner_reference)
            for type_symbol in type_candidates:
                candidates.extend(_methods_for_owner(context, type_symbol.qualified_name, name, arity))
                candidates.extend(_methods_for_owner(context, f"{type_symbol.qualified_name}.Companion", name, arity))
            candidates.extend(_extension_methods(context, name, arity, owner_reference))
            if not candidates and context.analysis_context.type_index.has_method(owner_reference, name):
                return (
                    context.external_node(
                        f"call:{receiver}.{name}/{arity}",
                        context_source="classpath",
                    ),
                    "external",
                    0.8,
                )
        if not candidates:
            candidates.extend(_methods_for_name(context, name, arity))

    candidates = _unique_symbols(candidates)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    label = f"call:{receiver + '.' if receiver else ''}{name}/{arity}"
    if len(candidates) > 1:
        return (
            context.external_node(label, unknown=True, kotlin_file=kotlin_file, span=_span_for_tree(node)),
            "unresolved",
            0.2,
        )
    return (
        context.external_node(
            label,
            context_source="classpath" if context.analysis_context.type_index.has_method(receiver, name) else None,
        ),
        "external",
        0.8 if context.analysis_context.type_index.has_method(receiver, name) else 0.7,
    )


def _unqualified_call_candidates(
    context: KotlinAnalysisContext,
    caller: str,
    name: str,
    arity: int,
) -> list[KotlinSymbol]:
    caller_symbol = context.definitions.get(caller)
    candidates: list[KotlinSymbol] = []
    if caller_symbol:
        owner_q = caller_symbol.owner_q
        if caller_symbol.kind in _TYPE_KINDS:
            owner_q = caller_symbol.qualified_name
        if owner_q:
            candidates.extend(_methods_for_owner(context, owner_q, name, arity))
            candidates.extend(_methods_for_owner(context, f"{owner_q}.Companion", name, arity))
    candidates.extend(_methods_for_name(context, name, arity, include_functions=True))
    return _unique_symbols(candidates)


def _constructors_for_types(
    context: KotlinAnalysisContext,
    type_symbols: Iterable[KotlinSymbol],
    arity: int,
) -> list[KotlinSymbol]:
    constructors: list[KotlinSymbol] = []
    for type_symbol in type_symbols:
        declared = _methods_for_owner(context, type_symbol.qualified_name, "<init>", arity)
        constructors.extend(declared or [type_symbol])
    return _unique_symbols(constructors)


def _extension_methods(
    context: KotlinAnalysisContext,
    name: str,
    arity: int,
    receiver: str,
) -> list[KotlinSymbol]:
    normalized = _strip_type_reference(receiver).rsplit(".", 1)[-1]
    return [
        symbol
        for symbol in _methods_for_name(context, name, arity, include_functions=True)
        if symbol.receiver_type
        and _strip_type_reference(symbol.receiver_type).rsplit(".", 1)[-1] == normalized
    ]


def _methods_for_name(
    context: KotlinAnalysisContext,
    name: str,
    arity: int,
    *,
    include_functions: bool = False,
) -> list[KotlinSymbol]:
    kinds = {"method", "function"} if include_functions else {"method"}
    return [symbol for symbol in context.symbols_for_name(name, kinds=kinds) if symbol.arity == arity]


def _methods_for_owner(
    context: KotlinAnalysisContext,
    owner_q: str | None,
    name: str,
    arity: int,
) -> list[KotlinSymbol]:
    if not owner_q:
        return []
    return [
        symbol
        for symbol in _methods_for_name(context, name, arity, include_functions=True)
        if symbol.owner_q == owner_q
    ]


def _receiver_type_reference(
    context: KotlinAnalysisContext,
    kotlin_file: KotlinFile,
    caller: str,
    receiver: str,
) -> str | None:
    cleaned = receiver.strip()
    if not cleaned or cleaned in {"this", "super"}:
        return None
    constructor_match = re.match(r"^([A-Za-z_][\w.]*)\s*\(", cleaned)
    if constructor_match:
        return constructor_match.group(1)
    if "." in cleaned and not cleaned.startswith("this."):
        first = cleaned.split(".", 1)[0]
        if first and first[0].isupper():
            return cleaned.rsplit(".", 1)[0]
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
            owner_symbols = context.symbols_for_qualified_name(owner_q, kinds=_TYPE_KINDS)
            for owner_symbol in owner_symbols:
                reference = context.variable_types.get((owner_symbol.node_id, simple_name))
                if reference:
                    return reference
        current = _parent_definition_id(context, current)
    if re.fullmatch(r"[A-Za-z_][\w.]*", cleaned):
        return cleaned
    return None


def _parent_definition_id(context: KotlinAnalysisContext, node_id: str) -> str | None:
    symbol = context.definitions.get(node_id)
    if symbol is None:
        return None
    for candidate in context.definitions.values():
        if candidate.kind in _SCOPE_KINDS and candidate.qualified_name == symbol.owner_q:
            return candidate.node_id
    return None


def _collect_constructor_delegation(context: KotlinAnalysisContext, kotlin_file: KotlinFile, node: Any) -> None:
    caller = _enclosing_definition(context, kotlin_file, node) or kotlin_file.module_id
    raw = _node_text(node, kotlin_file.source).strip()
    callee = "super" if raw.startswith("super") else "this"
    arity = _argument_arity(next((child for child in node.named_children if child.type == "value_arguments"), None))
    caller_symbol = context.definitions.get(caller)
    owner_q = caller_symbol.owner_q if caller_symbol else None
    candidates = _methods_for_owner(context, owner_q, "<init>", arity) if callee == "this" else []
    candidates = _unique_symbols(candidates)
    if len(candidates) == 1:
        target, status, confidence = candidates[0].node_id, "resolved", 1.0
    elif len(candidates) > 1:
        target = context.external_node(
            f"call:{callee}/{arity}",
            unknown=True,
            kotlin_file=kotlin_file,
            span=_span_for_tree(node),
        )
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


def _collect_callable_reference(context: KotlinAnalysisContext, kotlin_file: KotlinFile, node: Any) -> None:
    caller = _enclosing_definition(context, kotlin_file, node) or kotlin_file.module_id
    raw = _node_text(node, kotlin_file.source).strip()
    name = raw.removeprefix("::").rsplit(".", 1)[-1]
    candidates = _methods_for_name(context, name, 0, include_functions=True)
    if len(candidates) == 1:
        target, status, confidence = candidates[0].node_id, "resolved", 1.0
    elif len(candidates) > 1:
        target = context.external_node(f"reference:{raw}", unknown=True, kotlin_file=kotlin_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
        _diagnose_unresolved(context, kotlin_file, node, "unresolved_call", f"Kotlin function referenceを解決できません: {raw}", node_id=caller)
    else:
        target = context.external_node(f"reference:{raw}")
        status, confidence = "external", 0.7
    _add_relation(
        context,
        caller,
        target,
        "references",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"expression": raw, "reference": name},
    )


def _enclosing_definition(context: KotlinAnalysisContext, kotlin_file: KotlinFile, node: Any) -> str | None:
    current = node
    while current is not None:
        node_id = context.definition_by_node.get((kotlin_file.relative_path, current.id))
        if node_id is not None:
            return node_id
        current = current.parent
    return None


def _return_info_for_source(node: Any, source: bytes) -> tuple[str, list[dict[str, Any]]]:
    sites: list[dict[str, Any]] = []
    has_value = False
    has_none = False

    def visit(current: Any, *, root: bool = False) -> None:
        nonlocal has_value, has_none
        if not root and current.type in _DEFINITION_TYPES:
            return
        if current.type == "jump_expression":
            raw = _node_text(current, source).strip()
            match = re.match(r"return(?:@[A-Za-z_][\w]*)?\b(.*)", raw, re.DOTALL)
            if match:
                value = bool(match.group(1).strip())
                has_value = has_value or value
                has_none = has_none or not value
                sites.append({"span": _span_for_tree(current), "value_kind": "value" if value else "none"})
                return
        for child in current.named_children:
            visit(child)

    visit(node, root=True)
    if sites:
        if has_value and has_none:
            return "mixed", sites
        return ("returns_value" if has_value else "returns_none"), sites
    if node.type in {"lambda_literal", "anonymous_function"} and _has_callable_body(node):
        return "returns_value", []
    if node.type == "function_declaration" and _function_form(node, source) == "expression_body":
        return "returns_value", []
    return "no_explicit_return", []


def _has_callable_body(node: Any) -> bool:
    return any(child.type in {"statements", "function_body"} and bool(child.named_children) for child in node.named_children)


def _type_references(node: Any, source: bytes) -> list[tuple[str, Any]]:
    references: list[tuple[str, Any]] = []

    def visit(current: Any) -> None:
        if current.type == "user_type":
            reference = _primary_type_name(current, source)
            if reference:
                references.append((reference, current))
            for child in current.named_children:
                if child.type == "type_arguments":
                    visit(child)
            return
        if current.type == "type_arguments":
            for child in current.named_children:
                visit(child)
            return
        if current.type in {
            "nullable_type",
            "receiver_type",
            "function_type",
            "function_value_parameters",
            "variable_declaration",
            "class_parameter",
            "parameter",
            "type_projection",
            "type_parameter",
            "type_parameters",
            "parenthesized_type",
        }:
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
    if node.type in {"nullable_type", "receiver_type", "type_projection", "parenthesized_type"}:
        for child in node.named_children:
            result = _primary_type_name(child, source)
            if result:
                return result
        return ""
    if node.type == "type_arguments":
        return ""
    raw = _node_text(node, source).strip()
    if not raw:
        return ""
    raw = re.sub(r"^@[^\s]+\s*", "", raw)
    raw = re.sub(r"\s+", "", raw)
    raw = raw.replace("?", "").replace("*", "")
    raw = _strip_generic_arguments(raw)
    raw = re.sub(r"^(?:in|out)", "", raw)
    match = re.fullmatch(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*", raw)
    return match.group(0) if match else ""


def _strip_type_reference(value: str) -> str:
    return _primary_type_name_from_text(value)


def _primary_type_name_from_text(value: str) -> str:
    raw = re.sub(r"\s+", "", value).replace("?", "")
    raw = _strip_generic_arguments(raw)
    raw = re.sub(r"^(?:in|out)", "", raw)
    match = re.fullmatch(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*", raw)
    return match.group(0) if match else raw


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


def _declared_names(node: Any, source: bytes) -> list[str]:
    names: list[str] = []
    if node.type in {"class_parameter", "parameter"}:
        name = _first_direct_simple_identifier(node, source)
        return [name] if name else []
    for child in node.named_children:
        if child.type == "variable_declaration" or child.type in {"class_parameter", "parameter"}:
            name = _first_direct_simple_identifier(child, source)
            if name:
                names.append(name)
        elif child.type == "function_value_parameters":
            for parameter in child.named_children:
                if parameter.type == "parameter":
                    name = _first_direct_simple_identifier(parameter, source)
                    if name:
                        names.append(name)
    return list(dict.fromkeys(names))


def _package_name(node: Any, source: bytes) -> str:
    raw = _node_text(node, source).strip()
    return re.sub(r"^package\s+", "", raw).strip()


def _import_name_and_alias(node: Any, source: bytes) -> tuple[str, str | None]:
    raw = _node_text(node, source).strip()
    raw = re.sub(r"^import\s+", "", raw).strip()
    match = re.match(r"(.+?)\s+as\s+([A-Za-z_][\w]*)$", raw)
    if match:
        return match.group(1).strip(), match.group(2)
    return raw, None


def _first_direct_type_identifier(node: Any, source: bytes) -> str | None:
    child = next((item for item in node.named_children if item.type == "type_identifier"), None)
    return _node_text(child, source).strip() or None if child is not None else None


def _first_direct_simple_identifier(node: Any, source: bytes) -> str | None:
    child = next((item for item in node.named_children if item.type == "simple_identifier"), None)
    return _node_text(child, source).strip() or None if child is not None else None


def _receiver_type(node: Any, source: bytes) -> str | None:
    child = node.child_by_field_name("receiver")
    if child is None:
        child = next((item for item in node.named_children if item.type == "receiver_type"), None)
    if child is None:
        return None
    return _primary_type_name(child, source) or _node_text(child, source).strip() or None


def _function_form(node: Any, source: bytes) -> str:
    body = next((child for child in node.named_children if child.type == "function_body"), None)
    if body is not None and _node_text(body, source).lstrip().startswith("="):
        return "expression_body"
    return "block_body"


def _parameter_arity(node: Any) -> int:
    if node.type in {"primary_constructor", "secondary_constructor"}:
        parameters = next((child for child in node.named_children if child.type == "function_value_parameters"), None)
        if parameters is None:
            return sum(child.type == "class_parameter" for child in node.named_children)
    else:
        parameters = next((child for child in node.named_children if child.type == "function_value_parameters"), None)
    if parameters is None:
        return 0
    return sum(child.type == "parameter" for child in parameters.named_children)


def _lambda_arity(node: Any) -> int:
    if node.type == "anonymous_function":
        return _parameter_arity(node)
    parameters = next((child for child in node.named_children if child.type == "lambda_parameters"), None)
    if parameters is None:
        return 0
    return sum(child.type in {"variable_declaration", "parameter"} for child in parameters.named_children)


def _argument_arity(node: Any | None) -> int:
    if node is None:
        return 0
    return len(node.named_children)


def _call_parts(node: Any, source: bytes) -> tuple[str, str, Any | None]:
    call_suffix = next((child for child in node.named_children if child.type == "call_suffix"), None)
    arguments = None
    if call_suffix is not None:
        arguments = next((child for child in call_suffix.named_children if child.type == "value_arguments"), None)
    first = node.named_children[0] if node.named_children else None
    if first is None:
        return "", "", arguments
    if first.type == "simple_identifier":
        return _node_text(first, source).strip(), "", arguments
    if first.type == "navigation_expression":
        suffixes = [child for child in first.named_children if child.type == "navigation_suffix"]
        suffix = suffixes[-1] if suffixes else None
        name = _navigation_suffix_name(suffix, source) if suffix is not None else ""
        receiver = _node_text(first, source)
        if suffix is not None:
            receiver = receiver[: suffix.start_byte - first.start_byte].rstrip()
        return name, receiver, arguments
    raw = _node_text(first, source).strip()
    match = re.match(r"(.+?)(?:\?\.|\.)([A-Za-z_][\w]*)$", raw)
    if match:
        return match.group(2), match.group(1), arguments
    return raw, "", arguments


def _navigation_suffix_name(node: Any, source: bytes) -> str:
    raw = _node_text(node, source).strip()
    match = re.search(r"(?:\?\.|\.)([A-Za-z_][\w]*)", raw)
    return match.group(1) if match else raw.lstrip("?.")


def _modifiers_text(node: Any, source: bytes) -> str:
    modifiers = next((child for child in node.named_children if child.type == "modifiers"), None)
    return _node_text(modifiers, source) if modifiers is not None else ""


def _is_suspend(node: Any, source: bytes) -> bool:
    return bool(re.search(r"\bsuspend\b", _modifiers_text(node, source)))


def _visibility_for_node(node: Any, source: bytes, kind: str) -> str:
    raw = _modifiers_text(node, source)
    if re.search(r"\bprivate\b|\bprotected\b", raw):
        return "private"
    if kind in {"namespace", "module", "class", "interface", "type", "function", "method", "lambda"}:
        return "public"
    return "unknown"


def _owner_qualified_name(symbol: KotlinSymbol | None) -> str | None:
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS:
        return symbol.qualified_name
    return symbol.owner_q


def _unique_symbols(symbols: Iterable[KotlinSymbol]) -> list[KotlinSymbol]:
    result: list[KotlinSymbol] = []
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
        if stripped and not stripped.startswith("@"):
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
    context: KotlinAnalysisContext,
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
            repr(sorted(detail.items(), key=lambda item: item[0])),
        ]
    )
    edge_id = f"kotlin-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
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
    context: KotlinAnalysisContext,
    kotlin_file: KotlinFile,
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
        kotlin_file=kotlin_file,
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
