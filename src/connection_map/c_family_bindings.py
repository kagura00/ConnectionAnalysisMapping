"""Conservative lexical bindings for C/C++; no receiver or points-to inference."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .c_family_common import CFamilyAnalysisContext, CFile, CSymbol, node_text, walk_tree

_SCOPES = {
    "translation_unit", "namespace_definition", "class_specifier", "struct_specifier", "union_specifier",
    "function_definition", "compound_statement", "lambda_expression", "for_statement", "for_range_loop",
    "catch_clause", "if_statement", "switch_statement", "while_statement", "function_declarator", "template_declaration",
}
_FUNCTIONS = {"function", "method"}


def _inner_declarator(node: Any) -> Any | None:
    child = node.child_by_field_name("declarator")
    if child is not None:
        return child
    return next((n for n in node.named_children if n.type.endswith("declarator")
                 or n.type in {"identifier", "field_identifier", "qualified_identifier"}), None)


def declarator_name(node: Any | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type in {"identifier", "field_identifier", "type_identifier", "namespace_identifier"}:
        return node_text(node, source).strip()
    if node.type in {"qualified_identifier", "scoped_identifier"}:
        return re.sub(r"\s+", "", node_text(node, source)).removeprefix("::")
    for field in ("declarator", "name"):
        child = node.child_by_field_name(field)
        if child is not None:
            result = declarator_name(child, source)
            if result:
                return result
    child = _inner_declarator(node)
    if child is not None:
        return declarator_name(child, source)
    return None


def _scope(node: Any | None) -> Any | None:
    while node is not None:
        if node.type in _SCOPES:
            return node
        node = node.parent
    return None


def _ancestor_ids(node: Any) -> set[int]:
    result = set()
    while node is not None:
        result.add(node.id)
        node = node.parent
    return result


def _parameter_scope(node: Any) -> Any | None:
    current = node.parent
    declarator = None
    while current is not None:
        if current.type in {"parameter_declaration", "optional_parameter_declaration", "variadic_parameter_declaration"}:
            return declarator  # A parameter inside a function-pointer type does not enter the outer body.
        if current.type == "function_declarator" and declarator is None:
            declarator = current
        if current.type in {"function_definition", "lambda_expression", "catch_clause", "template_declaration"}:
            return current
        if current.type in {"declaration", "field_declaration"}:
            return declarator
        current = current.parent
    return declarator


@dataclass(frozen=True, slots=True)
class BindingResult:
    candidates: tuple[CSymbol, ...] = ()
    reason: str | None = None
    type_expression: bool = False


class CBindings:
    def __init__(self, context: CFamilyAnalysisContext):
        self.context = context
        self.values: dict[tuple[str, int, str], list[int]] = defaultdict(list)
        self.types: dict[tuple[str, int, str], list[int]] = defaultdict(list)
        self.functions: dict[tuple[str, int, str], list[int]] = defaultdict(list)
        self.callable_declarations: dict[tuple[str, int], Any] = {}
        self.trees: dict[str, Any] = {}
        self.visible_files: dict[str, set[str]] = {}
        self.using: dict[tuple[str, int], list[tuple[int, str | None]]] = defaultdict(list)
        self.known_types = set()
        self.bases: dict[str, list[str]] = defaultdict(list)
        self.base_bindings: dict[str, list[str | None]] = defaultdict(list)
        self.anonymous: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.parameter_keys: dict[str, tuple[str, ...]] = {}
        self.type_expression_roots: set[tuple[str, int]] = set()
        for c_file in context.files:
            for node in walk_tree(c_file.tree.root_node):
                if node.type in {"class_specifier", "struct_specifier", "union_specifier", "enum_specifier"}:
                    name = node.child_by_field_name("name")
                    if name is not None:
                        self.known_types.add(node_text(name, c_file.source))
                elif node.type in {"type_definition", "alias_declaration"}:
                    names = node.children_by_field_name("declarator") or [node.child_by_field_name("name")]
                    for target in names:
                        self._add_type(c_file, target, _scope(node.parent))
                elif node.type in {"type_parameter_declaration", "variadic_type_parameter_declaration",
                                   "optional_type_parameter_declaration"}:
                    target = node.child_by_field_name("name") or next(
                        (child for child in node.named_children if child.type == "type_identifier"), None)
                    self._add_type(c_file, target, _scope(node.parent))
                if node.type in {"parameter_declaration", "optional_parameter_declaration", "variadic_parameter_declaration"}:
                    self._add_value(c_file, node.child_by_field_name("declarator"), _parameter_scope(node))
                elif node.type == "for_range_loop":
                    body = node.child_by_field_name("body")
                    self._add_value(c_file, node.child_by_field_name("declarator"), node,
                                    start=body.start_byte if body is not None else node.end_byte)
                elif node.type == "lambda_capture_specifier":
                    scope = _scope(node.parent)
                    for child in node.named_children:
                        target = child.child_by_field_name("left") if child.type == "lambda_capture_initializer" else child
                        self._add_value(c_file, target, scope)
        for c_file in context.files:
            for node in walk_tree(c_file.tree.root_node):
                if node.type in {"declaration", "field_declaration"}:
                    scope = _scope(node.parent)
                    for decl in node.children_by_field_name("declarator"):
                        if self._is_callable(c_file, node, decl):
                            self.callable_declarations[(c_file.relative_path, node.id)] = decl
                            self._add_name(self.functions, c_file, decl, scope)
                        else:
                            self._add_value(c_file, decl, scope)
                elif node.type == "function_definition":
                    self._add_name(self.functions, c_file, node.child_by_field_name("declarator"), _scope(node.parent))
                elif node.type == "using_declaration":
                    scope = _scope(node.parent)
                    if scope is not None:
                        raw = node_text(node, c_file.source)
                        label = None if re.match(r"using\s+namespace\b", raw) else raw.rstrip("; ").rsplit("::", 1)[-1]
                        self.using[(c_file.relative_path, scope.id)].append((node.start_byte, label))
                elif node.type == "assignment_expression":
                    # The C++ grammar can parse int (*fp)() = value as nested calls.
                    # A primitive type in this precise declarator shape is not a callable expression.
                    left = node.child_by_field_name("left")
                    inner = left.child_by_field_name("function") if left is not None else None
                    type_node = inner.child_by_field_name("function") if inner is not None else None
                    arguments = inner.child_by_field_name("arguments") if inner is not None else None
                    if (left is not None and left.type == "call_expression" and inner is not None
                            and inner.type == "call_expression" and type_node is not None
                            and type_node.type == "primitive_type" and arguments is not None
                            and len(arguments.named_children) == 1):
                        pointer = arguments.named_children[0]
                        name = pointer.child_by_field_name("argument")
                        if pointer.type == "pointer_expression" and name is not None and name.type == "identifier":
                            self._add_value(c_file, name, _scope(node.parent))
                            self.type_expression_roots.add((c_file.relative_path, left.id))

    def _add_name(self, index: dict, c_file: CFile, target: Any | None, scope: Any | None) -> None:
        if target is None or scope is None:
            return
        name = declarator_name(target, c_file.source)
        if name:
            position = 0 if scope.type in {"class_specifier", "struct_specifier", "union_specifier"} else target.start_byte
            index[(c_file.relative_path, scope.id, name)].append(position)

    def _add_type(self, c_file: CFile, target: Any | None, scope: Any | None) -> None:
        name = declarator_name(target, c_file.source)
        if name:
            self.known_types.add(name)
            self._add_name(self.types, c_file, target, scope)

    def _scope_binding(self, c_file: CFile, node: Any, path: str, scope: Any, name: str) -> str | None:
        for kind, index in (("value", self.values), ("type", self.types), ("function", self.functions)):
            if any(path != c_file.relative_path or position <= node.start_byte
                   for position in index.get((path, scope.id, name), [])):
                return kind
        return None

    def _local_binding(self, c_file: CFile, node: Any, name: str) -> str | None:
        current = node
        while current is not None:
            if current.type in {"translation_unit", "namespace_definition", "class_specifier",
                                "struct_specifier", "union_specifier"}:
                break  # Named scopes also need declarations from headers and out-of-class definitions.
            binding = self._scope_binding(c_file, node, c_file.relative_path, current, name)
            if binding:
                return binding
            current = current.parent
        return None

    def _prefix_binding(self, prefix: str, c_file: CFile, node: Any, name: str) -> str | None:
        scopes = []
        if not prefix:
            scopes = [(path, self.context.files_by_path[path].tree.root_node)
                      for path in sorted(self.visible_files[c_file.relative_path])]
        else:
            for symbol in self.context.symbols_by_qualified_name.get(prefix, []):
                if symbol.kind not in {"class", "type", "namespace"} or not self._visible(symbol, c_file, node):
                    continue
                tree = self.trees.get(symbol.node_id)
                if tree is not None:
                    scopes.append((symbol.file_path, tree))
                    if tree.parent is not None and tree.parent.type == "template_declaration":
                        scopes.append((symbol.file_path, tree.parent))
        bindings = {self._scope_binding(c_file, node, path, scope, name) for path, scope in scopes} - {None}
        if len(bindings) > 1:
            return "ambiguous"
        return next(iter(bindings), None)

    @staticmethod
    def _non_function_binding(binding: str | None, *, qualified: bool = False) -> BindingResult | None:
        if binding == "type" and not qualified:
            return BindingResult(type_expression=True)
        if binding in {"value", "type", "ambiguous"}:
            return BindingResult(reason="引数・変数・型による名前の隠蔽または間接呼び出し")
        return None

    def _add_value(self, c_file: CFile, declarator: Any | None, scope: Any | None, *, start: int | None = None) -> None:
        if declarator is None or scope is None:
            return
        target = declarator
        while target.type not in {"identifier", "field_identifier", "structured_binding_declarator"}:
            child = _inner_declarator(target)
            if child is None:
                break
            target = child
        if target.type == "structured_binding_declarator":
            names = [node_text(n, c_file.source) for n in target.named_children if n.type == "identifier"]
        else:
            name = declarator_name(declarator, c_file.source)
            names = [name] if name else []
        # Members are visible throughout a class, including inline methods above the field.
        position = 0 if scope.type in {"class_specifier", "struct_specifier", "union_specifier"} else declarator.start_byte
        for name in names:
            self.values[(c_file.relative_path, scope.id, name)].append(start if start is not None else position)

    def value_shadow(self, c_file: CFile, node: Any, name: str) -> bool:
        current = node
        while current is not None:
            if any(position <= node.start_byte for position in self.values.get((c_file.relative_path, current.id, name), [])):
                return True
            current = current.parent
        # An out-of-class definition has its class in the graph, not in AST ancestry.
        for symbol in self._graph_scopes(c_file, node):
            if symbol.kind not in {"class", "type", "namespace"}:
                continue
            prefixes, pending = {symbol.qualified_name}, [symbol.qualified_name]
            if symbol.kind in {"class", "type"}:
                while pending:
                    current_prefix = pending.pop()
                    for base in self.bases.get(current_prefix, []):
                        if base not in prefixes:
                            prefixes.add(base)
                            pending.append(base)
            for scope_symbol in [s for prefix in prefixes for s in self.context.symbols_by_qualified_name[prefix]]:
                if scope_symbol.file_path not in self.visible_files.get(c_file.relative_path, set()):
                    continue
                tree = self.trees.get(scope_symbol.node_id)
                if tree is not None and any(
                    scope_symbol.file_path != c_file.relative_path or position <= node.start_byte
                    for position in self.values.get((scope_symbol.file_path, tree.id, name), [])
                ):
                    return True
        return False

    def _graph_scopes(self, c_file: CFile, node: Any) -> list[CSymbol]:
        node_id = self.context.enclosing_definition(c_file, node.parent)
        result, visited = [], set()
        while node_id in self.context.definitions and node_id not in visited:
            visited.add(node_id)
            result.append(self.context.definitions[node_id])
            node_id = self.context.builder.nodes[node_id].get("parent_id")
        return result

    def _is_callable(self, c_file: CFile, declaration: Any, declarator: Any) -> bool:
        while declarator.type in {"pointer_declarator", "reference_declarator", "attributed_declarator"}:
            child = _inner_declarator(declarator)
            if child is None:
                return False
            declarator = child
        if declarator.type == "function_declarator":
            inner = declarator.child_by_field_name("declarator")
            if inner is None or inner.type in {"parenthesized_declarator", "pointer_declarator"}:
                return False
            parameters = declarator.child_by_field_name("parameters")
            if parameters is None:
                return False
            for parameter in parameters.named_children:
                type_node = parameter.child_by_field_name("type")
                if type_node is None:
                    continue
                label = node_text(type_node, c_file.source)
                if self.value_shadow(c_file, declaration, label):
                    return False
                # A bare unknown name in a local ambiguous declaration may be an initializer value.
                if (type_node.type == "type_identifier" and parameter.child_by_field_name("declarator") is None
                        and label not in self.known_types and _scope(declaration.parent).type == "compound_statement"):
                    return False
            return True
        if declarator.type == "init_declarator":
            # Tree-sitter parses T f(T()) as initialization, although C++ declares a function.
            value = declarator.child_by_field_name("value")
            if value is None or value.type != "argument_list" or not value.named_children:
                return False
            for argument in value.named_children:
                function = argument.child_by_field_name("function")
                arguments = argument.child_by_field_name("arguments")
                if (argument.type != "call_expression" or function is None or arguments is None
                        or arguments.named_children or node_text(function, c_file.source) not in self.known_types):
                    return False
            return True
        return False

    def declaration(self, c_file: CFile, node: Any) -> Any | None:
        return self.callable_declarations.get((c_file.relative_path, node.id))

    def declaration_type_expression(self, c_file: CFile, node: Any) -> bool:
        current = node
        while current is not None:
            if (c_file.relative_path, current.id) in self.type_expression_roots:
                return True
            if current.type in {"declaration", "field_declaration"}:
                declarator = self.declaration(c_file, current)
                return declarator is not None and declarator.type == "init_declarator"
            current = current.parent
        return False

    def prepare(self) -> None:
        for c_file in self.context.files:
            for node in walk_tree(c_file.tree.root_node):
                node_id = self.context.definition_by_node.get((c_file.relative_path, node.id))
                if node_id:
                    self.trees[node_id] = node
        imports: dict[str, set[str]] = defaultdict(set)
        for edge in self.context.builder.edges.values():
            if edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved":
                source = self.context.builder.nodes[edge["source_id"]]["file"]
                target = self.context.builder.nodes[edge["target_id"]]["file"]
                imports[source].add(target)
        for c_file in self.context.files:
            reached, pending = {c_file.relative_path}, [c_file.relative_path]
            while pending:
                current = pending.pop()
                for included in imports[current] - reached:
                    reached.add(included)
                    pending.append(included)
            self.visible_files[c_file.relative_path] = reached
        for symbol in self.context.definitions.values():
            if symbol.kind != "namespace" or not symbol.name.startswith("<anonymous@"):
                continue
            prefix = symbol.qualified_name.rsplit("::", 1)[0] if "::" in symbol.qualified_name else ""
            for c_file in self.context.files:
                if symbol.file_path in self.visible_files[c_file.relative_path]:
                    self.anonymous[(c_file.relative_path, prefix)].add(symbol.qualified_name)

    def prepare_inheritance(self) -> None:
        for edge in self.context.builder.edges.values():
            if edge["relation_type"] == "inherits":
                source = self.context.definitions.get(edge["source_id"])
                target = self.context.definitions.get(edge["target_id"])
                if source is None:
                    continue
                # Template arguments/specializations are not instantiated by this analyzer.
                raw = edge.get("detail", {}).get("source_reference", "")
                if edge["resolution_status"] == "resolved" and target and not self._template_base(source, raw):
                    self.bases[source.qualified_name].append(target.qualified_name)
                    self.base_bindings[source.node_id].append(target.qualified_name)
                else:
                    self.base_bindings[source.node_id].append(None)

    def _template_base(self, source: CSymbol, raw: str, *, arguments: bool = True) -> bool:
        if arguments and "<" in raw:
            return True
        tree = self.trees.get(source.node_id)
        current = tree
        c_file = self.context.files_by_path[source.file_path]
        first = raw.split("::", 1)[0].split("<", 1)[0].strip()
        while current is not None:
            if (current.type == "template_declaration"
                    and self._scope_binding(c_file, tree, source.file_path, current, first) in {"value", "type"}):
                return True
            current = current.parent
        return False

    def inheritance_lookup(self, c_file: CFile, node: Any, source: CSymbol, reference: str,
                           raw: str, kinds: set[str]) -> BindingResult:
        if self._template_base(source, raw, arguments=False):
            return BindingResult(reason="テンプレート引数・特殊化に依存する継承元は実体化せず、確定できません")
        return self.resolve(c_file, node, reference, kinds=kinds)

    def _inherited(self, prefix: str, c_file: CFile, node: Any, name: str, kinds: set[str],
                   visited: frozenset[str] = frozenset()) -> BindingResult:
        if prefix in visited:
            return BindingResult(reason="循環した継承元の名前探索は確定できません")
        bases = {base for symbol in self.context.symbols_by_qualified_name.get(prefix, [])
                 if self._visible(symbol, c_file, node) for base in self.base_bindings.get(symbol.node_id, [])}
        if None in bases:
            return BindingResult(reason="解析範囲外またはテンプレート依存の継承元があり、名前探索を確定できません")
        found = []
        for base in sorted(bases):
            binding = self._non_function_binding(self._prefix_binding(base, c_file, node, name)) if kinds <= _FUNCTIONS else None
            candidates = self._candidates(base + "::" + name, c_file, node, kinds)
            result = binding or (BindingResult(tuple(candidates)) if candidates else
                                self._inherited(base, c_file, node, name, kinds, visited | {prefix}))
            if result.reason:
                return result
            if result.candidates or result.type_expression:
                found.append(result)
        if len(found) > 1:
            return BindingResult(reason="複数の継承経路に同名の宣言があり、名前探索を確定できません")
        return found[0] if found else BindingResult()

    def _visible(self, symbol: CSymbol, c_file: CFile, node: Any) -> bool:
        if symbol.file_path not in self.visible_files[c_file.relative_path]:
            return False
        tree = self.trees.get(symbol.node_id)
        if tree is None:
            return False
        scope = _scope(tree.parent)
        if symbol.file_path == c_file.relative_path:
            if scope is not None and scope.type in {
                "compound_statement", "for_statement", "for_range_loop", "catch_clause", "lambda_expression",
            } and scope.id not in _ancestor_ids(node):
                return False
            if tree.start_byte > node.start_byte and (scope is None or scope.type not in {
                "class_specifier", "struct_specifier", "union_specifier",
            }):
                return False
        elif scope is not None and scope.type in {"compound_statement", "function_definition", "lambda_expression"}:
            return False
        return True

    def _internal(self, symbol: CSymbol) -> bool:
        if "<anonymous@" in symbol.qualified_name:
            return True
        tree = self.trees.get(symbol.node_id)
        parent = self.context.definitions.get(self.context.builder.nodes[symbol.node_id].get("parent_id"))
        return bool(tree is not None and (parent is None or parent.kind not in {"class", "type"})
                    and any(n.type == "storage_class_specifier" and node_text(n, self.context.files_by_path[symbol.file_path].source) == "static"
                            for n in tree.named_children))

    def _parameter_key(self, symbol: CSymbol) -> tuple[str, ...]:
        if symbol.node_id in self.parameter_keys:
            return self.parameter_keys[symbol.node_id]
        tree = self.trees[symbol.node_id]
        c_file = self.context.files_by_path[symbol.file_path]
        declarator = tree.child_by_field_name("declarator")
        while declarator is not None and declarator.type not in {"function_declarator", "init_declarator"}:
            declarator = _inner_declarator(declarator)
        parameters = None if declarator is None else declarator.child_by_field_name("parameters")
        if parameters is None and declarator is not None:
            parameters = declarator.child_by_field_name("value")
        if parameters is None:
            return ()
        type_node = tree.child_by_field_name("type")
        return_type = node_text(type_node, c_file.source) if type_node is not None else ""
        result = ["return:" + re.sub(r"\s+", "", return_type)]
        for parameter in parameters.named_children:
            raw = node_text(parameter, c_file.source)
            value = parameter.child_by_field_name("default_value")
            if value is not None:
                raw = c_file.source[parameter.start_byte:value.start_byte].decode("utf-8", errors="replace").rstrip("= ")
            name = declarator_name(parameter.child_by_field_name("declarator"), c_file.source)
            if name:
                raw = re.sub(rf"\b{re.escape(name)}\b", "", raw, count=1)
            result.append(re.sub(r"\s+", "", raw))
        if declarator is not None:
            result.extend("qualifier:" + node_text(child, c_file.source)
                          for child in declarator.named_children
                          if child.type in {"type_qualifier", "ref_qualifier", "requires_clause"})
        key = tuple(result)
        self.parameter_keys[symbol.node_id] = key
        return key

    def _candidates(self, qualified: str, c_file: CFile, node: Any, kinds: set[str]) -> list[CSymbol]:
        all_symbols = [s for s in self.context.symbols_by_qualified_name.get(qualified, []) if s.kind in kinds]
        visible = [s for s in all_symbols if self._visible(s, c_file, node)]
        if not visible:
            return []
        # A visible declaration may refer to a definition in another translation unit.
        candidates = [s for s in all_symbols if s in visible or (s.declaration_kind == "definition" and not self._internal(s))]
        if kinds <= _FUNCTIONS:
            groups: dict[tuple[str, ...], list[CSymbol]] = defaultdict(list)
            for symbol in candidates:
                groups[self._parameter_key(symbol)].append(symbol)
            result = []
            for group in groups.values():
                definitions = [s for s in group if s.declaration_kind == "definition"]
                if definitions:
                    result.extend(definitions)
                else:
                    result.append(sorted(group, key=lambda s: (s.file_path != c_file.relative_path, s.node_id))[0])
            return result
        return visible

    def evidence(self, c_file: CFile, node: Any, target: CSymbol) -> dict[str, Any]:
        declarations = [symbol for symbol in self.context.symbols_by_qualified_name[target.qualified_name]
                        if symbol.kind in _FUNCTIONS and self._visible(symbol, c_file, node)
                        and self._parameter_key(symbol) == self._parameter_key(target)]
        declarations.sort(key=lambda symbol: (symbol.file_path != c_file.relative_path, symbol.node_id))
        return {"basis": "visible_lexical_declaration", "qualified_name": target.qualified_name,
                "target_file": target.file_path,
                "visible_declarations": [{"node_id": symbol.node_id, "file": symbol.file_path,
                                          "span": self.context.builder.nodes[symbol.node_id]["span"]}
                                         for symbol in declarations[:5]]}

    def _scope_names(self, c_file: CFile, node: Any) -> list[str]:
        names = [symbol.qualified_name for symbol in self._graph_scopes(c_file, node)]
        names.append("")
        return names

    def _anonymous_scopes(self, prefix: str, c_file: CFile) -> list[str]:
        return sorted(self.anonymous.get((c_file.relative_path, prefix), set()))

    def _has_using(self, c_file: CFile, node: Any, name: str) -> bool:
        current = node
        while current is not None:
            if any(byte <= node.start_byte and (label is None or label == name)
                   for byte, label in self.using.get((c_file.relative_path, current.id), [])):
                return True
            current = current.parent
        # A header's using directive can also affect unqualified lookup.
        for path in self.visible_files[c_file.relative_path] - {c_file.relative_path}:
            if any(label is None or label == name for (file, _), entries in self.using.items()
                   if file == path for _, label in entries):
                return True
        return False

    def resolve(self, c_file: CFile, node: Any, reference: str, *, kinds: set[str]) -> BindingResult:
        absolute = reference.startswith("::")
        reference = reference.removeprefix("::")
        if not reference:
            return BindingResult(reason="呼び出し先の構文を特定できません")
        if not absolute and kinds <= _FUNCTIONS:
            first = reference.split("::", 1)[0]
            binding = self._non_function_binding(self._local_binding(c_file, node, first), qualified="::" in reference)
            if binding:
                return binding
            scopes = self._graph_scopes(c_file, node)
            if ("::" not in reference and len(scopes) == 1 and scopes[0].kind in _FUNCTIONS
                    and "::" in scopes[0].qualified_name):
                return BindingResult(reason="修飾定義の所属スコープを解析範囲で特定できません")
        if not absolute and "::" not in reference and self._has_using(c_file, node, reference):
            return BindingResult(reason="using宣言を含む名前探索は確定できません")
        for prefix in [""] if absolute else self._scope_names(c_file, node):
            if kinds <= _FUNCTIONS:
                binding = self._non_function_binding(self._prefix_binding(prefix, c_file, node, reference.split("::", 1)[0]),
                                                     qualified="::" in reference)
                if binding:
                    return binding
            scopes = [prefix] + self._anonymous_scopes(prefix, c_file)
            candidates = []
            for scope in scopes:
                qualified = scope + "::" + reference if scope else reference
                candidates.extend(self._candidates(qualified, c_file, node, kinds))
            if not candidates and "::" not in reference:
                inherited = self._inherited(prefix, c_file, node, reference, kinds)
                if inherited.reason or inherited.type_expression:
                    return inherited
                candidates.extend(inherited.candidates)
            if candidates:
                unique = {s.node_id: s for s in candidates}
                return BindingResult(tuple(unique.values()))
        if any(s.kind in kinds for s in self.context.symbols_by_name.get(reference.rsplit("::", 1)[-1], [])):
            return BindingResult(reason="同名候補はありますが、修飾名・スコープ・宣言の可視性が一致しません")
        return BindingResult()
