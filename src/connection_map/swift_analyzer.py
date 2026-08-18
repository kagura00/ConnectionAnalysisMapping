"""Tree-sitter based static analyzer for Swift source files.

The Swift adapter intentionally stays at the syntax level. It does not invoke
the Swift compiler, SwiftPM, Xcode, macros, or target code.
"""

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

from .config import AnalysisConfig, discover_source_files, repository_id
from .contract import canonical_sha256, validate_document
from .model import GraphBuilder

ANALYZER_NAME = "connection-map-swift-tree-sitter"
ANALYZER_VERSION = "0.2.0"
SUPPORTED_LANGUAGES = {"swift"}

_PARSER_RECOVERY_STRATEGY = "mask-conditional-directives-and-string-continuations"
_CONDITIONAL_DIRECTIVE_RE = re.compile(rb"^[ \t]*#(?:if|elseif|else|endif)\b")

_TYPE_KINDS = {"class", "interface", "type"}
_SCOPE_KINDS = {
    "module",
    "namespace",
    "class",
    "interface",
    "type",
    "function",
    "method",
    "lambda",
}
_DEFINITION_NODE_TYPES = {
    "class_declaration",
    "protocol_declaration",
    "typealias_declaration",
    "associatedtype_declaration",
    "function_declaration",
    "protocol_function_declaration",
    "init_declaration",
    "initializer_declaration",
    "deinit_declaration",
    "deinitializer_declaration",
    "subscript_declaration",
    "lambda_literal",
    "closure_expression",
}
_PRIMITIVE_TYPES = {
    "Any",
    "AnyObject",
    "Bool",
    "Character",
    "Double",
    "Float",
    "Int",
    "Int8",
    "Int16",
    "Int32",
    "Int64",
    "Never",
    "ObjectIdentifier",
    "Optional",
    "Set",
    "String",
    "UInt",
    "UInt8",
    "UInt16",
    "UInt32",
    "UInt64",
    "Void",
    "Array",
    "Dictionary",
    "Result",
    "Swift",
}


class SwiftAnalyzerDependencyError(ValueError):
    """Raised when the optional Swift parser dependency is unavailable."""


@dataclass(slots=True)
class SwiftFile:
    path: Path
    relative_path: str
    source: bytes
    tree: Any
    module_id: str
    parser_recovery: str | None = None
    original_parse_issues: tuple[int, int] = (0, 0)
    parse_issues: tuple[int, int] = (0, 0)


@dataclass(frozen=True, slots=True)
class SwiftSymbol:
    node_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str
    declaration_kind: str
    arity: int | None = None
    owner_q: str | None = None
    argument_labels: tuple[str, ...] = ()
    receiver_type: str | None = None


@dataclass(slots=True)
class SwiftAnalysisContext:
    root: Path
    config: AnalysisConfig
    builder: GraphBuilder
    files: list[SwiftFile] = field(default_factory=list)
    files_by_path: dict[str, SwiftFile] = field(default_factory=dict)
    definitions: dict[str, SwiftSymbol] = field(default_factory=dict)
    definition_nodes: dict[str, Any] = field(default_factory=dict)
    definition_by_node: dict[tuple[str, int], str] = field(default_factory=dict)
    symbols_by_name: dict[str, list[SwiftSymbol]] = field(default_factory=dict)
    symbols_by_qualified_name: dict[str, list[SwiftSymbol]] = field(default_factory=dict)
    variable_types: dict[tuple[str, str], str] = field(default_factory=dict)
    extension_targets: dict[str, str] = field(default_factory=dict)
    parent_types: dict[str, str] = field(default_factory=dict)
    _external_node_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_module(self, swift_file: SwiftFile) -> None:
        self.builder.add_node(
            {
                "id": swift_file.module_id,
                "kind": "module",
                "qualified_name": swift_file.relative_path,
                "display_name": swift_file.relative_path,
                "file": swift_file.relative_path,
                "span": _span_for_tree(swift_file.tree.root_node),
                "parent_id": None,
                "visibility": "public",
                "signature": f"file {swift_file.relative_path}",
                "extensions": {"language": "swift", "grammar": "swift"},
            }
        )

    def add_definition(
        self,
        swift_file: SwiftFile,
        tree_node: Any,
        *,
        kind: str,
        name: str,
        qualified_name: str,
        declaration_kind: str,
        parent_id: str,
        arity: int | None = None,
        argument_labels: tuple[str, ...] = (),
        extensions: dict[str, Any] | None = None,
        return_behavior: str | None = None,
        return_sites: list[dict[str, Any]] | None = None,
    ) -> str:
        base_id = f"swift:{swift_file.relative_path}:{qualified_name}:{kind}"
        node_id = _unique_id(self.builder.nodes, base_id, int(tree_node.start_byte))
        node_extensions: dict[str, Any] = {
            "language": "swift",
            "grammar": "swift",
            "declaration_kind": declaration_kind,
        }
        if extensions:
            node_extensions.update(extensions)
        node: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": swift_file.relative_path,
            "span": _span_for_tree(tree_node),
            "parent_id": parent_id,
            "visibility": _visibility_for_node(tree_node, swift_file.source),
            "signature": _signature_for_node(tree_node, swift_file.source),
            "extensions": node_extensions,
        }
        if return_behavior is not None:
            node["return_behavior"] = return_behavior
        if return_sites:
            node["return_sites"] = return_sites
        if kind in {"function", "method", "lambda"}:
            node["execution_kind"] = "async" if _is_async(tree_node, swift_file.source) else "sync"
        self.builder.add_node(node)

        parent_symbol = self.definitions.get(parent_id)
        owner_q = _owner_type_q(parent_symbol)
        symbol = SwiftSymbol(
            node_id=node_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=swift_file.relative_path,
            declaration_kind=declaration_kind,
            arity=arity,
            owner_q=owner_q,
            argument_labels=argument_labels,
            receiver_type=(extensions or {}).get("extended_type"),
        )
        self.definitions[node_id] = symbol
        self.definition_nodes[node_id] = tree_node
        self.definition_by_node[(swift_file.relative_path, tree_node.id)] = node_id
        self.symbols_by_name.setdefault(name, []).append(symbol)
        self.symbols_by_qualified_name.setdefault(qualified_name, []).append(symbol)
        if declaration_kind == "extension":
            target = (extensions or {}).get("extended_type")
            if target:
                self.extension_targets[qualified_name] = target
        if declaration_kind in {"class", "actor", "struct", "enum", "protocol"}:
            self._index_inheritance_owner(node_id, swift_file, tree_node)
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

    def _index_inheritance_owner(self, node_id: str, swift_file: SwiftFile, node: Any) -> None:
        specs = [child for child in node.named_children if child.type == "inheritance_specifier"]
        if not specs:
            return
        reference = _type_name_from_node(specs[0].child_by_field_name("inherits_from"), swift_file.source)
        if reference:
            self.parent_types[self.definitions[node_id].qualified_name] = reference

    def symbols_for_name(self, name: str, *, kinds: set[str] | None = None) -> list[SwiftSymbol]:
        values = list(self.symbols_by_name.get(name, []))
        if kinds is not None:
            values = [item for item in values if item.kind in kinds]
        return values

    def external_node(
        self,
        label: str,
        *,
        unknown: bool = False,
        swift_file: SwiftFile | None = None,
        span: dict[str, int] | None = None,
    ) -> str:
        kind = "unknown" if unknown else "external"
        key = (kind, label)
        existing = self._external_node_ids.get(key)
        if existing is not None:
            return existing
        digest = hashlib.sha256("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
        node_id = f"swift:{kind}:{digest}"
        self.builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": label or "<unknown>",
                "display_name": label or "<unknown>",
                "file": swift_file.relative_path if unknown and swift_file else None,
                "span": span if unknown else None,
                "parent_id": None,
                "visibility": "unknown",
                "extensions": {
                    "language": "swift",
                    "grammar": "swift",
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
        swift_file: SwiftFile | None = None,
        tree_node: Any | None = None,
        node_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.builder.add_diagnostic(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "file": swift_file.relative_path if swift_file else None,
                "span": _span_for_tree(tree_node) if tree_node is not None else None,
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
    """Analyze Swift source without compiling or executing target code."""

    active_config = config or AnalysisConfig(language="swift")
    active_config.validate()
    if active_config.language != "swift":
        raise ValueError("Swift analyzer requires language = 'swift'")
    if set(active_config.active_languages()) != SUPPORTED_LANGUAGES:
        raise ValueError("Swift analyzer supports only swift")

    root = root.resolve()
    builder = GraphBuilder()
    context = SwiftAnalysisContext(root, active_config, builder)
    files, skipped = discover_source_files(root, active_config, languages={"swift"})
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped Swift source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            swift_file = _parse_file(path, root)
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
        context.files.append(swift_file)
        context.files_by_path[relative_path] = swift_file

    for swift_file in context.files:
        context.add_module(swift_file)
        if swift_file.parser_recovery:
            context.diagnostic(
                "parser_recovery",
                "info",
                f"Tree-sitter用の構文回復を適用しました: {swift_file.relative_path}",
                swift_file=swift_file,
                details={
                    "grammar": "swift",
                    "strategy": swift_file.parser_recovery,
                    "original_error_nodes": swift_file.original_parse_issues[0],
                    "original_missing_nodes": swift_file.original_parse_issues[1],
                    "recovered_error_nodes": swift_file.parse_issues[0],
                    "recovered_missing_nodes": swift_file.parse_issues[1],
                },
            )
        if swift_file.parse_issues != (0, 0):
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {swift_file.relative_path}",
                swift_file=swift_file,
                tree_node=swift_file.tree.root_node,
                details={
                    "grammar": "swift",
                    "strategy": swift_file.parser_recovery,
                    "error_nodes": swift_file.parse_issues[0],
                    "missing_nodes": swift_file.parse_issues[1],
                },
            )

    for swift_file in context.files:
        _collect_definitions(context, swift_file)
    for swift_file in context.files:
        _collect_variable_types(context, swift_file)
    for swift_file in context.files:
        _collect_relations(context, swift_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "swift",
        "languages": ["swift"],
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
            "grammars": ["swift"],
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
        raise SwiftAnalyzerDependencyError(
            "Swift解析には任意依存が必要です。'uv sync --extra swift' または "
            "'uv run --with tree-sitter-language-pack==1.14.3' を実行してから再試行してください。"
        ) from exc
    try:
        return get_parser("swift")
    except Exception as exc:  # pragma: no cover - package-specific error wording
        raise SwiftAnalyzerDependencyError(f"Tree-sitter grammar 'swift' を読み込めません: {exc}") from exc


def _parser_package_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _parse_file(path: Path, root: Path) -> SwiftFile:
    source = path.read_bytes()
    relative_path = path.relative_to(root).as_posix()
    parser = _parser_for()
    tree = parser.parse(source)
    original_issues = _tree_issue_counts(tree.root_node)
    parser_recovery = None
    if original_issues != (0, 0):
        recovered_source = _recover_parser_source(source)
        if recovered_source != source:
            recovered_tree = parser.parse(recovered_source)
            recovered_issues = _tree_issue_counts(recovered_tree.root_node)
            if recovered_issues < original_issues:
                tree = recovered_tree
                parser_recovery = _PARSER_RECOVERY_STRATEGY
    return SwiftFile(
        path=path,
        relative_path=relative_path,
        source=source,
        tree=tree,
        module_id=f"swift:{relative_path}:module",
        parser_recovery=parser_recovery,
        original_parse_issues=original_issues,
        parse_issues=_tree_issue_counts(tree.root_node),
    )


def _tree_issue_counts(root_node: Any) -> tuple[int, int]:
    """Return concrete and missing recovery nodes in a Tree-sitter tree."""

    error_nodes = 0
    missing_nodes = 0
    pending = [root_node]
    while pending:
        node = pending.pop()
        if node.type == "ERROR":
            error_nodes += 1
        if getattr(node, "is_missing", False):
            missing_nodes += 1
        pending.extend(node.children)
    return error_nodes, missing_nodes


def _recover_parser_source(source: bytes) -> bytes:
    """Mask known grammar gaps while preserving every source byte offset.

    The original source remains attached to the graph. The normalized bytes are
    only passed to Tree-sitter so that both conditional branches and multiline
    string continuations can be indexed without shifting spans. Compiler
    directives inside comments or strings are deliberately left untouched.
    """

    normalized = bytearray(source)
    state = "code"
    block_comment_depth = 0
    string_hashes = 0
    string_multiline = False
    line_start = 0
    index = 0

    while index < len(source):
        if state == "line_comment":
            if source[index] == 10:
                state = "code"
                line_start = index + 1
            index += 1
            continue

        if state == "block_comment":
            if source[index : index + 2] == b"/*":
                block_comment_depth += 1
                index += 2
                continue
            if source[index : index + 2] == b"*/":
                block_comment_depth -= 1
                index += 2
                if block_comment_depth == 0:
                    state = "code"
                continue
            if source[index] == 10:
                line_start = index + 1
            index += 1
            continue

        if state == "string":
            closing = (b'"""' if string_multiline else b'"') + (b"#" * string_hashes)
            if source.startswith(closing, index):
                index += len(closing)
                state = "code"
                continue
            if source[index] == 92 and index + 1 < len(source):
                if string_multiline and source[index + 1] in (10, 13):
                    normalized[index] = 32
                    index += 1
                    continue
                if string_hashes == 0:
                    index += 2
                    continue
            if source[index] == 10:
                line_start = index + 1
            index += 1
            continue

        if index == line_start:
            directive = _CONDITIONAL_DIRECTIVE_RE.match(source[index:])
            if directive:
                end = index + len(directive.group(0))
                while end < len(source) and source[end] not in (10, 13):
                    end += 1
                for offset in range(index, end):
                    normalized[offset] = 32
                index = end
                continue

        if source[index : index + 2] == b"//":
            state = "line_comment"
            index += 2
            continue
        if source[index : index + 2] == b"/*":
            state = "block_comment"
            block_comment_depth = 1
            index += 2
            continue

        delimiter = _string_delimiter(source, index)
        if delimiter is not None:
            string_hashes, string_multiline, index = delimiter
            state = "string"
            continue
        if source[index] == 10:
            line_start = index + 1
        index += 1

    return bytes(normalized)


def _string_delimiter(source: bytes, index: int) -> tuple[int, bool, int] | None:
    """Return raw hash count, multiline flag, and first body byte."""

    cursor = index
    hashes = 0
    while cursor < len(source) and source[cursor] == 35:
        hashes += 1
        cursor += 1
    if source[cursor : cursor + 3] == b'"""':
        return hashes, True, cursor + 3
    if source[cursor : cursor + 1] == b'"':
        return hashes, False, cursor + 1
    return None


def _collect_definitions(context: SwiftAnalysisContext, swift_file: SwiftFile) -> None:
    for node in _walk_tree(swift_file.tree.root_node):
        candidate = _definition_candidate(swift_file, node)
        if candidate is None:
            continue
        kind, display_name, identity_name, declaration_kind, extensions, arity, labels = candidate
        parent_id = _scope_parent(context, swift_file, node)
        parent_symbol = context.definitions.get(parent_id)
        if kind == "method" and (
            parent_id == swift_file.module_id
            or parent_symbol is None
            or parent_symbol.kind in {"module", "function", "lambda"}
        ):
            kind = "function"
        qualified_name = _qualified_name(
            context,
            swift_file,
            parent_id,
            identity_name,
            kind=kind,
            node=node,
        )
        return_behavior = None
        return_sites = None
        if kind in {"function", "method", "lambda"} and declaration_kind not in {"deinitializer", "constructor"}:
            return_behavior, return_sites = _return_info(node, swift_file.source)
            if kind == "lambda" and return_behavior == "returns_value" and not return_sites:
                extensions["return_inference"] = "single_expression"
        context.add_definition(
            swift_file,
            node,
            kind=kind,
            name=display_name,
            qualified_name=qualified_name,
            declaration_kind=declaration_kind,
            parent_id=parent_id,
            arity=arity,
            argument_labels=labels,
            extensions=extensions,
            return_behavior=return_behavior,
            return_sites=return_sites,
        )


def _definition_candidate(
    swift_file: SwiftFile,
    node: Any,
) -> tuple[str, str, str, str, dict[str, Any], int | None, tuple[str, ...]] | None:
    source = swift_file.source
    if node.type == "class_declaration":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        raw = _node_text(node, source).lstrip()
        name = _type_name_from_node(name_node, source) or "<anonymous>"
        if raw.startswith("extension"):
            target = name
            return (
                "namespace",
                f"extension {target}",
                f"extension:{target}@{int(node.start_point[0]) + 1}:{int(node.start_point[1])}",
                "extension",
                {"declaration_form": "extension", "extended_type": target},
                None,
                (),
            )
        for form, kind in (("actor", "class"), ("struct", "type"), ("enum", "type"), ("class", "class")):
            if re.match(rf"{form}\b", raw):
                return kind, name, name, form, {"declaration_form": form}, None, ()
        return "class", name, name, "class", {"declaration_form": "class"}, None, ()
    if node.type == "protocol_declaration":
        name_node = node.child_by_field_name("name")
        name = _type_name_from_node(name_node, source) if name_node is not None else None
        if not name:
            return None
        return "interface", name, name, "protocol", {"declaration_form": "protocol"}, None, ()
    if node.type in {"typealias_declaration", "associatedtype_declaration"}:
        name_node = node.child_by_field_name("name")
        name = _type_name_from_node(name_node, source) if name_node is not None else None
        if not name:
            return None
        form = "typealias" if node.type == "typealias_declaration" else "associatedtype"
        return "type", name, name, form, {"declaration_form": form}, None, ()
    if node.type in {
        "function_declaration",
        "protocol_function_declaration",
        "init_declaration",
        "initializer_declaration",
        "deinit_declaration",
        "deinitializer_declaration",
        "subscript_declaration",
    }:
        if node.type in {"init_declaration", "initializer_declaration"}:
            name, form, declaration_kind = "<init>", "initializer", "constructor"
        elif node.type in {"deinit_declaration", "deinitializer_declaration"}:
            name, form, declaration_kind = "<deinit>", "deinitializer", "deinitializer"
        elif node.type == "subscript_declaration":
            name, form, declaration_kind = "<subscript>", "subscript", "subscript"
        else:
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, source).strip() if name_node is not None else ""
            if not name:
                return None
            form = "protocol_requirement" if node.type == "protocol_function_declaration" else "function"
            declaration_kind = "protocol_requirement" if node.type == "protocol_function_declaration" else "function"
        arity, labels = _parameters(node, source)
        extensions: dict[str, Any] = {"declaration_form": form}
        if labels:
            extensions["argument_labels"] = list(labels)
        if name == "<init>":
            extensions["constructor"] = True
        if name == "<deinit>":
            extensions["deinitializer"] = True
        throws_kind = _throws_kind(node, source)
        if throws_kind:
            extensions["throws_kind"] = throws_kind
        if _is_async(node, source):
            extensions["async"] = True
        return "method", name, name, declaration_kind, extensions, arity, labels
    if node.type in {"lambda_literal", "closure_expression"}:
        line = int(node.start_point[0]) + 1
        col = int(node.start_point[1])
        name = f"<lambda@{line}:{col}>"
        arity, labels = _parameters(node, source)
        return (
            "lambda",
            name,
            name,
            "lambda",
            {"declaration_form": "lambda", "argument_labels": list(labels)},
            arity,
            labels,
        )
    return None


def _scope_parent(context: SwiftAnalysisContext, swift_file: SwiftFile, node: Any) -> str:
    current = node.parent
    while current is not None:
        node_id = context.definition_by_node.get((swift_file.relative_path, current.id))
        if node_id is not None and context.definitions[node_id].kind in _SCOPE_KINDS:
            return node_id
        current = current.parent
    return swift_file.module_id


def _qualified_name(
    context: SwiftAnalysisContext,
    swift_file: SwiftFile,
    parent_id: str,
    identity_name: str,
    *,
    kind: str,
    node: Any,
) -> str:
    parent = context.definitions.get(parent_id)
    if identity_name.startswith("extension:"):
        if parent is not None and parent.kind != "module":
            return f"{parent.qualified_name}.{identity_name}"
        return f"{swift_file.relative_path}:{identity_name}"
    if parent is None or parent.kind == "module":
        base = identity_name
    else:
        base = f"{parent.qualified_name}.{identity_name}"
    if kind in {"function", "method"}:
        labels = _parameters(node, swift_file.source)[1]
        label_suffix = f"({','.join(labels)})" if labels else "()"
        arity = _parameters(node, swift_file.source)[0]
        return f"{base}{label_suffix}({arity})"
    return base


def _collect_variable_types(context: SwiftAnalysisContext, swift_file: SwiftFile) -> None:
    source = swift_file.source
    for node in _walk_tree(swift_file.tree.root_node):
        if node.type == "parameter":
            caller = _enclosing_definition(context, swift_file, node)
            type_node = _parameter_type_node(node)
            name = _parameter_name(node, source)
            reference = _type_name_from_node(type_node, source) if type_node is not None else ""
            if caller and name and reference:
                context.variable_types[(caller, name)] = reference
        elif node.type == "property_declaration":
            owner = _enclosing_definition(context, swift_file, node)
            if owner is None:
                continue
            name = _property_name(node, source)
            type_node = next((child for child in node.named_children if child.type == "type_annotation"), None)
            reference = _type_name_from_node(type_node, source) if type_node is not None else ""
            if not reference:
                call = next((child for child in _walk_tree(node) if child.type == "call_expression"), None)
                first, _, _ = _call_parts(call, source) if call is not None else ("", "", None)
                reference = first if first and first[:1].isupper() else ""
            if name and reference:
                context.variable_types[(owner, name)] = reference


def _collect_relations(context: SwiftAnalysisContext, swift_file: SwiftFile) -> None:
    for node in _walk_tree(swift_file.tree.root_node):
        if node.type == "import_declaration":
            _collect_import(context, swift_file, node)
        elif node.type in {"class_declaration", "protocol_declaration"}:
            _collect_inheritance(context, swift_file, node)
        elif node.type == "user_type":
            _collect_type_use(context, swift_file, node)
        elif node.type == "call_expression":
            _collect_call(context, swift_file, node)
        elif node.type in {"function_reference_expression", "key_path_expression"}:
            _collect_reference(context, swift_file, node)


def _collect_import(context: SwiftAnalysisContext, swift_file: SwiftFile, node: Any) -> None:
    raw = _node_text(node, swift_file.source).strip()
    match = re.search(
        r"\bimport\s+(?:(?:func|class|struct|enum|let|var|protocol)\s+)?(.+)$",
        raw,
    )
    if not match:
        return
    imported = match.group(1).strip()
    target = context.external_node(f"import:{imported}")
    _add_relation(
        context,
        swift_file.module_id,
        target,
        "imports",
        resolution_status="external",
        confidence=0.9,
        source_span=_span_for_tree(node),
        detail={
            "module": imported,
            "import_kind": "testable" if "@testable" in raw else "normal",
            "exported": "@_exported" in raw,
        },
    )


def _collect_inheritance(context: SwiftAnalysisContext, swift_file: SwiftFile, node: Any) -> None:
    owner_id = context.definition_by_node.get((swift_file.relative_path, node.id))
    if owner_id is None:
        return
    form = str(context.builder.nodes[owner_id].get("extensions", {}).get("declaration_form"))
    specs = [child for child in node.named_children if child.type == "inheritance_specifier"]
    for index, spec in enumerate(specs):
        type_node = spec.child_by_field_name("inherits_from")
        reference = _type_name_from_node(type_node, swift_file.source)
        if not reference:
            continue
        target, status, confidence = _resolve_type(context, reference, swift_file, type_node or spec)
        if form == "protocol":
            role = "protocol_inherits"
        elif form == "extension":
            role = "conforms"
        elif index == 0 and form in {"class", "actor"}:
            role = "superclass"
        else:
            role = "conforms"
        _add_relation(
            context,
            owner_id,
            target,
            "inherits",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(spec),
            detail={"reference": reference, "role": role},
        )
    if form == "extension":
        extended = context.builder.nodes[owner_id].get("extensions", {}).get("extended_type")
        target, status, confidence = _resolve_type(context, str(extended), swift_file, node)
        _add_relation(
            context,
            owner_id,
            target,
            "uses",
            resolution_status=status,
            confidence=confidence,
            source_span=_span_for_tree(node),
            detail={"reference": str(extended), "role": "extension"},
        )


def _collect_type_use(context: SwiftAnalysisContext, swift_file: SwiftFile, node: Any) -> None:
    if _has_ancestor_type(node, "inheritance_specifier") or _is_declaration_name(node):
        return
    source_id = _enclosing_definition(context, swift_file, node) or swift_file.module_id
    reference = _type_name_from_node(node, swift_file.source)
    if not reference or reference in {"self", "Self"}:
        return
    target, status, confidence = _resolve_type(context, reference, swift_file, node)
    if target == source_id:
        return
    _add_relation(
        context,
        source_id,
        target,
        "uses",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail={"reference": reference, "role": _type_role(node)},
    )


def _collect_call(context: SwiftAnalysisContext, swift_file: SwiftFile, node: Any) -> None:
    caller_id = _enclosing_definition(context, swift_file, node) or swift_file.module_id
    callee, receiver, arguments = _call_parts(node, swift_file.source)
    if not callee:
        return
    arity = _argument_arity(arguments)
    labels = _argument_labels(arguments, swift_file.source)
    receiver_q = _receiver_type_reference(context, swift_file, caller_id, receiver)
    candidates: list[SwiftSymbol] = []
    call_kind = "function"
    if receiver:
        call_kind = "member"
        candidates = _method_candidates(context, receiver_q, callee, arity)
    else:
        type_candidates = context.symbols_for_name(callee, kinds=_TYPE_KINDS)
        if len(type_candidates) == 1:
            _record_call(
                context,
                swift_file,
                caller_id,
                type_candidates[0].node_id,
                node,
                callee,
                arity,
                status="resolved",
                confidence=0.95,
                detail=_call_detail(node, swift_file.source, callee, receiver, labels, arity, "constructor"),
            )
            return
        owner_q = _owner_type_q(context.definitions.get(caller_id))
        candidates = _method_candidates(context, owner_q, callee, arity)
        if not candidates:
            candidates = [
                item
                for item in context.symbols_for_name(callee, kinds={"function"})
                if item.arity == arity
            ]
    detail = _call_detail(node, swift_file.source, callee, receiver, labels, arity, call_kind)
    if len(candidates) == 1:
        _record_call(
            context,
            swift_file,
            caller_id,
            candidates[0].node_id,
            node,
            callee,
            arity,
            status="resolved",
            confidence=1.0,
            detail=detail,
        )
        return
    if len(candidates) > 1:
        _record_unresolved_call(
            context,
            swift_file,
            caller_id,
            node,
            callee,
            arity,
            detail=detail,
            reason="ambiguous_overload",
        )
        return
    local_receiver = receiver_q is not None
    target = context.external_node(
        f"call:{receiver + '.' if receiver else ''}{callee}({arity})",
        unknown=local_receiver,
        swift_file=swift_file,
        span=_span_for_tree(node),
    )
    status = "unresolved" if local_receiver else "external"
    if local_receiver:
        context.diagnostic(
            "unresolved_call",
            "warning",
            f"Swiftの呼び出し先を一意に解決できません: {callee}",
            swift_file=swift_file,
            tree_node=node,
            node_id=target,
            details={"receiver": receiver, "owner": receiver_q},
        )
    _record_call(
        context,
        swift_file,
        caller_id,
        target,
        node,
        callee,
        arity,
        status=status,
        confidence=0.2 if local_receiver else 0.65,
        detail=detail,
    )


def _collect_reference(context: SwiftAnalysisContext, swift_file: SwiftFile, node: Any) -> None:
    caller_id = _enclosing_definition(context, swift_file, node) or swift_file.module_id
    reference = _node_text(node, swift_file.source).strip()
    target = context.external_node(f"reference:{reference}")
    _add_relation(
        context,
        caller_id,
        target,
        "references",
        resolution_status="external",
        confidence=0.55,
        source_span=_span_for_tree(node),
        detail={"reference": reference},
    )


def _resolve_type(
    context: SwiftAnalysisContext,
    reference: str,
    swift_file: SwiftFile,
    node: Any,
) -> tuple[str, str, float]:
    base = _base_type_name(reference)
    candidates = context.symbols_for_name(base, kinds=_TYPE_KINDS)
    if len(candidates) == 1:
        return candidates[0].node_id, "resolved", 1.0
    if len(candidates) > 1:
        target = context.external_node(
            f"type:{reference}:ambiguous",
            unknown=True,
            swift_file=swift_file,
            span=_span_for_tree(node),
        )
        context.diagnostic(
            "unresolved_type",
            "warning",
            f"Swiftの型を一意に解決できません: {reference}",
            swift_file=swift_file,
            tree_node=node,
            node_id=target,
            details={"reference": reference, "candidate_count": len(candidates)},
        )
        return target, "unresolved", 0.2
    return context.external_node(f"type:{reference}"), "external", 0.65


def _method_candidates(
    context: SwiftAnalysisContext,
    owner_q: str | None,
    name: str,
    arity: int,
    _seen: set[str] | None = None,
) -> list[SwiftSymbol]:
    if not owner_q:
        return []
    seen = _seen or set()
    if owner_q in seen:
        return []
    seen.add(owner_q)
    owner_names = {owner_q}
    base_owner = _base_type_name(owner_q)
    for extension_q, target in context.extension_targets.items():
        if target == owner_q or _base_type_name(target) == base_owner:
            owner_names.add(extension_q)
    candidates = [
        item
        for item in context.symbols_for_name(name, kinds={"method"})
        if item.owner_q in owner_names and item.arity == arity
    ]
    if candidates:
        return candidates
    parent = context.parent_types.get(owner_q)
    if parent:
        return _method_candidates(context, _base_type_name(parent), name, arity, seen)
    return []


def _receiver_type_reference(
    context: SwiftAnalysisContext,
    swift_file: SwiftFile,
    caller_id: str,
    receiver: str,
) -> str | None:
    value = receiver.strip().rstrip("?")
    if not value:
        return None
    if value in {"self", "Self"}:
        return _owner_type_q(context.definitions.get(caller_id))
    if value == "super":
        owner_q = _owner_type_q(context.definitions.get(caller_id))
        return context.parent_types.get(owner_q or "")
    inferred = context.variable_types.get((caller_id, value))
    if inferred:
        candidates = context.symbols_for_name(_base_type_name(inferred), kinds=_TYPE_KINDS)
        if len(candidates) == 1:
            return candidates[0].qualified_name
        return _base_type_name(inferred)
    candidates = context.symbols_for_name(_base_type_name(value), kinds=_TYPE_KINDS)
    if len(candidates) == 1:
        return candidates[0].qualified_name
    if value and value[:1].isupper():
        return _base_type_name(value)
    return None


def _record_call(
    context: SwiftAnalysisContext,
    swift_file: SwiftFile,
    caller_id: str,
    target_id: str,
    node: Any,
    callee: str,
    arity: int,
    *,
    status: str,
    confidence: float,
    detail: dict[str, Any],
) -> None:
    _add_relation(
        context,
        caller_id,
        target_id,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span_for_tree(node),
        detail=detail,
    )


def _record_unresolved_call(
    context: SwiftAnalysisContext,
    swift_file: SwiftFile,
    caller_id: str,
    node: Any,
    callee: str,
    arity: int,
    *,
    detail: dict[str, Any],
    reason: str,
) -> None:
    target = context.external_node(
        f"call:{callee}({arity}):{reason}",
        unknown=True,
        swift_file=swift_file,
        span=_span_for_tree(node),
    )
    context.diagnostic(
        "unresolved_call",
        "warning",
        f"Swiftの呼び出し先を一意に解決できません: {callee}",
        swift_file=swift_file,
        tree_node=node,
        node_id=target,
        details={"reason": reason},
    )
    _record_call(
        context,
        swift_file,
        caller_id,
        target,
        node,
        callee,
        arity,
        status="unresolved",
        confidence=0.2,
        detail={**detail, "reason": reason},
    )


def _call_detail(
    node: Any,
    source: bytes,
    callee: str,
    receiver: str,
    labels: list[str],
    arity: int,
    call_kind: str,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "expression": _node_text(node, source).strip(),
        "callee": callee,
        "arity": arity,
        "argument_labels": labels,
        "call_kind": call_kind,
    }
    if receiver:
        detail["receiver"] = receiver
    ancestors: list[str] = []
    current = node.parent
    while current is not None:
        ancestors.append(current.type)
        current = current.parent
    if "await_expression" in ancestors:
        detail["async"] = True
    if "try_expression" in ancestors:
        detail["try_kind"] = "try"
    return detail


def _return_info(node: Any, source: bytes) -> tuple[str, list[dict[str, Any]]]:
    if node.type == "protocol_function_declaration":
        return "unknown", []
    sites: list[dict[str, Any]] = []
    for child in _walk_callable_body(node):
        if child.type != "control_transfer_statement":
            continue
        raw = _node_text(child, source).strip()
        if not raw.startswith("return"):
            continue
        result = child.child_by_field_name("result")
        value_kind = "value" if result is not None or re.match(r"^return\s+\S", raw) else "none"
        sites.append({"span": _span_for_tree(child), "value_kind": value_kind})
    if sites:
        kinds = {site["value_kind"] for site in sites}
        if kinds == {"value"}:
            return "returns_value", sites
        if kinds == {"none"}:
            return "returns_none", sites
        return "mixed", sites
    if node.type in {"lambda_literal", "closure_expression"}:
        body_text = _node_text(node, source)
        if re.search(r"\bin\b", body_text) and node.named_children:
            return "returns_value", []
    return "no_explicit_return", []


def _walk_callable_body(node: Any) -> Iterable[Any]:
    for child in node.named_children:
        if child.type in _DEFINITION_NODE_TYPES:
            continue
        yield child
        yield from _walk_callable_body(child)


def _enclosing_definition(context: SwiftAnalysisContext, swift_file: SwiftFile, node: Any) -> str | None:
    current = node
    while current is not None:
        found = context.definition_by_node.get((swift_file.relative_path, current.id))
        if found is not None:
            return found
        current = current.parent
    return None


def _owner_type_q(symbol: SwiftSymbol | None) -> str | None:
    if symbol is None:
        return None
    if symbol.kind in _TYPE_KINDS or (
        symbol.kind == "namespace" and symbol.declaration_kind == "extension"
    ):
        return symbol.qualified_name
    return symbol.owner_q


def _parameters(node: Any, source: bytes) -> tuple[int, tuple[str, ...]]:
    body = _body_node(node)
    body_start = body.start_byte if body is not None else node.end_byte
    parameters = [
        item
        for item in _walk_tree(node)
        if item.type == "parameter" and item.start_byte < body_start
    ]
    labels: list[str] = []
    for parameter in parameters:
        raw = _node_text(parameter, source).strip()
        before_colon = raw.split(":", 1)[0].strip()
        tokens = before_colon.split()
        if not tokens:
            continue
        label = tokens[-2] if len(tokens) >= 2 else tokens[0]
        if label == "inout" and len(tokens) >= 2:
            label = tokens[-1]
        if len(tokens) == 1 and tokens[0].startswith("_"):
            label = "_"
        labels.append(_clean_identifier(label))
    return len(parameters), tuple(labels)


def _parameter_name(node: Any, source: bytes) -> str:
    name = node.child_by_field_name("name")
    if name is not None and name.type not in {"user_type", "type_annotation"}:
        return _clean_identifier(_node_text(name, source))
    raw = _node_text(node, source).split(":", 1)[0].strip().split()
    return _clean_identifier(raw[-1]) if raw else ""


def _parameter_type_node(node: Any) -> Any | None:
    annotation = next((child for child in node.named_children if child.type == "type_annotation"), None)
    if annotation is not None:
        return annotation
    for child in node.named_children:
        if child.type in {"user_type", "type_identifier", "array_type", "dictionary_type", "function_type"}:
            return child
    return None


def _property_name(node: Any, source: bytes) -> str:
    for child in _walk_tree(node):
        if child.type == "bound_identifier":
            return _clean_identifier(_node_text(child, source))
    name = node.child_by_field_name("name")
    return _clean_identifier(_node_text(name, source)) if name is not None else ""


def _argument_arity(node: Any | None) -> int:
    return len(node.named_children) if node is not None else 0


def _argument_labels(node: Any | None, source: bytes) -> list[str]:
    labels: list[str] = []
    if node is None:
        return labels
    for argument in node.named_children:
        label_node = argument.child_by_field_name("name")
        labels.append(_clean_identifier(_node_text(label_node, source)) if label_node is not None else "_")
    return labels


def _call_parts(node: Any | None, source: bytes) -> tuple[str, str, Any | None]:
    if node is None:
        return "", "", None
    arguments = next(
        (
            child
            for child in node.named_children
            if child.type == "call_suffix"
            for child in child.named_children
            if child.type == "value_arguments"
        ),
        None,
    )
    first = node.named_children[0] if node.named_children else None
    if first is None:
        return "", "", arguments
    if first.type == "simple_identifier":
        return _clean_identifier(_node_text(first, source)), "", arguments
    if first.type == "navigation_expression":
        suffixes = [child for child in first.named_children if child.type == "navigation_suffix"]
        suffix = suffixes[-1] if suffixes else None
        if suffix is None:
            return _clean_identifier(_node_text(first, source)), "", arguments
        suffix_name = next(
            (
                child
                for child in reversed(suffix.named_children)
                if child.type in {"simple_identifier", "operator_identifier"}
            ),
            None,
        )
        callee = (
            _clean_identifier(_node_text(suffix_name, source))
            if suffix_name is not None
            else _node_text(suffix, source).strip()
        )
        receiver = source[first.start_byte : suffix.start_byte].decode("utf-8", errors="replace").rstrip(".? ")
        return callee, receiver, arguments
    raw = _node_text(first, source).strip()
    match = re.match(r"(.+?)(?:\.|\?\.)([A-Za-z_][\w]*)$", raw)
    if match:
        return match.group(2), match.group(1), arguments
    return _clean_identifier(raw), "", arguments


def _body_node(node: Any) -> Any | None:
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    return next(
        (
            child
            for child in node.named_children
            if child.type in {"function_body", "class_body", "enum_class_body", "protocol_body"}
        ),
        None,
    )


def _header_text(node: Any, source: bytes) -> str:
    body = _body_node(node)
    end = body.start_byte if body is not None else node.end_byte
    return source[node.start_byte:end].decode("utf-8", errors="replace")


def _throws_kind(node: Any, source: bytes) -> str | None:
    header = _header_text(node, source)
    if re.search(r"\brethrows\b", header):
        return "rethrows"
    if re.search(r"\bthrows\b", header):
        return "throws"
    return None


def _is_async(node: Any, source: bytes) -> bool:
    return bool(re.search(r"\basync\b", _header_text(node, source)))


def _type_name_from_node(node: Any | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "type_annotation":
        node = node.child_by_field_name("name") or node
    raw = _node_text(node, source).strip()
    raw = re.sub(r"^(?:some|any)\s+", "", raw)
    return raw.replace(chr(96), "")


def _base_type_name(value: str) -> str:
    raw = re.sub(r"^(?:some|any)\s+", "", value.strip()).rstrip("?")
    raw = re.sub(r"<.*>$", "", raw).rsplit(".", 1)[-1]
    return re.sub(r"[^\w$<>]", "", raw)


def _type_role(node: Any) -> str:
    current = node.parent
    while current is not None:
        if current.type in {"type_annotation", "parameter"}:
            return "signature"
        if current.type in {"typealias_declaration", "associatedtype_declaration"}:
            return "alias"
        if current.type in {"type_arguments", "type_parameters", "generic_argument_clause"}:
            return "generic"
        current = current.parent
    return "type_reference"


def _has_ancestor_type(node: Any, node_type: str) -> bool:
    current = node.parent
    while current is not None:
        if current.type == node_type:
            return True
        current = current.parent
    return False


def _is_declaration_name(node: Any) -> bool:
    current = node.parent
    return current is not None and current.child_by_field_name("name") is node


def _clean_identifier(value: str) -> str:
    return value.strip().strip(chr(96))


def _visibility_for_node(node: Any, source: bytes) -> str:
    if re.search(r"\b(?:private|fileprivate)\b", _header_text(node, source)):
        return "private"
    return "public"


def _signature_for_node(node: Any, source: bytes, limit: int = 240) -> str:
    text = " ".join(_header_text(node, source).split())
    return (text or _node_text(node, source).strip())[:limit]


def _node_text(node: Any | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _walk_tree(node: Any) -> Iterable[Any]:
    yield node
    for child in node.named_children:
        yield from _walk_tree(child)


def _span_for_tree(node: Any | None) -> dict[str, int] | None:
    if node is None:
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
    return f"{base}:{salt}"


def _add_relation(
    context: SwiftAnalysisContext,
    source_id: str,
    target_id: str,
    relation_type: str,
    *,
    resolution_status: str,
    confidence: float,
    source_span: dict[str, int] | None,
    detail: dict[str, Any],
) -> None:
    identity = {
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        "resolution_status": resolution_status,
        "source_span": source_span,
        "detail": detail,
    }
    edge_id = f"edge:swift:{canonical_sha256(identity)[:24]}"
    context.builder.add_edge(
        {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation_type": relation_type,
            "resolution_status": resolution_status,
            "confidence": confidence,
            "provenance": "ast",
            "source_span": source_span,
            "detail": detail,
        }
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
