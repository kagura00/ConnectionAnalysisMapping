"""Tree-sitter based static analyzer for Go source files."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import posixpath
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .analysis_context import GoBuildProfile, go_file_matches_build, load_analysis_context
from .config import AnalysisConfig, discover_source_files, repository_id
from .contract import validate_document
from .model import GraphBuilder

ANALYZER_NAME = "connection-map-go-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"go"}

_TYPE_KINDS = {"type", "interface", "class"}
_SCOPE_KINDS = {"namespace", "type", "interface", "class", "function", "method", "lambda"}
_BUILTIN_TYPES = {
    "any",
    "bool",
    "byte",
    "complex64",
    "complex128",
    "error",
    "float32",
    "float64",
    "int",
    "int8",
    "int16",
    "int32",
    "int64",
    "rune",
    "string",
    "uint",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "uintptr",
}
_TYPE_DECLARATION_TYPES = {"type_spec", "type_alias"}
_PARAMETER_TYPES = {"parameter_declaration", "variadic_parameter_declaration"}


class GoAnalyzerDependencyError(ValueError):
    """Raised when the optional Go parser dependency is unavailable."""


@dataclass(slots=True)
class GoFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str
    relative_directory: str
    package_name: str
    package_key: str
    package_import_path: str
    build_constraints: tuple[str, ...] = ()
    package_node_id: str | None = None


@dataclass(frozen=True, slots=True)
class GoSymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str | None
    declaration_kind: str
    arity: int | None = None
    owner_q: str | None = None


@dataclass(slots=True)
class GoPackage:
    key: str
    relative_directory: str
    package_name: str
    import_path: str
    files: list[GoFile] = field(default_factory=list)
    node_id: str | None = None


@dataclass(slots=True)
class GoImportInfo:
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class GoAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    module_path: str | None = None
    build_profile: GoBuildProfile = field(default_factory=GoBuildProfile)
    files: list[GoFile] = field(default_factory=list)
    files_by_path: dict[str, GoFile] = field(default_factory=dict)
    packages: dict[str, GoPackage] = field(default_factory=dict)
    packages_by_import_path: dict[str, list[GoPackage]] = field(default_factory=dict)
    definitions: dict[str, GoSymbol] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    symbols_by_name: dict[str, list[GoSymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[GoSymbol]] = field(default_factory=dict)
    imports_by_file: dict[str, GoImportInfo] = field(default_factory=dict)
    variable_types: dict[tuple[str, str], str] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_package(self, package: GoPackage) -> None:
        first_file = package.files[0]
        package_node_id = f"go:{package.import_path}:namespace"
        package.node_id = package_node_id
        package_span = _package_span(first_file.tree, first_file.source)
        self.builder.add_node(
            {
                "id": package_node_id,
                "kind": "namespace",
                "qualified_name": package.import_path,
                "display_name": package.package_name,
                "file": first_file.relative_path,
                "span": package_span,
                "parent_id": None,
                "visibility": "public",
                "signature": f"package {package.package_name}",
                "extensions": {
                    "language": "go",
                    "grammar": "go",
                    "package_name": package.package_name,
                    "import_path": package.import_path,
                },
            }
        )
        symbol = GoSymbol(
            node_id=package_node_id,
            name=package.package_name,
            qualified_name=package.import_path,
            kind="namespace",
            file_path=first_file.relative_path,
            declaration_kind="package",
        )
        self.definitions[package_node_id] = symbol
        self.symbols_by_name.setdefault(symbol.name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(symbol.qualified_name, []).append(symbol)

    def add_module(self, go_file: GoFile) -> None:
        package_node_id = go_file.package_node_id
        self.builder.add_node(
            {
                "id": go_file.module_id,
                "kind": "module",
                "qualified_name": go_file.relative_path,
                "display_name": go_file.relative_path,
                "file": go_file.relative_path,
                "span": _span_for_tree(go_file.tree.root_node),
                "parent_id": package_node_id,
                "visibility": "public",
                "signature": f"package {go_file.package_name}",
                "extensions": {
                    "language": "go",
                    "grammar": "go",
                    "package_name": go_file.package_name,
                    "import_path": go_file.package_import_path,
                    **(
                        {"build_constraints": list(go_file.build_constraints)}
                        if go_file.build_constraints
                        else {}
                    ),
                },
            }
        )
        if package_node_id is not None:
            _add_relation(
                self,
                package_node_id,
                go_file.module_id,
                "contains",
                resolution_status="resolved",
                confidence=1.0,
                source_span=_package_span(go_file.tree, go_file.source),
                detail={"kind": "package_file"},
            )

    def add_definition(
        self,
        go_file: GoFile,
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
        base_id = f"go:{go_file.relative_path}:{qualified_name}:{kind}"
        node_id = _unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": "go",
            "grammar": "go",
            "declaration_kind": declaration_kind,
        }
        if extensions:
            node_extensions.update(extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": go_file.relative_path,
            "span": _span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": _visibility_for_name(name),
            "signature": signature or _signature_for_node(tree_node, go_file.source),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        if kind in {"function", "method", "lambda"}:
            node["execution_kind"] = "sync"
        self.builder.add_node(node)
        parent_symbol = self.definitions.get(parent_id)
        resolved_owner = owner_q or _owner_qualified_name(parent_symbol)
        symbol = GoSymbol(
            node_id=node_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=go_file.relative_path,
            declaration_kind=declaration_kind,
            arity=arity,
            owner_q=resolved_owner,
        )
        self.definitions[node_id] = symbol
        self.definition_by_node[(go_file.relative_path, tree_node.id)] = node_id
        self.symbols_by_name.setdefault(symbol.name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(symbol.qualified_name, []).append(symbol)
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

    def symbols_for_name(self, name: str, *, kinds: set[str] | None = None) -> list[GoSymbol]:
        candidates = list(self.symbols_by_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def symbols_for_qualified_name(
        self,
        name: str,
        *,
        kinds: set[str] | None = None,
    ) -> list[GoSymbol]:
        candidates = list(self.symbols_by_qualified_name.get(name, []))
        if kinds is not None:
            candidates = [item for item in candidates if item.kind in kinds]
        return candidates

    def external_node(
        self,
        label: str,
        *,
        unknown: bool = False,
        go_file: GoFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (kind, label)
        if key in self._external_node_ids:
            return self._external_node_ids[key]
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"go:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": go_file.relative_path if unknown and go_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": "go",
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
        go_file: GoFile | None = None,
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
                "file": go_file.relative_path if go_file else file_path,
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
    """Analyze Go source without building, importing, or executing target code."""

    active_config = config or AnalysisConfig(language="go")
    active_config.validate()
    if active_config.language != "go":
        raise ValueError("Go analyzer requires language = 'go'")
    if set(active_config.active_languages()) != SUPPORTED_LANGUAGES:
        raise ValueError("Go analyzer supports only go")

    root = root.resolve()
    builder = GraphBuilder()
    context = GoAnalysisContext(root, active_config, builder, module_path=_read_module_path(root))
    analysis_context = load_analysis_context(root, active_config)
    context.build_profile = analysis_context.go
    for diagnostic in analysis_context.diagnostics:
        builder.add_diagnostic({"code": diagnostic["code"], "severity": diagnostic["severity"], "message": diagnostic["message"], "file": None, "span": None, "details": {}})
    files, skipped = discover_source_files(root, active_config, languages={"go"})
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped Go source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            go_file = _parse_file(path, root, context.module_path)
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
        if not go_file_matches_build(relative_path, go_file.source, context.build_profile):
            context.diagnostic(
                "build_condition_excluded",
                "info",
                f"Skipped Go source file by build condition: {relative_path}",
                file_path=relative_path,
                details={
                    "build_constraints": list(go_file.build_constraints),
                    "goos": context.build_profile.goos,
                    "goarch": context.build_profile.goarch,
                    "tags": list(context.build_profile.tags),
                },
            )
            continue
        context.files.append(go_file)
        context.files_by_path[go_file.relative_path] = go_file

    _group_packages(context)
    for package in sorted(context.packages.values(), key=lambda item: item.import_path):
        context.add_package(package)
    for go_file in context.files:
        context.add_module(go_file)
        if go_file.tree.root_node.has_error:
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {go_file.relative_path}",
                go_file=go_file,
                tree_node=go_file.tree.root_node,
                details={"grammar": "go"},
            )

    for go_file in context.files:
        _index_file_metadata(context, go_file)
    for go_file in context.files:
        _collect_type_definitions(context, go_file)
    for go_file in context.files:
        _collect_callable_definitions(context, go_file)
    for go_file in context.files:
        _collect_relations(context, go_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "go",
        "languages": ["go"],
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
            "grammars": ["go"],
            "build_context": analysis_context.summary(),
        },
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": active_config.to_dict(),
        "extensions": {
            "module_path": context.module_path,
            "package_count": len(context.packages),
            "build_constraints_recorded": sum(bool(item.build_constraints) for item in context.files),
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
        raise GoAnalyzerDependencyError(
            "Go解析には任意依存が必要です。'uv sync --extra go' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser("go")
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise GoAnalyzerDependencyError(f"Tree-sitter grammar 'go'を読み込めません: {exc}") from exc


def _parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _parse_file(path: Path, root: Path, module_path: str | None) -> GoFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    relative_directory = path.parent.relative_to(root).as_posix()
    if relative_directory == ".":
        relative_directory = ""
    tree = _parser_for().parse(source)
    package_name = _package_name(tree, source) or "<unknown>"
    package_import_path = _package_import_path(module_path, relative_directory, package_name)
    package_key = f"{relative_directory}\x1f{package_name}"
    return GoFile(
        path=path,
        relative_path=relative_path,
        source=source,
        tree=tree,
        module_id=f"go:{relative_path}:module",
        relative_directory=relative_directory,
        package_name=package_name,
        package_key=package_key,
        package_import_path=package_import_path,
        build_constraints=_build_constraints(source),
    )


def _group_packages(context: GoAnalysisContext) -> None:
    for go_file in context.files:
        package = context.packages.get(go_file.package_key)
        if package is None:
            package = GoPackage(
                key=go_file.package_key,
                relative_directory=go_file.relative_directory,
                package_name=go_file.package_name,
                import_path=go_file.package_import_path,
            )
            context.packages[go_file.package_key] = package
        package.files.append(go_file)
    packages_by_base: dict[str, list[GoPackage]] = {}
    for package in context.packages.values():
        packages_by_base.setdefault(package.import_path, []).append(package)
    for base_path, packages in packages_by_base.items():
        packages.sort(key=lambda item: item.package_name)
        primary = next((item for item in packages if not item.package_name.endswith("_test")), packages[0])
        for package in packages:
            if package is not primary:
                package.import_path = f"{base_path}#{package.package_name}"
    context.packages_by_import_path.clear()
    for package in context.packages.values():
        package.files.sort(key=lambda item: item.relative_path)
        context.packages_by_import_path.setdefault(package.import_path, []).append(package)
        for go_file in package.files:
            go_file.package_import_path = package.import_path
            go_file.package_node_id = package.node_id or f"go:{package.import_path}:namespace"


def _index_file_metadata(context: GoAnalysisContext, go_file: GoFile) -> None:
    imports = GoImportInfo()
    for node in _walk_tree(go_file.tree.root_node):
        if node.type != "import_spec":
            continue
        path = _import_path(node, go_file.source)
        if not path:
            continue
        name_node = node.child_by_field_name("name")
        local_name = _node_text(name_node, go_file.source).strip() if name_node is not None else ""
        if not local_name:
            local_name = path.rsplit("/", 1)[-1]
        imports.aliases[local_name] = path
    context.imports_by_file[go_file.relative_path] = imports


def _collect_type_definitions(context: GoAnalysisContext, go_file: GoFile) -> None:
    for node in _walk_tree(go_file.tree.root_node):
        if node.type not in _TYPE_DECLARATION_TYPES:
            continue
        name_node = node.child_by_field_name("name")
        type_node = node.child_by_field_name("type")
        if name_node is None or type_node is None:
            continue
        name = _node_text(name_node, go_file.source).strip()
        if not name:
            continue
        form, kind, declaration_kind = _type_form(node, type_node, go_file.source)
        package_id = go_file.package_node_id or go_file.module_id
        qualified_name = f"{go_file.package_import_path}.{name}"
        context.add_definition(
            go_file,
            node,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            declaration_kind=declaration_kind,
            parent_id=package_id,
            extensions={"declaration_form": form},
        )


def _collect_callable_definitions(context: GoAnalysisContext, go_file: GoFile) -> None:
    for node in _walk_tree(go_file.tree.root_node):
        if node.type not in {"function_declaration", "method_declaration", "method_elem", "func_literal"}:
            continue
        if node.type == "func_literal":
            _add_function_literal(context, go_file, node)
            continue
        if node.type == "method_elem" and not _interface_method_owner(context, go_file, node):
            continue
        if node.type == "function_declaration":
            _add_function_definition(context, go_file, node, method=False)
        elif node.type == "method_declaration":
            _add_function_definition(context, go_file, node, method=True)
        else:
            _add_interface_method(context, go_file, node)


def _add_function_definition(context: GoAnalysisContext, go_file: GoFile, node: Any, *, method: bool) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _node_text(name_node, go_file.source).strip()
    arity = _parameter_arity(node.child_by_field_name("parameters"))
    package_id = go_file.package_node_id or go_file.module_id
    owner_id = package_id
    owner_q: str | None = None
    if method:
        receiver_type = _receiver_type(node, go_file.source)
        candidates = _type_candidates(context, go_file, receiver_type) if receiver_type else []
        if len(candidates) == 1:
            owner_id = candidates[0].node_id
            owner_q = candidates[0].qualified_name
        else:
            owner_q = f"{go_file.package_import_path}.{receiver_type}" if receiver_type else None
    qualified_name = f"{owner_q or go_file.package_import_path}.{name}({arity})"
    declaration_kind = "definition" if node.child_by_field_name("body") is not None else "declaration"
    return_behavior = "unknown"
    return_sites: list[dict[str, Any]] | None = None
    if declaration_kind == "definition":
        return_behavior, return_sites = _return_info(node)
    extensions: dict[str, Any] = {}
    if method:
        receiver_name, receiver_type = _receiver_parts(node, go_file.source)
        if receiver_type:
            extensions["receiver_type"] = receiver_type
        if receiver_name:
            extensions["receiver_name"] = receiver_name
    node_id = context.add_definition(
        go_file,
        node,
        kind="method" if method else "function",
        name=name,
        qualified_name=qualified_name,
        declaration_kind=declaration_kind,
        parent_id=owner_id,
        arity=arity,
        return_behavior=return_behavior,
        return_sites=return_sites,
        extensions=extensions,
        owner_q=owner_q,
    )
    if method:
        receiver_name, receiver_type = _receiver_parts(node, go_file.source)
        if receiver_name and receiver_type:
            context.variable_types[(node_id, receiver_name)] = receiver_type


def _add_interface_method(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    owner_node = _interface_method_owner(context, go_file, node)
    if owner_node is None:
        return
    owner_id = context.definition_by_node.get((go_file.relative_path, owner_node.id))
    if owner_id is None:
        return
    owner_symbol = context.definitions[owner_id]
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _node_text(name_node, go_file.source).strip()
    arity = _parameter_arity(node.child_by_field_name("parameters"))
    qualified_name = f"{owner_symbol.qualified_name}.{name}({arity})"
    context.add_definition(
        go_file,
        node,
        kind="method",
        name=name,
        qualified_name=qualified_name,
        declaration_kind="interface_method",
        parent_id=owner_id,
        arity=arity,
        return_behavior="unknown",
        extensions={"interface_method": True},
        owner_q=owner_symbol.qualified_name,
    )


def _add_function_literal(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    parent_id = _enclosing_definition(context, go_file, node.parent) or go_file.package_node_id or go_file.module_id
    parent_symbol = context.definitions.get(parent_id)
    line = int(node.start_point[0]) + 1
    column = int(node.start_point[1])
    name = f"<lambda@{line}:{column}>"
    qualified_name = f"{parent_symbol.qualified_name if parent_symbol else go_file.package_import_path}.{name}"
    arity = _parameter_arity(node.child_by_field_name("parameters"))
    return_behavior, return_sites = _return_info(node)
    context.add_definition(
        go_file,
        node,
        kind="lambda",
        name=name,
        qualified_name=qualified_name,
        declaration_kind="lambda",
        parent_id=parent_id,
        arity=arity,
        return_behavior=return_behavior,
        return_sites=return_sites,
        extensions={"declaration_form": "function_literal"},
    )


def _collect_relations(context: GoAnalysisContext, go_file: GoFile) -> None:
    for node in _walk_tree(go_file.tree.root_node):
        if node.type == "import_spec":
            _collect_import(context, go_file, node)
        elif node.type in _TYPE_DECLARATION_TYPES or node.type in {
            "field_declaration",
            "var_spec",
            "const_spec",
            *_PARAMETER_TYPES,
        }:
            _collect_type_node(context, go_file, node.child_by_field_name("type"))
        elif node.type in {"function_declaration", "method_declaration", "method_elem"}:
            _collect_result_type(context, go_file, node)
        elif node.type == "short_var_declaration":
            _collect_short_var_types(context, go_file, node)
        elif node.type in {"var_declaration", "const_declaration"}:
            _collect_var_declaration_types(context, go_file, node)
        elif node.type == "call_expression":
            _collect_call(context, go_file, node)
        elif node.type == "composite_literal":
            _collect_composite_literal(context, go_file, node)
        elif node.type in {"struct_type", "interface_type"}:
            _collect_embedded_types(context, go_file, node)


def _collect_import(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    path = _import_path(node, go_file.source)
    raw = _node_text(node, go_file.source).strip()
    if not path:
        target = context.external_node(f"import:{raw}", unknown=True, go_file=go_file, span=_span_for_tree(node))
        status, confidence = "unresolved", 0.2
        _diagnose_unresolved(context, go_file, node, "unresolved_import", f"Go importを解釈できません: {raw}")
    else:
        target, status, confidence = _resolve_import(context, go_file, path, node)
    _add_relation(
        context,
        go_file.module_id,
        target,
        "imports",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"import": raw, "path": path},
    )


def _resolve_import(
    context: GoAnalysisContext,
    go_file: GoFile,
    path: str,
    node: Any,
) -> tuple[str, str, float]:
    local_path = _resolve_relative_import_path(go_file, path, context.module_path)
    candidates = context.packages_by_import_path.get(local_path or path, [])
    if len(candidates) == 1 and candidates[0].node_id:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(f"import:{path}", unknown=True, go_file=go_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, go_file, node, "unresolved_import", f"Go importを一意に解決できません: {path}")
        return target, "unresolved", 0.2
    if context.module_path and (path == context.module_path or path.startswith(f"{context.module_path}/")):
        target = context.external_node(f"import:{path}", unknown=True, go_file=go_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, go_file, node, "unresolved_import", f"対象内Go packageを解決できません: {path}")
        return target, "unresolved", 0.2
    return context.external_node(f"import:{path}"), "external", 0.75


def _collect_type_node(context: GoAnalysisContext, go_file: GoFile, type_node: Any | None) -> None:
    if type_node is None:
        return
    source_id = _enclosing_definition(context, go_file, type_node) or go_file.package_node_id or go_file.module_id
    for reference, reference_node in _type_references(type_node, go_file.source):
        if reference in _BUILTIN_TYPES:
            continue
        target, status, confidence = _resolve_type(context, go_file, reference, reference_node)
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
    primary = _primary_type_name(type_node, go_file.source)
    for name in _declared_names(_parent_declaration(type_node), go_file.source):
        if primary:
            context.variable_types[(source_id, name)] = primary


def _collect_result_type(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    result = node.child_by_field_name("result")
    if result is None:
        return
    _collect_type_node(context, go_file, result)


def _collect_var_declaration_types(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    for child in node.named_children:
        if child.type in {"var_spec", "const_spec"}:
            _collect_type_node(context, go_file, child.child_by_field_name("type"))


def _collect_short_var_types(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    caller = _enclosing_definition(context, go_file, node) or go_file.package_node_id or go_file.module_id
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    names = [
        _node_text(child, go_file.source).strip()
        for child in _walk_named_children(left)
        if child.type in {"identifier", "field_identifier"}
    ]
    values = list(_walk_named_children(right))
    for index, name in enumerate(names):
        if not name or index >= len(values):
            continue
        reference = _expression_type_reference(values[index], go_file.source)
        if reference:
            context.variable_types[(caller, name)] = reference


def _collect_call(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    caller = _enclosing_definition(context, go_file, node) or go_file.package_node_id or go_file.module_id
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    name, receiver = _invocation_parts(function, go_file.source)
    arity = _argument_arity(arguments)
    expression = _node_text(node, go_file.source).strip()
    if name == "new":
        target, status, confidence = _resolve_new_call(context, go_file, arguments, node)
    else:
        target, status, confidence = _resolve_call(context, go_file, caller, name, receiver, arity, node)
    if status == "unresolved":
        _diagnose_unresolved(
            context,
            go_file,
            node,
            "unresolved_call",
            f"Go呼び出しを静的に解決できません: {expression}",
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


def _resolve_new_call(
    context: GoAnalysisContext,
    go_file: GoFile,
    arguments: Any | None,
    node: Any,
) -> tuple[str, str, float]:
    first_argument = arguments.named_children[0] if arguments is not None and arguments.named_children else None
    reference = _primary_type_name(first_argument, go_file.source) if first_argument is not None else ""
    candidates = _type_candidates(context, go_file, reference) if reference else []
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        return (
            context.external_node(f"new:{reference}", unknown=True, go_file=go_file, span=_span_for_tree(node)),
            "unresolved",
            0.2,
        )
    if reference:
        return context.external_node(f"new:{reference}"), "external", 0.7
    return context.external_node("new:<unknown>", unknown=True, go_file=go_file, span=_span_for_tree(node)), "unresolved", 0.2


def _resolve_call(
    context: GoAnalysisContext,
    go_file: GoFile,
    caller: str,
    name: str,
    receiver: str,
    arity: int,
    node: Any,
) -> tuple[str, str, float]:
    if not name:
        return (
            context.external_node("call:<unknown>", unknown=True, go_file=go_file, span=_span_for_tree(node)),
            "unresolved",
            0.1,
        )
    receiver_path = _import_path_for_receiver(context, go_file, receiver)
    if receiver_path:
        candidates = _functions_for_package(context, receiver_path, name, arity)
        if not candidates:
            return context.external_node(f"call:{receiver}.{name}/{arity}"), "external", 0.7
    elif receiver:
        receiver_reference = _receiver_type_reference(context, go_file, caller, receiver)
        type_candidates = _type_candidates(context, go_file, receiver_reference) if receiver_reference else []
        candidates = []
        for type_symbol in type_candidates:
            candidates.extend(_methods_for_owner(context, type_symbol.qualified_name, name, arity))
        if not type_candidates:
            return (
                context.external_node(
                    f"call:{receiver}.{name}/{arity}",
                    unknown=True,
                    go_file=go_file,
                    span=_span_for_tree(node),
                ),
                "unresolved",
                0.2,
            )
    else:
        caller_symbol = context.definitions.get(caller)
        owner_q = caller_symbol.owner_q if caller_symbol else None
        candidates = _methods_for_owner(context, owner_q, name, arity) if owner_q else []
        if not candidates:
            package_q = _package_for_file(go_file).import_path
            candidates = _functions_for_package(context, package_q, name, arity)
        if not candidates:
            candidates = [symbol for symbol in context.symbols_for_name(name, kinds={"function"}) if symbol.arity == arity]
    candidates = _unique_symbols(candidates)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        return (
            context.external_node(
                f"call:{receiver + '.' if receiver else ''}{name}/{arity}",
                unknown=True,
                go_file=go_file,
                span=_span_for_tree(node),
            ),
            "unresolved",
            0.2,
        )
    return context.external_node(f"call:{receiver + '.' if receiver else ''}{name}/{arity}"), "external", 0.7


def _collect_composite_literal(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return
    reference = _primary_type_name(type_node, go_file.source)
    if not reference or reference in _BUILTIN_TYPES:
        return
    caller = _enclosing_definition(context, go_file, node) or go_file.package_node_id or go_file.module_id
    target, status, confidence = _resolve_type(context, go_file, reference, type_node)
    _add_relation(
        context,
        caller,
        target,
        "uses",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(type_node),
        detail={"reference": reference, "kind": "composite_literal"},
    )


def _collect_embedded_types(context: GoAnalysisContext, go_file: GoFile, node: Any) -> None:
    owner_node = node.parent
    while owner_node is not None and owner_node.type != "type_spec":
        owner_node = owner_node.parent
    if owner_node is None:
        return
    owner_id = context.definition_by_node.get((go_file.relative_path, owner_node.id))
    if owner_id is None:
        return
    if node.type == "struct_type":
        candidates = [child for child in _walk_tree(node) if child.type == "field_declaration"]
        for field in candidates:
            if field.child_by_field_name("name") is not None:
                continue
            type_node = field.child_by_field_name("type")
            _add_embedded_relation(context, go_file, owner_id, type_node)
    else:
        for child in node.named_children:
            if child.type != "type_elem":
                continue
            if any(grandchild.type == "method_elem" for grandchild in child.named_children):
                continue
            _add_embedded_relation(context, go_file, owner_id, child)


def _add_embedded_relation(context: GoAnalysisContext, go_file: GoFile, source_id: str, node: Any | None) -> None:
    if node is None:
        return
    reference = _primary_type_name(node, go_file.source)
    if not reference or reference in _BUILTIN_TYPES:
        return
    target, status, confidence = _resolve_type(context, go_file, reference, node)
    _add_relation(
        context,
        source_id,
        target,
        "inherits",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"reference": reference, "role": "embedded"},
    )


def _resolve_type(
    context: GoAnalysisContext,
    go_file: GoFile,
    reference: str,
    node: Any,
) -> tuple[str, str, float]:
    candidates = _type_candidates(context, go_file, reference)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(f"type:{reference}", unknown=True, go_file=go_file, span=_span_for_tree(node))
        _diagnose_unresolved(context, go_file, node, "unresolved_type", f"Go型を一意に解決できません: {reference}")
        return target, "unresolved", 0.2
    return context.external_node(f"type:{reference}"), "external", 0.7


def _type_candidates(context: GoAnalysisContext, go_file: GoFile, reference: str) -> list[GoSymbol]:
    cleaned = _clean_type_reference(reference)
    if not cleaned or cleaned in _BUILTIN_TYPES:
        return []
    possible_names: list[str] = []
    if "." in cleaned:
        prefix, suffix = cleaned.split(".", 1)
        imported_path = context.imports_by_file.get(go_file.relative_path, GoImportInfo()).aliases.get(prefix)
        if imported_path:
            possible_names.append(f"{imported_path}.{suffix}")
        possible_names.append(cleaned)
    else:
        possible_names.append(f"{go_file.package_import_path}.{cleaned}")
    candidates: list[GoSymbol] = []
    for name in possible_names:
        candidates.extend(context.symbols_for_qualified_name(name, kinds=_TYPE_KINDS))
    if not candidates:
        candidates = context.symbols_for_name(cleaned.rsplit(".", 1)[-1], kinds=_TYPE_KINDS)
    return _unique_symbols(candidates)


def _functions_for_package(context: GoAnalysisContext, package_path: str, name: str, arity: int) -> list[GoSymbol]:
    return [
        symbol
        for symbol in context.symbols_for_name(name, kinds={"function"})
        if symbol.arity == arity and symbol.qualified_name.startswith(f"{package_path}.")
    ]


def _methods_for_owner(context: GoAnalysisContext, owner_q: str | None, name: str, arity: int) -> list[GoSymbol]:
    if not owner_q:
        return []
    return [
        symbol
        for symbol in context.symbols_for_name(name, kinds={"method"})
        if symbol.arity == arity and symbol.owner_q == owner_q
    ]


def _receiver_type_reference(
    context: GoAnalysisContext,
    go_file: GoFile,
    caller: str,
    receiver: str,
) -> str | None:
    cleaned = receiver.strip()
    if not cleaned:
        return None
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1].strip()
    segments = [part for part in cleaned.split(".") if part]
    if not segments:
        return None
    first = segments[0]
    current_type = _lookup_variable_type(context, caller, first)
    if current_type is None:
        if len(segments) == 1 and _type_candidates(context, go_file, first):
            return first
        return None
    for field_name in segments[1:]:
        field_type = _field_type_reference(context, go_file, current_type, field_name)
        if field_type is None:
            return None
        current_type = field_type
    return current_type


def _lookup_variable_type(context: GoAnalysisContext, caller: str, name: str) -> str | None:
    reference = context.variable_types.get((caller, name))
    if reference:
        return reference
    symbol = context.definitions.get(caller)
    if symbol is None or not symbol.owner_q:
        return None
    for type_symbol in context.symbols_for_qualified_name(symbol.owner_q, kinds=_TYPE_KINDS):
        reference = context.variable_types.get((type_symbol.node_id, name))
        if reference:
            return reference
    return None


def _field_type_reference(
    context: GoAnalysisContext,
    go_file: GoFile,
    owner_reference: str,
    field_name: str,
) -> str | None:
    for type_symbol in _type_candidates(context, go_file, owner_reference):
        reference = context.variable_types.get((type_symbol.node_id, field_name))
        if reference:
            return reference
    return None


def _import_path_for_receiver(context: GoAnalysisContext, go_file: GoFile, receiver: str) -> str | None:
    if not receiver or "." in receiver:
        return None
    return context.imports_by_file.get(go_file.relative_path, GoImportInfo()).aliases.get(receiver)


def _enclosing_definition(context: GoAnalysisContext, go_file: GoFile, node: Any | None) -> str | None:
    current = node
    while current is not None:
        node_id = context.definition_by_node.get((go_file.relative_path, current.id))
        if node_id is not None:
            return node_id
        current = current.parent
    return None


def _interface_method_owner(context: GoAnalysisContext, go_file: GoFile, node: Any) -> Any | None:
    current = node.parent
    while current is not None:
        if current.type == "interface_type":
            parent = current.parent
            while parent is not None and parent.type != "type_spec":
                parent = parent.parent
            return parent
        current = current.parent
    return None


def _receiver_parts(node: Any, source: bytes) -> tuple[str, str]:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return "", ""
    declaration = next((child for child in receiver.named_children if child.type == "parameter_declaration"), None)
    if declaration is None:
        return "", ""
    name_node = declaration.child_by_field_name("name")
    type_node = declaration.child_by_field_name("type")
    return (
        _node_text(name_node, source).strip() if name_node is not None else "",
        _primary_type_name(type_node, source),
    )


def _receiver_type(node: Any, source: bytes) -> str:
    return _receiver_parts(node, source)[1]


def _return_info(node: Any) -> tuple[str, list[dict[str, Any]]]:
    sites: list[dict[str, Any]] = []
    has_value = False
    has_none = False

    def visit(current: Any) -> None:
        nonlocal has_value, has_none
        if current is not node and current.type in {"func_literal", "function_declaration", "method_declaration"}:
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


def _type_references(node: Any, source: bytes) -> list[tuple[str, Any]]:
    references: list[tuple[str, Any]] = []

    def visit(current: Any) -> None:
        if current is None:
            return
        if current.type == "qualified_type":
            reference = _primary_type_name(current, source)
            if reference:
                references.append((reference, current))
            return
        if current.type == "generic_type":
            reference = _primary_type_name(current, source)
            if reference:
                references.append((reference, current))
            for child in current.named_children:
                if child.type != "type_identifier" or _node_text(child, source).strip() != reference:
                    visit(child)
            return
        if current.type == "type_identifier":
            reference = _primary_type_name(current, source)
            if reference:
                references.append((reference, current))
            return
        if current.type in {"package_identifier", "field_identifier", "identifier", "type_parameter"}:
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


def _declared_names(node: Any | None, source: bytes) -> list[str]:
    if node is None:
        return []
    names: list[str] = []
    for child in node.named_children:
        if child.type in {"identifier", "field_identifier"}:
            name = _node_text(child, source).strip()
            if name and name not in names:
                names.append(name)
    name_node = node.child_by_field_name("name")
    if name_node is not None and name_node.type in {"identifier", "field_identifier"}:
        name = _node_text(name_node, source).strip()
        if name and name not in names:
            names.append(name)
    return names


def _parent_declaration(node: Any | None) -> Any | None:
    current = node.parent if node is not None else None
    while current is not None:
        if current.type in {"field_declaration", "var_spec", "const_spec", *_PARAMETER_TYPES}:
            return current
        current = current.parent
    return None


def _expression_type_reference(node: Any, source: bytes) -> str:
    if node.type == "composite_literal":
        type_node = node.child_by_field_name("type")
        return _primary_type_name(type_node, source)
    if node.type == "call_expression":
        function = node.child_by_field_name("function")
        if function is not None and _node_text(function, source).strip() == "new":
            arguments = node.child_by_field_name("arguments")
            if arguments is not None and arguments.named_children:
                return _primary_type_name(arguments.named_children[0], source)
    if node.type in {"unary_expression", "parenthesized_expression"} and node.named_children:
        return _expression_type_reference(node.named_children[-1], source)
    return ""


def _type_form(node: Any, type_node: Any, source: bytes) -> tuple[str, str, str]:
    raw = _node_text(node, source).strip()
    if node.type == "type_alias" or re.search(r"^type\s+[^=]+=", raw):
        return "alias", "type", "alias"
    if type_node.type == "struct_type":
        return "struct", "type", "struct"
    if type_node.type == "interface_type":
        return "interface", "interface", "interface"
    if type_node.type == "function_type":
        return "function", "type", "function_type"
    if type_node.type == "map_type":
        return "map", "type", "map"
    if type_node.type in {"slice_type", "array_type"}:
        return type_node.type.removesuffix("_type"), "type", type_node.type.removesuffix("_type")
    return "named", "type", "named"


def _package_name(tree: Any, source: bytes) -> str | None:
    for node in _walk_tree(tree.root_node):
        if node.type == "package_clause" and node.named_children:
            return _node_text(node.named_children[0], source).strip()
    return None


def _package_import_path(module_path: str | None, relative_directory: str, package_name: str) -> str:
    base = module_path or ""
    if relative_directory:
        base = f"{base}/{relative_directory}" if base else relative_directory
    return base or package_name


def _read_module_path(root: Path) -> str | None:
    path = root / "go.mod"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = re.search(r"(?m)^\s*module\s+([^\s]+)\s*$", text)
    return match.group(1).strip() if match else None


def _build_constraints(source: bytes) -> tuple[str, ...]:
    text = source.decode("utf-8", errors="replace")
    constraints: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//go:build"):
            constraints.append(stripped.removeprefix("//go:build").strip())
        elif stripped.startswith("// +build"):
            constraints.append(stripped.removeprefix("// +build").strip())
        elif stripped and not stripped.startswith("//"):
            break
    return tuple(constraints)


def _import_path(node: Any, source: bytes) -> str:
    path_node = node.child_by_field_name("path")
    if path_node is None:
        return ""
    raw = _node_text(path_node, source).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw.strip("`")
    return value if isinstance(value, str) else ""


def _resolve_relative_import_path(go_file: GoFile, path: str, module_path: str | None) -> str | None:
    if path.startswith(("./", "../")):
        current = Path(go_file.relative_directory or ".")
        relative = (current / path).as_posix()
        normalized = posixpath.normpath(relative).replace("\\", "/")
        normalized = normalized.removeprefix("./")
        return f"{module_path}/{normalized}" if module_path else normalized
    return path


def _package_for_file(go_file: GoFile) -> GoPackage:
    # This helper is replaced by the package metadata embedded in the file.
    return GoPackage(
        key=go_file.package_key,
        relative_directory=go_file.relative_directory,
        package_name=go_file.package_name,
        import_path=go_file.package_import_path,
    )


def _clean_type_reference(value: str) -> str:
    raw = value.strip()
    raw = re.sub(r"\s+", "", raw)
    raw = raw.removeprefix("*").removeprefix("...")
    raw = raw.replace("<-chan", "").replace("chan<-", "").replace("chan", "")
    raw = raw.replace("[]", "")
    if raw.startswith("["):
        closing = raw.find("]")
        if closing >= 0:
            raw = raw[closing + 1 :]
    raw = _strip_generic_arguments(raw)
    return raw


def _primary_type_name(node: Any | None, source: bytes) -> str:
    if node is None:
        return ""
    raw = _node_text(node, source).strip()
    if not raw:
        return ""
    cleaned = _clean_type_reference(raw)
    if re.fullmatch(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*", cleaned):
        return cleaned
    return ""


def _strip_generic_arguments(value: str) -> str:
    if "[" not in value:
        return value
    result: list[str] = []
    depth = 0
    for char in value:
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif depth == 0:
            result.append(char)
    return "".join(result)


def _invocation_parts(node: Any | None, source: bytes) -> tuple[str, str]:
    if node is None:
        return "", ""
    if node.type == "selector_expression":
        named = node.named_children
        if not named:
            return "", ""
        name_node = named[-1]
        name = _node_text(name_node, source).strip()
        receiver = source[node.start_byte : name_node.start_byte].decode("utf-8", errors="replace").rstrip(".").strip()
        return name, receiver
    return _node_text(node, source).strip(), ""


def _parameter_arity(node: Any | None) -> int:
    if node is None:
        return 0
    count = 0
    for child in node.named_children:
        if child.type not in _PARAMETER_TYPES:
            continue
        names = [item for item in child.named_children if item.type in {"identifier", "field_identifier"}]
        count += len(names) or 1
    return count


def _argument_arity(node: Any | None) -> int:
    return len(node.named_children) if node is not None else 0


def _visibility_for_name(name: str) -> str:
    if not name:
        return "unknown"
    return "public" if name[0].isupper() else "private"


def _owner_qualified_name(symbol: GoSymbol | None) -> str | None:
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS:
        return symbol.qualified_name
    return symbol.owner_q


def _unique_symbols(symbols: Iterable[GoSymbol]) -> list[GoSymbol]:
    result: list[GoSymbol] = []
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
        if stripped:
            return stripped[:limit]
    return _node_text(node, source).strip()[:limit]


def _walk_tree(node: Any) -> Iterable[Any]:
    yield node
    for child in node.children:
        yield from _walk_tree(child)


def _walk_named_children(node: Any | None) -> Iterable[Any]:
    if node is None:
        return
    yield from node.named_children


def _span_for_tree(node: Any | None) -> dict[str, int] | None:
    if node is None or not hasattr(node, "start_point"):
        return None
    return {
        "start_line": int(node.start_point[0]) + 1,
        "start_col": int(node.start_point[1]),
        "end_line": int(node.end_point[0]) + 1,
        "end_col": int(node.end_point[1]),
    }


def _package_span(tree: Any, source: bytes) -> dict[str, int] | None:
    for node in _walk_tree(tree.root_node):
        if node.type == "package_clause":
            return _span_for_tree(node)
    return _span_for_tree(tree.root_node)


def _unique_id(existing: dict[str, Any], base: str, salt: int) -> str:
    if base not in existing:
        return base
    return f"{base}~{salt}"


def _add_relation(
    context: GoAnalysisContext,
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
    edge_id = f"go-edge:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
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
    context: GoAnalysisContext,
    go_file: GoFile,
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
        go_file=go_file,
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
