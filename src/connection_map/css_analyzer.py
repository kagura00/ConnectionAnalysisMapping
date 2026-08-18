"""Tree-sitter analyzer for CSS rules and imports."""

from __future__ import annotations

import re
from typing import Any

from .web_common import (
    CssRuleInfo,
    WebAnalysisContext,
    WebFile,
    add_relation,
    descendant,
    direct_child,
    node_text,
    resolve_reference,
    span_for_tree,
    string_value,
    unique_id,
    walk_tree,
)


def collect(context: WebAnalysisContext, web_file: WebFile) -> None:
    ordinal = 0
    for node in walk_tree(web_file.tree.root_node):
        if node.type == "import_statement":
            _collect_import(context, web_file, node)
        elif node.type == "rule_set":
            ordinal += 1
            _collect_rule(context, web_file, node, ordinal)


def _collect_import(context: WebAnalysisContext, web_file: WebFile, node: Any) -> None:
    value_node = descendant(node, "string_value") or descendant(node, "string_content")
    reference = string_value(value_node, web_file.source)
    if not reference:
        context.diagnostic(
            "unsupported_construct",
            "warning",
            "CSS @importの参照先を静的な文字列として取得できません。",
            web_file=web_file,
            tree_node=node,
            details={"construct": "css_import"},
        )
        return
    target_file = resolve_reference(context, web_file, reference, allow_bare=True)
    target_id = target_file.module_id if target_file else context.external_node(f"stylesheet:{reference}")
    status = "resolved" if target_file else "external"
    add_relation(
        context,
        web_file.module_id,
        target_id,
        "imports",
        resolution_status=status,
        confidence=1.0 if target_file else 0.7,
        source_span=span_for_tree(node),
        detail={"reference": reference, "kind": "css_import"},
    )
    if target_file is None:
        context.diagnostic(
            "unresolved_import",
            "warning",
            f"CSSのimport先を対象リポジトリ内で解決できません: {reference}",
            web_file=web_file,
            tree_node=node,
            details={"reference": reference},
        )


def _collect_rule(context: WebAnalysisContext, web_file: WebFile, node: Any, ordinal: int) -> None:
    selector_node = node.child_by_field_name("selectors") or direct_child(node, "selectors")
    raw = node_text(selector_node, web_file.source).strip() if selector_node is not None else ""
    selectors = tuple(item.strip() for item in raw.split(",") if item.strip()) or (raw or "<unknown>",)
    base_id = f"css:{web_file.relative_path}:rule:{ordinal}:style_rule"
    node_id = unique_id(context.builder.nodes, base_id, node.start_byte)
    token_info = _selector_tokens(selectors)
    context.builder.add_node(
        {
            "id": node_id,
            "kind": "style_rule",
            "qualified_name": f"{web_file.relative_path}:rule:{ordinal}",
            "display_name": raw or "<selector>",
            "file": web_file.relative_path,
            "span": span_for_tree(node),
            "parent_id": web_file.module_id,
            "visibility": "public",
            "extensions": {
                "language": "css",
                "selectors": list(selectors),
                "simple_selectors": token_info,
            },
        }
    )
    context.definition_by_node[(web_file.relative_path, node.id)] = node_id
    context.definition_qualified_name[node_id] = f"{web_file.relative_path}:rule:{ordinal}"
    context.definition_kind[node_id] = "style_rule"
    add_relation(
        context,
        web_file.module_id,
        node_id,
        "contains",
        resolution_status="resolved",
        confidence=1.0,
        source_span=span_for_tree(node),
        detail={"kind": "css_rule"},
    )
    context.css_rules.append(CssRuleInfo(node_id, web_file.relative_path, selectors, span_for_tree(node)))


def _selector_tokens(selectors: tuple[str, ...]) -> dict[str, list[str]]:
    classes: list[str] = []
    ids: list[str] = []
    tags: list[str] = []
    for selector in selectors:
        if not _is_simple_selector(selector):
            continue
        if selector.startswith(".") and re.fullmatch(r"\.[A-Za-z_][\w-]*", selector):
            classes.append(selector[1:])
        elif selector.startswith("#") and re.fullmatch(r"#[A-Za-z_][\w-]*", selector):
            ids.append(selector[1:])
        elif re.fullmatch(r"[A-Za-z][\w-]*", selector):
            tags.append(selector.lower())
    return {"classes": classes, "ids": ids, "tags": tags}


def _is_simple_selector(selector: str) -> bool:
    return bool(
        re.fullmatch(r"\.[A-Za-z_][\w-]*", selector)
        or re.fullmatch(r"#[A-Za-z_][\w-]*", selector)
        or re.fullmatch(r"[A-Za-z][\w-]*", selector)
    )
