from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from connection_map.analyzer import analyze_repository
from connection_map.cli import main
from connection_map.config import AnalysisConfig
from connection_map.contract import canonical_sha256
from connection_map.evidence import check_freshness, coverage_summary
from connection_map.investigate_cli import changed_targets, configuration, prepare_analysis
from connection_map.investigation import build_investigation, encode_packet, repository_path, select_targets
from connection_map.query import GraphQuery
from connection_map.server import AnalysisRequestHandler, WorkspaceRequestHandler
from connection_map.workspace import Workspace

DEFINITIONS = """\
class Service:
    def ping(self):
        return 'pong'

def factory() -> Service:
    return Service()

"""


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def investigation_graph(tmp_path):
    root = tmp_path / "repo"
    write(
        root / "app.py",
        DEFINITIONS + "def run():\n    client = factory()\n    return client.ping()\n"
        "\ndef dynamic(client):\n    return client.ping()\n",
    )
    write(
        root / "tests/test_app.py",
        "from app import factory\n\ndef test_ping():\n    client = factory()\n    assert client.ping() == 'pong'\n",
    )
    doc = analyze_repository(root, AnalysisConfig(include_tests=True), deterministic=True)
    query = GraphQuery(doc)
    ids = {n["qualified_name"]: n["id"] for n in doc["nodes"]}
    return root, query, ids


def test_single_assignment_follows_explicit_return_type_and_records_its_origin(investigation_graph):
    _, query, ids = investigation_graph
    callers = [
        e for e in query.document["edges"] if e["relation_type"] == "calls" and e["target_id"] == ids["Service.ping"]
    ]
    assert {query.nodes[e["source_id"]]["qualified_name"] for e in callers} == {"run", "test_ping"}
    for edge in callers:
        evidence = edge["detail"]["resolution_evidence"]
        assert evidence["strategy"] == "return_annotation"
        assert evidence["return_annotation"] == "Service"
        assert evidence["assignment"]["name"] == "client"
        assert evidence["assignment"]["span"]["start_line"] < edge["source_span"]["start_line"]


@pytest.mark.parametrize(
    "body",
    [
        "client = factory()\nclient = unknown()\nclient.ping()",
        "client = factory()\nif condition:\n    client = unknown()\nclient.ping()",
        "client.ping()\nclient = factory()",
        "client = factory()\ndel client\nclient.ping()",
        "client = factory()\nclient.ping = unknown\nclient.ping()",
        "client = factory()\nsetattr(client, 'ping', unknown)\nclient.ping()",
        "client = factory()\nalias = client\nalias.ping = unknown\nclient.ping()",
        "client = factory()\nalias: object = client\nalias.ping = unknown\nclient.ping()",
        "client = factory()\nfrom other import client\nclient.ping()",
        "global client\nclient = factory()\nclient.ping()",
        "client = unknown()\nclient.ping()",
        "client = factory()\nfactory = unknown\nclient.ping()",
    ],
)
def test_uncertain_or_mutated_local_receiver_is_not_promoted_to_a_method(tmp_path, body):
    write(tmp_path / "app.py", DEFINITIONS + "def run():\n" + textwrap.indent(body, "    ") + "\n")
    query = GraphQuery(analyze_repository(tmp_path, deterministic=True))
    method = next(n for n in query.nodes.values() if n["qualified_name"] == "Service.ping")
    assert not any(e["target_id"] == method["id"] and e["relation_type"] == "calls" for e in query.document["edges"])


@pytest.mark.parametrize(
    "prefix, signature",
    [
        ("", "def run(client):"),
        ("async ", "def run():"),
    ],
)
def test_parameters_and_unawaited_async_factory_do_not_gain_an_instance_type(tmp_path, prefix, signature):
    definitions = DEFINITIONS.replace("def factory", prefix + "def factory")
    write(tmp_path / "app.py", definitions + signature + "\n    client = factory()\n    client.ping()\n")
    query = GraphQuery(analyze_repository(tmp_path, deterministic=True))
    method = select_targets(query, symbol="Service.ping")[0]
    assert not any(e["target_id"] == method and e["relation_type"] == "calls" for e in query.document["edges"])


def test_packet_contains_verified_code_callers_test_candidates_and_uncertainty(investigation_graph):
    root, query, ids = investigation_graph
    packet = build_investigation(query, [ids["Service.ping"]], root=root)
    payload = encode_packet(packet)
    assert len(payload) == packet["budget"]["used_chars"] <= 12000
    assert packet["freshness"]["status"] == "current"
    assert packet["coverage"]["tests_included"] is True
    assert packet["coverage"]["relationships_complete"] is False
    assert packet["possible_callers"][0]["source"] == "dynamic"
    assert packet["possible_callers"][0]["status"] == "candidate"
    assert packet["possible_callers"][0]["basis"] == "unresolved_call_name"
    assert any(t["symbol"] == "test_ping" and t["status"] == "resolved" for t in packet["related_tests"])
    source = next(s for s in packet["sources"] if s["node_id"] == ids["Service.ping"])
    assert source["sha256"] == hashlib.sha256((root / "app.py").read_bytes()).hexdigest()
    assert "return 'pong'" in source["ranges"][0]["text"]
    assert not any(
        e["expression"].startswith("client.ping") and e["file"] == "app.py" and e["line"] == 13 for e in packet["edges"]
    )  # Candidate never becomes a resolved graph edge.
    for options in ({"direction": "out"}, {"relations": ["imports"]}, {"resolution": "resolved"}):
        assert build_investigation(query, [ids["Service.ping"]], root=root, **options)["possible_callers"] == []


def test_candidate_call_name_does_not_match_string_arguments(tmp_path):
    write(
        tmp_path / "app.py",
        DEFINITIONS + "def log_only():\n    logger('client.ping()')\n\ndef dynamic(client):\n    client.ping()\n",
    )
    query = GraphQuery(analyze_repository(tmp_path))
    target = select_targets(query, symbol="Service.ping")[0]
    packet = build_investigation(query, [target], root=tmp_path)
    assert [c["source"] for c in packet["possible_callers"]] == ["dynamic"]


@pytest.mark.parametrize("budget", [3000, 6000, 12000])
def test_size_budget_keeps_valid_references_and_reports_whole_record_omissions(tmp_path, budget):
    write(
        tmp_path / "app.py",
        DEFINITIONS + "\n".join(f"def caller_{i}():\n    return factory().ping()\n" for i in range(40)),
    )
    query = GraphQuery(analyze_repository(tmp_path, deterministic=True))
    target = select_targets(query, symbol="Service.ping")[0]
    packet = build_investigation(query, [target], root=tmp_path, max_chars=budget)
    payload = encode_packet(packet)
    decoded = json.loads(payload)
    assert len(payload) == decoded["budget"]["used_chars"] <= budget
    assert decoded["budget"]["limited"]
    assert decoded["truncation"]["nodes"] > 0
    assert decoded["truncation"]["edges"] > 0
    refs = {n["ref"] for n in decoded["nodes"]}
    assert set(decoded["focus"]) <= refs
    assert all(e["from"] in refs and e["to"] in refs for e in decoded["edges"])
    assert decoded["coverage"]["relationships_complete"] is False
    assert decoded["counts"]["edges"] > len(decoded["edges"])


def test_selectors_prefer_enclosing_method_and_refuse_ambiguous_names(investigation_graph):
    root, query, ids = investigation_graph
    assert select_targets(query, file="app.py", line=3) == [ids["Service.ping"]]
    assert select_targets(query, file="app.py", symbol="ping") == [ids["Service.ping"]]
    other = copy.deepcopy(query.document)
    extra = copy.deepcopy(query.nodes[ids["Service.ping"]])
    extra.update(id="another", file="other.py", qualified_name="other.Service.ping")
    other["nodes"].append(extra)
    other["meta"]["counts"]["nodes"] += 1
    with pytest.raises(ValueError, match="ambiguous"):
        select_targets(GraphQuery(other), symbol="ping")
    assert repository_path(root, "app.py") == root / "app.py"
    for options in (
        {"line": 0, "file": "app.py"},
        {"line": 3},
        {"node": "missing"},
        {"node": ids["run"], "symbol": "run"},
        {"file": "missing.py"},
    ):
        with pytest.raises(ValueError):
            select_targets(query, **options)


@pytest.mark.parametrize("relative", ["../secret.py", "/secret.py", "D:/secret.py", ".git/config", "..\\secret.py"])
def test_excerpt_paths_cannot_escape_or_access_git(tmp_path, relative):
    with pytest.raises(ValueError):
        repository_path(tmp_path, relative)


def test_stale_and_old_artifacts_do_not_mix_current_code_with_old_relationships(investigation_graph):
    root, query, ids = investigation_graph
    write(root / "app.py", "def totally_different(): pass\n")
    packet = build_investigation(query, [ids["Service.ping"]], root=root)
    assert packet["freshness"]["status"] == "stale"
    assert not any(s.get("ranges") for s in packet["sources"])
    old = copy.deepcopy(query.document)
    old["meta"]["extensions"].pop("source_snapshot")
    packet = build_investigation(GraphQuery(old), [ids["Service.ping"]], root=root)
    assert packet["freshness"]["status"] == "unknown"
    assert not any(s.get("ranges") for s in packet["sources"])


def test_one_command_cli_selects_location_and_reuses_only_verified_cache(investigation_graph, tmp_path, capsys):
    root, _, _ = investigation_graph
    cache = tmp_path / "cache"
    args = ["investigate", "--root", str(root), "--file", "app.py", "--line", "3", "--cache-dir", str(cache)]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["workflow"]["cache"] == "rebuilt"
    assert first["workflow"]["languages"] == ["python"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["workflow"]["cache"] == "reused"
    write(root / "extra.py", "def new_function(): pass\n")
    assert main(args) == 0
    fresh = json.loads(capsys.readouterr().out)
    assert fresh["workflow"]["cache"] == "rebuilt"
    assert fresh["freshness"]["status"] == "current"
    assert fresh["coverage"]["selected_file_count"] == 3
    for path in cache.glob("*/cache.json"):
        path.write_text("{}", encoding="utf-8")
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["workflow"]["reason"] == "invalid_cache"
    assert main(args[:-1] + [str(root / "cache")]) == 2
    assert "outside" in capsys.readouterr().err


def test_cli_stdout_is_utf8_lf_and_meets_budget_with_non_ascii_source(tmp_path):
    root = tmp_path / "repo"
    write(root / "app.py", "def 日本語():\n    return '😀'\n")
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONIOENCODING": "ascii",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "connection_map",
            "investigate",
            "--root",
            str(root),
            "--file",
            "app.py",
            "--line",
            "2",
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        env=environment,
        capture_output=True,
        check=True,
    )
    text = result.stdout.decode("utf-8")
    packet = json.loads(text)
    assert "日本語" in text and "😀" in text
    assert "\r\n" not in text
    assert packet["budget"]["used_chars"] == len(text) <= 12000


def test_cache_identity_includes_root_settings_code_and_deleted_sources(investigation_graph, tmp_path, monkeypatch):
    root, _, _ = investigation_graph
    config = AnalysisConfig(include_tests=True)
    cache = tmp_path / "cache"
    prepare_analysis(root, config, cache)
    (root / "tests/test_app.py").unlink()
    document, workflow = prepare_analysis(root, config, cache)
    assert workflow["reason"] == "source_stale"
    assert not any(n.get("file") == "tests/test_app.py" for n in document["nodes"])
    monkeypatch.setattr("connection_map.investigate_cli._fingerprint", lambda: "new-engine")
    assert prepare_analysis(root, config, cache)[1]["cache"] == "rebuilt"
    second = tmp_path / "different"
    write(second / "app.py", DEFINITIONS)
    assert prepare_analysis(second, config, cache)[1]["cache"] == "rebuilt"
    assert prepare_analysis(root, AnalysisConfig(include_tests=False), cache)[1]["cache"] == "rebuilt"


def test_sources_changing_during_excerpt_read_never_receive_a_current_label(investigation_graph, monkeypatch):
    root, query, ids = investigation_graph
    original = check_freshness

    def change_after_verification(document, repository):
        result = original(document, repository)
        write(root / "app.py", "def changed(): pass\n")
        return result

    monkeypatch.setattr("connection_map.investigation.check_freshness", change_after_verification)
    packet = build_investigation(query, [ids["Service.ping"]], root=root)
    assert packet["freshness"]["status"] == "stale"
    assert packet["sources"] == []


def test_parser_unavailable_cache_retries_when_parser_becomes_available(investigation_graph, tmp_path, monkeypatch):
    root, _, _ = investigation_graph
    original = analyze_repository
    attempts = []

    def analyze(*args, **kwargs):
        doc = original(*args, **kwargs)
        attempts.append(True)
        if len(attempts) == 1:
            doc["diagnostics"].append(
                {
                    "code": "parser_unavailable",
                    "severity": "error",
                    "file": "app.py",
                    "span": None,
                    "message": "parser unavailable",
                }
            )
            doc["meta"]["counts"]["diagnostics"] += 1
        return doc

    monkeypatch.setattr("connection_map.investigate_cli.analyze_repository", analyze)
    config = AnalysisConfig(include_tests=True)
    cache = tmp_path / "cache"
    prepare_analysis(root, config, cache)
    assert prepare_analysis(root, config, cache)[1]["reason"] == "parser_unavailable"
    assert prepare_analysis(root, config, cache)[1]["cache"] == "reused"
    assert len(attempts) == 2


def test_frozen_application_cache_fingerprint_changes_with_the_executable(tmp_path, monkeypatch):
    from connection_map.investigate_cli import _fingerprint

    executable = tmp_path / "engine.exe"
    executable.write_bytes(b"version one")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    first = _fingerprint()
    executable.write_bytes(b"version two")
    assert _fingerprint() != first


def git(root, *args):
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=Investigation Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.autocrlf=false",
            "-C",
            str(root),
            *args,
        ],
        stderr=subprocess.PIPE,
    )


def test_git_diff_maps_staged_unstaged_untracked_and_reports_deleted_regions(tmp_path):
    root = tmp_path / "repo"
    write(root / "日本語 name.py", "def keep():\n    return 1\n\ndef removed():\n    return 2\n")
    git(root, "init")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    write(root / "日本語 name.py", "def keep():\n    return 3\n")
    git(root, "add", ".")
    write(root / "日本語 name.py", "def keep():\n    return 4\n")
    write(root / "untracked.py", "def added():\n    return 5\n")
    query = GraphQuery(analyze_repository(root, deterministic=True))
    targets, changes, anchors = changed_targets(query, root, "HEAD")
    assert {query.nodes[t]["display_name"] for t in targets} == {"keep", "added"}
    assert all(query.nodes[t]["span"]["start_line"] <= anchors[t] for t in targets)
    assert {c["file"] for c in changes} == {"日本語 name.py", "untracked.py"}
    assert next(c for c in changes if c["file"] == "日本語 name.py")["removed_lines"] > 0
    for base in ("missing-ref", "--help"):
        with pytest.raises(ValueError, match="Git diff"):
            changed_targets(query, root, base)
    (root / "日本語 name.py").unlink()
    targets, changes, _ = changed_targets(GraphQuery(analyze_repository(root)), root, "HEAD")
    assert next(c for c in changes if c["file"] == "日本語 name.py")["status"] == "deleted"
    assert {query.nodes[t]["display_name"] for t in targets} == {"added"}


def test_git_diff_never_runs_repository_filters_or_external_diff_commands(tmp_path):
    root = tmp_path / "repo"
    write(root / "app.py", "def run():\n    return 1\n")
    git(root, "init")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    marker = tmp_path / "filter-executed"
    helper = tmp_path / "filter.py"
    write(
        helper,
        f"import sys\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        "sys.stdout.write(sys.stdin.read())\n",
    )
    command = subprocess.list2cmdline([sys.executable, str(helper)])
    git(root, "config", "filter.observer.clean", command)
    git(root, "config", "filter.observer.process", command)
    git(root, "config", "filter.observer.required", "true")
    git(root, "config", "diff.external", command)
    git(root, "config", "core.fsmonitor", command)
    write(root / ".gitattributes", "*.py filter=observer\n")
    write(root / "app.py", "def run():\n    return 2\n")
    query = GraphQuery(analyze_repository(root))
    targets, _, _ = changed_targets(query, root, "HEAD")
    assert {query.nodes[t]["display_name"] for t in targets} == {"run"}
    assert not marker.exists()


def test_changed_cli_exports_current_declaration_and_separate_removal_warning(tmp_path, capsys):
    root = tmp_path / "repo"
    write(root / "app.py", "def run():\n    return 1\n")
    git(root, "init")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    write(root / "app.py", "def run():\n    return 2\n")
    args = ["investigate", "--root", str(root), "--changed", "--cache-dir", str(tmp_path / "cache")]
    assert main(args) == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["selection"]["kind"] == "git_diff"
    assert packet["selection"]["removed_regions_require_review"]
    assert packet["changes"][0]["status"] == "mapped"
    assert "return 2" in packet["sources"][0]["ranges"][0]["text"]
    assert main(args + ["--symbol", "run"]) == 2
    assert "combined" in capsys.readouterr().err


def test_large_diff_spreads_focus_over_files_and_keeps_tests_changes_and_changed_code(tmp_path, capsys):
    root = tmp_path / "repo"
    for index in range(8):
        functions = []
        for name in ("first", "second", "third"):
            functions.append(
                f"def {name}():\n" + "\n".join("    text = '" + "x" * 300 + "'" for _ in range(30)) + "\n    return 1\n"
            )
        write(root / f"file{index}.py", "\n".join(functions))
    write(root / "tests/test_app.py", "from file0 import first\n\ndef test_first():\n    return first()\n")
    git(root, "init")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    for index in range(8):
        path = root / f"file{index}.py"
        write(path, path.read_text(encoding="utf-8").replace("return 1", "return 2"))
    assert main(["investigate", "--root", str(root), "--changed", "--cache-dir", str(tmp_path / "cache")]) == 0
    text = capsys.readouterr().out
    packet = json.loads(text)
    assert len(text) <= 12000
    assert packet["counts"]["targets"] == 24
    assert packet["truncation"]["targets"] == 16
    assert {n["file"] for n in packet["nodes"] if n["ref"] in packet["focus"]} == {f"file{i}.py" for i in range(8)}
    assert packet["related_tests"][0]["symbol"] == "test_first"
    assert len(packet["changes"]) == 8
    assert any("return 2" in r["text"] for s in packet["sources"] for r in s.get("ranges", []))


def test_diff_anchors_use_each_declarations_actual_hunk_and_skip_containing_class(tmp_path):
    root = tmp_path / "repo"
    original = (
        "def first():\n    return 1\n\nclass Worker:\n    def second(self):\n" + "    \n" * 30 + "        return 1\n"
    )
    write(root / "app.py", original)
    git(root, "init")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    write(root / "app.py", original.replace("return 1", "return 2"))
    query = GraphQuery(analyze_repository(root))
    targets, _, anchors = changed_targets(query, root, "HEAD")
    assert {query.nodes[t]["qualified_name"] for t in targets} == {"first", "Worker.second"}
    assert {query.nodes[t]["qualified_name"]: line for t, line in anchors.items()} == {"first": 2, "Worker.second": 36}


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--line", "1"],
        ["--symbol", "run", "--max-chars", "1"],
        ["--file", "../secret.py"],
        ["--symbol", "run", "--depth", "9"],
    ],
)
def test_invalid_cli_options_fail_before_analysis_or_cache_writes(tmp_path, capsys, options):
    root = tmp_path / "repo"
    write(root / "app.py", "def run(): pass\n")
    cache = tmp_path / "cache"
    assert main(["investigate", "--root", str(root), "--cache-dir", str(cache), *options]) == 2
    assert capsys.readouterr().err
    assert not cache.exists()


def test_configuration_respects_local_project_config_and_test_opt_out(tmp_path):
    write(tmp_path / "app.py", "def main(): pass\n")
    assert configuration(tmp_path, None, None, None).include_tests
    write(tmp_path / ".connection-map/config.toml", 'language = "python"\ninclude_tests = false\n')
    assert not configuration(tmp_path, None, None, None).include_tests
    assert configuration(tmp_path, None, None, True).include_tests
    assert configuration(tmp_path, None, "python", None).include_tests
    with pytest.raises(ValueError, match="either"):
        configuration(tmp_path, tmp_path / ".connection-map/config.toml", "python", None)


def test_declared_src_layout_links_tests_and_tracks_build_metadata_without_execution(tmp_path):
    write(tmp_path / "src/package/__init__.py", "")
    write(tmp_path / "src/package/service.py", DEFINITIONS)
    write(
        tmp_path / "tests/test_service.py",
        "from package.service import factory\n\ndef test_ping():\n    client = factory()\n    return client.ping()\n",
    )
    project = tmp_path / "pyproject.toml"
    write(project, '[tool.setuptools.packages.find]\nwhere = ["src"]\n')
    document = analyze_repository(tmp_path, AnalysisConfig(include_tests=True), deterministic=True)
    query = GraphQuery(document)
    target = select_targets(query, symbol="Service.ping")[0]
    packet = build_investigation(query, [target], root=tmp_path)
    assert any(t["symbol"] == "test_ping" and t["status"] == "resolved" for t in packet["related_tests"])
    assert "pyproject.toml" in document["meta"]["extensions"]["source_snapshot"]["context_files"]
    write(project, '[tool.setuptools.packages.find]\nwhere = ["another"]\n')
    assert check_freshness(document, tmp_path)["status"] == "stale"
    assert check_freshness(document, tmp_path)["changes"]["modified"] == ["pyproject.toml"]


def test_python_multiple_import_roots_do_not_choose_an_arbitrary_duplicate(tmp_path):
    for prefix in ("src", "lib"):
        write(tmp_path / prefix / "service.py", DEFINITIONS)
    write(
        tmp_path / "app.py",
        "from service import factory\n\ndef run():\n    client = factory()\n    return client.ping()\n",
    )
    document = analyze_repository(tmp_path, AnalysisConfig(context={"python_source_roots": ["src", "lib"]}))
    assert not any(
        e["relation_type"] == "calls"
        and e["resolution_status"] == "resolved"
        and e["detail"].get("expression") == "client.ping()"
        for e in document["edges"]
    )
    assert coverage_summary(document)["status"] == "partial"
    assert any("Ambiguous Python module" in item for item in coverage_summary(document)["extraction_limitations"])


def test_invalid_python_import_root_metadata_is_visible_and_explicit_escape_is_rejected(tmp_path):
    write(tmp_path / "app.py", DEFINITIONS)
    write(tmp_path / "pyproject.toml", '[tool.setuptools.packages.find]\nwhere = ["../outside"]\n')
    document = analyze_repository(tmp_path)
    assert coverage_summary(document)["status"] == "partial"
    assert any(d["code"] == "python_import_context" for d in document["diagnostics"])
    with pytest.raises(ValueError, match="inside"):
        analyze_repository(tmp_path, AnalysisConfig(context={"python_source_roots": ["../outside"]}))


@pytest.mark.parametrize("workspace_mode", [False, True])
def test_investigation_http_uses_the_registered_root_and_exact_character_budget(
    investigation_graph, tmp_path, workspace_mode
):
    root, query, _ = investigation_graph
    if workspace_mode:
        workspace = Workspace(tmp_path / "workspace")
        record = workspace.publish_analysis(workspace.register(root), query.document)

        class Handler(WorkspaceRequestHandler):
            validation_states = {record.repository_id: {"status": "valid"}}
            validation_lock = threading.Lock()
            artifact_hashes = {record.repository_id: {"analysis.json": query.sha256}}

        Handler.workspace = workspace
        endpoint = f"/api/repositories/{record.repository_id}/investigate"
    else:
        path = tmp_path / "analysis.json"
        path.write_text(json.dumps(query.document), encoding="utf-8")

        class Handler(AnalysisRequestHandler):
            analysis_path = path
            analysis_sha256 = canonical_sha256(query.document)
            source_root = root

        endpoint = "/investigate"
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path))) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}{endpoint}?"
        try:
            with urlopen(url + urlencode({"file": "app.py", "line": 3, "max_chars": 6000})) as response:
                payload = response.read().decode("utf-8")
            packet = json.loads(payload)
            assert len(payload) == packet["budget"]["used_chars"] <= 6000
            assert packet["freshness"]["status"] == "current"
            assert packet["sources"]
            for invalid in (
                "root=/",
                "symbol=ping&symbol=run",
                "line=3",
                "symbol=ping&snippets=maybe",
                "symbol=ping&max_chars=20",
                "symbol=ping&depth=10",
            ):
                with pytest.raises(HTTPError) as error:
                    urlopen(url + invalid)
                assert error.value.code == 400
        finally:
            server.shutdown()
            thread.join()


def test_summary_describes_actual_scope_and_a_concrete_follow_up(investigation_graph):
    root, query, ids = investigation_graph
    packet = build_investigation(
        query, [ids["Service.ping"]], root=root, direction="in", relations=["calls"], max_chars=20000
    )
    summary = packet["summary"]
    assert list(packet).index("summary") < list(packet).index("nodes")
    assert summary["scope"] == {"direction": "in", "depth": 1, "relations": ["calls"], "resolution": "all"}
    assert summary["returned"]["nodes"] == len(packet["nodes"])
    assert summary["returned"]["edges"] == len(packet["edges"])
    assert summary["returned"]["source_excerpts"] == sum(bool(s.get("ranges")) for s in packet["sources"])
    assert summary["returned"]["related_tests"] == len(packet["related_tests"])
    focus = next(n for n in packet["nodes"] if n["ref"] == packet["focus"][0])
    follow = summary["follow_up"]
    assert follow["file"] == focus["file"] and follow["line"] == focus["line"]
    assert follow["directions"] == ["in"] and follow["relations"] == ["calls"]
    assert select_targets(query, file=follow["file"], line=follow["line"]) == [focus["id"]]
    assert packet["budget"]["used_chars"] == len(encode_packet(packet)) <= 20000


def test_summary_distinguishes_character_budget_from_query_limits(tmp_path):
    write(tmp_path / "app.py", "def leaf():\n    return 1\n\n" + "\n".join(
        f"def caller_{i}():\n    return leaf()\n" for i in range(20)
    ))
    query = GraphQuery(analyze_repository(tmp_path, deterministic=True))
    target = select_targets(query, symbol="leaf")[0]
    packet = build_investigation(
        query, [target], root=tmp_path, snippets=False, max_chars=6000, max_nodes=200, max_edges=400
    )
    omissions = packet["summary"]["omissions"]
    assert packet["budget"]["limited"] is True
    assert omissions["character_budget"]["edges"] > 0
    assert "neighborhood_limits" not in omissions
    assert "depth_limit" not in omissions
    assert "record_caps" not in omissions
    for key, count in omissions["character_budget"].items():
        assert count == packet["truncation"][key]
    assert packet["budget"]["used_chars"] == len(encode_packet(packet)) <= 6000
    assert packet["summary"]["returned"]["edges"] == len(packet["edges"])


def test_summary_depth_limit_does_not_claim_character_budget_omission(tmp_path):
    write(tmp_path / "app.py",
          "def leaf():\n    return 1\n\ndef service():\n    return leaf()\n\ndef entry():\n    return service()\n")
    query = GraphQuery(analyze_repository(tmp_path, deterministic=True))
    target = select_targets(query, symbol="entry")[0]
    packet = build_investigation(query, [target], root=tmp_path, direction="out", depth=1, max_chars=20000)
    assert packet["budget"]["limited"] is False
    assert packet["summary"]["omissions"]["depth_limit"] is True
    assert "character_budget" not in packet["summary"]["omissions"]
    assert packet["summary"]["follow_up"]["directions"] == ["out"]


def test_summary_record_caps_remain_visible_without_character_budget_limit(tmp_path):
    write(tmp_path / "app.py", "def leaf():\n    return 1\n\n" + "\n".join(
        f"def caller_{i}():\n    return leaf()\n" for i in range(16)
    ))
    query = GraphQuery(analyze_repository(tmp_path, deterministic=True))
    target = select_targets(query, symbol="leaf")[0]
    packet = build_investigation(
        query, [target], root=tmp_path, max_chars=200000, max_nodes=200, max_edges=400
    )
    assert packet["budget"]["limited"] is False
    assert packet["summary"]["omissions"]["record_caps"]["sources"] == 5
    assert "character_budget" not in packet["summary"]["omissions"]
    assert packet["summary"]["returned"]["source_excerpts"] == 12
    assert packet["budget"]["used_chars"] == len(encode_packet(packet)) <= 200000


def test_summary_neighborhood_cap_does_not_invent_unseen_counts(tmp_path):
    write(tmp_path / "app.py", "def leaf():\n    return 1\n\n" + "\n".join(
        f"def caller_{i}():\n    return leaf()\n" for i in range(5)
    ))
    query = GraphQuery(analyze_repository(tmp_path, deterministic=True))
    target = select_targets(query, symbol="leaf")[0]
    packet = build_investigation(
        query, [target], root=tmp_path, max_nodes=2, max_edges=100, max_chars=20000
    )
    assert packet["summary"]["omissions"]["neighborhood_limits"] is True
    assert packet["budget"]["limited"] is False
    assert "character_budget" not in packet["summary"]["omissions"]
    assert packet["counts"]["nodes"] == packet["summary"]["returned"]["nodes"] == 2


def test_summary_reports_unavailable_source_separately(investigation_graph):
    root, query, ids = investigation_graph
    write(root / "app.py", "def changed():\n    return 2\n")
    packet = build_investigation(query, [ids["Service.ping"]], root=root, max_chars=20000)
    assert packet["freshness"]["status"] == "stale"
    assert packet["summary"]["returned"]["source_excerpts"] == 0
    assert packet["summary"]["omissions"]["source_unavailable"] is True
