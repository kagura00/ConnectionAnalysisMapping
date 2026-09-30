"""Conservative lexical bindings and explicit receiver resolution for JS/TS.

Names in other files are never implicit bindings. Writes and ambiguous scopes
stop resolution; this is a static declaration graph, not runtime dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .web_common import descendant, node_text, resolve_reference, span_for_tree, walk_tree
from .web_common import literal_string_value as string_value

FUNCTIONS = {'function_declaration', 'generator_function_declaration', 'function_expression',
             'function', 'generator_function', 'arrow_function', 'method_definition'}
SCOPES = FUNCTIONS | {'program', 'statement_block', 'catch_clause', 'for_statement', 'for_in_statement'}
GLOBALS = {'Array', 'Boolean', 'Date', 'Error', 'JSON', 'Map', 'Math', 'Number', 'Object', 'Promise',
           'RegExp', 'Set', 'String', 'Symbol', 'console', 'fetch', 'globalThis', 'parseInt', 'parseFloat',
           'setInterval', 'setTimeout', 'clearInterval', 'clearTimeout', 'window', 'document'}


@dataclass
class Value:
    kind: str
    target: str | None = None
    origin: tuple[str, str] | None = None
    node: Any = None
    file: Any = None
    evidence: list[dict] = field(default_factory=list)
    blocked_members: frozenset[str] = frozenset()


@dataclass
class Binding:
    file: Any
    node: Any
    value: Any = None
    target: str | None = None
    origin: tuple[str, str] | None = None
    written: bool = False
    type_node: Any = None
    member_writes: set[str] = field(default_factory=set)


def binding_names(node: Any) -> list[str]:
    if node is None:
        return []
    if node.type in {'identifier', 'type_identifier', 'shorthand_property_identifier_pattern'}:
        return [node.text.decode('utf-8')]
    if node.type in {'required_parameter', 'optional_parameter'}:
        return binding_names(node.child_by_field_name('pattern') or node.child_by_field_name('name'))
    if node.type in {'assignment_pattern', 'object_assignment_pattern'}:
        return binding_names(node.child_by_field_name('left'))
    if node.type == 'pair_pattern':
        return binding_names(node.child_by_field_name('value'))
    if node.type in {'object_pattern', 'array_pattern', 'rest_pattern', 'formal_parameters'}:
        return [name for child in node.named_children for name in binding_names(child)]
    return []


class Bindings:
    def __init__(self, context: Any):
        self.context = context
        self.tables: dict[tuple[str, int], dict[str, list[Binding]]] = {}
        self.fields: dict[tuple[str, str], list[Binding]] = {}
        self.definitions: dict[str, tuple[Any, Any]] = {}
        self.exports: dict[tuple[str, str], tuple[Any, str, str | None]] = {}
        for source in context.files:
            if source.language not in {'javascript', 'typescript'}:
                continue
            self._collect(source)
        for source in context.files:
            if source.language in {'javascript', 'typescript'}:
                self._writes(source)

    def _scope(self, node: Any, *, var: bool = False) -> Any:
        while node is not None:
            if node.type in (FUNCTIONS | {'program'} if var else SCOPES):
                return node
            node = node.parent
        return None

    def _add(self, source: Any, scope: Any, name: str, binding: Binding) -> None:
        if scope is not None:
            self.tables.setdefault((source.relative_path, scope.id), {}).setdefault(name, []).append(binding)

    def _collect(self, source: Any) -> None:
        for node in walk_tree(source.tree.root_node):
            target = self.context.definition_by_node.get((source.relative_path, node.id))
            if target:
                self.definitions[target] = (source, node)
            if node.type in {'class_declaration', 'function_declaration', 'generator_function_declaration'}:
                name = node.child_by_field_name('name')
                if name is not None:
                    self._add(source, self._scope(node.parent), node_text(name, source.source), Binding(source, node, target=target))
            elif node.type == 'variable_declarator':
                pattern = node.child_by_field_name('name')
                for name in binding_names(pattern):
                    value = node.child_by_field_name('value') if pattern.type == 'identifier' else None
                    self._add(source, self._scope(node.parent, var=node.parent.type == 'variable_declaration'),
                              name, Binding(source, node, value=value, type_node=node.child_by_field_name('type')))
            if node.type in FUNCTIONS:
                parameters = node.child_by_field_name('parameters') or node.child_by_field_name('parameter')
                for name in binding_names(parameters):
                    self._add(source, node, name, Binding(source, parameters))
                if node.type in {'function_expression', 'function', 'generator_function'}:
                    name = node.child_by_field_name('name')
                    if name is not None:
                        self._add(source, node, node_text(name, source.source), Binding(source, node, target=target))
            elif node.type == 'catch_clause':
                for name in binding_names(node.child_by_field_name('parameter')):
                    self._add(source, node, name, Binding(source, node))
            elif node.type == 'for_in_statement' and any(c.type in {'let', 'const', 'var'} for c in node.children):
                for name in binding_names(node.child_by_field_name('left')):
                    self._add(source, node, name, Binding(source, node))
            if node.type in {'method_definition', 'public_field_definition', 'field_definition'}:
                class_id = self.enclosing_class(source, node)
                name = node.child_by_field_name('name')
                if class_id and name is not None:
                    self.fields.setdefault((class_id, node_text(name, source.source)), []).append(
                        Binding(source, node, value=node.child_by_field_name('value'), target=target))
            if node.type == 'import_statement':
                reference = string_value(node.child_by_field_name('source'), source.source)
                clause = descendant(node, 'import_clause')
                if not reference or clause is None:
                    continue
                for child in walk_tree(clause):
                    name, imported = None, None
                    if child.type == 'import_specifier':
                        imported = node_text(child.child_by_field_name('name'), source.source)
                        name = child.child_by_field_name('alias') or child.child_by_field_name('name')
                    elif child.type == 'namespace_import':
                        name = next((c for c in child.named_children if c.type == 'identifier'), None)
                        imported = '*'
                    elif child.type == 'identifier' and child.parent == clause:
                        name, imported = child, 'default'
                    if name is not None:
                        self._add(source, source.tree.root_node, node_text(name, source.source),
                                  Binding(source, child, origin=(reference, imported)))
            if node.type == 'export_statement':
                reference = string_value(node.child_by_field_name('source'), source.source)
                declaration = node.child_by_field_name('declaration')
                if declaration is not None:
                    name = declaration.child_by_field_name('name')
                    if name is not None:
                        local = node_text(name, source.source)
                        exported = 'default' if any(c.type == 'default' for c in node.children) else local
                        self.exports[(source.relative_path, exported)] = (source, local, None)
                    for child in declaration.named_children:
                        if child.type == 'variable_declarator':
                            for local in binding_names(child.child_by_field_name('name')):
                                self.exports[(source.relative_path, local)] = (source, local, None)
                clause = descendant(node, 'export_clause')
                if clause:
                    for child in clause.named_children:
                        name = child.child_by_field_name('name')
                        if name is not None:
                            local = node_text(name, source.source)
                            exported = node_text(child.child_by_field_name('alias') or name, source.source)
                            self.exports[(source.relative_path, exported)] = (source, local, reference)

    def _writes(self, source: Any) -> None:
        for node in walk_tree(source.tree.root_node):
            if node.type not in {'assignment_expression', 'augmented_assignment_expression', 'update_expression'}:
                continue
            left = node.child_by_field_name('left') or node.child_by_field_name('argument')
            if left is None:
                continue
            member_name = None
            while left is not None and left.type in {'member_expression', 'subscript_expression'}:
                if left.type == 'member_expression':
                    member_name = node_text(left.child_by_field_name('property'), source.source)
                else:
                    member_name = string_value(left.child_by_field_name('index'), source.source) or '*'
                left = left.child_by_field_name('object')
            if left is not None and left.type == 'this' and member_name is not None:
                class_id = self.enclosing_class(source, node)
                for (owner, name), bindings in self.fields.items():
                    if owner == class_id and (member_name == '*' or member_name == name):
                        for binding in bindings:
                            binding.written = True
            for name in binding_names(left):
                bindings = self.lookup(source, node, name)
                for binding in bindings or []:
                    if member_name is None:
                        binding.written = True
                    else:
                        binding.member_writes.add(member_name)

    def lookup(self, source: Any, node: Any, name: str) -> list[Binding] | None:
        while node is not None:
            table = self.tables.get((source.relative_path, node.id), {})
            if name in table:
                return table[name]
            node = node.parent
        return None

    def enclosing_class(self, source: Any, node: Any) -> str | None:
        current = node.parent
        while current is not None:
            if current.type in {'function_declaration', 'function_expression', 'function', 'generator_function'}:
                return None  # A normal nested function has its own this.
            if current.type == 'class_declaration':
                return self.context.definition_by_node.get((source.relative_path, current.id))
            current = current.parent
        return None

    def exported(self, source: Any, name: str, seen: frozenset) -> Value | None:
        key = ('export', source.relative_path, name)
        if key in seen or len(seen) >= 64:
            return None
        entry = self.exports.get((source.relative_path, name))
        if entry is None:
            return None
        owner, local, reference = entry
        if reference:
            target = resolve_reference(self.context, owner, reference)
            return self.exported(target, local, seen | {key}) if target else None
        return self.name(owner, owner.tree.root_node, local, seen | {key})

    def _binding(self, binding: Binding, seen: frozenset) -> Value | None:
        source, node = binding.file, binding.node
        key = (source.relative_path, node.id)
        if key in seen or len(seen) >= 64:
            return None
        # A typed ComponentFixture provides a declared DOM scope, even when the
        # fixture is assigned by beforeEach; it is never used as a call target.
        if binding.type_node is not None:
            generic = descendant(binding.type_node, 'generic_type')
            if generic:
                owner = self.expression(source, generic.child_by_field_name('name'), seen | {key})
                args = generic.child_by_field_name('type_arguments')
                if owner and owner.origin == ('@angular/core/testing', 'ComponentFixture') and args and args.named_children:
                    component = self.expression(source, args.named_children[0], seen | {key})
                    if component and component.target:
                        return Value('fixture', component.target, evidence=component.evidence)
        if binding.written:
            return None
        if binding.origin:
            reference, imported = binding.origin
            module = resolve_reference(self.context, source, reference)
            if module:
                value = Value('module', module.module_id, file=module) if imported == '*' else self.exported(module, imported, seen | {key})
            elif reference.startswith(('.', '/')) or self.context.projects.is_alias(reference):
                value = None
            else:
                value = Value('external', origin=binding.origin)
        elif binding.target:
            value = Value('symbol', binding.target)
        else:
            value = self.expression(source, binding.value, seen | {key})
        if value is not None:
            item = {'file': source.relative_path, 'span': span_for_tree(node), 'kind': node.type}
            return replace(value, evidence=[item, *value.evidence][:16],
                           blocked_members=value.blocked_members | binding.member_writes)
        return None

    def name(self, source: Any, node: Any, name: str, seen: frozenset = frozenset()) -> Value | None:
        bindings = self.lookup(source, node, name)
        if bindings is not None:
            return self._binding(bindings[0], seen) if len(bindings) == 1 else None
        if name == 'document':
            return Value('document')
        if name in GLOBALS:
            return Value('external', origin=('global', name))
        return None

    def member(self, receiver: Value, name: str, seen: frozenset = frozenset()) -> Value | None:
        if name in receiver.blocked_members or '*' in receiver.blocked_members:
            return None
        if receiver.kind == 'module':
            return self.exported(receiver.file, name, seen)
        if receiver.kind == 'fixture':
            if name == 'nativeElement':
                return Value('dom', receiver.target, evidence=receiver.evidence)
            if name == 'componentInstance':
                return Value('instance', receiver.target, evidence=receiver.evidence)
        if receiver.kind == 'external':
            if receiver.origin == ('global', 'window') and name == 'document':
                return Value('document')
            return Value('external', origin=(receiver.origin[0], receiver.origin[1] + '.' + name), evidence=receiver.evidence)
        if receiver.kind == 'object':
            candidates = [n for n in receiver.node.named_children if n.child_by_field_name('key') is not None
                          and node_text(n.child_by_field_name('key'), receiver.file.source).strip('"\'') == name]
            if len(candidates) == 1:
                return self.expression(receiver.file, candidates[0].child_by_field_name('value'), seen)
            return None
        if receiver.kind in {'instance', 'symbol'} and receiver.target:
            values = self.fields.get((receiver.target, name), [])
            if len(values) == 1:
                static = any(c.type == 'static' for c in values[0].node.children)
                if static != (receiver.kind == 'symbol'):
                    return None
                value = self._binding(values[0], seen)
                if value:
                    return replace(value, evidence=[*receiver.evidence, *value.evidence][:16])
        return None

    def expression(self, source: Any, node: Any, seen: frozenset = frozenset()) -> Value | None:
        if node is None or len(seen) >= 64:
            return None
        kind = node.type
        if kind in {'identifier', 'type_identifier'}:
            return self.name(source, node, node_text(node, source.source), seen)
        if kind in {'parenthesized_expression', 'as_expression', 'satisfies_expression', 'non_null_expression', 'await_expression'}:
            return self.expression(source, node.named_children[0] if node.named_children else None, seen)
        if kind in {'arrow_function', 'function_expression', 'function', 'class_declaration'}:
            target = self.context.definition_by_node.get((source.relative_path, node.id))
            return Value('symbol', target) if target else None
        if kind == 'this':
            target = self.enclosing_class(source, node)
            return Value('instance', target) if target else None
        if kind == 'object':
            return Value('object', node=node, file=source)
        if kind == 'member_expression':
            receiver = self.expression(source, node.child_by_field_name('object'), seen)
            prop = node.child_by_field_name('property')
            return self.member(receiver, node_text(prop, source.source), seen) if receiver and prop is not None else None
        if kind == 'new_expression':
            constructor = self.expression(source, node.child_by_field_name('constructor'), seen)
            if constructor and constructor.kind == 'symbol' and self.context.builder.nodes[constructor.target]['kind'] == 'class':
                return replace(constructor, kind='instance')
            return constructor if constructor and constructor.kind == 'external' else None
        if kind == 'call_expression':
            function = node.child_by_field_name('function')
            callee = self.expression(source, function, seen)
            arguments = node.child_by_field_name('arguments')
            args = arguments.named_children if arguments else []
            if callee and callee.origin == ('@angular/core', 'inject') and args:
                token = self.expression(source, args[0], seen)
                if token and token.origin in {('@angular/common', 'DOCUMENT'), ('@angular/core', 'DOCUMENT')}:
                    return Value('document', evidence=token.evidence)
                if token and token.kind == 'symbol' and self.context.builder.nodes[token.target]['kind'] == 'class':
                    return replace(token, kind='instance', evidence=[*callee.evidence, *token.evidence])
                return token if token and token.kind == 'external' else None
            if callee and callee.origin == ('@angular/core/testing', 'TestBed.createComponent') and args:
                token = self.expression(source, args[0], seen)
                if token and token.target:
                    return Value('fixture', token.target, evidence=token.evidence)
            if callee and callee.origin in {('express', 'default'), ('express', 'Router'), ('express', 'default.Router')}:
                return Value('router', target=f'{source.relative_path}:{node.start_byte}', evidence=callee.evidence)
            if function is not None and function.type == 'member_expression':
                prop = node_text(function.child_by_field_name('property'), source.source)
                owner = self.expression(source, function.child_by_field_name('object'), seen)
                if prop in {'querySelector', 'getElementById'} and owner and owner.kind in {'document', 'dom'}:
                    return Value('dom', owner.target, evidence=owner.evidence)
        return None
