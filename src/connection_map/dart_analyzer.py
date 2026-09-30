"""Static Dart relationship analyzer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import AnalysisConfig
from .contract import validate_document
from .dart_bindings import DartBindings, call_parts, local_uri
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
    external_cache: dict[str, str] = {}
    modules_by_path = {item.relative_path: item.module_id for item in files}
    for tree_file in files:
        _collect_definitions(tree_file, builder, scopes)
    bindings = DartBindings(files, builder.nodes, scopes)
    for tree_file in files:
        _collect_imports(tree_file, builder, modules_by_path, external_cache)
        _collect_type_references(tree_file, builder, scopes, bindings, external_cache)
        _collect_calls(tree_file, builder, scopes, bindings, external_cache)
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
) -> None:
    definition_types = {
        "class_definition": ("class", {"identifier"}),
        "mixin_declaration": ("class", {"identifier"}),
        "enum_declaration": ("type", {"identifier"}),
        "extension_declaration": ("type", {"identifier"}),
        "extension_type_declaration": ("type", {"identifier"}),
        "function_signature": ("function", {"identifier"}),
        "constructor_signature": ("method", {"identifier", "type_identifier"}),
        "getter_signature": ("function", {"identifier"}),
        "setter_signature": ("function", {"identifier"}),
    }
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        spec = definition_types.get(node.type)
        if spec is None:
            continue
        kind, name_types = spec
        if any(parent.type == "formal_parameter" for parent in ancestors):
            continue
        name_node = next((candidate for candidate in node.named_children if candidate.type in name_types), None)
        if name_node is None:
            continue
        name = node_text(name_node, tree_file.source).strip()
        if not name or name in {"void", "dynamic"}:
            continue
        parent_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        is_member = builder.nodes[parent_id]["kind"] in {"class", "type"}
        if kind == "function" and is_member:
            kind = "method"
        constructor = node.type == "constructor_signature"
        names = [node_text(c, tree_file.source) for c in node.named_children if c.type == "identifier"]
        member_name = names[-1] if constructor else name
        if constructor:
            name = ".".join(names)
        parent_name = builder.nodes[parent_id]["qualified_name"]
        qualified = f"{tree_file.relative_path}:{name}" if parent_id == tree_file.module_id else f"{parent_name}.{name}"
        node_id = unique_id(builder.nodes, f"dart:{qualified}:{kind}", node.start_byte)
        wrapper = node
        if node.parent and node.parent.type in {"method_signature", "declaration"}:
            wrapper = node.parent
        body = wrapper.next_named_sibling
        while body is not None and body.type == "comment":
            body = body.next_named_sibling
        if body is None or body.type != "function_body":
            body = next((c for c in wrapper.named_children if c.type == "function_body"), None)
        if kind not in {"function", "method"}:
            body = None
        span = span_for_tree(wrapper)
        if body:
            span.update({key: value for key, value in span_for_tree(body).items() if key.startswith("end_")})
        signature = node_text(wrapper, tree_file.source).strip()
        if kind in {"class", "type"}:
            class_body = next((c for c in node.named_children if c.type in {"class_body", "enum_body"}), None)
            if class_body:
                signature = tree_file.source[node.start_byte:class_body.start_byte].decode("utf-8", "replace").strip()
        is_async = bool(body and re.match(r"\s*async\b", node_text(body, tree_file.source)))
        builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": qualified,
                "display_name": name,
                "file": tree_file.relative_path,
                "span": span,
                "parent_id": parent_id,
                "visibility": "private" if name.startswith("_") else "public",
                "signature": signature,
                "return_behavior": "unknown" if kind in {"function", "method"} else None,
                "execution_kind": "async" if is_async else "sync" if kind in {"function", "method"} else "unknown",
                "extensions": {
                    "language": "dart",
                    "grammar": "dart",
                    "declaration_kind": node.type,
                    "member": is_member,
                    "member_name": member_name,
                    "static": any(c.type == "static" for c in wrapper.children),
                    "constructor": constructor,
                    "accessor": node.type in {"getter_signature", "setter_signature"},
                    "has_inheritance": any(c.type in {"superclass", "interfaces", "mixins"} for c in node.named_children),
                },
            }
        )
        builder.nodes[node_id].pop("return_behavior", None) if kind not in {"function", "method"} else None
        builder.nodes[node_id].pop("execution_kind", None) if kind not in {"function", "method"} else None
        scopes[node.id] = node_id
        scopes[wrapper.id] = node_id
        if body:
            scopes[body.id] = node_id
        add_relation(
            builder,
            source_id=parent_id,
            target_id=node_id,
            relation_type="contains",
            source_span=span,
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
        candidate_path = local_uri(tree_file.relative_path, reference)
        target_id = modules_by_path.get(candidate_path)
        conditional = len(values) > 1
        if conditional:
            target_id = None
        status = "resolved" if target_id else "unresolved" if conditional else "external"
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
    bindings: DartBindings,
    external_cache: dict[str, str],
) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type not in {"type_identifier", "type_identifier_with_type_arguments"}:
            continue
        name = node_text(node, tree_file.source).strip()
        if not name:
            continue
        source_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        if node.next_sibling and node.next_sibling.type == ".":
            continue
        parts = [name]
        if node.prev_sibling and node.prev_sibling.type == "." and node.prev_named_sibling:
            parts.insert(0, node_text(node.prev_named_sibling, tree_file.source))
        target_id, evidence = bindings.resolve(tree_file, node, parts, types_only=True)
        status = "resolved" if target_id else "external"
        if target_id is None:
            target_id = external_node(
                builder,
                external_cache,
                node_id=f"dart:type:{'.'.join(parts)}",
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
            detail={"reference": ".".join(parts), "kind": "type", **evidence},
            resolution_status=status,
            confidence=0.95 if status == "resolved" else 0.5,
            edge_prefix="dart",
        )


def _collect_calls(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    bindings: DartBindings,
    external_cache: dict[str, str],
) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "argument_part":
            continue
        parts, expression = call_parts(node, tree_file.source)
        source_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        target_id, evidence = bindings.resolve(tree_file, node, parts)
        status = "resolved" if target_id else "unresolved"
        if target_id is None:
            target_id = external_node(
                builder,
                external_cache,
                node_id=f"dart:call:{tree_file.relative_path}:{node.start_byte}",
                qualified_name=f"Dart call {expression}",
                display_name=expression,
                language="dart",
                extensions={"dart_object_type": "call"},
            )
        add_relation(
            builder,
            source_id=source_id,
            target_id=target_id,
            relation_type="calls",
            source_span=span_for_tree(node),
            detail={"expression": expression, "call_kind": "direct" if parts and len(parts) == 1 else "member", **evidence},
            resolution_status=status,
            confidence=0.85 if status == "resolved" else 0.45,
            edge_prefix="dart",
        )
