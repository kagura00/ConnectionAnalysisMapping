"""Static Dart relationship analyzer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import AnalysisConfig
from .contract import validate_document
from .model import GraphBuilder
from .phase3_common import (
    TreeFile,
    add_module,
    add_relation,
    add_skipped_diagnostics,
    diagnostic,
    external_node,
    finish_document,
    load_tree_files,
    nearest_scope,
    node_text,
    span_for_tree,
    unique_id,
    walk_with_ancestors,
)

ANALYZER_NAME = "connection-map-dart-tree-sitter"
ANALYZER_VERSION = "0.1.0"


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    active_config = config or AnalysisConfig(language="dart")
    active_config.validate()
    if active_config.language != "dart":
        raise ValueError("Dart analyzer requires language = 'dart'")
    files, skipped = load_tree_files(
        root.resolve(), active_config, language="dart", grammar="dart", extra="dart"
    )
    builder = GraphBuilder()
    for tree_file in files:
        add_module(builder, tree_file, language="dart", grammar="dart")
    add_skipped_diagnostics(builder, skipped)

    scopes: dict[int, str] = {}
    symbols: dict[str, str] = {}
    external_cache: dict[str, str] = {}
    modules_by_path = {item.relative_path: item.module_id for item in files}
    for tree_file in files:
        _collect_definitions(tree_file, builder, scopes, symbols)
    for tree_file in files:
        _collect_imports(tree_file, builder, modules_by_path, external_cache)
        _collect_type_references(tree_file, builder, scopes, symbols, external_cache)
        _collect_calls(tree_file, builder, scopes, symbols, external_cache)
        if tree_file.tree.root_node.has_error:
            diagnostic(
                builder,
                code="parse_error",
                message="Tree-sitter reported a Dart syntax error; extracted nodes are partial",
                file=tree_file.relative_path,
                span=span_for_tree(tree_file.tree.root_node),
                details={"grammar": "dart"},
            )
    document = finish_document(
        builder,
        root=root.resolve(),
        language="dart",
        languages=["dart"],
        analyzer_name=ANALYZER_NAME,
        analyzer_version=ANALYZER_VERSION,
        config=active_config,
        deterministic=deterministic,
        commit_sha=commit_sha,
        grammar="dart",
    )
    validate_document(document)
    return document


def _collect_definitions(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols: dict[str, str],
) -> None:
    definition_types = {
        "class_definition": ("class", {"identifier"}),
        "mixin_declaration": ("class", {"identifier"}),
        "enum_declaration": ("type", {"identifier"}),
        "extension_declaration": ("type", {"identifier"}),
        "extension_type_declaration": ("type", {"identifier"}),
        "function_signature": ("function", {"identifier"}),
        "constructor_signature": ("method", {"identifier", "type_identifier"}),
    }
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        spec = definition_types.get(node.type)
        if spec is None:
            continue
        kind, name_types = spec
        name_node = next((candidate for candidate, _ in walk_with_ancestors(node) if candidate.type in name_types), None)
        if name_node is None:
            continue
        name = node_text(name_node, tree_file.source).strip()
        if not name or name in {"void", "dynamic"}:
            continue
        is_member = any(parent.type in {"class_definition", "mixin_declaration", "enum_declaration", "extension_declaration", "extension_type_declaration", "class_body"} for parent in ancestors)
        if node.type == "function_signature" and any(parent.type == "method_signature" for parent in ancestors):
            kind = "method"
        parent_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        qualified = f"{tree_file.relative_path}:{name}"
        node_id = unique_id(builder.nodes, f"dart:{qualified}:{kind}", node.start_byte)
        signature = node_text(node, tree_file.source).strip()
        builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": qualified,
                "display_name": name,
                "file": tree_file.relative_path,
                "span": span_for_tree(node),
                "parent_id": parent_id,
                "visibility": "private" if name.startswith("_") else "public",
                "signature": signature,
                "return_behavior": "unknown" if kind in {"function", "method"} else None,
                "execution_kind": "async" if "async" in signature else "sync" if kind in {"function", "method"} else "unknown",
                "extensions": {
                    "language": "dart",
                    "grammar": "dart",
                    "declaration_kind": node.type,
                    "member": is_member,
                },
            }
        )
        builder.nodes[node_id].pop("return_behavior", None) if kind not in {"function", "method"} else None
        builder.nodes[node_id].pop("execution_kind", None) if kind not in {"function", "method"} else None
        scopes[node.id] = node_id
        symbols.setdefault(name.casefold(), node_id)
        add_relation(
            builder,
            source_id=parent_id,
            target_id=node_id,
            relation_type="contains",
            source_span=span_for_tree(node),
            detail={"kind": "lexical_definition", "declaration_kind": node.type},
            edge_prefix="dart",
        )


def _collect_imports(
    tree_file: TreeFile,
    builder: GraphBuilder,
    modules_by_path: dict[str, str],
    external_cache: dict[str, str],
) -> None:
    for node, _ in walk_with_ancestors(tree_file.tree.root_node):
        if node.type not in {"import_or_export", "part_directive"}:
            continue
        values = [
            node_text(candidate, tree_file.source).strip().strip("'\";")
            for candidate, _ in walk_with_ancestors(node)
            if candidate.type == "string_literal"
        ]
        reference = values[0] if values else None
        if not reference:
            diagnostic(
                builder,
                code="unresolved_import",
                message="Dart import/part URI is dynamic or missing",
                file=tree_file.relative_path,
                span=span_for_tree(node),
            )
            continue
        is_local = reference.startswith(".")
        candidate_path = (Path(tree_file.relative_path).parent / reference).as_posix() if is_local else reference
        target_id = modules_by_path.get(candidate_path)
        status = "resolved" if target_id else "external"
        if target_id is None:
            target_id = external_node(
                builder,
                external_cache,
                node_id=f"dart:import:{reference}",
                qualified_name=f"Dart library {reference}",
                display_name=reference,
                language="dart",
                extensions={"dart_object_type": "library"},
            )
        add_relation(
            builder,
            source_id=tree_file.module_id,
            target_id=target_id,
            relation_type="imports",
            source_span=span_for_tree(node),
            detail={"reference": reference, "kind": "part" if node.type == "part_directive" else "library"},
            resolution_status=status,
            confidence=1.0 if status == "resolved" else 0.7,
            edge_prefix="dart",
        )


def _collect_type_references(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols: dict[str, str],
    external_cache: dict[str, str],
) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type not in {"type_identifier", "type_identifier_with_type_arguments"}:
            continue
        name = node_text(node, tree_file.source).strip()
        if not name:
            continue
        source_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        target_id = symbols.get(name.casefold())
        status = "resolved" if target_id else "external"
        if target_id is None:
            target_id = external_node(
                builder,
                external_cache,
                node_id=f"dart:type:{name.casefold()}",
                qualified_name=f"Dart type {name}",
                display_name=name,
                language="dart",
                kind="type",
                extensions={"dart_object_type": "type"},
            )
        add_relation(
            builder,
            source_id=source_id,
            target_id=target_id,
            relation_type="references",
            source_span=span_for_tree(node),
            detail={"reference": name, "kind": "type"},
            resolution_status=status,
            confidence=0.95 if status == "resolved" else 0.5,
            edge_prefix="dart",
        )


def _collect_calls(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols: dict[str, str],
    external_cache: dict[str, str],
) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "argument_part":
            continue
        before = tree_file.source[: node.start_byte].decode("utf-8", "replace")
        match = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*$", before)
        if not match:
            continue
        name = match.group(1)
        source_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        target_id = symbols.get(name.casefold())
        status = "resolved" if target_id else "unresolved"
        if target_id is None:
            target_id = external_node(
                builder,
                external_cache,
                node_id=f"dart:call:{name.casefold()}",
                qualified_name=f"Dart call {name}",
                display_name=name,
                language="dart",
                extensions={"dart_object_type": "call"},
            )
        add_relation(
            builder,
            source_id=source_id,
            target_id=target_id,
            relation_type="calls",
            source_span=span_for_tree(node),
            detail={"expression": f"{name}(...)", "call_kind": "direct"},
            resolution_status=status,
            confidence=0.85 if status == "resolved" else 0.45,
            edge_prefix="dart",
        )
