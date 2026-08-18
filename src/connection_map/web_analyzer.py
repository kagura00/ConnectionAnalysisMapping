"""Repository-level orchestration for the language-specific Web analyzers."""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from pathlib import Path

from .config import (
    AnalysisConfig,
    discover_source_files,
    language_for_path,
    repository_id,
)
from .contract import validate_document
from .css_analyzer import collect as collect_css
from .html_analyzer import collect as collect_html
from .javascript_analyzer import collect_definitions as collect_javascript_definitions
from .javascript_analyzer import collect_relations as collect_javascript_relations
from .model import GraphBuilder
from .typescript_analyzer import collect_definitions as collect_typescript_definitions
from .typescript_analyzer import collect_relations as collect_typescript_relations
from .web_common import (
    WebAnalysisContext,
    _git_commit,
    add_relation,
    parse_file,
    parser_package_version,
)

ANALYZER_NAME = "connection-map-web-tree-sitter"
ANALYZER_VERSION = "0.1.0"


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict:
    active_config = config or AnalysisConfig(language="web")
    active_config.validate()
    if active_config.language == "python":
        raise ValueError("Web analyzer cannot be used with language = 'python'")
    root = root.resolve()
    builder = GraphBuilder()
    context = WebAnalysisContext(root, active_config, builder)
    files, skipped = discover_source_files(root, active_config)
    for relative_path, reason in skipped:
        code = "generated_file" if reason == "generated" else "excluded_file"
        builder.add_diagnostic(
            {
                "code": code,
                "severity": "info",
                "message": f"Skipped Web source file: {relative_path} ({reason})",
                "file": relative_path,
                "span": None,
                "details": {"reason": reason},
            }
        )

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        language = language_for_path(relative_path)
        if language is None:
            continue
        try:
            web_file = parse_file(path, root, language)
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
        context.files.append(web_file)
        context.files_by_path[web_file.relative_path] = web_file

    for web_file in context.files:
        context.add_module(web_file)
        if web_file.tree.root_node.has_error:
            context.diagnostic(
                "parse_error",
                "error",
                f"Tree-sitterが構文エラーを回復しました: {web_file.relative_path}",
                web_file=web_file,
                tree_node=web_file.tree.root_node,
                details={"grammar": web_file.grammar},
            )

    # Definitions must be collected for every file before relation resolution.
    for web_file in context.files:
        if web_file.language == "javascript":
            collect_javascript_definitions(context, web_file)
        elif web_file.language == "typescript":
            collect_typescript_definitions(context, web_file)

    for web_file in context.files:
        if web_file.language == "html":
            collect_html(context, web_file)
        elif web_file.language == "css":
            collect_css(context, web_file)
        elif web_file.language == "javascript":
            collect_javascript_relations(context, web_file)
        elif web_file.language == "typescript":
            collect_typescript_relations(context, web_file)

    _resolve_dom_references(context)
    _resolve_css_styles(context)
    grammars = sorted({web_file.grammar for web_file in context.files})
    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": active_config.language,
        "languages": list(active_config.active_languages()),
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
            "grammars": grammars,
        },
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": active_config.to_dict(),
    }
    document = builder.document(meta)
    validate_document(document)
    return document


def _resolve_dom_references(context: WebAnalysisContext) -> None:
    for reference in context.dom_references:
        matches = _matching_elements(context, reference.selector)
        if matches:
            for element in matches:
                add_relation(
                    context,
                    reference.source_id,
                    element.node_id,
                    reference.relation_type,
                    resolution_status="resolved",
                    confidence=0.9,
                    source_span=reference.span,
                    detail=reference.detail,
                )
            continue
        target = context.external_node(f"dom:{reference.selector}", unknown=True)
        add_relation(
            context,
            reference.source_id,
            target,
            reference.relation_type,
            resolution_status="unresolved",
            confidence=0.2,
            source_span=reference.span,
            detail=reference.detail,
        )
        context.diagnostic(
            "unresolved_dom_reference",
            "warning",
            f"DOM要素を静的に解決できません: {reference.selector}",
            file_path=reference.file_path,
            span=reference.span,
            node_id=reference.source_id,
            details={"selector": reference.selector},
        )


def _resolve_css_styles(context: WebAnalysisContext) -> None:
    for rule in context.css_rules:
        for selector in rule.selectors:
            matches = _matching_elements(context, selector)
            if not matches:
                if selector and not _is_simple_selector(selector):
                    context.diagnostic(
                        "unsupported_construct",
                        "info",
                        f"複雑なCSSセレクターは自動マッチングしません: {selector}",
                        file_path=rule.file_path,
                        span=rule.span,
                        node_id=rule.node_id,
                        details={"selector": selector},
                    )
                continue
            for element in matches:
                add_relation(
                    context,
                    rule.node_id,
                    element.node_id,
                    "styles",
                    resolution_status="resolved",
                    confidence=0.85,
                    source_span=rule.span,
                    detail={"selector": selector},
                )


def _matching_elements(context: WebAnalysisContext, selector: str) -> list:
    selector = selector.strip()
    if selector.startswith("#") and _is_simple_selector(selector):
        value = selector[1:]
        return [element for element in context.html_elements if element.element_id == value]
    if selector.startswith(".") and _is_simple_selector(selector):
        value = selector[1:]
        return [element for element in context.html_elements if value in element.classes]
    if _is_simple_selector(selector):
        value = selector.lower()
        return [element for element in context.html_elements if element.tag == value]
    return []


def _is_simple_selector(selector: str) -> bool:
    import re

    return bool(
        re.fullmatch(r"\.[A-Za-z_][\w-]*", selector)
        or re.fullmatch(r"#[A-Za-z_][\w-]*", selector)
        or re.fullmatch(r"[A-Za-z][\w-]*", selector)
    )
