"""Aggregate multiple concrete language analyzers into one graph document."""

from __future__ import annotations

import importlib
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AnalysisConfig, repository_id
from .contract import validate_document
from .model import GraphBuilder

ANALYZER_NAME = "connection-map-mixed"
ANALYZER_VERSION = "0.1.0"

# The order is part of the deterministic metadata emitted by the mixed graph.
# Grouped entries share one analyzer pass; per-language entries below remain
# separate when the same implementation supports several concrete languages.
_ANALYZER_GROUPS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("python", "python", frozenset({"python"})),
    ("web", "web", frozenset({"html", "css", "javascript", "typescript"})),
    ("c-family", "c_family", frozenset({"c", "cpp"})),
    ("java", "java", frozenset({"java"})),
    ("csharp", "csharp", frozenset({"csharp"})),
    ("go", "go", frozenset({"go"})),
    ("rust", "rust", frozenset({"rust"})),
    ("php", "php", frozenset({"php"})),
    ("ruby", "ruby", frozenset({"ruby"})),
    ("kotlin", "kotlin", frozenset({"kotlin"})),
    ("swift", "swift", frozenset({"swift"})),
)
_PHASE3_ANALYZERS = {
    "bash": "bash",
    "posix-shell": "shell",
    "powershell": "powershell",
    "dart": "dart",
    "scala": "scala",
}
_SQL_ANALYZERS = {
    "mysql": "mysql",
    "postgresql": "postgresql",
    "sqlite": "sqlite",
    "sqlserver": "sqlserver",
    "oracle": "oracle",
}
_ADDITIONAL_LANGUAGES = frozenset(
    {
        "vbnet",
        "vba",
        "lua",
        "haskell",
        "perl",
        "matlab",
        "cobol",
        "fortran",
        "r",
        "objective-c",
        "cuda",
        "groovy",
        "fsharp",
        "assembly",
        "hcl",
        "gdscript",
        "elixir",
        "zig",
        "julia",
        "pascal",
        "erlang",
    }
)


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Run every selected concrete analyzer once and merge its contract output."""

    active_config = config or AnalysisConfig(language="mixed")
    active_config.validate()
    if active_config.language not in {"mixed", "all"}:
        raise ValueError("Mixed analyzer requires language = 'mixed' or 'all'")

    root = root.resolve()
    selected = active_config.active_languages()
    resolved_commit_sha = commit_sha if commit_sha is not None else _git_commit(root)
    child_documents: list[tuple[str, dict[str, Any]]] = []
    for component_language, analyzer_module, accepted in _ANALYZER_GROUPS:
        languages = tuple(language for language in selected if language in accepted)
        if languages:
            child_documents.append(
                _run_child(
                    root,
                    active_config,
                    component_language=component_language,
                    analyzer_module=analyzer_module,
                    languages=languages,
                    deterministic=deterministic,
                    commit_sha=resolved_commit_sha,
                )
            )

    for language in selected:
        analyzer_module = _PHASE3_ANALYZERS.get(language)
        if analyzer_module:
            child_documents.append(
                _run_child(
                    root,
                    active_config,
                    component_language=language,
                    analyzer_module=analyzer_module,
                    languages=(language,),
                    deterministic=deterministic,
                    commit_sha=resolved_commit_sha,
                )
            )

    for language in selected:
        analyzer_module = _SQL_ANALYZERS.get(language)
        if analyzer_module:
            child_documents.append(
                _run_child(
                    root,
                    active_config,
                    component_language=language,
                    analyzer_module=analyzer_module,
                    languages=(language,),
                    deterministic=deterministic,
                    commit_sha=resolved_commit_sha,
                )
            )

    additional_languages = tuple(language for language in selected if language in _ADDITIONAL_LANGUAGES)
    if additional_languages:
        child_documents.append(
            _run_child(
                root,
                active_config,
                component_language="mixed",
                analyzer_module="extended",
                languages=additional_languages,
                deterministic=deterministic,
                commit_sha=resolved_commit_sha,
            )
        )

    builder = GraphBuilder()
    analyzers: list[dict[str, str]] = []
    runtimes: list[dict[str, Any]] = []
    for component_language, document in child_documents:
        analyzers.append(document["meta"]["analyzer"])
        runtimes.append(document["meta"]["runtime"])
        for node in document["nodes"]:
            merged_node = _with_language(node, component_language)
            builder.add_node(merged_node)
        for edge in document["edges"]:
            builder.add_edge(edge)
        for diagnostic in document["diagnostics"]:
            builder.add_diagnostic(diagnostic)

    runtime = _merge_runtime(runtimes)
    target_commit = resolved_commit_sha
    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": "mixed",
        "languages": list(selected),
        "target": {
            "repository_id": repository_id(root),
            "relative_root": ".",
            "commit_sha": target_commit,
        },
        "runtime": runtime,
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": active_config.to_dict(),
        "extensions": {"analyzers": analyzers},
    }
    document = builder.document(meta)
    validate_document(document)
    return document


def _run_child(
    root: Path,
    parent_config: AnalysisConfig,
    *,
    component_language: str,
    analyzer_module: str,
    languages: tuple[str, ...],
    deterministic: bool,
    commit_sha: str | None,
) -> tuple[str, dict[str, Any]]:
    """Run one child analyzer with the parent selection copied safely."""

    module = importlib.import_module(f".{analyzer_module}_analyzer", package=__package__)
    document = module.analyze_repository(
        root,
        _child_config(parent_config, component_language, languages),
        deterministic=deterministic,
        commit_sha=commit_sha,
    )
    return component_language, document


def _child_config(config: AnalysisConfig, language: str, languages: tuple[str, ...]) -> AnalysisConfig:
    return AnalysisConfig(
        language=language,
        languages=list(languages) if language in {"web", "c-family", "mixed"} else [],
        include_tests=config.include_tests,
        follow_symlinks=config.follow_symlinks,
        max_file_bytes=config.max_file_bytes,
        include=list(config.include or []),
        exclude=list(config.exclude),
        test_patterns=list(config.test_patterns),
        generated=list(config.generated),
        context=dict(config.context),
    )


def _with_language(node: dict[str, Any], component_language: str) -> dict[str, Any]:
    merged = dict(node)
    extensions = dict(node.get("extensions") or {})
    if "language" not in extensions and component_language == "python":
        extensions["language"] = "python"
    if extensions:
        merged["extensions"] = extensions
    return merged


def _merge_runtime(runtimes: list[dict[str, Any]]) -> dict[str, Any]:
    runtime: dict[str, Any] = {
        "python_version": platform.python_version(),
        "ast_version": ";".join(
            sorted({str(item.get("ast_version")) for item in runtimes if item.get("ast_version")})
        )
        or "mixed",
    }
    parsers = sorted({str(item["parser"]) for item in runtimes if item.get("parser")})
    parser_versions = sorted({str(item["parser_version"]) for item in runtimes if item.get("parser_version")})
    grammars = sorted(
        {
            grammar
            for item in runtimes
            for grammar in item.get("grammars", [])
            if isinstance(grammar, str) and grammar
        }
    )
    if parsers:
        runtime["parser"] = ";".join(parsers)
    if parser_versions:
        runtime["parser_version"] = ";".join(parser_versions)
    if grammars:
        runtime["grammars"] = grammars
    build_contexts = [item["build_context"] for item in runtimes if isinstance(item.get("build_context"), dict)]
    if build_contexts:
        runtime["build_context"] = {"analyzers": build_contexts}
    return runtime


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
