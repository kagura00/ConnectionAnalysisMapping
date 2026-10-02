"""One-command investigation with a source-verified cache outside the target."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .analyzer import analyze_repository
from .config import AnalysisConfig, discover_source_files, ensure_repository_root
from .contract import canonical_sha256, validate_document
from .evidence import check_freshness
from .investigation import build_investigation, encode_packet, repository_path, select_targets, write_packet_stdout
from .language_registry import language_for_path
from .output_profile import (
    PathSetting,
    absolute_path,
    configured_grammar_cache,
    current_grammar_cache,
    derived_path,
    preflight_destinations,
    preflight_profile_layout,
    report_paths,
)
from .query import GraphQuery


def _fingerprint() -> str:
    digest = hashlib.sha256(platform.python_version().encode())
    if getattr(sys, "frozen", False):
        with Path(sys.executable).open("rb") as executable:
            for chunk in iter(lambda: executable.read(1024 * 1024), b""):
                digest.update(chunk)
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for dependency in ("tree-sitter-language-pack", "tree-sitter-vbnet", "sqlglot"):
        try:
            version = importlib.metadata.version(dependency)
        except importlib.metadata.PackageNotFoundError:
            version = "unavailable"
        digest.update(f"{dependency}:{version}".encode())
    return digest.hexdigest()


def configuration(root: Path, path: Path | None, language: str | None, include_tests: bool | None) -> AnalysisConfig:
    local = root / ".connection-map/config.toml"
    if path and language:
        raise ValueError("use either config or language, not both")
    selected = path or (local if not language and local.is_file() else None)
    if selected:
        config = AnalysisConfig.from_toml(selected)
        if include_tests is not None:
            config.include_tests = include_tests
        return config
    tests = include_tests if include_tests is not None else True
    if language:
        return AnalysisConfig(language=language, include_tests=tests)
    discovery = AnalysisConfig(language="all", include_tests=False)
    files, _ = discover_source_files(root, discovery)
    if not files:
        files, _ = discover_source_files(root, AnalysisConfig(language="all", include_tests=tests))
    # Resolve shared header suffixes using the concrete implementation files.
    languages = {language_for_path(p) for p in files if p.suffix.lower() not in {".h", ".inc"}} - {None}
    for file in files:
        detected = language_for_path(file, sorted(languages) or None)
        if detected:
            languages.add(detected)
    if not languages:
        raise ValueError("no supported source files found; specify language/configuration")
    return AnalysisConfig(language="mixed", languages=sorted(languages), include_tests=tests)


def _atomic_json(path: Path, value: dict, *, strict_root: Path | None = None) -> None:
    if strict_root is not None:
        preflight_destinations(strict_root, [("investigation cache file", path, "file")])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".analysis-{os.urandom(16).hex()}.tmp")
    if strict_root is not None:
        preflight_destinations(
            strict_root,
            [("investigation cache file", path, "file"), ("investigation cache temporary", temporary, "file")],
        )
    try:
        with temporary.open("xb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_analysis(
    root: Path,
    config: AnalysisConfig,
    cache_dir: Path | None,
    *,
    refresh: bool = False,
    strict_root: Path | None = None,
) -> tuple[dict, dict]:
    requested_cache = cache_dir or Path(tempfile.gettempdir()) / "connection-map-investigations"
    if strict_root is not None:
        preflight_destinations(
            strict_root,
            [("investigation cache", requested_cache, "directory")],
            storage_roots=[("investigation cache", requested_cache)],
        )
        cache = absolute_path(requested_cache)
    else:
        cache = requested_cache.resolve()
    if cache.is_relative_to(root):
        raise ValueError("cache directory must be outside the target repository")
    identity = {"root": str(root), "settings": config.to_dict(), "analyzer_fingerprint": _fingerprint()}
    key = canonical_sha256(identity)
    directory = cache / key
    path = directory / "analysis.json"
    metadata = directory / "cache.json"
    if strict_root is not None:
        preflight_destinations(
            strict_root,
            [
                ("investigation cache", cache, "directory"),
                ("investigation cache identity", directory, "directory"),
                ("investigation cache analysis", path, "file"),
                ("investigation cache metadata", metadata, "file"),
            ],
            storage_roots=[("investigation cache", cache)],
        )
    reason = "explicit_refresh" if refresh else "cache_unavailable"
    if not refresh and path.is_file() and metadata.is_file():
        try:
            receipt = json.loads(metadata.read_text(encoding="utf-8"))
            document = json.loads(path.read_text(encoding="utf-8"))
            validate_document(document)
            if receipt.get("identity") != identity or receipt.get("sha256") != canonical_sha256(document):
                raise ValueError("cache identity/integrity mismatch")
            if document["meta"]["settings"] != config.to_dict():
                raise ValueError("cache settings mismatch")
            freshness = check_freshness(document, root)
            unavailable = any(d["code"] == "parser_unavailable" for d in document["diagnostics"])
            if freshness["status"] == "current" and not unavailable:
                return document, {
                    "cache": "reused",
                    "reason": "verified_current_sources",
                    "repository_root": str(root),
                    "languages": list(config.active_languages()),
                }
            reason = "parser_unavailable" if unavailable else "source_" + freshness["status"]
        except (OSError, ValueError, KeyError, TypeError):
            reason = "invalid_cache"
    document = analyze_repository(root, config, deterministic=True)
    if check_freshness(document, root)["status"] != "current":
        raise ValueError("sources changed or could not be verified during analysis; retry after edits finish")
    _atomic_json(path, document, strict_root=strict_root)
    _atomic_json(metadata, {"identity": identity, "sha256": canonical_sha256(document)}, strict_root=strict_root)
    return document, {
        "cache": "rebuilt",
        "reason": reason,
        "repository_root": str(root),
        "languages": list(config.active_languages()),
    }


def _git(root: Path, *args: str) -> bytes:
    command = [
        "git",
        "-c",
        f"safe.directory={root.as_posix()}",
        "-c",
        "color.ui=false",
        "-c",
        "core.quotePath=false",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(root),
    ]
    # --no-textconv disables diff drivers, but Git may still run clean/process
    # filters when reading the working tree. Disable each declared filter.
    filters = subprocess.run(
        [*command, "config", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
        capture_output=True,
        check=False,
    )
    if filters.returncode not in {0, 1}:
        raise ValueError("Git filter configuration could not be checked")
    families = {
        item.split(b"\n", 1)[0].rsplit(b".", 1)[0].decode("utf-8") for item in filters.stdout.split(b"\0") if item
    }
    overrides = []
    for family in sorted(families):
        for setting in ("clean=", "smudge=", "process=", "required=false"):
            overrides.extend(["-c", family + "." + setting])
    result = subprocess.run([*command, *overrides, *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError("Git diff is unavailable: " + result.stderr.decode("utf-8", errors="replace")[:300])
    return result.stdout


def changed_targets(query: GraphQuery, root: Path, base: str) -> tuple[list[str], list[dict], dict[str, int]]:
    if Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise ValueError("changed-file investigation requires the Git repository root")
    revision = _git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
    names = [
        os.fsdecode(n)
        for n in _git(
            root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "--no-renames", revision, "--"
        ).split(b"\0")
        if n
    ]
    untracked = [
        os.fsdecode(n) for n in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if n
    ]
    names = sorted(set(names + untracked))
    if len(names) > 200:
        raise ValueError("more than 200 changed files; narrow the change or investigate individual files")
    targets, changes, anchors = [], [], {}
    for name in names:
        path = repository_path(root, name)
        ranges = []
        removed_lines = 0
        if name not in untracked:
            diff = _git(
                root,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--unified=0",
                "--no-renames",
                revision,
                "--",
                name,
            ).decode("utf-8", errors="replace")
            for match in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
                old_count = int(match[2]) if match[2] is not None else 1
                new_count = int(match[4]) if match[4] is not None else 1
                removed_lines += old_count
                if new_count:
                    ranges.append((int(match[3]), int(match[3]) + new_count - 1))
        else:
            ranges = [(1, 2**31 - 1)]
        nodes = [
            n
            for n in query.nodes.values()
            if n.get("file") == name and n.get("span") and n["kind"] not in {"external", "unknown", "lambda", "module"}
        ]
        selected = [
            n for n in nodes if any(n["span"]["start_line"] <= hi and n["span"]["end_line"] >= lo for lo, hi in ranges)
        ]
        # A change wholly inside a method should not select its enclosing
        # class as a second focus. Header/outer-body changes still select it.
        selected = [
            n
            for n in selected
            if not all(
                any(
                    other["id"] != n["id"]
                    and (other["span"]["start_line"], other["span"]["end_line"])
                    != (n["span"]["start_line"], n["span"]["end_line"])
                    and n["span"]["start_line"] <= other["span"]["start_line"] <= max(lo, n["span"]["start_line"])
                    and min(hi, n["span"]["end_line"]) <= other["span"]["end_line"] <= n["span"]["end_line"]
                    for other in selected
                )
                for lo, hi in ranges
                if n["span"]["start_line"] <= hi and n["span"]["end_line"] >= lo
            )
        ]
        if not selected and nodes and path.is_file() and removed_lines:
            # A deleted function cannot be reconstructed from the current graph.
            # Keep that gap explicit instead of guessing the nearest function.
            status = "removed_regions_unmapped"
        else:
            status = "mapped" if selected else "deleted" if not path.exists() else "unmapped"
        changes.append(
            {
                "file": name,
                "status": status,
                "removed_lines": removed_lines,
                "new_ranges": [[lo, hi] for lo, hi in ranges[:20]],
                "range_count": len(ranges),
                "targets": len(selected),
            }
        )
        for node in sorted(selected, key=lambda n: (n["span"]["start_line"], n["id"])):
            targets.append(node["id"])
            anchors[node["id"]] = next(
                max(node["span"]["start_line"], lo)
                for lo, hi in ranges
                if node["span"]["start_line"] <= hi and node["span"]["end_line"] >= lo
            )
    config = AnalysisConfig(**query.document["meta"]["settings"])
    groups: dict[str, list[str]] = {}
    for target in dict.fromkeys(targets):
        groups.setdefault(query.nodes[target]["file"], []).append(target)
    files = sorted(groups, key=lambda f: (config.matches(f, config.test_patterns), f))
    spread = []
    while any(groups.values()):
        for file in files:
            if groups[file]:
                spread.append(groups[file].pop(0))
    return spread, changes, anchors


def run_investigate(args) -> int:
    root = ensure_repository_root(args.root)
    external_dir = args.external_dir
    strict_profile = external_dir is not None
    if args.cache_dir is not None:
        cache_setting = PathSetting(args.cache_dir, "--cache-dir")
    elif strict_profile:
        cache_setting = PathSetting(derived_path(external_dir, "investigation-cache"), "--external-dir")
    else:
        cache_setting = PathSetting(Path(tempfile.gettempdir()) / "connection-map-investigations", "OS temporary default")
    if args.grammar_cache is not None:
        grammar_setting = PathSetting(args.grammar_cache, "--grammar-cache")
    elif strict_profile:
        grammar_setting = PathSetting(derived_path(external_dir, "grammar-cache"), "--external-dir")
    else:
        grammar_setting = current_grammar_cache()
    output_setting = PathSetting(args.output, "--output") if args.output is not None else PathSetting(None, "default stdout")

    if strict_profile:
        destinations = [
            ("external profile", external_dir, "directory"),
            ("investigation cache", cache_setting.path, "directory"),
        ]
        storage_roots = [("investigation cache", cache_setting.path)]
        if grammar_setting.path is not None:
            destinations.append(("grammar cache", grammar_setting.path, "directory"))
            storage_roots.append(("grammar cache", grammar_setting.path))
        if args.output is not None:
            destinations.append(("investigation packet", args.output, "file"))
        preflight_profile_layout(external_dir, storage_roots, args.output)
        preflight_destinations(root, destinations, storage_roots=storage_roots)
    elif args.grammar_cache is not None:
        preflight_destinations(
            root,
            [("grammar cache", grammar_setting.path, "directory")],
            storage_roots=[("grammar cache", grammar_setting.path)],
        )

    if strict_profile or args.cache_dir is not None or args.grammar_cache is not None or args.output is not None:
        report_paths(
            [
                ("external profile", PathSetting(external_dir, "--external-dir") if strict_profile else None),
                ("investigation cache", cache_setting),
                ("investigation packet", output_setting),
                ("grammar cache", grammar_setting),
            ]
        )
    grammar_redirect = grammar_setting.path if args.grammar_cache is not None or strict_profile else None
    with configured_grammar_cache(absolute_path(grammar_redirect) if grammar_redirect is not None else None):
        return _run_investigate_with_paths(
            args,
            root,
            cache_setting.path,
            strict_root=root if strict_profile else None,
        )


def _run_investigate_with_paths(
    args, root: Path, cache_dir: Path, *, strict_root: Path | None = None
) -> int:
    if args.changed and (args.node or args.symbol or args.file or args.line):
        raise ValueError("changed cannot be combined with node/symbol/file/line")
    if not args.changed and not (args.node or args.symbol or args.file):
        raise ValueError("provide node, symbol, file/line, or changed")
    if args.node and (args.symbol or args.file or args.line):
        raise ValueError("node cannot be combined with symbol/file/line")
    if args.line is not None and (args.line < 1 or not args.file):
        raise ValueError("a positive line number requires --file")
    if not 2048 <= args.max_chars <= 200_000 or not 1 <= args.snippet_lines <= 80:
        raise ValueError("max_chars must be 2048..200000 and snippet_lines 1..80")
    if not 1 <= args.max_targets <= 20:
        raise ValueError("max_targets must be between 1 and 20")
    for name, low, high in (("depth", 0, 5), ("max_nodes", 1, 500), ("max_edges", 1, 1000)):
        if not low <= getattr(args, name) <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
    if args.file:
        repository_path(root, args.file)
    config = configuration(root, args.config, args.language, args.include_tests)
    document, workflow = prepare_analysis(root, config, cache_dir, refresh=args.refresh, strict_root=strict_root)
    query = GraphQuery(document)
    changes, anchors = [], {}
    if args.changed:
        targets, changes, anchors = changed_targets(query, root, args.base)
        if not targets:
            raise ValueError(
                "no changed declarations mapped; deleted/unsupported files require source review: "
                + ", ".join(c["file"] for c in changes[:8])
            )
        selection = {
            "kind": "git_diff",
            "base": args.base,
            "unmapped_files": sum(c["status"] != "mapped" for c in changes),
            "removed_regions_require_review": any(c["removed_lines"] for c in changes),
        }
    else:
        file = None
        if args.file:
            file = repository_path(root, args.file).relative_to(root).as_posix()
        targets = select_targets(query, node=args.node, symbol=args.symbol, file=file, line=args.line)
        anchors = {t: args.line for t in targets} if args.line else {}
        selection = {
            "kind": "node" if args.node else "symbol" if args.symbol else "location",
            "value": args.node or args.symbol or file,
            "file": file,
            "line": args.line,
        }
    packet = build_investigation(
        query,
        targets,
        root=root,
        selection=selection,
        anchors=anchors,
        changes=changes,
        workflow=workflow,
        direction=args.direction,
        relations=args.relation,
        resolution=args.resolution,
        depth=args.depth,
        max_nodes=args.max_nodes,
        max_edges=args.max_edges,
        max_chars=args.max_chars,
        max_targets=args.max_targets,
        snippets=not args.no_snippets,
        snippet_lines=args.snippet_lines,
    )
    payload = encode_packet(packet)
    if args.output is None:
        write_packet_stdout(payload)
    else:
        if strict_root is not None:
            preflight_destinations(strict_root, [("investigation packet", args.output, "file")])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8", newline="\n")
        print(f"wrote investigation {args.output} ({len(payload)} characters)")
    return 0
