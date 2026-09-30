"""Source freshness and coverage, separate from graph artifact integrity."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from .config import AnalysisConfig, discover_source_files, ensure_repository_root
from .contract import canonical_sha256
from .language_registry import analyzer_for_language


def capture_sources(root: Path, config: AnalysisConfig) -> dict[str, Any]:
    files, skipped = discover_source_files(root, config)
    inventory = {}
    errors = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            content = path.read_bytes()
        except OSError:
            errors.append(relative)
            continue
        inventory[relative] = hashlib.sha256(content).hexdigest()
    context_files, context_errors, unsupported_styles = {}, [], []
    if set(config.active_languages()) & {"html", "css", "javascript", "typescript"}:
        from .typescript_context import TypeScriptProjects
        projects = TypeScriptProjects(root, config)
        unsupported_styles = projects.unsupported_styles
        for path in sorted(projects.inputs):
            relative = path.relative_to(root).as_posix()
            try:
                context_files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                context_errors.append(relative)
    return {
        "version": 1,
        "scope": "selected_source_files",
        "settings_sha256": canonical_sha256(config.to_dict()),
        "files": inventory,
        "read_errors": errors,
        "skipped_by_reason": dict(sorted(Counter(reason for _, reason in skipped).items())),
        "discovery_errors": [path for path, reason in skipped if reason in {"stat_failed", "directory_unreadable"}],
        "context_files": context_files,
        "context_read_errors": context_errors,
        "unsupported_styles": unsupported_styles,
    }


def attach_evidence(document: dict, before: dict, after: dict) -> None:
    """Keep the pre-analysis inventory and detect edits during analysis."""
    snapshot = {**before, "stable_during_analysis": before == after and not before["read_errors"]
                and not before["discovery_errors"] and not before.get("context_read_errors")}
    extensions = document["meta"].setdefault("extensions", {})
    extensions["source_snapshot"] = snapshot
    extensions["coverage"] = coverage_summary(document)


def coverage_summary(document: dict) -> dict[str, Any]:
    meta = document["meta"]
    snapshot = meta.get("extensions", {}).get("source_snapshot", {})
    selected = set(snapshot.get("files", {})) | set(snapshot.get("read_errors", []))
    represented = {node["file"] for node in document["nodes"] if node.get("file")}
    errors = [item for item in document["diagnostics"] if item["severity"] == "error"]
    failed = {item["file"] for item in errors if item.get("file")}
    unrepresented = selected - represented
    limitations = meta.get("extensions", {}).get("extraction_limitations", [])
    unsupported = snapshot.get("unsupported_styles", [])
    partial = bool(errors or unrepresented or limitations or unsupported or snapshot.get("stable_during_analysis") is False)
    languages = meta.get("languages", [meta.get("language")])
    extraction = {}
    for language in languages:
        try:
            analyzer = analyzer_for_language(language)
        except ValueError:
            analyzer = "unknown"
        extraction[language] = "profile-regex" if analyzer == "extended" else analyzer
    return {
        "status": "partial" if partial else "completed" if snapshot else "unknown",
        "selected_file_count": len(selected) if snapshot else None,
        "files_with_nodes": len(represented),
        "files_with_errors": sorted(failed),
        "files_without_nodes": sorted(unrepresented),
        "error_count": len(errors),
        "tests_included": meta.get("settings", {}).get("include_tests"),
        "skipped_by_reason": snapshot.get("skipped_by_reason", {}),
        "languages": languages,
        "extraction_by_language": extraction,
        "parser_unavailable_files": sorted({item["file"] for item in document["diagnostics"]
                                            if item["code"] == "parser_unavailable" and item.get("file")}),
        "relationships_complete": False,
        "unsupported_source_files": unsupported,
        "extraction_limitations": limitations,
        "context_file_count": len(snapshot.get("context_files", {})),
        "limitations": [
            "Static analysis does not establish the absence of runtime or dynamic relationships.",
            "Confidence scores are heuristic scores, not calibrated probabilities.",
            "Skipped counts do not enumerate files inside pruned directories.",
        ],
    }


def check_freshness(document: dict, root: Path | None = None) -> dict[str, Any]:
    snapshot = document["meta"].get("extensions", {}).get("source_snapshot")
    base = {"scope": "selected_source_files", "external_context_verified": False}
    if not snapshot or snapshot.get("version") != 1:
        return {**base, "status": "unknown", "reason": "source snapshot is unavailable"}
    if not snapshot.get("stable_during_analysis"):
        return {**base, "status": "unknown", "reason": "sources changed or could not be read during analysis"}
    if root is None:
        return {**base, "status": "unchecked", "reason": "source root was not supplied"}
    try:
        root = ensure_repository_root(root)
        config = AnalysisConfig(**document["meta"]["settings"])
        config.validate()
        if snapshot["settings_sha256"] != canonical_sha256(config.to_dict()):
            return {**base, "status": "unknown", "reason": "analysis settings differ from the source snapshot"}
        current = capture_sources(root, config)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**base, "status": "unknown", "reason": str(exc)}
    if current["read_errors"] or current["discovery_errors"] or current.get("context_read_errors"):
        return {**base, "status": "unknown", "reason": "some selected sources could not be read"}
    old = {**snapshot["files"], **snapshot.get("context_files", {})}
    new = {**current["files"], **current.get("context_files", {})}
    base["typescript_context_verified"] = "context_files" in snapshot
    changes = {
        "added": sorted(new.keys() - old.keys()),
        "deleted": sorted(old.keys() - new.keys()),
        "modified": sorted(path for path in old.keys() & new.keys() if old[path] != new[path]),
    }
    # Bound the response even when an entire repository has been replaced.
    counts = {name: len(paths) for name, paths in changes.items()}
    return {
        **base,
        "status": "stale" if any(counts.values()) else "current",
        "changes": {name: paths[:100] for name, paths in changes.items()},
        "change_counts": counts,
        "changes_truncated": any(count > 100 for count in counts.values()),
    }
