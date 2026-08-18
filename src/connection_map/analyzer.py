"""Dispatch repository analysis to the configured language analyzer."""

from __future__ import annotations

import importlib
from pathlib import Path

from .config import AnalysisConfig, ensure_repository_root
from .language_registry import analyzer_for_language


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict:
    """Analyze a repository without importing or executing its source code."""

    root = ensure_repository_root(root)
    active_config = config or AnalysisConfig()
    active_config.validate()
    analyzer_name = analyzer_for_language(active_config.language)
    try:
        module = importlib.import_module(f".{analyzer_name}_analyzer", package=__package__)
    except ModuleNotFoundError as exc:
        raise ValueError(
            f"no analyzer module is registered for language {active_config.language!r}: "
            f"{analyzer_name}_analyzer.py"
        ) from exc
    analyze = getattr(module, "analyze_repository", None)
    if analyze is None:
        raise ValueError(f"analyzer module {analyzer_name!r} has no analyze_repository function")
    return analyze(root, active_config, deterministic=deterministic, commit_sha=commit_sha)
