"""Tree-sitter based static analyzers for C and C++."""

from __future__ import annotations

import platform
import posixpath
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .analysis_context import load_analysis_context
from .c_family_bindings import CBindings
from .c_family_common import (
    CFamilyAnalysisContext,
    CFile,
    CSymbol,
    _git_commit,
    add_relation,
    friend_declaration_for,
    node_text,
    parse_file,
    parser_package_version,
    span_for_tree,
    walk_tree,
)
from .config import AnalysisConfig, discover_source_files, repository_id
from .contract import validate_document
from .model import GraphBuilder

ANALYZER_NAME = "connection-map-c-family-tree-sitter"
ANALYZER_VERSION = "0.1.0"
SUPPORTED_LANGUAGES = {"c", "cpp"}
_FUNCTION_KINDS = {"function", "method"}
_TYPE_KINDS = {"class", "type", "interface"}
_PRIMITIVE_TYPES = {
    "auto",
    "bool",
    "char",
    "double",
    "float",
    "int",
    "long",
    "short",
    "signed",
    "size_t",
    "unsigned",
    "void",
    "wchar_t",
}
_TYPE_NODE_TYPES = {
    "type_identifier",
    "qualified_identifier",
    "scoped_identifier",
    "template_type",
    "dependent_type",
}
_ACCESS_NODE_TYPES = {"access_specifier", "public", "private", "protected", "virtual"}


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Analyze C/C++ source without compiling or executing the target code."""

    active_config = config or AnalysisConfig(language="c-family")
    active_config.validate()
    selected = active_config.active_languages()
    if not set(selected) <= SUPPORTED_LANGUAGES:
        raise ValueError("C family analyzer supports only c and cpp")

    root = root.resolve()
    builder = GraphBuilder()
    context = CFamilyAnalysisContext(root, active_config, builder, load_analysis_context(root, active_config))
    for diagnostic in context.analysis_context.diagnostics:
        builder.add_diagnostic(
            {
                "code": diagnostic["code"],
                "severity": diagnostic["severity"],
                "message": diagnostic["message"],
                "file": None,
                "span": None,
                "details": {},
            }
        )
    files, skipped = discover_source_files(root, active_config, languages=set(selected))
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped C/C++ source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        language = _language_for_path(path, selected)
        if language is None:
            continue
        try:
            c_file = parse_file(path, root, language)
        except (OSError, UnicodeError) as exc:
            builder.add_diagnostic(
                {
                    "code": "read_error",
                    "severity": "error",
                    "message": str(exc),
                    "file": relative_path,
                    "span": None,
                    "details": {},
                }
            )
            continue
        context.files.append(c_file)
        context.files_by_path[c_file.relative_path] = c_file

    for c_file in context.files:
        context.add_module(c_file)

    bindings = CBindings(context)
    for c_file in context.files:
        _collect_definitions(context, c_file, bindings, structural_only=True)
    for c_file in context.files:
        _collect_definitions(context, c_file, bindings)
    for c_file in context.files:
        for node in walk_tree(c_file.tree.root_node):
            if node.type == "preproc_include":
                _collect_include(context, c_file, node)
    bindings.prepare()
    for c_file in context.files:
        for node in walk_tree(c_file.tree.root_node):
            if node.type == "base_class_clause":
                _collect_inheritance(context, c_file, node, bindings)
    bindings.prepare_inheritance()
    for c_file in context.files:
        _collect_relations(context, c_file, bindings)
    for c_file in context.files:
        if c_file.tree.root_node.has_error:
            _add_parse_diagnostics(context, c_file)

    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": active_config.language,
        "languages": list(selected),
        "target": {
            "repository_id": repository_id(root),
            "relative_root": ".",
            "commit_sha": commit_sha if commit_sha is not None else _git_commit(root),
        },
        "runtime": {
            "python_version": platform.python_version(),
            "ast_version": "tree-sitter",
            "parser": "tree-sitter-language-pack",
            "parser_version": parser_package_version(),
            "grammars": sorted({c_file.grammar for c_file in context.files}),
            "build_context": context.analysis_context.summary(),
        },
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": active_config.to_dict(),
    }
    document = builder.document(meta)
    validate_document(document)
    return document


def _language_for_path(path: Path, selected: tuple[str, ...]) -> str | None:
    suffix = path.suffix.lower()
    if suffix in {".c"}:
        return "c" if "c" in selected else None
    if suffix in {".cc", ".cpp", ".cxx", ".c++", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".inl"}:
        return "cpp" if "cpp" in selected else None
    if suffix in {".h", ".inc"}:
        if "c" not in selected and "cpp" not in selected:
            return None
        if "c" not in selected:
            return "cpp"
        if "cpp" not in selected:
            return "c"
        try:
            sample = path.read_bytes()[:256_000].decode("utf-8", errors="ignore")
        except OSError:
            return "c"
        cpp_markers = (
            r"\bnamespace\b",
            r"\btemplate\b",
            r"\bclass\b",
            r"\b(public|private|protected)\s*:",
            r"\busing\s+namespace\b",
            r"\bstd::",
        )
        return "cpp" if any(re.search(marker, sample) for marker in cpp_markers) else "c"
    return None


_MAX_PARSE_ERROR_DETAILS = 24


def _nearby_context_excerpt(source: bytes, node: Any, *, limit: int = 120) -> str:
    start_byte = int(node.start_byte)
    end_byte = int(node.end_byte)
    start = max(0, start_byte - 60)
    end = min(len(source), max(end_byte, start_byte) + 60)
    excerpt = re.sub(r"\s+", " ", source[start:end].decode("utf-8", errors="replace")).strip()
    if start:
        excerpt = "…" + excerpt
    if end < len(source):
        excerpt += "…"
    if len(excerpt) > limit:
        excerpt = excerpt[: limit - 1] + "…"
    return excerpt


def _add_parse_diagnostics(context: CFamilyAnalysisContext, c_file: CFile) -> None:
    """Record one bounded file summary plus local Tree-sitter recovery spans."""

    markers: list[tuple[Any, bool]] = []
    seen: set[tuple[int, int, str, bool]] = set()
    for node in walk_tree(c_file.tree.root_node):
        missing = bool(node.is_missing)
        if node.type != "ERROR" and not missing:
            continue
        # Some grammars nest same-span ERROR nodes. They describe one recovery
        # range, while a missing anonymous token at that point remains distinct.
        key = (int(node.start_byte), int(node.end_byte), str(node.type), missing)
        if key not in seen:
            seen.add(key)
            markers.append((node, missing))
    markers.sort(key=lambda item: (
        int(item[0].start_byte), int(item[0].end_byte), item[1], str(item[0].type),
    ))
    error_count = sum(1 for _, missing in markers if not missing)
    missing_count = sum(1 for _, missing in markers if missing)
    emitted = markers[:_MAX_PARSE_ERROR_DETAILS]
    extracted_kinds = Counter(
        symbol.kind for symbol in context.definitions.values() if symbol.file_path == c_file.relative_path
    )
    details: dict[str, Any] = {
        "grammar": c_file.grammar,
        "error_node_count": error_count,
        "missing_token_count": missing_count,
        "detail_limit": _MAX_PARSE_ERROR_DETAILS,
        "details_emitted": len(emitted),
        "details_omitted": len(markers) - len(emitted),
        "extracted_kind_counts": dict(sorted(extracted_kinds.items())),
    }
    suffix = c_file.path.suffix.lower()
    if suffix in {".inl", ".ipp"}:
        details["analysis_kind"] = "standalone_include_fragment"
        details["fragment_limitations"] = [
            "Parsed as a standalone fragment; including translation-unit declarations and macro context are not supplied.",
            "Recovered symbols are partial extraction and do not establish complete C++ semantics.",
        ]
    context.diagnostic(
        "parse_error",
        "error",
        f"Tree-sitterが構文エラーを回復しました: {c_file.relative_path}",
        c_file=c_file,
        details=details,
    )
    for node, missing in emitted:
        excerpt = re.sub(r"\s+", " ", node_text(node, c_file.source)).strip()
        if len(excerpt) > 120:
            excerpt = excerpt[:117] + "..."
        details = {
            "node_type": node.type,
            "is_missing": missing,
            "excerpt": excerpt,
        }
        if missing:
            context_excerpt = _nearby_context_excerpt(c_file.source, node)
            details["context_excerpt"] = context_excerpt
            message = f"Tree-sitterが欠落した構文要素を補完しました: {node.type}"
            if context_excerpt:
                message += f" (周辺: {context_excerpt})"
            code = "parse_missing_token"
        else:
            message = "Tree-sitter ERROR構文を検出しました"
            if excerpt:
                message += f": {excerpt}"
            code = "parse_error_node"
        context.diagnostic(
            code,
            "error",
            message,
            c_file=c_file,
            tree_node=node,
            details=details,
        )


def _scope_parent(context: CFamilyAnalysisContext, c_file: CFile, node: Any) -> str:
    """Return the nearest lexical scope, skipping declaration wrappers."""

    current = node.parent
    while current is not None:
        node_id = context.definition_by_node.get((c_file.relative_path, current.id))
        if node_id is not None:
            symbol = context.definitions[node_id]
            if symbol.kind in {"namespace", "class", "type", "function", "method"}:
                return node_id
        current = current.parent
    return c_file.module_id


def _friend_namespace_owner(context: CFamilyAnalysisContext, c_file: CFile, friend_node: Any) -> str | None:
    current = friend_node.parent
    granting_class = None
    while current is not None:
        if current.type in {"function_definition", "lambda_expression"}:
            return None
        if current.type in {"class_specifier", "struct_specifier", "union_specifier"}:
            granting_class = current
            break
        current = current.parent
    if granting_class is None:
        return None

    current = granting_class.parent
    while current is not None:
        if current.type in {"function_definition", "lambda_expression"}:
            # A local class cannot safely introduce an ordinary namespace name.
            return None
        if current.type == "namespace_definition":
            return context.definition_by_node.get((c_file.relative_path, current.id))
        if current.type == "translation_unit":
            return c_file.module_id
        current = current.parent
    return None


def _friend_granting_class(context: CFamilyAnalysisContext, c_file: CFile, friend_node: Any) -> CSymbol | None:
    current = friend_node.parent
    while current is not None:
        if current.type in {"function_definition", "lambda_expression"}:
            return None
        if current.type in {"class_specifier", "struct_specifier", "union_specifier"}:
            node_id = context.definition_by_node.get((c_file.relative_path, current.id))
            return context.definitions.get(node_id) if node_id is not None else None
        current = current.parent
    return None


def _friend_owner_scope(
    context: CFamilyAnalysisContext,
    c_file: CFile,
    friend_node: Any,
    name: str,
) -> tuple[str, str, str, str] | None:
    namespace_id = _friend_namespace_owner(context, c_file, friend_node)
    if namespace_id is None or not name or "<" in name or ">" in name:
        return None
    namespace_symbol = context.definitions.get(namespace_id)
    namespace_name = namespace_symbol.qualified_name if namespace_symbol else ""
    if "::" not in name:
        qualified_name = f"{namespace_name}::{name}" if namespace_name else name
        return namespace_id, qualified_name, "function", "namespace"

    absolute = name.startswith("::")
    qualifier, simple_name = name.rsplit("::", 1)
    qualifier = qualifier.removeprefix("::")
    if not simple_name:
        return None
    if not qualifier and absolute:
        return c_file.module_id, simple_name, "function", "namespace"

    if absolute:
        candidate_names = [qualifier]
    else:
        prefixes: list[str] = []
        current_id = namespace_id
        while current_id in context.definitions:
            symbol = context.definitions[current_id]
            if symbol.kind == "namespace":
                prefixes.append(symbol.qualified_name)
            current_id = context.builder.nodes[current_id].get("parent_id")
        prefixes.append("")
        candidate_names = [f"{prefix}::{qualifier}" if prefix else qualifier for prefix in prefixes]

    for candidate_name in candidate_names:
        candidates = [
            symbol for symbol in context.symbols_by_qualified_name.get(candidate_name, [])
            if symbol.kind in {"namespace", "class", "type"}
        ]
        if not candidates:
            continue
        if any(
            symbol.kind == "type"
            and context.builder.nodes[symbol.node_id]["extensions"].get("declaration_form") not in {"struct", "union"}
            for symbol in candidates
        ):
            # An alias names another type; it does not own that type's members.
            # Without confirming the target, do not manufacture Alias::member.
            return None
        owner_kinds = {"namespace" if symbol.kind == "namespace" else "class" for symbol in candidates}
        if len(owner_kinds) != 1:
            return None
        owner_kind = next(iter(owner_kinds))
        same_file = [symbol for symbol in candidates if symbol.file_path == c_file.relative_path]
        owner = sorted(same_file or candidates, key=lambda symbol: symbol.node_id)[0]
        callable_kind = "method" if owner_kind == "class" else "function"
        return owner.node_id, f"{owner.qualified_name}::{simple_name}", callable_kind, owner_kind
    return None


def _friend_declarator_name(node: Any | None, source: bytes) -> str | None:
    """Read a friend declarator name while retaining an absolute ``::`` prefix."""

    if node is None:
        return None
    if node.type in {"identifier", "field_identifier", "type_identifier", "namespace_identifier"}:
        return node_text(node, source).strip()
    if node.type in {"qualified_identifier", "scoped_identifier"}:
        return re.sub(r"\s+", "", node_text(node, source))
    for field_name in ("declarator", "name"):
        child = node.child_by_field_name(field_name)
        if child is not None:
            result = _friend_declarator_name(child, source)
            if result:
                return result
    for child in node.named_children:
        result = _friend_declarator_name(child, source)
        if result:
            return result
    return None


def _collect_definitions(
    context: CFamilyAnalysisContext, c_file: CFile, bindings: CBindings, *, structural_only: bool = False,
) -> None:
    for node in walk_tree(c_file.tree.root_node):
        candidate = _definition_candidate(context, c_file, node, bindings)
        if candidate is None:
            continue
        kind, name, declaration_kind, extensions = candidate
        if (c_file.relative_path, node.id) in context.definition_by_node:
            continue
        if structural_only:
            if kind in _FUNCTION_KINDS:
                continue
            current = node.parent
            while current is not None and current.type not in {"function_definition", "lambda_expression"}:
                current = current.parent
            if current is not None:
                continue
        friend_node = friend_declaration_for(node)
        if friend_node is not None:
            friend_scope = _friend_owner_scope(context, c_file, friend_node, name)
            if friend_scope is None:
                context.diagnostic(
                    "friend_scope_unresolved",
                    "warning",
                    f"friend callableの所有scopeを解析範囲で特定できません: {name}",
                    c_file=c_file,
                    tree_node=friend_node,
                    details={"owner_resolution": "uncertain", "name": name},
                )
                continue
            parent_id, qualified_name, kind, owner_kind = friend_scope
            granting_class = _friend_granting_class(context, c_file, friend_node)
            extensions = {
                **extensions,
                "friend_declaration": True,
                "ordinary_lookup": "hidden",
                "friend_owner_kind": owner_kind,
            }
            if granting_class is not None:
                extensions["friend_of"] = granting_class.qualified_name
        else:
            parent_id = _scope_parent(context, c_file, node)
            if kind != "namespace":
                parent_id, qualified_name = _qualified_scope(context, c_file, node, name, parent_id)
        if kind == "namespace":
            # Compact C++17 syntax still introduces every namespace segment.
            for segment in name.split("::"):
                parent_symbol = context.definitions.get(parent_id)
                prefix = parent_symbol.qualified_name + "::" if parent_symbol else ""
                parent_id = context.add_definition(
                    c_file, node, kind=kind, name=segment, qualified_name=prefix + segment,
                    declaration_kind=declaration_kind, parent_id=parent_id,
                )
            continue
        parent_symbol = context.definitions.get(parent_id)
        if kind == "function" and parent_symbol is not None and parent_symbol.kind in {"class", "type"}:
            kind = "method"
        display_name = name.rsplit("::", 1)[-1]
        return_behavior = None
        return_sites = None
        if kind in _FUNCTION_KINDS:
            if declaration_kind == "definition":
                return_behavior, return_sites = _return_info(node)
            else:
                return_behavior = "unknown"
        context.add_definition(
            c_file,
            node,
            kind=kind,
            name=display_name,
            qualified_name=qualified_name,
            declaration_kind=declaration_kind,
            parent_id=parent_id,
            return_behavior=return_behavior,
            return_sites=return_sites,
            extensions=extensions,
        )


def _definition_candidate(
    context: CFamilyAnalysisContext,
    c_file: CFile,
    node: Any,
    bindings: CBindings,
) -> tuple[str, str, str, dict[str, Any]] | None:
    node_type = node.type
    if node_type == "namespace_definition":
        name_node = node.child_by_field_name("name")
        name = node_text(name_node, c_file.source).strip() if name_node else f"<anonymous@{node.start_point[0] + 1}>"
        return "namespace", name, "namespace", {}
    if node_type == "class_specifier":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        return "class", node_text(name_node, c_file.source).strip(), "class", {"declaration_form": "class"}
    if node_type in {"struct_specifier", "union_specifier", "enum_specifier"}:
        name_node = node.child_by_field_name("name")
        name = node_text(name_node, c_file.source).strip() if name_node is not None else ""
        if not name and node_type != "enum_specifier":
            return None
        if not name:
            name = f"<anonymous@{node.start_point[0] + 1}>"
        return (
            "type",
            name,
            node_type.removesuffix("_specifier"),
            {"declaration_form": node_type.removesuffix("_specifier")},
        )
    if node_type == "type_definition":
        declarator = node.child_by_field_name("declarator")
        name = _declarator_name(declarator, c_file.source) if declarator else None
        if not name:
            return None
        underlying = node.child_by_field_name("type")
        underlying_name = underlying.child_by_field_name("name") if underlying is not None else None
        if (
            underlying is not None
            and underlying.type in {"struct_specifier", "union_specifier", "enum_specifier"}
            and underlying_name is not None
            and node_text(underlying_name, c_file.source).strip() == name
        ):
            # ``typedef struct Item { ... } Item`` introduces one named type,
            # not two useful graph nodes.
            return None
        return "type", name, "alias", {"declaration_form": "typedef"}
    if node_type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        friend = friend_declaration_for(node)
        name = (
            _friend_declarator_name(declarator, c_file.source) if friend is not None else _declarator_name(declarator, c_file.source)
        ) if declarator else None
        if not name:
            return None
        if friend is not None:
            return "function", name, "definition", {"has_body": True}
        parent_id = _scope_parent(context, c_file, node)
        parent_symbol = context.definitions.get(parent_id) if parent_id else None
        parent_kind = parent_symbol.kind if parent_symbol else None
        kind = "method" if parent_kind in {"class", "type"} else "function"
        return kind, name, "definition", {"has_body": True}
    if node_type in {"declaration", "field_declaration"}:
        declarator = bindings.declaration(c_file, node)
        if declarator is None:
            return None
        friend = friend_declaration_for(node)
        if friend is not None:
            name = _friend_declarator_name(declarator, c_file.source)
            if not name:
                return None
            return "function", name, "prototype", {"has_body": False}
        parent_id = _scope_parent(context, c_file, node)
        parent_symbol = context.definitions.get(parent_id) if parent_id else None
        parent_kind = parent_symbol.kind if parent_symbol else None
        if node_type == "field_declaration" and parent_kind not in {"class", "type"}:
            return None
        name = _declarator_name(declarator, c_file.source)
        if not name:
            return None
        kind = "method" if parent_kind in {"class", "type"} else "function"
        return kind, name, "prototype", {"has_body": False}
    return None


def _qualified_scope(
    context: CFamilyAnalysisContext,
    c_file: CFile,
    node: Any,
    name: str,
    parent_id: str,
) -> tuple[str, str]:
    parent_symbol = context.definitions.get(parent_id)
    parent_q = parent_symbol.qualified_name if parent_symbol else ""
    if "::" in name:
        scope, simple_name = name.rsplit("::", 1)
        candidate_q = f"{parent_q}::{scope}" if parent_q else scope
        resolved_parent = context.find_qualified_parent(candidate_q, kinds={"namespace", "class", "type"})
        if resolved_parent is None:
            resolved_parent = context.find_qualified_parent(scope, kinds={"namespace", "class", "type"})
        if resolved_parent is not None:
            resolved_symbol = context.definitions[resolved_parent]
            return resolved_parent, f"{resolved_symbol.qualified_name}::{simple_name}"
        # An unavailable parent must not erase an explicit namespace/class prefix.
        return parent_id, f"{candidate_q}::{simple_name}"
    qualified_name = f"{parent_q}::{name}" if parent_q else name
    return parent_id, qualified_name


def _collect_relations(context: CFamilyAnalysisContext, c_file: CFile, bindings: CBindings) -> None:
    for node in walk_tree(c_file.tree.root_node):
        if node.type == "call_expression":
            _collect_call(context, c_file, node, bindings)
        elif node.type in {"declaration", "field_declaration", "parameter_declaration"}:
            _collect_type_use(context, c_file, node, bindings)


def _collect_include(context: CFamilyAnalysisContext, c_file: CFile, node: Any) -> None:
    raw = node_text(node, c_file.source).strip()
    match = re.search(r"#\s*include\s*([<\"])(.*?)[>\"]", raw)
    if match is None:
        target = context.external_node(
            f"include:{raw}", language=c_file.language, unknown=True, c_file=c_file, span=span_for_tree(node)
        )
        status = "unresolved"
        confidence = 0.2
        context.diagnostic(
            "unresolved_import",
            "warning",
            f"C/C++ includeを静的に解決できません: {raw}",
            c_file=c_file,
            tree_node=node,
            details={"include": raw},
        )
    else:
        delimiter, included = match.groups()
        included = included.strip()
        target_file = _resolve_include(context, c_file, included, delimiter=delimiter)
        if target_file is not None:
            target = target_file.module_id
            status = "resolved"
            confidence = 1.0
        elif delimiter == "<":
            target = context.external_node(f"include:{included}", language=c_file.language)
            status = "external"
            confidence = 0.75
        else:
            target = context.external_node(
                f"include:{included}", language=c_file.language, unknown=True, c_file=c_file, span=span_for_tree(node)
            )
            status = "unresolved"
            confidence = 0.2
            context.diagnostic(
                "unresolved_import",
                "warning",
                f"ローカルincludeを解決できません: {included}",
                c_file=c_file,
                tree_node=node,
                details={"include": included},
            )
    add_relation(
        context,
        c_file.module_id,
        target,
        "imports",
        resolution_status=status,
        confidence=confidence,
        source_span=span_for_tree(node),
        detail={"include": raw, "include_kind": "local" if '"' in raw else "system"},
    )


def _resolve_include(
    context: CFamilyAnalysisContext,
    c_file: CFile,
    included: str,
    *,
    delimiter: str = '"',
) -> CFile | None:
    relative_candidate = posixpath.normpath(
        posixpath.join(posixpath.dirname(c_file.relative_path), included.replace("\\", "/"))
    )
    root_candidate = posixpath.normpath(included.replace("\\", "/").lstrip("/"))
    repository_candidates: list[str] = []
    if relative_candidate != ".." and not relative_candidate.startswith("../") and not relative_candidate.startswith("/"):
        repository_candidates.append(relative_candidate)
        if not Path(relative_candidate).suffix:
            repository_candidates.extend([f"{relative_candidate}.h", f"{relative_candidate}.hpp"])
    if root_candidate != ".." and not root_candidate.startswith("../") and not root_candidate.startswith("/"):
        repository_candidates.append(root_candidate)
        if not Path(root_candidate).suffix:
            repository_candidates.extend([f"{root_candidate}.h", f"{root_candidate}.hpp"])
    include_candidates: list[str] = []
    for include_dir in context.analysis_context.compilation_database.include_dirs_for(c_file.path, context.root):
        candidate = (include_dir / included).resolve()
        try:
            relative = candidate.relative_to(context.root).as_posix()
        except ValueError:
            relative = ""
        if relative:
            include_candidates.append(relative)
            if not Path(relative).suffix:
                include_candidates.extend([f"{relative}.h", f"{relative}.hpp"])
    # For angle-bracket includes the compiler's configured include directories
    # take precedence over repository fallbacks.  Quoted includes retain the
    # source-file-first behavior and use configured directories as fallback.
    candidates = include_candidates + repository_candidates if delimiter == "<" else repository_candidates + include_candidates
    for item in candidates:
        target = context.files_by_path.get(item)
        if target is not None:
            return target
    return None


def _collect_call(context: CFamilyAnalysisContext, c_file: CFile, node: Any, bindings: CBindings) -> None:
    if bindings.declaration_type_expression(c_file, node):
        return
    function_node = node.child_by_field_name("function")
    caller = _scope_parent(context, c_file, node)
    span = span_for_tree(node)
    expression = node_text(node, c_file.source).strip()
    callee = node_text(function_node, c_file.source).strip() if function_node is not None else ""
    call_kind = _call_kind(function_node)
    evidence = None
    if function_node is None or not callee:
        target = context.external_node("call:<unknown>", language=c_file.language, unknown=True, c_file=c_file, span=span)
        status = "unresolved"
        confidence = 0.1
        _call_diagnostic(context, c_file, node, caller, expression, "呼び出し先の構文を取得できません")
    elif call_kind in {"member", "unknown"}:
        target = context.external_node(
            f"call:{_clean_reference_name(callee)}",
            language=c_file.language,
            unknown=True,
            c_file=c_file,
            span=span,
        )
        status = "unresolved"
        confidence = 0.2
        _call_diagnostic(context, c_file, node, caller, expression, "受け手の型または間接呼び出し先を静的に特定できません")
    else:
        reference = _clean_reference_name(callee)
        lookup = bindings.resolve(c_file, node, ("::" if callee.startswith("::") else "") + reference,
                                  kinds=_FUNCTION_KINDS)
        if lookup.type_expression:
            return  # A functional type conversion is not a function call.
        candidates = lookup.candidates
        if len(candidates) == 1:
            target = candidates[0].node_id
            status = "resolved"
            confidence = 1.0
            evidence = bindings.evidence(c_file, node, candidates[0])
        elif len(candidates) > 1 or lookup.reason:
            target = context.external_node(
                f"call:{reference}", language=c_file.language, unknown=True, c_file=c_file, span=span
            )
            status = "unresolved"
            confidence = 0.2
            _call_diagnostic(context, c_file, node, caller, expression, lookup.reason or "同名またはoverloadの候補が複数あります")
        else:
            target = context.external_node(f"call:{reference}", language=c_file.language)
            status = "external"
            confidence = 0.7
    add_relation(
        context,
        caller,
        target,
        "calls",
        resolution_status=status,
        confidence=confidence,
        source_span=span,
        detail={"expression": expression, "callee": callee, "call_kind": call_kind,
                **({"resolution_evidence": evidence} if evidence else {})},
    )


def _call_diagnostic(
    context: CFamilyAnalysisContext,
    c_file: CFile,
    node: Any,
    caller: str,
    expression: str,
    reason: str,
) -> None:
    context.diagnostic(
        "unresolved_call",
        "warning",
        f"C/C++呼び出しを静的に解決できません: {expression} ({reason})",
        c_file=c_file,
        tree_node=node,
        node_id=caller,
        details={"expression": expression, "reason": reason},
    )


def _collect_inheritance(context: CFamilyAnalysisContext, c_file: CFile, node: Any, bindings: CBindings) -> None:
    parent_id = _scope_parent(context, c_file, node)
    parent = context.definitions.get(parent_id) if parent_id else None
    if parent is None or parent.kind not in _TYPE_KINDS:
        return
    for child in node.named_children:
        if child.type in _ACCESS_NODE_TYPES:
            continue
        reference = _type_reference_text(child, c_file.source)
        if not reference:
            continue
        raw = node_text(child, c_file.source).strip()
        lookup = bindings.inheritance_lookup(c_file, node, parent, reference, raw, _TYPE_KINDS)
        candidates = lookup.candidates
        if len(candidates) == 1:
            target = candidates[0].node_id
            status = "resolved"
            confidence = 1.0
        elif len(candidates) > 1 or lookup.reason:
            target = context.external_node(
                f"inherit:{reference}", language=c_file.language, unknown=True, c_file=c_file, span=span_for_tree(child)
            )
            status = "unresolved"
            confidence = 0.2
            context.diagnostic(
                "unresolved_inheritance",
                "warning",
                f"継承元を一意に解決できません: {reference} ({lookup.reason or '候補が複数あります'})",
                c_file=c_file,
                tree_node=child,
                node_id=parent_id,
                details={"reference": reference},
            )
        else:
            target = context.external_node(f"type:{reference}", language=c_file.language)
            status = "external"
            confidence = 0.7
        add_relation(
            context,
            parent_id,
            target,
            "inherits",
            resolution_status=status,
            confidence=confidence,
            source_span=span_for_tree(child),
            detail={"reference": reference, "source_reference": raw},
        )


def _collect_type_use(context: CFamilyAnalysisContext, c_file: CFile, node: Any, bindings: CBindings) -> None:
    type_node = node.child_by_field_name("type")
    if type_node is None or type_node.type not in _TYPE_NODE_TYPES:
        return
    reference = _type_reference_text(type_node, c_file.source)
    if not reference or reference in _PRIMITIVE_TYPES:
        return
    candidates = bindings.resolve(c_file, node, reference, kinds=_TYPE_KINDS).candidates
    if len(candidates) != 1:
        return
    source_id = _scope_parent(context, c_file, node)
    add_relation(
        context,
        source_id,
        candidates[0].node_id,
        "uses",
        resolution_status="resolved",
        confidence=0.9,
        source_span=span_for_tree(type_node),
        detail={"reference": reference, "kind": "type_reference"},
    )


def _return_info(node: Any) -> tuple[str, list[dict[str, Any]]]:
    sites: list[dict[str, Any]] = []
    has_value = False
    has_none = False
    for child in walk_tree(node):
        if child.type != "return_statement":
            continue
        value = bool(child.named_children)
        has_value = has_value or value
        has_none = has_none or not value
        sites.append({"span": span_for_tree(child), "value_kind": "value" if value else "none"})
    if not sites:
        return "no_explicit_return", []
    if has_value and has_none:
        return "mixed", sites
    return ("returns_value" if has_value else "returns_none"), sites


def _declarator_name(node: Any | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type in {"identifier", "field_identifier", "type_identifier", "namespace_identifier"}:
        return node_text(node, source).strip()
    if node.type in {"qualified_identifier", "scoped_identifier"}:
        return _clean_reference_name(node_text(node, source))
    for field_name in ("declarator", "name"):
        child = node.child_by_field_name(field_name)
        if child is not None:
            result = _declarator_name(child, source)
            if result:
                return result
    for child in node.named_children:
        result = _declarator_name(child, source)
        if result:
            return result
    return None


def _type_reference_text(node: Any, source: bytes) -> str:
    raw = node_text(node, source).strip()
    if not raw:
        return ""
    if node.type == "template_type":
        name_node = node.named_children[0] if node.named_children else None
        raw = node_text(name_node, source).strip() if name_node is not None else raw
    raw = re.sub(r"^(const|volatile|typename|struct|class|enum|union)\s+", "", raw)
    raw = _strip_template_arguments(raw).strip()
    raw = raw.removeprefix("::")
    raw = re.sub(r"[&*]+$", "", raw).strip()
    if raw in _PRIMITIVE_TYPES:
        return ""
    return _clean_reference_name(raw)


def _clean_reference_name(value: str) -> str:
    cleaned = re.sub(r"\s+", "", value.strip())
    cleaned = cleaned.removeprefix("::")
    return _strip_template_arguments(cleaned)


def _strip_template_arguments(value: str) -> str:
    result: list[str] = []
    depth = 0
    for char in value:
        if char == "<":
            depth += 1
        elif char == ">" and depth:
            depth -= 1
        elif depth == 0:
            result.append(char)
    return "".join(result)


def _call_kind(node: Any | None) -> str:
    if node is None:
        return "unknown"
    if node.type in {"identifier", "field_identifier"}:
        return "direct"
    if node.type in {"qualified_identifier", "scoped_identifier"}:
        return "qualified"
    if node.type == "field_expression":
        return "member"
    return "unknown"
