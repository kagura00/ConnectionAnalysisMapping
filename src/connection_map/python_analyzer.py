"""Dependency-free Python 3 AST analyzer for Analyzer Contract v1."""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import platform
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AnalysisConfig, discover_python_files, repository_id
from .contract import validate_document
from .model import GraphBuilder, span_for

ANALYZER_NAME = "connection-map-python-ast"
ANALYZER_VERSION = "0.1.0"
_PYTHON_BUILTINS = frozenset(name for name in dir(builtins) if not name.startswith("_"))


@dataclass(slots=True)
class ModuleInfo:
    path: Path
    relative_path: str
    module_name: str
    source: str
    tree: ast.Module
    node_id: str


@dataclass(slots=True)
class ImportBinding:
    module_name: str
    member: str | None
    local_name: str
    span: dict[str, int] | None


@dataclass(slots=True)
class DefinitionInfo:
    node_id: str
    ast_node: ast.AST
    kind: str
    name: str
    qualified_name: str
    module: ModuleInfo
    parent_id: str
    scope_id: str
    class_context: str | None


class PythonAnalyzer:
    """Analyze a repository without importing or executing repository code."""

    def __init__(
        self,
        root: Path,
        config: AnalysisConfig | None = None,
        *,
        deterministic: bool = False,
        commit_sha: str | None = None,
    ) -> None:
        self.root = root.resolve()
        self.config = config or AnalysisConfig()
        self.config.validate()
        self.deterministic = deterministic
        self.commit_sha = commit_sha if commit_sha is not None else _git_commit(self.root)
        self.builder = GraphBuilder()
        self.modules: list[ModuleInfo] = []
        self.module_by_name: dict[str, ModuleInfo] = {}
        self.module_node_by_id: dict[str, ModuleInfo] = {}
        self.definitions: dict[str, DefinitionInfo] = {}
        self.definition_by_ast: dict[int, str] = {}
        # A local binding can be deliberately unknown (for example a function
        # argument or an assignment).  Keeping that name in the scope with a
        # None value is important: resolution must stop at the local scope
        # instead of incorrectly falling through to a module-level symbol.
        self.scope_symbols: dict[str, dict[str, str | None]] = {}
        self.scope_imports: dict[str, dict[str, ImportBinding]] = {}
        self.scope_parent: dict[str, str | None] = {}
        self.scope_kind: dict[str, str] = {}
        self.scope_qualname: dict[str, str] = {}
        self.scope_class_context: dict[str, str | None] = {}
        self.node_kind: dict[str, str] = {}
        self.node_name: dict[str, str] = {}
        self.node_module: dict[str, ModuleInfo] = {}
        self.return_annotations: dict[str, ast.AST] = {}
        self.resolution_evidence: dict[int, dict[str, Any]] = {}
        self._collision_counts: dict[str, int] = {}

    def analyze(self) -> dict[str, Any]:
        files, skipped = discover_python_files(self.root, self.config)
        for relative_path, reason in skipped:
            self._add_skip_diagnostic(relative_path, reason)

        # Parse every file before resolving any symbol. This makes resolution
        # independent of filesystem traversal order.
        for path in files:
            self._parse_module(path)

        for module in self.modules:
            DefinitionCollector(self, module).collect()
        for module in self.modules:
            RelationCollector(self, module).collect()

        meta = {
            "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
            "language": "python",
            "languages": ["python"],
            "target": {
                "repository_id": repository_id(self.root),
                "relative_root": ".",
                "commit_sha": self.commit_sha,
            },
            "runtime": {
                "python_version": platform.python_version(),
                "ast_version": f"python-{sys.version_info.major}.{sys.version_info.minor}",
            },
            "generated_at": None if self.deterministic else _utc_now(),
            "deterministic": self.deterministic,
            "settings": self.config.to_dict(),
        }
        document = self.builder.document(meta)
        validate_document(document)
        return document

    def _parse_module(self, path: Path) -> None:
        relative_path = path.relative_to(self.root).as_posix()
        try:
            with tokenize.open(path) as handle:
                source = handle.read()
            tree = ast.parse(source, filename=relative_path, type_comments=True)
        except SyntaxError as exc:
            self.builder.add_diagnostic(
                {
                    "code": "parse_error",
                    "severity": "error",
                    "message": str(exc),
                    "file": relative_path,
                    "span": _syntax_error_span(source if "source" in locals() else "", exc),
                    "details": {"text": getattr(exc, "text", None)},
                }
            )
            return
        except (OSError, UnicodeError) as exc:
            self.builder.add_diagnostic(
                {
                    "code": "read_error",
                    "severity": "error",
                    "message": str(exc),
                    "file": relative_path,
                    "span": None,
                }
            )
            return

        module_name = module_name_for_path(relative_path)
        node_id = f"python:{relative_path}:module"
        lines = source.splitlines() or [""]
        module_span = {
            "start_line": 1,
            "start_col": 0,
            "end_line": len(lines),
            "end_col": len(lines[-1].encode("utf-8")),
        }
        node = {
            "id": node_id,
            "kind": "module",
            "qualified_name": module_name,
            "display_name": module_name,
            "file": relative_path,
            "span": module_span,
            "parent_id": None,
            "visibility": "public",
            "extensions": {"language": "python"},
        }
        self.builder.add_node(node)
        module = ModuleInfo(path, relative_path, module_name, source, tree, node_id)
        self.modules.append(module)
        self.module_by_name[module_name] = module
        self.module_node_by_id[node_id] = module
        self.node_kind[node_id] = "module"
        self.node_name[node_id] = module_name
        self.node_module[node_id] = module
        self.scope_symbols[node_id] = {}
        self.scope_imports[node_id] = {}
        self.scope_parent[node_id] = None
        self.scope_kind[node_id] = "module"
        self.scope_qualname[node_id] = ""
        self.scope_class_context[node_id] = None

    def _add_definition(
        self,
        module: ModuleInfo,
        node: ast.AST,
        *,
        name: str,
        kind: str,
        parent_id: str,
        scope_id: str,
        qualified_name: str,
    ) -> str:
        base_id = f"python:{module.relative_path}:{qualified_name}:{kind}"
        node_id = base_id
        collision = self._collision_counts.get(base_id, 0)
        while node_id in self.builder.nodes:
            collision += 1
            node_id = f"{base_id}~{collision}"
        self._collision_counts[base_id] = collision
        class_context = self.scope_class_context.get(scope_id)
        item: dict[str, Any] = {
            "id": node_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "display_name": name,
            "file": module.relative_path,
            "span": span_for(node),
            "parent_id": parent_id,
            "visibility": "private" if name.startswith("_") else "public",
            "extensions": {"language": "python"},
        }
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            item["signature"] = _signature_for(node)
            return_behavior, return_sites, execution_kind = _return_info(node)
            item["return_behavior"] = return_behavior
            item["return_sites"] = return_sites
            item["execution_kind"] = execution_kind
            if node.returns is not None:
                self.return_annotations[node_id] = node.returns
        elif kind == "lambda":
            item["execution_kind"] = "sync"
        self.builder.add_node(item)
        definition = DefinitionInfo(
            node_id=node_id,
            ast_node=node,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            module=module,
            parent_id=parent_id,
            scope_id=scope_id,
            class_context=class_context,
        )
        self.definitions[node_id] = definition
        self.definition_by_ast[id(node)] = node_id
        self.node_kind[node_id] = kind
        self.node_name[node_id] = name
        self.node_module[node_id] = module
        self.scope_symbols.setdefault(parent_id, {})[name] = node_id
        self.scope_imports.setdefault(node_id, {})
        self.scope_symbols.setdefault(node_id, {})
        self.scope_parent[node_id] = parent_id
        self.scope_kind[node_id] = kind
        self.scope_qualname[node_id] = qualified_name
        self.scope_class_context[node_id] = node_id if kind == "class" else class_context
        self._add_relation(
            parent_id,
            node_id,
            "contains",
            resolution_status="resolved",
            confidence=1.0,
            source_span=span_for(node),
            detail={"name": name},
        )
        return node_id

    def _add_external_node(self, label: str, *, unknown: bool = False, module: ModuleInfo | None = None, span: dict[str, int] | None = None) -> str:
        safe_label = label.replace("\\", "/")
        digest = hashlib.sha1(safe_label.encode("utf-8")).hexdigest()[:16]
        kind = "unknown" if unknown else "external"
        node_id = f"python:{kind}:{digest}"
        if node_id not in self.builder.nodes:
            self.builder.add_node(
                {
                    "id": node_id,
                    "kind": kind,
                    "qualified_name": safe_label or "<unknown>",
                    "display_name": safe_label or "<unknown>",
                    "file": module.relative_path if unknown and module else None,
                    "span": span if unknown else None,
                    "parent_id": None,
                    "visibility": "unknown",
                    "extensions": (
                        {"language": "python", "external_label": safe_label}
                        if not unknown
                        else {"language": "python", "unresolved": True}
                    ),
                }
            )
            self.node_kind[node_id] = kind
            self.node_name[node_id] = safe_label
            if module is not None:
                self.node_module[node_id] = module
        return node_id

    def _add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        *,
        resolution_status: str,
        confidence: float,
        source_span: dict[str, int] | None,
        detail: dict[str, Any],
    ) -> str:
        identity = {
            "source_id": source_id,
            "target_id": target_id,
            "relation_type": relation_type,
            "source_span": source_span,
            "detail": detail,
        }
        digest = hashlib.sha1(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
        edge_id = f"edge:{digest}"
        edge = {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation_type": relation_type,
            "resolution_status": resolution_status,
            "provenance": "ast",
            "confidence": confidence,
            "source_span": source_span,
            "detail": detail,
        }
        self.builder.add_edge(edge)
        return edge_id

    def _add_diagnostic(
        self,
        code: str,
        severity: str,
        message: str,
        *,
        module: ModuleInfo | None = None,
        file: str | None = None,
        node_id: str | None = None,
        span: dict[str, int] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.builder.add_diagnostic(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "file": file if file is not None else (module.relative_path if module else None),
                "span": span,
                "node_id": node_id,
                "details": details or {},
            }
        )

    def _add_skip_diagnostic(self, relative_path: str, reason: str) -> None:
        code = "generated_file" if reason == "generated" else "excluded_file"
        self._add_diagnostic(
            code,
            "info",
            f"Skipped Python file: {relative_path} ({reason})",
            file=relative_path,
            details={"reason": reason},
        )

    def resolve_name(self, name: str, scope_id: str) -> str | None:
        current: str | None = scope_id
        while current is not None:
            symbols = self.scope_symbols.get(current, {})
            if name in symbols:
                return symbols[name]
            binding = self.scope_imports.get(current, {}).get(name)
            if binding is not None:
                return self.resolve_binding(binding, current)
            current = self.scope_parent.get(current)
        if name in _PYTHON_BUILTINS:
            return self._add_external_node(f"builtin:{name}")
        return None

    def resolve_binding(self, binding: ImportBinding, scope_id: str) -> str | None:
        if binding.member is None:
            module = self.module_by_name.get(binding.module_name)
            if module is not None:
                return module.node_id
            first = binding.module_name.split(".", 1)[0]
            module = self.module_by_name.get(first)
            if module is not None:
                return module.node_id
            return self._add_external_node(f"module:{binding.module_name}")
        module = self.module_by_name.get(binding.module_name)
        if module is not None:
            symbol_id = self.scope_symbols.get(module.node_id, {}).get(binding.member)
            if symbol_id is not None:
                return symbol_id
            submodule = self.module_by_name.get(f"{binding.module_name}.{binding.member}")
            if submodule is not None:
                return submodule.node_id
        return self._add_external_node(f"symbol:{binding.module_name}.{binding.member}")

    def resolve_annotation(self, annotation: ast.AST, scope_id: str) -> str | None:
        """Resolve the deliberately small subset of annotations used as hints.

        String annotations are parsed as expressions so ``from __future__ import
        annotations`` remains usable without importing or evaluating anything.
        Complex annotations (for example ``Base | None`` or ``list[Base]``)
        intentionally remain unresolved until a later analyzer version defines
        their semantics.
        """
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(annotation, ast.Name | ast.Attribute):
            return self.resolve_expr(annotation, scope_id)
        return None

    def resolve_call_result(self, expression: ast.Call, scope_id: str) -> str | None:
        """Resolve a call result to an in-repository class when statically known."""
        target = self.resolve_expr(expression.func, scope_id)
        if target is None:
            return None
        if self.node_kind.get(target) == "class":
            self.resolution_evidence[id(expression)] = {
                "strategy": "class_constructor",
                "call_target_id": target,
                "call_target": self.builder.nodes.get(target, {}).get("qualified_name"),
                "inferred_class_id": target,
                "inferred_class": self.builder.nodes.get(target, {}).get("qualified_name"),
            }
            return target
        definition = self.definitions.get(target)
        if definition is None:
            return None
        annotation = self.return_annotations.get(target)
        if annotation is None:
            return None
        resolved = self.resolve_annotation(annotation, definition.scope_id)
        if self.node_kind.get(resolved) != "class":
            return None
        self.resolution_evidence[id(expression)] = {
            "strategy": "return_annotation",
            "call_target_id": target,
            "call_target": self.builder.nodes.get(target, {}).get("qualified_name"),
            "return_annotation": _annotation_text(annotation),
            "inferred_class_id": resolved,
            "inferred_class": self.builder.nodes.get(resolved, {}).get("qualified_name"),
        }
        return resolved

    def consume_resolution_evidence(self, expression: ast.AST) -> dict[str, Any] | None:
        return self.resolution_evidence.pop(id(expression), None)

    def resolve_expr(self, expression: ast.AST, scope_id: str) -> str | None:
        if isinstance(expression, ast.Name):
            return self.resolve_name(expression.id, scope_id)
        if isinstance(expression, ast.Attribute):
            base = self.resolve_expr(expression.value, scope_id)
            if base is None and isinstance(expression.value, ast.Name) and expression.value.id in {"self", "cls"}:
                base = self.scope_class_context.get(scope_id)
            if base is None and isinstance(expression.value, ast.Call):
                base = self.resolve_call_result(expression.value, scope_id)
                if base is not None:
                    call_evidence = self.resolution_evidence.get(id(expression.value))
                    if call_evidence is not None:
                        self.resolution_evidence[id(expression)] = {
                            **call_evidence,
                            "attribute": expression.attr,
                            "inferred_class_id": base,
                            "inferred_class": self.builder.nodes.get(base, {}).get("qualified_name"),
                        }
            if base is None:
                return None
            if self.node_kind.get(base) == "module":
                module = self.module_node_by_id.get(base)
                if module is not None:
                    submodule = self.module_by_name.get(f"{module.module_name}.{expression.attr}")
                    if submodule is not None:
                        return submodule.node_id
                    return self.scope_symbols.get(base, {}).get(expression.attr)
            if self.node_kind.get(base) == "class":
                return self.scope_symbols.get(base, {}).get(expression.attr)
            if self.node_kind.get(base) == "external":
                base_node = self.builder.nodes.get(base, {})
                base_name = base_node.get("qualified_name") or base
                return self._add_external_node(f"{base_name}.{expression.attr}")
        return None


class DefinitionCollector(ast.NodeVisitor):
    def __init__(self, analyzer: PythonAnalyzer, module: ModuleInfo) -> None:
        self.analyzer = analyzer
        self.module = module
        self.current_scope = module.node_id

    def collect(self) -> None:
        self.visit(self.module.tree)

    def visit_Module(self, node: ast.Module) -> None:
        for statement in node.body:
            self.visit(statement)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent = self.current_scope
        qualified_name = _child_qualname(self.analyzer, parent, node.name)
        class_id = self.analyzer._add_definition(
            self.module,
            node,
            name=node.name,
            kind="class",
            parent_id=parent,
            scope_id=parent,
            qualified_name=qualified_name,
        )
        self.current_scope = class_id
        for statement in node.body:
            self.visit(statement)
        self.current_scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self.current_scope
        kind = "method" if self.analyzer.scope_kind.get(parent) == "class" else "function"
        qualified_name = _child_qualname(self.analyzer, parent, node.name)
        function_id = self.analyzer._add_definition(
            self.module,
            node,
            name=node.name,
            kind=kind,
            parent_id=parent,
            scope_id=parent,
            qualified_name=qualified_name,
        )
        _register_local_bindings(self.analyzer, node, function_id)
        self._visit_outside_body(node)
        previous = self.current_scope
        self.current_scope = function_id
        for statement in node.body:
            self.visit(statement)
        self.current_scope = previous

    def visit_Lambda(self, node: ast.Lambda) -> None:
        parent = self.current_scope
        label = f"lambda@{getattr(node, 'lineno', 0)}:{getattr(node, 'col_offset', 0)}"
        qualified_name = _child_qualname(self.analyzer, parent, label)
        lambda_id = self.analyzer._add_definition(
            self.module,
            node,
            name="<lambda>",
            kind="lambda",
            parent_id=parent,
            scope_id=parent,
            qualified_name=qualified_name,
        )
        previous = self.current_scope
        self.current_scope = lambda_id
        self.generic_visit(node.body)
        self.current_scope = previous

    def _visit_outside_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".", 1)[0]
            self.analyzer.scope_imports[self.current_scope][local_name] = ImportBinding(
                module_name=alias.name,
                member=None,
                local_name=local_name,
                span=span_for(node),
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module_name = resolve_import_module(self.module, node.level, node.module)
        for alias in node.names:
            local_name = alias.asname or alias.name
            self.analyzer.scope_imports[self.current_scope][local_name] = ImportBinding(
                module_name=module_name,
                member=alias.name,
                local_name=local_name,
                span=span_for(node),
            )


class _LocalBindingCollector(ast.NodeVisitor):
    """Record function-local names without evaluating their values.

    Python determines whether a name is local for the whole function from its
    syntax, even when an assignment appears after a call site.  We therefore
    collect stores before relation resolution and mark them as unknown.  A
    conservative unresolved edge is preferable to a false edge to an
    unrelated module-level function.
    """

    def __init__(self, analyzer: PythonAnalyzer, scope_id: str) -> None:
        self.analyzer = analyzer
        self.scope_id = scope_id

    def bind(self, name: str) -> None:
        if not name:
            return
        symbols = self.analyzer.scope_symbols.setdefault(self.scope_id, {})
        if name not in symbols:
            symbols[name] = None

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.bind(node.id)

    def visit_arg(self, node: ast.arg) -> None:
        self.bind(node.arg)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # The definition name itself is registered by DefinitionCollector;
        # the nested body is a different lexical scope.
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _register_local_bindings(
    analyzer: PythonAnalyzer,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope_id: str,
) -> None:
    collector = _LocalBindingCollector(analyzer, scope_id)
    arguments = node.args
    for argument in (*getattr(arguments, "posonlyargs", []), *arguments.args, *arguments.kwonlyargs):
        collector.bind(argument.arg)
    if arguments.vararg is not None:
        collector.bind(arguments.vararg.arg)
    if arguments.kwarg is not None:
        collector.bind(arguments.kwarg.arg)
    for statement in node.body:
        collector.visit(statement)


class RelationCollector(ast.NodeVisitor):
    def __init__(self, analyzer: PythonAnalyzer, module: ModuleInfo) -> None:
        self.analyzer = analyzer
        self.module = module
        self.current_scope = module.node_id

    def collect(self) -> None:
        self.visit(self.module.tree)

    def visit_Module(self, node: ast.Module) -> None:
        for statement in node.body:
            self.visit(statement)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        class_id = self.analyzer.definition_by_ast.get(id(node), self.current_scope)
        parent_scope = self.current_scope
        for base in node.bases:
            target = self.analyzer.resolve_expr(base, parent_scope)
            status = "resolved"
            confidence = 1.0
            if target is None:
                label = _expression_text(base)
                target = self.analyzer._add_external_node(
                    _site_label("unresolved", self.module, base, label),
                    unknown=True,
                    module=self.module,
                    span=span_for(base),
                )
                status = "unresolved"
                confidence = 0.2
                self.analyzer._add_diagnostic(
                    "unresolved_inheritance",
                    "warning",
                    f"Could not resolve base class {_expression_text(base)}",
                    module=self.module,
                    node_id=class_id,
                    span=span_for(base),
                )
            else:
                status = _resolution_status(self.analyzer.node_kind.get(target))
                confidence = _confidence(status)
            self.analyzer._add_relation(
                class_id,
                target,
                "inherits",
                resolution_status=status,
                confidence=confidence,
                source_span=span_for(base),
                detail={"expression": _expression_text(base)},
            )
        for decorator in node.decorator_list:
            self.visit(decorator)
        for keyword in node.keywords:
            self.visit(keyword.value)
        previous = self.current_scope
        self.current_scope = class_id
        for statement in node.body:
            self.visit(statement)
        self.current_scope = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        function_id = self.analyzer.definition_by_ast.get(id(node), self.current_scope)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        previous = self.current_scope
        self.current_scope = function_id
        for statement in node.body:
            self.visit(statement)
        self.current_scope = previous

    def visit_Lambda(self, node: ast.Lambda) -> None:
        lambda_id = self.analyzer.definition_by_ast.get(id(node), self.current_scope)
        previous = self.current_scope
        self.current_scope = lambda_id
        self.visit(node.body)
        self.current_scope = previous

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".", 1)[0]
            binding = self.analyzer.scope_imports.get(self.current_scope, {}).get(local_name)
            target = self.analyzer.resolve_binding(binding, self.current_scope) if binding else None
            status = _resolution_status(self.analyzer.node_kind.get(target)) if target else "unresolved"
            if target is None:
                target = self.analyzer._add_external_node(f"module:{alias.name}")
                status = "external"
            self.analyzer._add_relation(
                self.current_scope,
                target,
                "imports",
                resolution_status=status,
                confidence=_confidence(status),
                source_span=span_for(node),
                detail={"expression": f"import {alias.name}", "local_name": local_name},
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module_name = resolve_import_module(self.module, node.level, node.module)
        for alias in node.names:
            local_name = alias.asname or alias.name
            binding = self.analyzer.scope_imports.get(self.current_scope, {}).get(local_name)
            target = self.analyzer.resolve_binding(binding, self.current_scope) if binding else None
            status = _resolution_status(self.analyzer.node_kind.get(target)) if target else "unresolved"
            if target is None:
                target = self.analyzer._add_external_node(f"symbol:{module_name}.{alias.name}")
                status = "external"
            self.analyzer._add_relation(
                self.current_scope,
                target,
                "imports",
                resolution_status=status,
                confidence=_confidence(status),
                source_span=span_for(node),
                detail={
                    "expression": f"from {module_name} import {alias.name}",
                    "local_name": local_name,
                },
            )

    def visit_Call(self, node: ast.Call) -> None:
        dynamic_kind = dynamic_import_kind(node)
        expression = _expression_text(node)
        if dynamic_kind is not None:
            target_name = static_import_name(node)
            if target_name:
                target = self.analyzer.module_by_name.get(target_name)
                if target is not None:
                    target_id = target.node_id
                    status = "resolved"
                else:
                    target_id = self.analyzer._add_external_node(f"module:{target_name}")
                    status = "external"
                confidence = _confidence(status)
            else:
                target_id = self.analyzer._add_external_node(
                    _site_label("dynamic", self.module, node, expression),
                    unknown=True,
                    module=self.module,
                    span=span_for(node),
                )
                status = "unresolved"
                confidence = 0.2
            edge_id = self.analyzer._add_relation(
                self.current_scope,
                target_id,
                "dynamic_imports",
                resolution_status=status,
                confidence=confidence,
                source_span=span_for(node),
                detail={
                    "expression": expression,
                    "call_kind": "dynamic",
                    "import_name": target_name,
                    "return_behavior": None,
                },
            )
            self.analyzer._add_diagnostic(
                "dynamic_import",
                "info" if target_name else "warning",
                f"Dynamic import detected: {expression}",
                module=self.module,
                node_id=self.current_scope,
                span=span_for(node),
                details={"edge_id": edge_id, "import_name": target_name},
            )
        else:
            target = self.analyzer.resolve_expr(node.func, self.current_scope)
            if target is None:
                target = self.analyzer._add_external_node(
                    _site_label("call", self.module, node.func, _expression_text(node.func)),
                    unknown=True,
                    module=self.module,
                    span=span_for(node.func),
                )
                status = "unresolved"
                confidence = 0.2
                self.analyzer._add_diagnostic(
                    "unresolved_call",
                    "warning",
                    f"Could not resolve call target {_expression_text(node.func)}",
                    module=self.module,
                    node_id=self.current_scope,
                    span=span_for(node.func),
                    details={"expression": expression},
                )
            else:
                status = _resolution_status(self.analyzer.node_kind.get(target))
                confidence = _confidence(status)
            target_node = self.analyzer.builder.nodes.get(target, {})
            detail = {
                "expression": expression,
                "call_kind": "attribute" if isinstance(node.func, ast.Attribute) else "direct",
                "return_behavior": target_node.get("return_behavior"),
            }
            evidence = self.analyzer.consume_resolution_evidence(node.func)
            if evidence is not None:
                detail["resolution_evidence"] = evidence
            self.analyzer._add_relation(
                self.current_scope,
                target,
                "calls",
                resolution_status=status,
                confidence=confidence,
                source_span=span_for(node),
                detail=detail,
            )
        self.generic_visit(node)


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    return PythonAnalyzer(root, config, deterministic=deterministic, commit_sha=commit_sha).analyze()


def module_name_for_path(relative_path: str) -> str:
    parts = relative_path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else "__root__"


def resolve_import_module(module: ModuleInfo, level: int, imported_module: str | None) -> str:
    if level == 0:
        return imported_module or ""
    package_parts = module.module_name.split(".")
    if module.relative_path.endswith("/__init__.py") or module.relative_path == "__init__.py":
        current_package = package_parts
    else:
        current_package = package_parts[:-1]
    keep = max(len(current_package) - (level - 1), 0)
    base = current_package[:keep]
    if imported_module:
        base.extend(imported_module.split("."))
    return ".".join(part for part in base if part)


def _child_qualname(analyzer: PythonAnalyzer, parent_id: str, name: str) -> str:
    parent = analyzer.scope_qualname.get(parent_id, "")
    if not parent:
        return name
    if analyzer.scope_kind.get(parent_id) in {"function", "method", "lambda"}:
        return f"{parent}.<locals>.{name}"
    return f"{parent}.{name}"


def _signature_for(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    try:
        arguments = ast.unparse(node.args)
        result = f"{prefix}{node.name}({arguments})"
        if node.returns is not None:
            result += f" -> {ast.unparse(node.returns)}"
        return result
    except Exception:  # pragma: no cover - defensive fallback for future AST changes
        return f"{prefix}{node.name}(…)"


def _return_info(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, list[dict[str, Any]], str]:
    class ReturnVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.returns: list[ast.Return] = []
            self.has_yield = False

        def visit_Return(self, item: ast.Return) -> None:
            self.returns.append(item)

        def visit_Yield(self, item: ast.Yield) -> None:
            self.has_yield = True
            self.generic_visit(item)

        def visit_YieldFrom(self, item: ast.YieldFrom) -> None:
            self.has_yield = True
            self.generic_visit(item)

        def visit_FunctionDef(self, item: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, item: ast.AsyncFunctionDef) -> None:
            return

        def visit_Lambda(self, item: ast.Lambda) -> None:
            return

        def visit_ClassDef(self, item: ast.ClassDef) -> None:
            return

    visitor = ReturnVisitor()
    # Only the function's own body contributes return behavior. Nested
    # definitions have independent return contracts and are skipped by the
    # visitor methods above.
    for statement in node.body:
        visitor.visit(statement)
    sites: list[dict[str, Any]] = []
    kinds: list[str] = []
    for item in visitor.returns:
        is_none = item.value is None or (isinstance(item.value, ast.Constant) and item.value.value is None)
        value_kind = "none" if is_none else "value"
        kinds.append(value_kind)
        sites.append({"span": span_for(item), "value_kind": value_kind})
    if not kinds:
        behavior = "no_explicit_return"
    elif all(kind == "none" for kind in kinds):
        behavior = "returns_none"
    elif all(kind == "value" for kind in kinds):
        behavior = "returns_value"
    else:
        behavior = "mixed"
    is_async = isinstance(node, ast.AsyncFunctionDef)
    if visitor.has_yield and is_async:
        execution_kind = "async_generator"
    elif visitor.has_yield:
        execution_kind = "generator"
    elif is_async:
        execution_kind = "async"
    else:
        execution_kind = "sync"
    return behavior, sites, execution_kind


def _expression_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover
        return type(node).__name__


def _annotation_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return _expression_text(node)


def _site_label(prefix: str, module: ModuleInfo, node: ast.AST, expression: str) -> str:
    span = span_for(node) or {"start_line": 0, "start_col": 0}
    return f"{prefix}:{module.relative_path}:{span['start_line']}:{span['start_col']}:{expression}"


def dynamic_import_kind(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}:
        return function.id
    if isinstance(function, ast.Attribute) and function.attr in {"import_module", "__import__"}:
        return function.attr
    return None


def static_import_name(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _resolution_status(kind: str | None) -> str:
    if kind in {"module", "class", "function", "method", "lambda"}:
        return "resolved"
    if kind == "external":
        return "external"
    return "unresolved"


def _confidence(status: str) -> float:
    return {"resolved": 1.0, "external": 0.7, "unresolved": 0.2, "unsupported": 0.1}.get(status, 0.2)


def _syntax_error_span(source: str, error: SyntaxError) -> dict[str, int] | None:
    if error.lineno is None:
        return None
    line_number = max(int(error.lineno), 1)
    line = source.splitlines()[line_number - 1] if source.splitlines() and line_number <= len(source.splitlines()) else ""
    column = max(int(error.offset or 1) - 1, 0)
    byte_column = len(line[:column].encode("utf-8"))
    return {
        "start_line": line_number,
        "start_col": byte_column,
        "end_line": line_number,
        "end_col": byte_column + 1,
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None
