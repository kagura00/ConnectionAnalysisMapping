"""Tree-sitter analyzer for HTML structure and static asset references."""

from __future__ import annotations

from typing import Any

from .web_common import (
    HtmlElementInfo,
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

_ELEMENT_TYPES = {"element", "script_element", "style_element", "self_closing_tag"}


def collect(context: WebAnalysisContext, web_file: WebFile) -> None:
    ordinal = 0
    for node in walk_tree(web_file.tree.root_node):
        if node.type not in _ELEMENT_TYPES:
            continue
        ordinal += 1
        tag, attributes = _tag_and_attributes(node, web_file.source)
        tag = tag or "unknown"
        element_id = attributes.get("id")
        classes = tuple(item for item in attributes.get("class", "").split() if item)
        base_id = f"html:{web_file.relative_path}:element:{ordinal}:{tag}:element"
        node_id = unique_id(context.builder.nodes, base_id, node.start_byte)
        parent_id = _nearest_element_id(context, web_file, node.parent) or web_file.module_id
        context.builder.add_node(
            {
                "id": node_id,
                "kind": "element",
                "qualified_name": f"{web_file.relative_path}:element:{ordinal}:{tag}",
                "display_name": f"<{tag}>",
                "file": web_file.relative_path,
                "span": span_for_tree(node),
                "parent_id": parent_id,
                "visibility": "public",
                "extensions": {
                    "language": "html",
                    "tag": tag,
                    "id": element_id,
                    "classes": list(classes),
                    "attributes": attributes,
                },
            }
        )
        context.definition_by_node[(web_file.relative_path, node.id)] = node_id
        context.definition_qualified_name[node_id] = f"{web_file.relative_path}:element:{ordinal}:{tag}"
        context.definition_kind[node_id] = "element"
        add_relation(
            context,
            parent_id,
            node_id,
            "contains",
            resolution_status="resolved",
            confidence=1.0,
            source_span=span_for_tree(node),
            detail={"kind": "html_element", "tag": tag},
        )
        context.html_elements.append(
            HtmlElementInfo(node_id, web_file.relative_path, tag, element_id, classes)
        )
        _collect_asset_reference(context, web_file, node, tag, attributes)

        if tag == "script" and "src" not in attributes and _has_inline_content(node, web_file.source):
            context.diagnostic(
                "unsupported_construct",
                "info",
                "inline scriptはHTML要素として記録しますが、独立したJavaScript解析は行いません。",
                web_file=web_file,
                tree_node=node,
                node_id=node_id,
                details={"construct": "inline_script"},
            )
        if tag == "style" and _has_inline_content(node, web_file.source):
            context.diagnostic(
                "unsupported_construct",
                "info",
                "inline styleはHTML要素として記録しますが、独立したCSS解析は行いません。",
                web_file=web_file,
                tree_node=node,
                node_id=node_id,
                details={"construct": "inline_style"},
            )


def _nearest_element_id(context: WebAnalysisContext, web_file: WebFile, node: Any | None) -> str | None:
    current = node
    while current is not None:
        node_id = context.definition_by_node.get((web_file.relative_path, current.id))
        if node_id and context.definition_kind.get(node_id) == "element":
            return node_id
        current = current.parent
    return None


def _tag_and_attributes(node: Any, source: bytes) -> tuple[str | None, dict[str, str]]:
    start_tag = node if node.type == "self_closing_tag" else direct_child(node, "start_tag")
    if start_tag is None:
        start_tag = node
    tag_node = direct_child(start_tag, "tag_name") or descendant(start_tag, "tag_name")
    tag = node_text(tag_node, source).strip().lower() if tag_node is not None else None
    attributes: dict[str, str] = {}
    for attribute in walk_tree(start_tag):
        if attribute.type != "attribute":
            continue
        name_node = descendant(attribute, "attribute_name")
        if name_node is None:
            continue
        name = node_text(name_node, source).strip().lower()
        value_node = (
            attribute.child_by_field_name("value")
            or descendant(attribute, "quoted_attribute_value")
            or descendant(attribute, "attribute_value")
        )
        value = string_value(value_node, source) if value_node is not None else ""
        attributes[name] = value if value is not None else node_text(value_node, source).strip() if value_node else ""
    return tag, attributes


def _collect_asset_reference(
    context: WebAnalysisContext,
    web_file: WebFile,
    node: Any,
    tag: str,
    attributes: dict[str, str],
) -> None:
    reference: str | None = None
    relation_kind = "html_asset"
    if tag == "script":
        reference = attributes.get("src")
        relation_kind = "script"
    elif tag == "link" and attributes.get("rel", "").lower() == "stylesheet":
        reference = attributes.get("href")
        relation_kind = "stylesheet"
    if not reference:
        return
    target_file = resolve_reference(context, web_file, reference, allow_bare=True)
    target_id = target_file.module_id if target_file else context.external_node(f"asset:{reference}")
    status = "resolved" if target_file else "external"
    add_relation(
        context,
        web_file.module_id,
        target_id,
        "imports",
        resolution_status=status,
        confidence=1.0 if target_file else 0.7,
        source_span=span_for_tree(node),
        detail={"reference": reference, "kind": relation_kind, "tag": tag},
    )
    if target_file is None:
        context.diagnostic(
            "unresolved_import",
            "warning",
            f"HTMLの参照先を対象リポジトリ内で解決できません: {reference}",
            web_file=web_file,
            tree_node=node,
            details={"reference": reference, "kind": relation_kind},
        )


def _has_inline_content(node: Any, source: bytes) -> bool:
    raw_text = descendant(node, "raw_text")
    if raw_text is None:
        return False
    return bool(node_text(raw_text, source).strip())
