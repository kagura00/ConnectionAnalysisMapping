"""Static Bash/POSIX Shell relationship analyzer."""

from __future__ import annotations

import re
from itertools import pairwise
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

ANALYZER_NAME = "connection-map-shell-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"bash", "posix-shell", "shell"}


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    active_config = config or AnalysisConfig(language="bash")
    active_config.validate()
    if active_config.language not in SUPPORTED_LANGUAGES:
        raise ValueError("Shell analyzer requires language = 'bash', 'posix-shell', or 'shell'")
    selected = active_config.active_languages()
    if active_config.language == "shell":
        # The preset represents both concrete grammars.  Run both child
        # analyzers so selecting the preset does not silently discard the
        # POSIX-shell result when Bash appears first in the preset order.
        from .mixed_analyzer import analyze_repository as analyze_mixed

        mixed_config = AnalysisConfig(
            language="mixed",
            languages=list(selected),
            include=list(active_config.include or []),
            exclude=list(active_config.exclude),
            include_tests=active_config.include_tests,
            follow_symlinks=active_config.follow_symlinks,
            max_file_bytes=active_config.max_file_bytes,
            test_patterns=list(active_config.test_patterns),
            generated=list(active_config.generated),
            context=dict(active_config.context),
        )
        return analyze_mixed(root, mixed_config, deterministic=deterministic, commit_sha=commit_sha)
    concrete = next((item for item in selected if item in {"bash", "posix-shell"}), "bash")
    files, skipped = load_tree_files(
        root.resolve(), active_config, language=concrete, grammar="bash", extra="shell"
    )
    builder = GraphBuilder()
    for tree_file in files:
        add_module(builder, tree_file, language=concrete, grammar="bash")
    add_skipped_diagnostics(builder, skipped)

    definitions_by_id: dict[int, str] = {}
    functions_by_name: dict[str, str] = {}
    external_cache: dict[str, str] = {}
    modules_by_path = {item.relative_path: item.module_id for item in files}

    for tree_file in files:
        _collect_functions(tree_file, builder, definitions_by_id, functions_by_name, concrete)
    for tree_file in files:
        _collect_commands(
            tree_file,
            builder,
            definitions_by_id,
            functions_by_name,
            modules_by_path,
            external_cache,
            concrete,
            root.resolve(),
        )
        _collect_variables(tree_file, builder, definitions_by_id, external_cache, concrete)
        _collect_parse_diagnostics(tree_file, builder)

    document = finish_document(
        builder,
        root=root.resolve(),
        language=active_config.language,
        languages=list(selected),
        analyzer_name=ANALYZER_NAME,
        analyzer_version=ANALYZER_VERSION,
        config=active_config,
        deterministic=deterministic,
        commit_sha=commit_sha,
        grammar="bash",
        extensions={"shell_flavor": concrete},
    )
    validate_document(document)
    return document


def _collect_functions(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scope_ids: dict[int, str],
    functions_by_name: dict[str, str],
    language: str,
) -> None:
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "function_definition":
            continue
        name = _function_name(node, tree_file.source)
        if not name:
            diagnostic(
                builder,
                code="unresolved_definition",
                message="shell function definition has no static name",
                file=tree_file.relative_path,
                span=span_for_tree(node),
            )
            continue
        parent_id = nearest_scope(ancestors, scope_ids, tree_file.module_id)
        qualified = f"{tree_file.relative_path}:{name}"
        node_id = unique_id(builder.nodes, f"{language}:{qualified}:function", node.start_byte)
        body_text = node_text(node, tree_file.source)
        has_return = bool(re.search(r"\breturn(?:\s+[^;}]*)?", body_text))
        builder.add_node(
            {
                "id": node_id,
                "kind": "function",
                "qualified_name": qualified,
                "display_name": name,
                "file": tree_file.relative_path,
                "span": span_for_tree(node),
                "parent_id": parent_id,
                "visibility": "public",
                "signature": node_text(node, tree_file.source).split("{", 1)[0].strip(),
                "return_behavior": "unknown" if has_return else "no_explicit_return",
                "execution_kind": "sync",
                "extensions": {"language": language, "grammar": "bash", "declaration_kind": "function_definition"},
            }
        )
        add_relation(
            builder,
            source_id=parent_id,
            target_id=node_id,
            relation_type="contains",
            source_span=span_for_tree(node),
            detail={"kind": "lexical_definition", "declaration_kind": "function_definition"},
            edge_prefix=language,
        )
        scope_ids[node.id] = node_id
        functions_by_name.setdefault(name.casefold(), node_id)


def _collect_commands(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scope_ids: dict[int, str],
    functions_by_name: dict[str, str],
    modules_by_path: dict[str, str],
    external_cache: dict[str, str],
    language: str,
    root: Path,
) -> None:
    command_ids: dict[int, str] = {}
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "command":
            continue
        command_name = _command_name(node, tree_file.source)
        if not command_name:
            continue
        parent_id = nearest_scope(ancestors, scope_ids, tree_file.module_id)
        command_id = unique_id(
            builder.nodes,
            f"{language}:{tree_file.relative_path}:command:{command_name}:{node.start_point[0] + 1}",
            node.start_byte,
        )
        builder.add_node(
            {
                "id": command_id,
                "kind": "unknown",
                "qualified_name": f"{tree_file.relative_path}:command:{command_name}:{node.start_point[0] + 1}",
                "display_name": command_name,
                "file": tree_file.relative_path,
                "span": span_for_tree(node),
                "parent_id": parent_id,
                "visibility": "unknown",
                "signature": node_text(node, tree_file.source).strip(),
                "extensions": {
                    "language": language,
                    "grammar": "bash",
                    "shell_object_type": "command",
                },
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
            edge_prefix=language,
        )

        name_key = command_name.casefold()
        if name_key in functions_by_name and functions_by_name[name_key] != parent_id:
            add_relation(
                builder,
                source_id=parent_id,
                target_id=functions_by_name[name_key],
                relation_type="calls",
                source_span=span_for_tree(node),
                detail={"expression": node_text(node, tree_file.source).strip(), "call_kind": "direct"},
                edge_prefix=language,
            )
        else:
            target_id = external_node(
                builder,
                external_cache,
                node_id=f"{language}:command:{name_key}",
                qualified_name=f"shell command {command_name}",
                display_name=command_name,
                language=language,
                extensions={"shell_object_type": "command"},
            )
            add_relation(
                builder,
                source_id=command_id,
                target_id=target_id,
                relation_type="uses",
                source_span=span_for_tree(node),
                detail={"command": command_name, "call_kind": "external_command"},
                resolution_status="external",
                confidence=0.9,
                edge_prefix=language,
            )

        if name_key in {"source", "."}:
            argument = _first_argument(node, tree_file.source)
            _add_source_relation(
                builder,
                command_id,
                argument,
                modules_by_path,
                tree_file,
                root,
                language,
                external_cache,
            )

    _collect_pipelines(tree_file, builder, command_ids, language)


def _collect_variables(
    tree_file: TreeFile,
    builder: GraphBuilder,
    scope_ids: dict[int, str],
    external_cache: dict[str, str],
    language: str,
) -> None:
    seen: set[tuple[int, str]] = set()
    for node, ancestors in walk_with_ancestors(tree_file.tree.root_node):
        if node.type not in {"variable_name", "special_variable_name"}:
            continue
        name = node_text(node, tree_file.source).strip().lstrip("$")
        if not name or (node.start_byte, name) in seen:
            continue
        seen.add((node.start_byte, name))
        source_id = nearest_scope(ancestors, scope_ids, tree_file.module_id)
        target_id = external_node(
            builder,
            external_cache,
            node_id=f"{language}:variable:{name.casefold()}",
            qualified_name=f"shell variable {name}",
            display_name=f"${name}",
            language=language,
            extensions={"shell_object_type": "variable", "environment": name.isupper()},
        )
        add_relation(
            builder,
            source_id=source_id,
            target_id=target_id,
            relation_type="uses",
            source_span=span_for_tree(node),
            detail={"variable": name, "environment": name.isupper()},
            resolution_status="external",
            confidence=0.85,
            edge_prefix=language,
        )


def _collect_pipelines(tree_file: TreeFile, builder: GraphBuilder, command_ids: dict[int, str], language: str) -> None:
    for node, _ in walk_with_ancestors(tree_file.tree.root_node):
        if node.type != "pipeline":
            continue
        commands = [child for child in node.children if child.type == "command"]
        for left, right in pairwise(commands):
            if left.id not in command_ids or right.id not in command_ids:
                continue
            add_relation(
                builder,
                source_id=command_ids[left.id],
                target_id=command_ids[right.id],
                relation_type="handles",
                source_span=span_for_tree(left),
                detail={"pipeline": True, "operator": "|"},
                edge_prefix=language,
            )


def _add_source_relation(
    builder: GraphBuilder,
    source_id: str,
    argument: str | None,
    modules_by_path: dict[str, str],
    tree_file: TreeFile,
    root: Path,
    language: str,
    external_cache: dict[str, str],
) -> None:
    if not argument:
        diagnostic(
            builder,
            code="dynamic_import",
            message="shell source path is dynamic or missing",
            file=tree_file.relative_path,
            span=span_for_tree(tree_file.tree.root_node),
        )
        return
    cleaned = argument.strip("'\"")
    candidate = (Path(tree_file.relative_path).parent / cleaned).as_posix()
    candidates = [candidate]
    if not Path(candidate).suffix:
        candidates.append(f"{candidate}.sh")
    target_id = next((modules_by_path[item] for item in candidates if item in modules_by_path), None)
    status = "resolved" if target_id else "external"
    if target_id is None:
        target_id = external_node(
            builder,
            external_cache,
            node_id=f"{language}:source:{cleaned}",
            qualified_name=f"source {cleaned}",
            display_name=cleaned,
            language=language,
            extensions={"shell_object_type": "source"},
        )
    add_relation(
        builder,
        source_id=source_id,
        target_id=target_id,
        relation_type="imports" if status == "resolved" else "dynamic_imports",
        source_span=span_for_tree(tree_file.tree.root_node),
        detail={"reference": cleaned, "kind": "source", "resolved_path": candidate},
        resolution_status=status,
        confidence=1.0 if status == "resolved" else 0.6,
        edge_prefix=language,
    )


def _collect_parse_diagnostics(tree_file: TreeFile, builder: GraphBuilder) -> None:
    root = tree_file.tree.root_node
    if root.has_error:
        diagnostic(
            builder,
            code="parse_error",
            message="Tree-sitter reported a syntax error; extracted nodes are partial",
            file=tree_file.relative_path,
            span=span_for_tree(root),
            details={"grammar": "bash"},
        )


def _function_name(node: Any, source: bytes) -> str | None:
    names = [child for child in node.children if child.type in {"word", "name"}]
    for candidate in reversed(names):
        value = node_text(candidate, source).strip()
        if value and value != "function":
            return value
    return None


def _command_name(node: Any, source: bytes) -> str | None:
    for candidate, _ in walk_with_ancestors(node):
        if candidate.type in {"command_name", "word"}:
            value = node_text(candidate, source).strip().strip("'\"")
            if value and value not in {"function"}:
                return value
    return None


def _first_argument(node: Any, source: bytes) -> str | None:
    name_seen = False
    for child in node.children:
        if child.type == "command_name":
            name_seen = True
            continue
        if name_seen and child.type in {"word", "string", "raw_string"}:
            value = node_text(child, source).strip()
            if value:
                return value
    return None


def bash_analyze_repository(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return analyze_repository(*args, **kwargs)
