"""Command-line entry point for repository analysis and local serving."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from .analyzer import analyze_repository
from .bundle import (
    BundleError,
    normalize_search_text,
    search_bundle,
    split_analysis_file,
    validate_bundle,
)
from .config import AnalysisConfig, ensure_repository_root
from .contract import ContractError, validate_document
from .distribution import install_core, rollback_core
from .manual import ManualOverlayError, load_manual, merge_manual, validate_manual
from .report import summarize_document
from .scaffold import initialize_target
from .server import serve_analysis, serve_workspace
from .workspace import Workspace, WorkspaceError, workspace_from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connection-map",
        description="Analyze repository relationships and emit a Contract v1 graph.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="analyze a repository with the configured language analyzer")
    analyze.add_argument("--root", type=Path, default=Path("."), help="repository root (default: current directory)")
    analyze.add_argument("--config", type=Path, help="TOML configuration file")
    analyze.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output JSON path (central workspace mode uses its repository data path by default)",
    )
    analyze.add_argument("--workspace", type=Path, help="central workspace data directory; otherwise use CONNECTION_MAP_WORKSPACE")
    analyze.add_argument("--deterministic", action="store_true", help="omit time-varying metadata")
    analyze.add_argument(
        "--allow-empty",
        action="store_true",
        help="allow an analysis with no nodes (useful for an intentionally empty repository)",
    )
    analyze.add_argument(
        "--include-tests",
        dest="include_tests",
        action="store_true",
        default=None,
        help="include paths matching test_patterns",
    )
    analyze.add_argument(
        "--exclude-tests",
        dest="include_tests",
        action="store_false",
        help="exclude paths matching test_patterns",
    )

    validate = commands.add_parser("validate", help="validate a Contract v1 JSON graph")
    validate.add_argument("input", type=Path, help="analysis JSON path")

    validate_manual_parser = commands.add_parser("validate-manual", help="validate a manual-v1 overlay")
    validate_manual_parser.add_argument("--input", type=Path, required=True, help="manual overlay JSON path")
    validate_manual_parser.add_argument("--analysis", type=Path, help="optional base analysis for reference checks")

    merge = commands.add_parser("merge", help="apply a manual-v1 overlay to an analysis JSON")
    merge.add_argument("--analysis", type=Path, required=True, help="base analysis JSON path")
    merge.add_argument("--manual", type=Path, required=True, help="manual overlay JSON path")
    merge.add_argument("--output", type=Path, required=True, help="derived graph JSON path")
    merge.add_argument(
        "--ignore-analysis-hash",
        action="store_true",
        help="allow an overlay hash mismatch when reapplying intentionally",
    )

    split = commands.add_parser("split", help="split a graph into a static bundle")
    split.add_argument("input", type=Path, help="analysis JSON path")
    split.add_argument("--output", type=Path, required=True, help="bundle directory")
    split.add_argument("--node-chunk-size", type=int, default=2000)
    split.add_argument("--edge-chunk-size", type=int, default=5000)
    split.add_argument("--diagnostic-chunk-size", type=int, default=2000)
    split.add_argument("--search-chunk-size", type=int, default=5000)
    split.add_argument("--force", action="store_true", help="update an existing non-empty bundle directory")

    validate_bundle_parser = commands.add_parser("validate-bundle", help="validate a static graph bundle")
    validate_bundle_parser.add_argument("input", type=Path, help="bundle directory")

    search = commands.add_parser("search", help="search an analysis JSON or graph bundle")
    search.add_argument("input", type=Path, help="analysis JSON path or bundle directory")
    search.add_argument("query", help="case-insensitive substring query")
    search.add_argument("--limit", type=int, default=80)

    report = commands.add_parser("report", help="summarize graph quality and resolution coverage")
    report.add_argument("--input", type=Path, required=True, help="analysis JSON path")
    report.add_argument("--output", type=Path, help="optional report JSON path")

    init = commands.add_parser("init", help="create a repository-local analyzer scaffold")
    init.add_argument("--root", type=Path, default=Path("."), help="target repository root")
    init.add_argument("--install-dir", default=".connection-map", help="single directory to create inside the target")
    init.add_argument("--force", action="store_true", help="overwrite files generated by init")

    install_core_parser = commands.add_parser(
        "install-core",
        help="install or update .connection-map/core from a source archive",
    )
    install_core_parser.add_argument("--root", type=Path, default=Path("."), help="target repository root")
    install_core_parser.add_argument("--install-dir", default=".connection-map", help="target installation directory")
    install_core_parser.add_argument("--archive", type=Path, required=True, help="source archive (.tar/.tar.gz)")

    rollback = commands.add_parser("rollback-core", help="restore a previous .connection-map/core backup")
    rollback.add_argument("--root", type=Path, default=Path("."), help="target repository root")
    rollback.add_argument("--install-dir", default=".connection-map", help="target installation directory")
    rollback.add_argument("--backup", help="backup directory name; defaults to the newest backup")

    serve = commands.add_parser("serve", help="serve the static viewer on localhost")
    serve.add_argument("--input", type=Path, help="analysis JSON path")
    serve.add_argument("--bundle", type=Path, help="optional static graph bundle directory")
    serve.add_argument("--layout", type=Path, help="optional layout JSON path")
    serve.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="bind port (default: 8765)")
    serve.add_argument("--workspace", type=Path, help="central workspace data directory; otherwise use CONNECTION_MAP_WORKSPACE")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            return _run_analyze(args)
        if args.command == "validate":
            return _run_validate(args)
        if args.command == "validate-manual":
            return _run_validate_manual(args)
        if args.command == "merge":
            return _run_merge(args)
        if args.command == "split":
            return _run_split(args)
        if args.command == "validate-bundle":
            return _run_validate_bundle(args)
        if args.command == "search":
            return _run_search(args)
        if args.command == "report":
            return _run_report(args)
        if args.command == "init":
            return _run_init(args)
        if args.command == "install-core":
            return _run_install_core(args)
        if args.command == "rollback-core":
            return _run_rollback_core(args)
        if args.command == "serve":
            return _run_serve(args)
    except (BundleError, ContractError, ManualOverlayError, OSError, ValueError, WorkspaceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


def _run_analyze(args: argparse.Namespace) -> int:
    root = ensure_repository_root(args.root)
    if args.config is not None and not args.config.is_file():
        raise FileNotFoundError(args.config)
    workspace = Workspace(args.workspace) if args.workspace is not None else workspace_from_env()
    record = None
    registered_new = False
    previous_active_repository_id = None
    previous_records = []
    previous_config_bytes: bytes | None = None
    config_destination = None
    published = False
    if workspace is not None:
        previous_active_repository_id = workspace.load()["active_repository_id"]
        previous_records = workspace.records()
        configured_path = args.config.resolve() if args.config else None
        if configured_path is None:
            local_config = root / ".connection-map" / "config.toml"
            configured_path = local_config if local_config.is_file() else None
        existing = workspace.find(root)
        existing_config = workspace.path_for(existing, existing.config_path) if existing is not None else None
        if existing_config is not None and existing_config.is_file():
            previous_config_bytes = existing_config.read_bytes()
        if configured_path is not None:
            # Parse user-supplied configuration before touching the catalog; a
            # typo must not leave an empty or rebound repository record.
            config = AnalysisConfig.from_toml(configured_path)
        elif existing_config is not None and existing_config.is_file():
            config = AnalysisConfig.from_toml(existing_config)
        else:
            config = AnalysisConfig.from_toml(None)
    else:
        config = AnalysisConfig.from_toml(args.config.resolve() if args.config else None)
    if args.include_tests is not None:
        config.include_tests = args.include_tests
    config.validate()
    document = analyze_repository(root, config, deterministic=args.deterministic)
    if not document["nodes"] and not args.allow_empty:
        raise ValueError(
            "analysis produced no nodes; check --root, --config, include/exclude patterns, "
            "or pass --allow-empty intentionally"
        )
    if workspace is not None:
        try:
            record = workspace.register(root)
            registered_new = existing is None
            config_destination = workspace.path_for(record, record.config_path)
            if configured_path is not None:
                workspace.copy_config(record, configured_path)
            elif existing_config is None or not existing_config.is_file():
                workspace.copy_config(record, None)
            if args.include_tests is not None:
                config_destination.write_text(config.to_toml(), encoding="utf-8")
            central_output = workspace.path_for(record, record.analysis_path)
            requested_output = args.output if args.output is not None and args.output.is_absolute() else root / args.output if args.output is not None else central_output
            output = requested_output
            record = workspace.publish_analysis(record, document)
            published = True
            if output.resolve() != central_output.resolve():
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(central_output, output)
        except BaseException:
            if not published:
                try:
                    if config_destination is not None:
                        if previous_config_bytes is None:
                            config_destination.unlink(missing_ok=True)
                        else:
                            config_destination.write_bytes(previous_config_bytes)
                    if record is not None:
                        if registered_new:
                            workspace.remove(
                                record.repository_id,
                                active_repository_id=previous_active_repository_id,
                            )
                        else:
                            workspace.restore_registration(previous_records, previous_active_repository_id)
                except Exception:
                    # Preserve the original analysis/publish error.  The
                    # registry can be repaired on the next workspace load.
                    pass
            raise
    else:
        output = args.output if args.output is not None and args.output.is_absolute() else root / args.output if args.output is not None else root / ".connection-map/snapshots/analysis.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(output)
    counts = document["meta"]["counts"]
    print(
        f"wrote {output} ({counts['nodes']} nodes, {counts['edges']} edges, "
        f"{counts['diagnostics']} diagnostics)"
    )
    return 0


def _run_validate(args: argparse.Namespace) -> int:
    document = json.loads(args.input.read_text(encoding="utf-8"))
    validate_document(document)
    print(f"valid Contract v1 graph: {args.input}")
    return 0


def _load_analysis(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid analysis JSON: {path}") from exc
    validate_document(document)
    return document


def _run_validate_manual(args: argparse.Namespace) -> int:
    manual = load_manual(args.input)
    if args.analysis is not None:
        analysis = _load_analysis(args.analysis)
        validate_manual(
            manual,
            node_ids={node["id"] for node in analysis["nodes"]},
            edge_ids={edge["id"] for edge in analysis["edges"]},
        )
    print(f"valid manual v1 overlay: {args.input}")
    return 0


def _run_merge(args: argparse.Namespace) -> int:
    analysis = _load_analysis(args.analysis)
    manual = load_manual(args.manual)
    merged = merge_manual(analysis, manual, ignore_analysis_hash=args.ignore_analysis_hash)
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts = merged["meta"]["counts"]
    print(f"wrote {output} ({counts['nodes']} nodes, {counts['edges']} edges, {counts['diagnostics']} diagnostics)")
    return 0


def _run_split(args: argparse.Namespace) -> int:
    index = split_analysis_file(
        args.input,
        args.output,
        node_chunk_size=args.node_chunk_size,
        edge_chunk_size=args.edge_chunk_size,
        diagnostic_chunk_size=args.diagnostic_chunk_size,
        search_chunk_size=args.search_chunk_size,
        force=args.force,
    )
    print(
        f"wrote bundle {args.output} ({index['counts']['nodes']} nodes, "
        f"{index['counts']['edges']} edges, {index['counts']['diagnostics']} diagnostics)"
    )
    return 0


def _run_validate_bundle(args: argparse.Namespace) -> int:
    index = validate_bundle(args.input)
    print(
        f"valid graph bundle: {args.input} ({index['counts']['nodes']} nodes, "
        f"{index['counts']['edges']} edges, {index['counts']['diagnostics']} diagnostics)"
    )
    return 0


def _search_document(document: dict, query: str, limit: int) -> list[dict[str, str]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("search limit must be a positive integer")
    normalized = normalize_search_text(query.strip())
    if not normalized.strip():
        return []
    found: list[dict[str, str]] = []
    for node in document["nodes"]:
        values = {field: str(node.get(field) or "") for field in ("id", "qualified_name", "display_name", "file")}
        if normalized in normalize_search_text("\n".join(values.values())):
            found.append({"id": node["id"], "kind": node["kind"], **values})
    return sorted(found, key=lambda item: item["id"])[:limit]


def _run_search(args: argparse.Namespace) -> int:
    if args.input.is_dir():
        matches = search_bundle(args.input, args.query, limit=args.limit)
    else:
        matches = _search_document(_load_analysis(args.input), args.query, args.limit)
    print(json.dumps({"query": args.query, "matches": matches}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _run_report(args: argparse.Namespace) -> int:
    document = _load_analysis(args.input)
    report = summarize_document(document)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"wrote report {args.output}")
    return 0


def _run_init(args: argparse.Namespace) -> int:
    result = initialize_target(args.root, args.install_dir, force=args.force)
    for path in result.created:
        print(f"created {path}")
    for path in result.skipped:
        print(f"kept {path}")
    return 0


def _run_install_core(args: argparse.Namespace) -> int:
    result = install_core(args.root, args.archive, args.install_dir)
    print(f"installed core {result.version} at {result.core_path}")
    print(f"archive sha256: {result.sha256}")
    if result.backup_path is not None:
        print(f"previous core backed up at {result.backup_path}")
    return 0


def _run_rollback_core(args: argparse.Namespace) -> int:
    result = rollback_core(args.root, args.install_dir, args.backup)
    print(f"restored core backup {result.restored_backup_name} at {result.core_path}")
    if result.saved_current_path is not None:
        print(f"previous active core saved at {result.saved_current_path}")
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    if not 0 <= args.port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    workspace = Workspace(args.workspace) if args.workspace is not None else workspace_from_env()
    if args.input is None and workspace is not None:
        serve_workspace(workspace, host=args.host, port=args.port)
        return 0
    if args.input is None:
        raise ValueError("serve requires --input, or CONNECTION_MAP_WORKSPACE/--workspace for central mode")
    serve_analysis(args.input, layout_path=args.layout, bundle_path=args.bundle, host=args.host, port=args.port)
    return 0
