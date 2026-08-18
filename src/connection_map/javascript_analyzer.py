"""Tree-sitter analyzer for JavaScript and the JavaScript-shaped part of TypeScript."""

from __future__ import annotations

from typing import Any

from .web_common import (
    DomReference,
    WebAnalysisContext,
    WebFile,
    add_relation,
    descendant,
    direct_child,
    node_name,
    node_text,
    resolve_reference,
    string_value,
    walk_tree,
)

_FUNCTION_DECLARATIONS = {
    "function_declaration",
    "generator_function_declaration",
    "async_function_declaration",
    "async_generator_function_declaration",
}
_FUNCTION_VALUES = {
    "arrow_function",
    "function",
    "function_expression",
    "generator_function",
}
_CLASS_DECLARATIONS = {"class_declaration"}
_KNOWN_GLOBALS = {
    "Array",
    "Boolean",
    "Date",
    "Error",
    "JSON",
    "Map",
    "Math",
    "Number",
    "Object",
    "Promise",
    "RegExp",
    "Set",
    "String",
    "Symbol",
    "console",
    "document",
    "fetch",
    "globalThis",
    "parseInt",
    "parseFloat",
    "setInterval",
    "setTimeout",
    "window",
}


def collect_definitions(context: WebAnalysisContext, web_file: WebFile, *, typescript: bool = False) -> None:
    """Collect JavaScript definitions before any cross-file resolution."""

    for node in walk_tree(web_file.tree.root_node):
        node_type = node.type
        if node_type in _CLASS_DECLARATIONS:
            context.add_definition(
                web_file,
                node,
                kind="class",
                name=node_name(node, web_file.source, "name"),
            )
        elif node_type in _FUNCTION_DECLARATIONS:
            return_behavior, return_sites = _return_info(web_file, node)
            context.add_definition(
                web_file,
                node,
                kind="function",
                name=node_name(node, web_file.source, "name"),
                return_behavior=return_behavior,
                return_sites=return_sites,
                extensions={"async": node_type.startswith("async")},
            )
        elif node_type == "method_definition":
            return_behavior, return_sites = _return_info(web_file, node)
            context.add_definition(
                web_file,
                node,
                kind="method",
                name=node_name(node, web_file.source, "name", "property"),
                return_behavior=return_behavior,
                return_sites=return_sites,
            )
        elif node_type in _FUNCTION_VALUES and _function_value_is_named(node):
            return_behavior, return_sites = _return_info(web_file, node)
            context.add_definition(
                web_file,
                node,
                kind="function" if _assigned_name(node, web_file.source) else "lambda",
                name=_assigned_name(node, web_file.source),
                return_behavior=return_behavior,
                return_sites=return_sites,
            )
        elif typescript and node_type == "interface_declaration":
            context.add_definition(
                web_file,
                node,
                kind="interface",
                name=node_name(node, web_file.source, "name"),
            )
        elif typescript and node_type in {"type_alias_declaration", "enum_declaration"}:
            context.add_definition(
                web_file,
                node,
                kind="type",
                name=node_name(node, web_file.source, "name"),
            )


def collect_relations(context: WebAnalysisContext, web_file: WebFile, *, typescript: bool = False) -> None:
    """Collect imports, exports, calls, inheritance and DOM references."""

    for node in walk_tree(web_file.tree.root_node):
        if node.type == "import_statement":
            _collect_import(context, web_file, node)
        elif node.type == "export_statement":
            _collect_export(context, web_file, node)
        elif node.type == "call_expression":
            _collect_call(context, web_file, node)
        elif node.type == "new_expression":
            _collect_new(context, web_file, node)
        elif node.type in _CLASS_DECLARATIONS:
            _collect_inheritance(context, web_file, node)


def _function_value_is_named(node: Any) -> bool:
    if node.child_by_field_name("name") is not None:
        return True
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "variable_declarator":
        return parent.child_by_field_name("name") is not None
    if parent.type == "pair":
        return parent.child_by_field_name("key") is not None
    return False


def _assigned_name(node: Any, source: bytes | None) -> str | None:
    own_name = node.child_by_field_name("name")
    if own_name is not None and source is not None:
        return node_text(own_name, source).strip()
    parent = node.parent
    if parent is not None and parent.type == "variable_declarator":
        name_node = parent.child_by_field_name("name")
        if name_node is not None and source is not None:
            return node_text(name_node, source).strip()
    if parent is not None and parent.type == "pair":
        key = parent.child_by_field_name("key")
        if key is not None and source is not None:
            return node_text(key, source).strip()
    return None


def _return_info(web_file: WebFile, node: Any) -> tuple[str, list[dict[str, Any]]]:
    return_nodes: list[Any] = []

    def visit(current: Any, *, is_root: bool = False) -> None:
        for child in current.children:
            if child.type == "return_statement":
                return_nodes.append(child)
                continue
            if not is_root and child.type in (_FUNCTION_DECLARATIONS | _FUNCTION_VALUES | _CLASS_DECLARATIONS):
                continue
            visit(child)

    visit(node, is_root=True)
    if not return_nodes:
        body = node.child_by_field_name("body")
        if node.type == "arrow_function" and body is not None and body.type != "statement_block":
            return "returns_value", []
        return "no_explicit_return", []
    sites: list[dict[str, Any]] = []
    value_kinds: list[str] = []
    for return_node in return_nodes:
        value_node = next(
            (child for child in return_node.children if child.type not in {"return", ";"}),
            None,
        )
        value_kind = "value" if value_node is not None else "none"
        value_kinds.append(value_kind)
        sites.append({"span": _span(return_node), "value_kind": value_kind})
    if all(kind == "value" for kind in value_kinds):
        behavior = "returns_value"
    elif all(kind == "none" for kind in value_kinds):
        behavior = "returns_none"
    else:
        behavior = "mixed"
    return behavior, sites


def _import_source(node: Any, source: bytes) -> str | None:
    return string_value(node.child_by_field_name("source"), source)


def _collect_import(context: WebAnalysisContext, web_file: WebFile, node: Any) -> None:
    reference = _import_source(node, web_file.source)
    source_node = node.child_by_field_name("source")
    if not reference:
        context.diagnostic(
            "unsupported_construct",
            "warning",
            "静的な文字列として取得できないimportは解決できません。",
            web_file=web_file,
            tree_node=source_node or node,
            details={"construct": "import"},
        )
        return
    target_file = resolve_reference(context, web_file, reference)
    target_id = target_file.module_id if target_file else context.external_node(f"module:{reference}")
    status = "resolved" if target_file else "external"
    add_relation(
        context,
        web_file.module_id,
        target_id,
        "imports",
        resolution_status=status,
        confidence=1.0 if target_file else 0.7,
        source_span=source_node and _span(source_node),
        detail={"expression": node_text(node, web_file.source).strip(), "reference": reference},
    )
    _bind_imports(context, web_file, node, target_file)
    if target_file is None:
        context.diagnostic(
            "unresolved_import",
            "warning",
            f"import先を対象リポジトリ内で解決できません: {reference}",
            web_file=web_file,
            tree_node=source_node or node,
            details={"reference": reference},
        )


def _bind_imports(context: WebAnalysisContext, web_file: WebFile, node: Any, target_file: WebFile | None) -> None:
    clause = node.child_by_field_name("import") or descendant(node, "import_clause")
    if clause is None:
        return
    bindings = context.import_bindings.setdefault(web_file.relative_path, {})
    target_module_id = target_file.module_id if target_file else context.external_node(
        f"module:{string_value(node.child_by_field_name('source'), web_file.source) or 'unknown'}"
    )
    for child in walk_tree(clause):
        if child.type == "import_specifier":
            imported_node = child.child_by_field_name("name")
            local_node = child.child_by_field_name("alias") or imported_node
            if imported_node is None or local_node is None:
                continue
            imported = node_text(imported_node, web_file.source).strip()
            local = node_text(local_node, web_file.source).strip()
            bindings[local] = _target_symbol_or_module(context, target_file, imported, target_module_id)
        elif child.type == "namespace_import":
            name_node = child.child_by_field_name("name") or direct_child(child, "identifier", "type_identifier")
            if name_node is not None:
                bindings[node_text(name_node, web_file.source).strip()] = target_module_id
        elif child.type in {"identifier", "type_identifier"} and child.parent is clause:
            bindings[node_text(child, web_file.source).strip()] = _target_symbol_or_module(
                context, target_file, "default", target_module_id
            )


def _target_symbol_or_module(
    context: WebAnalysisContext,
    target_file: WebFile | None,
    name: str,
    target_module_id: str,
) -> str:
    if target_file is not None:
        return context.resolve_symbol(name, file_path=target_file.relative_path) or target_module_id
    return target_module_id


def _collect_export(context: WebAnalysisContext, web_file: WebFile, node: Any) -> None:
    # `export ... from "./module"` is also a module-level dependency.  Keeping
    # it as an explicit edge makes re-export chains traversable even when the
    # export does not declare a local symbol.
    source_node = node.child_by_field_name("source")
    reference = string_value(source_node, web_file.source)
    if reference:
        target_file = resolve_reference(context, web_file, reference)
        target_id = target_file.module_id if target_file else context.external_node(f"module:{reference}")
        status = "resolved" if target_file else "external"
        add_relation(
            context,
            web_file.module_id,
            target_id,
            "exports",
            resolution_status=status,
            confidence=1.0 if target_file else 0.7,
            source_span=_span(source_node),
            detail={
                "expression": node_text(node, web_file.source).strip(),
                "reference": reference,
                "kind": "re_export",
            },
        )
        if target_file is None:
            context.diagnostic(
                "unresolved_import",
                "warning",
                f"再公開元を対象リポジトリ内で解決できません: {reference}",
                web_file=web_file,
                tree_node=source_node,
                details={"reference": reference, "kind": "re_export"},
            )
    declaration = next(
        (
            child
            for child in node.children
            if child.type in _CLASS_DECLARATIONS
            or child.type in _FUNCTION_DECLARATIONS
            or child.type in {"interface_declaration", "type_alias_declaration", "enum_declaration"}
        ),
        None,
    )
    targets: list[str] = []
    if declaration is not None:
        target_id = context.definition_by_node.get((web_file.relative_path, declaration.id))
        if target_id:
            targets.append(target_id)
    else:
        declaration = next(
            (child for child in node.children if child.type in {"lexical_declaration", "variable_declaration"}),
            None,
        )
        if declaration is not None:
            for child in walk_tree(declaration):
                target_id = context.definition_by_node.get((web_file.relative_path, child.id))
                if target_id and context.builder.nodes.get(target_id, {}).get("kind") in {"function", "lambda", "class"}:
                    targets.append(target_id)
    for target_id in targets:
        add_relation(
            context,
            web_file.module_id,
            target_id,
            "exports",
            resolution_status="resolved",
            confidence=1.0,
            source_span=_span(node),
            detail={"expression": node_text(node, web_file.source).strip()},
        )
    clause = descendant(node, "export_clause")
    if clause is not None:
        for specifier in walk_tree(clause):
            if specifier.type != "export_specifier":
                continue
            name_node = specifier.child_by_field_name("name")
            if name_node is None:
                continue
            name = node_text(name_node, web_file.source).strip()
            target_id = context.resolve_symbol(name, file_path=web_file.relative_path)
            target_id = target_id or context.external_node(f"export:{web_file.relative_path}:{name}", unknown=True)
            status = "resolved" if target_id in context.builder.nodes and context.builder.nodes[target_id].get("file") else "unresolved"
            add_relation(
                context,
                web_file.module_id,
                target_id,
                "exports",
                resolution_status=status,
                confidence=1.0 if status == "resolved" else 0.3,
                source_span=_span(specifier),
                detail={"name": name},
            )


def _collect_call(context: WebAnalysisContext, web_file: WebFile, node: Any) -> None:
    function_node = node.child_by_field_name("function")
    if function_node is None:
        return
    callee = node_text(function_node, web_file.source).strip()
    caller = context.enclosing_definition(web_file, node.parent) or web_file.module_id
    args = node.child_by_field_name("arguments")
    selector = _dom_selector(callee, args, web_file.source)
    if selector is not None:
        relation_type = "handles" if callee.endswith(".addEventListener") else "references"
        context.dom_references.append(
            DomReference(
                source_id=caller,
                selector=selector,
                relation_type=relation_type,
                file_path=web_file.relative_path,
                span=_span(node),
                detail={"expression": node_text(node, web_file.source).strip(), "selector": selector},
            )
        )
        if (
            callee.endswith((".querySelector", ".querySelectorAll", ".getElementById", ".addEventListener"))
        ):
            return

    if callee == "require":
        _collect_require(context, web_file, node)
        return
    if callee == "import":
        _collect_dynamic_import(context, web_file, node)
        return

    target_id, status, confidence = _resolve_call(context, web_file, function_node, callee)
    detail = {
        "expression": node_text(node, web_file.source).strip(),
        "call_kind": "attribute" if function_node.type == "member_expression" else "direct",
    }
    add_relation(
        context,
        caller,
        target_id,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span(node),
        detail=detail,
    )
    if status == "unresolved":
        context.diagnostic(
            "unresolved_call",
            "warning",
            f"呼び出し先を静的に解決できません: {callee}",
            web_file=web_file,
            tree_node=function_node,
            node_id=caller,
            details={"expression": node_text(node, web_file.source).strip()},
        )


def _collect_new(context: WebAnalysisContext, web_file: WebFile, node: Any) -> None:
    constructor = node.child_by_field_name("constructor") or direct_child(node, "identifier", "member_expression")
    if constructor is None:
        return
    callee = node_text(constructor, web_file.source).strip()
    caller = context.enclosing_definition(web_file, node.parent) or web_file.module_id
    target_id, status, confidence = _resolve_call(context, web_file, constructor, callee)
    add_relation(
        context,
        caller,
        target_id,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=_span(node),
        detail={"expression": node_text(node, web_file.source).strip(), "call_kind": "constructor"},
    )


def _collect_require(context: WebAnalysisContext, web_file: WebFile, node: Any) -> None:
    arguments = node.child_by_field_name("arguments")
    value_node = _first_argument(arguments)
    reference = string_value(value_node, web_file.source)
    if not reference:
        _collect_dynamic_import(context, web_file, node, relation_type="dynamic_imports")
        return
    target_file = resolve_reference(context, web_file, reference)
    target_id = target_file.module_id if target_file else context.external_node(f"module:{reference}")
    add_relation(
        context,
        web_file.module_id,
        target_id,
        "imports",
        resolution_status="resolved" if target_file else "external",
        confidence=1.0 if target_file else 0.7,
        source_span=_span(node),
        detail={"expression": node_text(node, web_file.source).strip(), "reference": reference, "kind": "require"},
    )
    parent = node.parent
    if parent is not None and parent.type == "variable_declarator":
        name_node = parent.child_by_field_name("name")
        if name_node is not None:
            context.import_bindings.setdefault(web_file.relative_path, {})[
                node_text(name_node, web_file.source).strip()
            ] = target_id


def _collect_dynamic_import(
    context: WebAnalysisContext,
    web_file: WebFile,
    node: Any,
    *,
    relation_type: str = "dynamic_imports",
) -> None:
    arguments = node.child_by_field_name("arguments")
    reference = string_value(_first_argument(arguments), web_file.source)
    target_file = resolve_reference(context, web_file, reference) if reference else None
    target_id = (
        target_file.module_id
        if target_file
        else context.external_node(f"dynamic-import:{reference or node_text(node, web_file.source).strip()}", unknown=True)
    )
    status = "resolved" if target_file else "unresolved"
    add_relation(
        context,
        web_file.module_id,
        target_id,
        relation_type,
        resolution_status=status,
        confidence=1.0 if target_file else 0.3,
        source_span=_span(node),
        detail={"expression": node_text(node, web_file.source).strip(), "reference": reference},
    )
    if not target_file:
        context.diagnostic(
            "dynamic_import",
            "warning",
            "動的importの対象を静的に解決できません。",
            web_file=web_file,
            tree_node=node,
            details={"expression": node_text(node, web_file.source).strip()},
        )


def _resolve_call(
    context: WebAnalysisContext,
    web_file: WebFile,
    function_node: Any,
    callee: str,
) -> tuple[str, str, float]:
    if function_node.type in {"identifier", "type_identifier", "predefined_type"}:
        target = context.resolve_symbol(callee, file_path=web_file.relative_path)
        if target:
            target_kind = context.builder.nodes.get(target, {}).get("kind")
            if target_kind in {"function", "method", "lambda", "class"}:
                return target, "resolved", 0.95
            return target, "unresolved", 0.4
        if callee in _KNOWN_GLOBALS:
            return context.external_node(f"global:{callee}"), "external", 0.5
        return context.external_node(f"call:{web_file.relative_path}:{callee}", unknown=True), "unresolved", 0.2
    if function_node.type == "member_expression":
        property_node = function_node.child_by_field_name("property")
        object_node = function_node.child_by_field_name("object")
        property_name = node_text(property_node, web_file.source).strip() if property_node is not None else callee
        object_name = node_text(object_node, web_file.source).strip() if object_node is not None else ""
        if object_name and object_name not in {"this", "super"}:
            target = context.resolve_symbol(
                property_name,
                file_path=web_file.relative_path,
                qualified_name=f"{object_name}.{property_name}",
            )
            if target:
                return target, "resolved", 0.85
        target = context.resolve_symbol(property_name, file_path=web_file.relative_path)
        if target and context.builder.nodes.get(target, {}).get("kind") in {"method", "function"}:
            return target, "resolved", 0.65
        return context.external_node(f"call:{web_file.relative_path}:{callee}", unknown=True), "unresolved", 0.2
    return context.external_node(f"call:{web_file.relative_path}:{callee}", unknown=True), "unresolved", 0.15


def _collect_inheritance(context: WebAnalysisContext, web_file: WebFile, node: Any) -> None:
    class_id = context.definition_by_node.get((web_file.relative_path, node.id))
    heritage = node.child_by_field_name("heritage") or descendant(node, "class_heritage")
    if class_id is None or heritage is None:
        return
    base_node = next(
        (child for child in heritage.children if child.type not in {"extends", ",", "implements"}),
        None,
    )
    if base_node is None:
        return
    base_name = node_text(base_node, web_file.source).strip()
    target = context.resolve_symbol(base_name, file_path=web_file.relative_path)
    target = target or context.external_node(f"inheritance:{base_name}", unknown=True)
    status = "resolved" if target in context.builder.nodes and context.builder.nodes[target].get("kind") == "class" else "unresolved"
    add_relation(
        context,
        class_id,
        target,
        "inherits",
        resolution_status=status,
        confidence=1.0 if status == "resolved" else 0.25,
        source_span=_span(heritage),
        detail={"base": base_name},
    )
    if status == "unresolved":
        context.diagnostic(
            "unresolved_inheritance",
            "warning",
            f"継承元を静的に解決できません: {base_name}",
            web_file=web_file,
            tree_node=heritage,
            node_id=class_id,
            details={"base": base_name},
        )


def _dom_selector(callee: str, arguments: Any | None, source: bytes) -> str | None:
    first = _first_argument(arguments)
    value = string_value(first, source)
    if value is None:
        return None
    if callee.endswith((".getElementById", "getElementById")):
        return f"#{value}"
    if callee.endswith((".querySelector", ".querySelectorAll")):
        return value
    if callee.endswith(".addEventListener"):
        call_node = arguments.parent if arguments is not None else None
        function_node = call_node.child_by_field_name("function") if call_node is not None else None
        object_node = function_node.child_by_field_name("object") if function_node is not None else None
        if object_node is not None and object_node.type == "call_expression":
            nested_function = object_node.child_by_field_name("function")
            nested_args = object_node.child_by_field_name("arguments")
            nested_callee = node_text(nested_function, source).strip() if nested_function else ""
            return _dom_selector(nested_callee, nested_args, source)
    return None


def _first_argument(arguments: Any | None) -> Any | None:
    if arguments is None:
        return None
    return next(
        (
            child
            for child in arguments.children
            if child.type not in {"(", ")", ","}
        ),
        None,
    )


def _span(node: Any) -> dict[str, int] | None:
    from .web_common import span_for_tree

    return span_for_tree(node)
