"""Static PowerShell relationship analyzer."""

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

ANALYZER_NAME = "connection-map-powershell-tree-sitter"
ANALYZER_VERSION = "0.1.0"


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    active_config = config or AnalysisConfig(language="powershell")
    active_config.validate()
    if active_config.language != "powershell":
        raise ValueError("PowerShell analyzer requires language = 'powershell'")
    files, skipped = load_tree_files(
        root.resolve(), active_config, language="powershell", grammar="powershell", extra="powershell"
    )
    builder = GraphBuilder()
    for tree_file in files:
        add_module(builder, tree_file, language="powershell", grammar="powershell")
    add_skipped_diagnostics(builder, skipped)

    scopes: dict[int, str] = {}
    symbols_by_name: dict[str, str] = {}
    external_cache: dict[str, str] = {}
    modules_by_path = {item.relative_path: item.module_id for item in files}
    for tree_file in files:
        _collect_definitions(tree_file, builder, scopes, symbols_by_name)
    for tree_file in files:
        _collect_relations(
            tree_file,
            builder,
            scopes,
            symbols_by_name,
            external_cache,
            modules_by_path,
            root.resolve(),
        )
        if tree_file.tree.root_node.has_error:
            diagnostic(
                builder,
                code="parse_error",
                message="Tree-sitter reported a PowerShell syntax error; extracted nodes are partial",
                file=tree_file.relative_path,
                span=span_for_tree(tree_file.tree.root_node),
                details={"grammar": "powershell"},
            )
    document = finish_document(
        builder,
        root=root.resolve(),
        language="powershell",
        languages=["powershell"],
        analyzer_name=ANALYZER_NAME,
        analyzer_version=ANALYZER_VERSION,
        config=active_config,
        deterministic=deterministic,
        commit_sha=commit_sha,
        grammar="powershell",
    )
    validate_document(document)
    return document


def _collect_definitions(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols_by_name: dict[str, str],
) -> None:
    definition_types = {
        "function_statement": ("function", {"function_name"}),
        "filter_statement": ("function", {"function_name"}),
        "class_statement": ("class", {"simple_name"}),
        "class_method_definition": ("method", {"simple_name"}),
        "enum_statement": ("type", {"simple_name"}),
    }
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        spec = definition_types.get(node.type)
        if spec is None:
            continue
        kind, name_types = spec
        name_node = next((child for child in node.children if child.type in name_types), None)
        if name_node is None:
            name_node = next((candidate for candidate, _ in walk_with_ancestors(node) if candidate.type in name_types), None)
        if name_node is None:
            continue
        name = node_text(name_node, tree_file.source).strip()
        if not name:
            continue
        parent_id = nearest_scope(ancestors, scopes, tree_file.module_id)
        qualified = f"{tree_file.relative_path}:{name}"
        node_id = unique_id(builder.nodes, f"powershell:{qualified}:{kind}", node.start_byte)
        body = node_text(node, tree_file.source)
        return_behavior = "unknown" if re.search(r"\breturn\b", body, re.IGNORECASE) else "no_explicit_return"
        builder.add_node(
            {
                "id": node_id,
                "kind": kind,
                "qualified_name": qualified,
                "display_name": name,
                "file": tree_file.relative_path,
                "span": span_for_tree(node),
                "parent_id": parent_id,
                "visibility": "public",
                "signature": node_text(node, tree_file.source).split("{", 1)[0].strip(),
                "return_behavior": return_behavior if kind in {"function", "method"} else None,
                "execution_kind": "sync" if kind in {"function", "method"} else "unknown",
                "extensions": {
                    "language": "powershell",
                    "grammar": "powershell",
                    "declaration_kind": node.type,
                },
            }
        )
        node_value = builder.nodes[node_id]
        if node_value.get("return_behavior") is None:
            node_value.pop("return_behavior", None)
        if node_value.get("execution_kind") == "unknown":
            node_value.pop("execution_kind", None)
        scopes[node.id] = node_id
        symbols_by_name.setdefault(name.casefold(), node_id)
        add_relation(
            builder,
            source_id=parent_id,
            target_id=node_id,
            relation_type="contains",
            source_span=span_for_tree(node),
            detail={"kind": "lexical_definition", "declaration_kind": node.type},
            edge_prefix="powershell",
        )


def _collect_relations(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scopes: dict[int, str],
    symbols_by_name: dict[str, str],
    external_cache: dict[str, str],
    modules_by_path: dict[str, str],
    root: Path,
) -> None:
    command_ids: dict[int, str] = {}
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type == "command":
            name = _command_name(node, tree_file.source)
            if not name:
                continue
            parent_id = nearest_scope(ancestors, scopes, tree_file.module_id)
            command_id = unique_id(
                builder.nodes,
                f"powershell:{tree_file.relative_path}:command:{name}:{node.start_point[0] + 1}",
                node.start_byte,
            )
            builder.add_node(
                {
                    "id": command_id,
                    "kind": "unknown",
                    "qualified_name": f"{tree_file.relative_path}:command:{name}:{node.start_point[0] + 1}",
                    "display_name": name,
                    "file": tree_file.relative_path,
                    "span": span_for_tree(node),
                    "parent_id": parent_id,
                    "visibility": "unknown",
                    "signature": node_text(node, tree_file.source).strip(),
                    "extensions": {"language": "powershell", "grammar": "powershell", "powershell_object_type": "command"},
                }
            )
            command_ids[node.id] = command_id
            add_relation(
                builder,
                source_id=parent_id,
                target_id=command_id,
                relation_type="contains",
                source_span=span_for_tree(node),
                detail={"kind": "command_invocation"},
                edge_prefix="powershell",
            )
            target_name = name.casefold()
            if target_name in symbols_by_name:
                add_relation(
                    builder,
                    source_id=parent_id,
                    target_id=symbols_by_name[target_name],
                    relation_type="calls",
                    source_span=span_for_tree(node),
                    detail={"expression": node_text(node, tree_file.source).strip(), "call_kind": "command_invocation"},
                    edge_prefix="powershell",
                )
            else:
                target_id = external_node(
                    builder,
                    external_cache,
                    node_id=f"powershell:command:{target_name}",
                    qualified_name=f"PowerShell command {name}",
                    display_name=name,
                    language="powershell",
                    extensions={"powershell_object_type": "command"},
                )
                add_relation(
                    builder,
                    source_id=command_id,
                    target_id=target_id,
                    relation_type="uses",
                    source_span=span_for_tree(node),
                    detail={"command": name},
                    resolution_status="external",
                    confidence=0.9,
                    edge_prefix="powershell",
                )
            if name.casefold() in {"import-module", "using-module"} or _has_dot_operator(node, tree_file.source):
                _add_module_relation(
                    builder,
                    command_id,
                    _command_argument(node, tree_file.source),
                    tree_file,
                    modules_by_path,
                    root,
                )
        elif node.type in {"variable", "variable_name"}:
            name = node_text(node, tree_file.source).strip()
            if not name:
                continue
            source_id = nearest_scope(ancestors, scopes, tree_file.module_id)
            target_id = external_node(
                builder,
                external_cache,
                node_id=f"powershell:variable:{name.casefold()}",
                qualified_name=f"PowerShell variable {name}",
                display_name=name,
                language="powershell",
                extensions={"powershell_object_type": "variable", "environment": name.casefold().startswith("$env:")},
            )
            add_relation(
                builder,
                source_id=source_id,
                target_id=target_id,
                relation_type="uses",
                source_span=span_for_tree(node),
                detail={"variable": name},
                resolution_status="external",
                confidence=0.85,
                edge_prefix="powershell",
            )


def _command_name(node: Any, source: bytes) -> str | None:
    for candidate, _ in walk_with_ancestors(node):
        if candidate.type in {"command_name", "command_name_expr"}:
            value = node_text(candidate, source).strip().strip("'\"")
            if value and value not in {"."}:
                return value
    return None


def _has_dot_operator(node: Any, source: bytes) -> bool:
    return any(candidate.type == "command_invokation_operator" and node_text(candidate, source).strip() == "." for candidate, _ in walk_with_ancestors(node))


def _command_argument(node: Any, source: bytes) -> str | None:
    command_name_seen = False
    for child in node.children:
        if child.type in {"command_name", "command_name_expr"}:
            command_name_seen = True
            continue
        if command_name_seen and child.type not in {"command_argument_sep", "command_parameter"}:
            value = node_text(child, source).strip()
            if value:
                return value
    values = [node_text(candidate, source).strip() for candidate, _ in walk_with_ancestors(node) if candidate.type in {"generic_token", "array_literal_expression"}]
    return values[0] if values else None


def _add_module_relation(
    builder: GraphBuilder,
    source_id: str,
    argument: str | None,
    tree_file: TreeFile,
    modules_by_path: dict[str, str],
    root: Path,
) -> None:
    if not argument:
        diagnostic(
            builder,
            code="dynamic_import",
            message="PowerShell module path is dynamic or missing",
            file=tree_file.relative_path,
            span=span_for_tree(tree_file.tree.root_node),
        )
        return
    cleaned = argument.strip("'\"")
    candidate = (Path(tree_file.relative_path).parent / cleaned).as_posix()
    candidates = [candidate]
    if not Path(candidate).suffix:
        candidates.extend([f"{candidate}.psm1", f"{candidate}.ps1"])
    target_id = next((modules_by_path[item] for item in candidates if item in modules_by_path), None)
    status = "resolved" if target_id else "external"
    if target_id is None:
        target_id = external_node(
            builder,
            {},
            node_id=f"powershell:module:{cleaned.casefold()}",
            qualified_name=f"PowerShell module {cleaned}",
            display_name=cleaned,
            language="powershell",
            extensions={"powershell_object_type": "module"},
        )
    add_relation(
        builder,
        source_id=source_id,
        target_id=target_id,
        relation_type="imports" if status == "resolved" else "dynamic_imports",
        source_span=span_for_tree(tree_file.tree.root_node),
        detail={"reference": cleaned, "kind": "module"},
        resolution_status=status,
        confidence=1.0 if status == "resolved" else 0.6,
        edge_prefix="powershell",
    )
