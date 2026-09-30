"""Conservative Dart lexical bindings; no package loading or type execution."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from .phase3_common import TreeFile, node_text, span_for_tree, walk_with_ancestors


def local_uri(file: str, reference: str) -> str | None:
    if ":" in reference or reference.startswith(("/", "\\")) or "\\" in reference:
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(file), reference))
    return None if path == ".." or path.startswith("../") else path


@dataclass
class Scope:
    graph_id: str
    parent: Scope | None = None
    bindings: dict[str, set[str | None]] = field(default_factory=dict)

    def add(self, name: str, target: str | None) -> None:
        self.bindings.setdefault(name, set()).add(target)


@dataclass
class Import:
    module: str | None
    prefix: str | None
    shown: set[str] | None
    hidden: set[str]
    evidence: dict

    def allows(self, name: str) -> bool:
        return not name.startswith("_") and (self.shown is None or name in self.shown) and name not in self.hidden


class DartBindings:
    def __init__(self, files: list[TreeFile], nodes: dict[str, dict], graph_scopes: dict[int, str]):
        self.nodes = nodes
        self.node_scopes: dict[int, Scope] = {}
        self.definitions: dict[str, Scope] = {}
        self.modules: dict[str, Scope] = {}
        self.imports: dict[str, list[Import]] = {}
        self.opaque_modules = {
            file.relative_path for file in files
            if any(re.match(r"(?:export|part)\b", node_text(n, file.source).lstrip())
                   for n in file.tree.root_node.named_children)
        }
        for file in files:
            scope = Scope(file.module_id)
            self.modules[file.relative_path] = scope
            self.definitions[file.module_id] = scope
            self._visit(file.tree.root_node, scope, file, graph_scopes)
        for file in files:
            self.imports[file.relative_path] = self._imports(file)

    def _visit(self, node: Any, scope: Scope, file: TreeFile, graph_scopes: dict[int, str]) -> None:
        graph_id = graph_scopes.get(node.id)
        if graph_id and graph_id != scope.graph_id:
            if graph_id not in self.definitions:
                definition = self.nodes[graph_id]
                extensions = definition.get("extensions", {})
                name = extensions.get("member_name", definition["display_name"])
                # Constructors are reached via their class, never as a bare
                # local function. A named constructor is a class member.
                if not extensions.get("constructor") or name != self.nodes[definition["parent_id"]]["display_name"]:
                    scope.add(name, graph_id)
                self.definitions[graph_id] = Scope(graph_id, scope)
            scope = self.definitions[graph_id]
        elif node.type in {"block", "function_expression", "for_statement", "catch_clause"}:
            scope = Scope(scope.graph_id, scope)
            previous = node.prev_named_sibling
            while previous is not None and previous.type == "comment":
                previous = previous.prev_named_sibling
            if node.type == "block" and previous is not None and previous.type == "catch_clause":
                for child, _ in walk_with_ancestors(previous):
                    if child.type == "catch_parameters":
                        for name in child.named_children:
                            if name.type == "identifier":
                                scope.add(node_text(name, file.source), None)
        self.node_scopes[node.id] = scope

        if node.type in {"formal_parameter", "initialized_identifier", "initialized_variable_definition",
                         "declared_identifier", "type_parameter", "variable_pattern"}:
            identifiers = [child for child in node.named_children if child.type in {"identifier", "type_identifier"}]
            if node.type == "formal_parameter":
                identifiers = [child for child in node.named_children if child.type == "identifier"]
                if not identifiers:
                    wrapper = next((c for c in node.named_children if c.type in {"constructor_param", "function_signature"}), None)
                    identifiers = [c for c in wrapper.named_children if c.type == "identifier"] if wrapper else []
            elif node.type != "type_parameter":
                identifiers = [child for child in node.named_children if child.type == "identifier"]
            if identifiers:
                scope.add(node_text(identifiers[0], file.source), None)
        if node.type == "for_loop_parts":
            for child in node.children:
                if child.type == "in":
                    break
                if child.type == "identifier":
                    scope.add(node_text(child, file.source), None)
        if node.type == "pattern_variable_declaration":
            pattern = next((c for c in node.named_children if c.type.endswith("pattern")), None)
            if pattern:
                for name, _ in walk_with_ancestors(pattern):
                    if name.type == "identifier":
                        scope.add(node_text(name, file.source), None)
        # Assignment can invalidate a visible function/import binding. Mark
        # this lexical region conservatively instead of choosing by name.
        if node.type == "assignment_expression" and node.named_children:
            left = node_text(node.named_children[0], file.source).strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", left):
                scope.add(left, None)
        for child in node.named_children:
            self._visit(child, scope, file, graph_scopes)

    def _imports(self, file: TreeFile) -> list[Import]:
        result = []
        for node, _ in walk_with_ancestors(file.tree.root_node):
            if node.type != "import_specification":
                continue
            strings = [n for n, _ in walk_with_ancestors(node) if n.type == "string_literal"]
            uri = node_text(strings[0], file.source).strip("'\"") if strings else ""
            path = local_uri(file.relative_path, uri)
            # Conditional or deferred imports cannot select a known runtime
            # library solely from this declaration.
            text = node_text(node, file.source)
            available = len(strings) == 1 and not re.search(r"\bdeferred\b", text)
            direct_names = [n for n in node.named_children if n.type == "identifier"]
            prefix = node_text(direct_names[0], file.source) if direct_names else None
            shown, hidden = None, set()
            for child in node.named_children:
                if child.type != "combinator":
                    continue
                names = {node_text(n, file.source) for n in child.named_children if n.type == "identifier"}
                if node_text(child, file.source).lstrip().startswith("show"):
                    shown = names if shown is None else shown & names
                else:
                    hidden.update(names)
            result.append(Import(path if available and path in self.modules else None, prefix, shown, hidden,
                                 {"file": file.relative_path, "span": span_for_tree(node), "uri": uri}))
        return result

    def _visible(self, scope: Scope, name: str) -> tuple[bool, str | None]:
        while scope:
            if name in scope.bindings:
                values = scope.bindings[name]
                return True, next(iter(values)) if len(values) == 1 else None
            definition = self.nodes[scope.graph_id]
            if definition.get("extensions", {}).get("has_inheritance"):
                # An inherited member can shadow a library-level name.
                return True, None
            scope = scope.parent
        return False, None

    def _imported(self, file: str, name: str, prefix: str | None) -> tuple[str | None, list[dict]]:
        targets: set[str | None] = set()
        evidence = []
        for item in self.imports.get(file, []):
            if item.prefix != prefix or not item.allows(name):
                continue
            if item.module is None or item.module in self.opaque_modules:
                targets.add(None)
            else:
                # Export chains and part libraries are intentionally not
                # guessed. Only declarations of the imported file are used.
                values = self.modules[item.module].bindings.get(name, set())
                targets.update(values)
            evidence.append(item.evidence)
        return (next(iter(targets)) if len(targets) == 1 else None), evidence

    def resolve(self, file: TreeFile, node: Any, parts: list[str] | None, *, types_only: bool = False) -> tuple[str | None, dict]:
        if not parts:
            return None, {"resolution_basis": "unsupported_receiver"}
        scope = self.node_scopes[node.id]
        evidence: list[dict] = []
        basis = "dart_lexical_scope"
        if parts[0] == "this":
            owner = scope
            while owner and self.nodes[owner.graph_id]["kind"] not in {"class", "type"}:
                owner = owner.parent
            if owner is None or len(parts) != 2 or self.nodes[scope.graph_id].get("extensions", {}).get("static"):
                return None, {"resolution_basis": "unknown_receiver"}
            values = owner.bindings.get(parts[1], set())
            target = next(iter(values)) if len(values) == 1 else None
            basis = "dart_this_member"
        else:
            found, target = self._visible(scope, parts[0])
            if not found:
                if len(parts) == 1:
                    target, evidence = self._imported(file.relative_path, parts[0], None)
                else:
                    target, evidence = self._imported(file.relative_path, parts[1], parts[0])
                    if target and len(parts) == 2:
                        parts = [parts[1]]
                    elif target:
                        parts = parts[1:]
                basis = "dart_explicit_import"
            if target and len(parts) > 1:
                owner = self.nodes[target]
                if len(parts) != 2 or owner["kind"] not in {"class", "type"}:
                    target = None
                else:
                    values = self.definitions[target].bindings.get(parts[1], set())
                    target = next(iter(values)) if len(values) == 1 else None
                    if target and not any(self.nodes[target].get("extensions", {}).get(k) for k in ("static", "constructor")):
                        target = None
                basis = "dart_static_member"
        if target:
            definition = self.nodes[target]
            extension = definition.get("extensions", {})
            if types_only and definition["kind"] not in {"class", "type"}:
                target = None
            elif definition["file"] != file.relative_path and extension.get("member_name", "").startswith("_"):
                target = None
            elif extension.get("accessor"):
                target = None
            elif (definition["kind"] == "method" and not extension.get("static")
                  and self.nodes[scope.graph_id].get("extensions", {}).get("static")):
                target = None
            else:
                evidence.append({"file": definition["file"], "span": definition["span"], "node_id": target})
        return target, {"resolution_basis": basis if target else "unproven_binding", "resolution_evidence": evidence}


def call_parts(node: Any, source: bytes) -> tuple[list[str] | None, str]:
    """Read the callee's AST siblings, never a regex over preceding source."""
    if node.parent.type != "selector":
        return None, node_text(node.parent, source)[:180]
    previous = node.parent.prev_named_sibling
    names: list[str] = []
    while previous is not None and previous.type in {"selector", "comment"}:
        if previous.type == "comment":
            previous = previous.prev_named_sibling
            continue
        children = previous.named_children
        if len(children) == 1 and children[0].type in {"unconditional_assignable_selector", "conditional_assignable_selector"}:
            identifiers = [c for c in children[0].named_children if c.type == "identifier"]
            if len(identifiers) != 1:
                return None, node_text(previous, source) + "(...)"
            names.insert(0, node_text(identifiers[0], source))
        elif len(children) != 1 or children[0].type != "type_arguments":
            return None, ".".join(names) + "(...)"
        previous = previous.prev_named_sibling
    if previous is None or previous.type not in {"identifier", "this"}:
        return None, ".".join(names) + "(...)"
    names.insert(0, node_text(previous, source))
    return names, ".".join(names) + "(...)"
