"""Static Scala relationship analyzer."""

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

ANALYZER_NAME = "connection-map-scala-tree-sitter"
ANALYZER_VERSION = "0.1.1"


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    active_config = config or AnalysisConfig(language="scala")
    active_config.validate()
    if active_config.language != "scala":
        raise ValueError("Scala analyzer requires language = 'scala'")
    files, skipped = load_tree_files(
        root.resolve(), active_config, language="scala", grammar="scala", extra="scala"
    )
    builder = GraphBuilder()
    for tree_file in files:
        add_module(builder, tree_file, language="scala", grammar="scala")
    add_skipped_diagnostics(builder, skipped)

    scopes: dict[int, str] = {}
    symbols: dict[str, str] = {}
    external_cache: dict[str, str] = {}
    for tree_file in files:
        _collect_packages(tree_file, builder, scopes)
    for tree_file in files:
        _collect_definitions(tree_file, builder, scopes, symbols)
    for tree_file in files:
        _collect_imports(tree_file, builder, external_cache)
        _collect_inheritance(tree_file, builder, scopes, symbols, external_cache)
        _collect_types(tree_file, builder, scopes, symbols, external_cache)
        _collect_calls(tree_file, builder, scopes, symbols, external_cache)
        if tree_file.tree.root_node.has_error:
            diagnostic(
                builder,
                code="parse_error",
                message="Tree-sitter reported a Scala syntax error; extracted nodes are partial",
                file=tree_file.relative_path,
                span=span_for_tree(tree_file.tree.root_node),
                details={"grammar": "scala"},
            )
    document = finish_document(
        builder,
        root=root.resolve(),
        language="scala",
        languages=["scala"],
        analyzer_name=ANALYZER_NAME,
        analyzer_version=ANALYZER_VERSION,
        config=active_config,
        deterministic=deterministic,
        commit_sha=commit_sha,
        grammar="scala",
    )
    validate_document(document)
    return document


def _collect_packages(tree_file: TreeFile, builder: GraphBuilder, scopes: dict[int, str]) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "package_clause":
            continue
        package_node = next((candidate for candidate, _ in walk_with_ancestors(node) if candidate.type == "package_identifier"), None)
        if package_node is None:
            continue
        name = node_text(package_node, tree_file.source).strip()
        if not name:
            continue
        parent_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        node_id = unique_id(builder.nodes, f"scala:{tree_file.relative_path}:{name}:namespace", node.start_byte)
        builder.add_node(
            {
                "id": node_id,
                "kind": "namespace",
                "qualified_name": name,
                "display_name": name.rsplit(".", 1)[-1],
                "file": tree_file.relative_path,
                "span": span_for_tree(node),
                "parent_id": parent_id,
                "visibility": "public",
                "extensions": {"language": "scala", "grammar": "scala", "declaration_kind": "package_clause"},
            }
        )
        scopes[node.id] = node_id
        add_relation(
            builder,
            source_id=parent_id,
            target_id=node_id,
            relation_type="contains",
            source_span=span_for_tree(node),
            detail={"kind": "package"},
            edge_prefix="scala",
        )


def _collect_definitions(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols: dict[str, str],
) -> None:
    definition_types = {
        "class_definition": ("class", {"identifier", "type_identifier"}),
        "trait_definition": ("interface", {"identifier", "type_identifier"}),
        "object_definition": ("class", {"identifier", "type_identifier"}),
        "enum_definition": ("type", {"identifier", "type_identifier"}),
        "function_definition": ("function", {"identifier"}),
        "function_declaration": ("function", {"identifier"}),
        "given_definition": ("type", {"identifier", "type_identifier"}),
        "extension_definition": ("type", {"identifier", "type_identifier"}),
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
        if not name:
            continue
        is_member = any(parent.type in {"class_definition", "trait_definition", "object_definition", "enum_definition", "template_body"} for parent in ancestors)
        if node.type == "function_definition" and is_member:
            kind = "method"
        if node.type == "function_declaration" and is_member:
            kind = "method"
        parent_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        parent = builder.nodes.get(parent_id)
        parent_kind = parent.get("kind") if parent else None
        parent_qualified = parent.get("qualified_name") if parent else None
        if is_member and parent_kind in {"class", "interface", "type"} and parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        else:
            qualified = f"{tree_file.relative_path}:{name}"
        node_id = unique_id(builder.nodes, f"scala:{qualified}:{kind}", node.start_byte)
        node_data: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified,
            "display_name": name,
            "file": tree_file.relative_path,
            "span": span_for_tree(node),
            "parent_id": parent_id,
            "visibility": "public",
            "signature": node_text(node, tree_file.source).split("=", 1)[0].strip(),
            "extensions": {
                "language": "scala",
                "grammar": "scala",
                "declaration_kind": node.type,
                "member": is_member,
            },
        }
        if kind in {"function", "method"}:
            node_data["return_behavior"] = "unknown"
            node_data["execution_kind"] = "sync"
        builder.add_node(node_data)
        scopes[node.id] = node_id
        symbols.setdefault(name.casefold(), node_id)
        add_relation(
            builder,
            source_id=parent_id,
            target_id=node_id,
            relation_type="contains",
            source_span=span_for_tree(node),
            detail={"kind": "lexical_definition", "declaration_kind": node.type},
            edge_prefix="scala",
        )


def _collect_imports(tree_file: TreeFile, builder: GraphBuilder, external_cache: dict[str, str]) -> None:
    for node, _ in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "import_declaration":
            continue
        reference = node_text(node, tree_file.source).strip()
        target_id = external_node(
            builder,
            external_cache,
            node_id=f"scala:import:{reference.casefold()}",
            qualified_name=f"Scala import {reference}",
            display_name=reference,
            language="scala",
            extensions={"scala_object_type": "package"},
        )
        add_relation(
            builder,
            source_id=tree_file.module_id,
            target_id=target_id,
            relation_type="imports",
            source_span=span_for_tree(node),
            detail={"reference": reference, "kind": "import"},
            resolution_status="external",
            confidence=0.75,
            edge_prefix="scala",
        )


def _collect_inheritance(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols: dict[str, str],
    external_cache: dict[str, str],
) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "extends_clause":
            continue
        source_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        for target_node, _ in walk_with_ancestors(node):
            if target_node.type not in {"type_identifier", "identifier"}:
                continue
            name = node_text(target_node, tree_file.source).strip()
            if name in {"extends", "with"} or not name:
                continue
            target_id = symbols.get(name.casefold())
            status = "resolved" if target_id else "external"
            if target_id is None:
                target_id = external_node(
                    builder,
                    external_cache,
                    node_id=f"scala:type:{name.casefold()}",
                    qualified_name=f"Scala type {name}",
                    display_name=name,
                    language="scala",
                    kind="type",
                    extensions={"scala_object_type": "type"},
                )
            add_relation(
                builder,
                source_id=source_id,
                target_id=target_id,
                relation_type="inherits",
                source_span=span_for_tree(target_node),
                detail={"reference": name, "kind": "extends_or_with"},
                resolution_status=status,
                confidence=0.95 if status == "resolved" else 0.5,
                edge_prefix="scala",
            )


def _collect_types(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols: dict[str, str],
    external_cache: dict[str, str],
) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "type_identifier":
            continue
        name = node_text(node, tree_file.source).strip()
        if not name or name in {"extends", "with"}:
            continue
        source_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        target_id = symbols.get(name.casefold())
        status = "resolved" if target_id else "external"
        if target_id is None:
            target_id = external_node(
                builder,
                external_cache,
                node_id=f"scala:type-ref:{name.casefold()}",
                qualified_name=f"Scala type {name}",
                display_name=name,
                language="scala",
                kind="type",
                extensions={"scala_object_type": "type_reference"},
            )
        add_relation(
            builder,
            source_id=source_id,
            target_id=target_id,
            relation_type="references",
            source_span=span_for_tree(node),
            detail={"reference": name, "kind": "type"},
            resolution_status=status,
            confidence=0.9 if status == "resolved" else 0.45,
            edge_prefix="scala",
        )


def _collect_calls(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols: dict[str, str],
    external_cache: dict[str, str],
) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "call_expression":
            continue
        expression_text = node_text(node, tree_file.source).strip()
        match = re.match(r"(?:[A-Za-z_$][A-Za-z0-9_$]*\.)*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", expression_text)
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
                node_id=f"scala:call:{name.casefold()}",
                qualified_name=f"Scala call {name}",
                display_name=name,
                language="scala",
                extensions={"scala_object_type": "call"},
            )
        add_relation(
            builder,
            source_id=source_id,
            target_id=target_id,
            relation_type="calls",
            source_span=span_for_tree(node),
            detail={"expression": expression_text.split("(", 1)[0].strip(), "call_kind": "direct"},
            resolution_status=status,
            confidence=0.85 if status == "resolved" else 0.45,
            edge_prefix="scala",
        )
