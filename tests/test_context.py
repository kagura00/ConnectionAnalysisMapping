from __future__ import annotations

import copy
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from connection_map.analyzer import analyze_repository
from connection_map.cli import main
from connection_map.config import AnalysisConfig
from connection_map.contract import canonical_sha256, validate_document
from connection_map.evidence import check_freshness, coverage_summary
from connection_map.manual import merge_manual
from connection_map.query import GraphQuery
from connection_map.server import AnalysisRequestHandler, WorkspaceRequestHandler
from connection_map.workspace import Workspace


@pytest.fixture
def graph(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(
        "def leaf():\n    return 1\n\ndef service():\n    return leaf()\n\ndef entry():\n    return service()\n",
        encoding="utf-8",
    )
    document = analyze_repository(root, deterministic=True, commit_sha="same-commit")
    ids = {node["display_name"]: node["id"] for node in document["nodes"]}
    return root, document, ids


def test_freshness_detects_uncommitted_edits_additions_and_deletions(graph):
    root, document, _ = graph
    assert check_freshness(document, root)["status"] == "current"
    (root / "app.py").write_text("def entry():\n    pass\n", encoding="utf-8")
    assert check_freshness(document, root)["changes"]["modified"] == ["app.py"]
    (root / "other.py").write_text("", encoding="utf-8")
    (root / "app.py").unlink()
    assert check_freshness(document, root)["changes"] == {"added": ["other.py"], "deleted": ["app.py"], "modified": []}
    validate_document(document)  # Artifact validity does not imply source freshness.


def test_freshness_scope_old_graph_and_missing_root(graph):
    root, document, _ = graph
    (root / "tests").mkdir()
    (root / "tests/test_new.py").write_text("def test_new(): pass", encoding="utf-8")
    assert check_freshness(document, root)["status"] == "current"
    assert coverage_summary(document)["tests_included"] is False
    assert check_freshness(document)["status"] == "unchecked"
    assert check_freshness(document, root / "missing")["status"] == "unknown"
    document["meta"]["extensions"].pop("source_snapshot")
    assert check_freshness(document, root)["status"] == "unknown"


def test_uv_cache_is_not_application_source(graph):
    root, document, _ = graph
    for name in (".uv-cache", ".uv-cache-local", "nested/.uv-cache"):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "generated.py").write_text("def cached_package(): pass", encoding="utf-8")
    assert check_freshness(document, root)["status"] == "current"
    fresh = analyze_repository(root, deterministic=True)
    assert fresh["meta"]["extensions"]["coverage"]["selected_file_count"] == 1


def test_changes_during_analysis_never_look_current(tmp_path, monkeypatch):
    from connection_map import python_analyzer

    source = tmp_path / "app.py"
    source.write_text("def f(): pass", encoding="utf-8")
    original = python_analyzer.analyze_repository

    def changing_analysis(*args, **kwargs):
        document = original(*args, **kwargs)
        source.write_text("def changed(): pass", encoding="utf-8")
        return document

    monkeypatch.setattr(python_analyzer, "analyze_repository", changing_analysis)
    document = analyze_repository(tmp_path)
    assert check_freshness(document, tmp_path)["status"] == "unknown"
    assert coverage_summary(document)["status"] == "partial"


def test_partial_parse_and_cli_strict_mode(graph, tmp_path, capsys):
    root, _, _ = graph
    (root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    output = tmp_path / "analysis.json"
    assert main(["analyze", "--root", str(root), "--output", str(output), "--fail-on-error"]) == 3
    document = json.loads(output.read_text(encoding="utf-8"))
    coverage = coverage_summary(document)
    assert coverage["status"] == "partial"
    assert coverage["files_with_errors"] == ["broken.py"]
    assert coverage["files_without_nodes"] == ["broken.py"]
    assert "partial" in capsys.readouterr().err


def test_context_direction_depth_evidence_and_budget(graph):
    root, document, ids = graph
    query = GraphQuery(document)
    incoming = query.neighborhood(ids["service"], direction="in", relations=["calls"], root=root)
    assert {node["id"] for node in incoming["nodes"]} == {ids["entry"], ids["service"]}
    assert incoming["edges"][0]["source_file"] == "app.py"
    assert incoming["edges"][0]["source_span"]["start_line"] == 8
    assert incoming["freshness"]["status"] == "current"
    one = query.neighborhood(ids["entry"], direction="out")
    assert one["truncation"]["depth_limited"] is True
    two = query.neighborhood(ids["entry"], direction="out", depth=2)
    assert len(two["edges"]) == 2
    assert not any(two["truncation"].values())
    limited = query.neighborhood(ids["service"], max_nodes=1)
    assert limited["edges"] == []
    assert limited["truncation"]["budget_limited"] is True
    assert query.neighborhood(ids["service"], max_edges=1)["truncation"]["budget_limited"] is True
    assert not any(edge["relation_type"] == "contains" for edge in two["edges"])
    assert query.neighborhood(ids["service"], relations=["custom_relation"])["edges"] == []


@pytest.mark.parametrize("options", [{"depth": 6}, {"max_nodes": 501}, {"max_edges": 0}, {"direction": "bad"}])
def test_context_rejects_unbounded_options(graph, options):
    _, document, ids = graph
    with pytest.raises(ValueError):
        GraphQuery(document).neighborhood(ids["entry"], **options)


def test_context_handles_cycles_and_resolution_filters(graph):
    _, document, ids = graph
    edge = copy.deepcopy(next(edge for edge in document["edges"] if edge["relation_type"] == "calls"))
    edge.update(id="cycle", source_id=ids["leaf"], target_id=ids["entry"], resolution_status="unresolved")
    document["edges"].append(edge)
    document["meta"]["counts"]["edges"] += 1
    query = GraphQuery(document)
    assert len(query.neighborhood(ids["entry"], depth=5)["edges"]) == 3
    unresolved = query.neighborhood(ids["entry"], resolution="unresolved")
    assert [edge["id"] for edge in unresolved["edges"]] == ["cycle"]


def test_context_preserves_manual_notes_and_cli_matches_api(graph, tmp_path, capsys):
    root, document, ids = graph
    manual = {"format": "connection-analysis-manual", "schema_version": "1.0", "analysis_schema_version": "1.0",
              "nodes": [], "edges": [], "annotations": [
                  {"id": "manual:note", "kind": "note", "node_id": ids["entry"], "text": "runtime trigger"}]}
    document = merge_manual(document, manual)
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert main(["context", "--input", str(path), "--node", ids["entry"], "--root", str(root)]) == 0
    expected = json.loads(capsys.readouterr().out)
    assert expected["annotations"][0]["text"] == "runtime trigger"

    class Handler(AnalysisRequestHandler):
        analysis_path = path
        analysis_sha256 = canonical_sha256(document)
        source_root = root

    with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path))) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base}/context?{urlencode({'node': ids['entry']})}") as response:
                assert json.load(response) == expected
            with pytest.raises(HTTPError) as error:
                urlopen(f"{base}/context?{urlencode({'node': ids['entry'], 'root': '/'})}")
            assert error.value.code == 400
            (root / "app.py").write_text("", encoding="utf-8")
            with urlopen(f"{base}/quality") as response:
                assert json.load(response)["freshness"]["status"] == "stale"
            path.write_text("{}", encoding="utf-8")
            with pytest.raises(HTTPError) as error:
                urlopen(f"{base}/context?{urlencode({'node': ids['entry']})}")
            assert error.value.code == 409
        finally:
            server.shutdown()
            thread.join()


def test_diagnostics_api_sees_errors_beyond_chunk_boundaries_and_validates_pages(graph, tmp_path):
    root, document, _ = graph
    warnings = [{"code": "unresolved_call", "severity": "warning", "file": "a.ts", "span": None,
                 "message": f"warning {index}", "details": {}} for index in range(2500)]
    errors = [{"code": "parse_error", "severity": "error", "file": path, "span": None,
               "message": "invalid source", "details": {}} for path in ("chat.html", "login.html", "minecraft.html")]
    document["diagnostics"] = warnings + errors
    document["meta"]["counts"]["diagnostics"] = len(document["diagnostics"])
    path = tmp_path / "diagnostics.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    class Handler(AnalysisRequestHandler):
        analysis_path = path
        analysis_sha256 = canonical_sha256(document)
        source_root = root

    with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path))) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/diagnostics"
        try:
            with urlopen(base) as response:
                page = json.load(response)
            assert [item["severity"] for item in page["diagnostics"][:3]] == ["error"] * 3
            assert page["total"] == 2503 and page["next_offset"] == 200
            with urlopen(base + "?severity=error&file=LOGIN") as response:
                page = json.load(response)
            assert page["total"] == 1 and page["diagnostics"][0]["file"] == "login.html"
            with urlopen(base + "?offset=2400") as response:
                page = json.load(response)
            assert len(page["diagnostics"]) == 103 and page["next_offset"] is None
            for query in ("?limit=201", "?offset=-1", "?severity=critical", "?file=x&file=y", "?root=/"):
                with pytest.raises(HTTPError) as error:
                    urlopen(base + query)
                assert error.value.code == 400
        finally:
            server.shutdown()
            thread.join()


def test_workspace_context_uses_registered_root(graph, tmp_path):
    root, document, ids = graph
    workspace = Workspace(tmp_path / "workspace")
    record = workspace.publish_analysis(workspace.register(root), document)

    class Handler(WorkspaceRequestHandler):
        validation_states = {record.repository_id: {"status": "valid"}}
        validation_lock = threading.Lock()
        artifact_hashes = {record.repository_id: {"analysis.json": canonical_sha256(document)}}

    Handler.workspace = workspace
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path))) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/repositories/{record.repository_id}/context?"
            with urlopen(url + urlencode({"node": ids["service"]})) as response:
                assert json.load(response)["freshness"]["status"] == "current"
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("binding", ["parameter", "local", "assignment", "other_file"])
def test_lexical_call_cannot_resolve_a_shadowed_or_unimported_name(tmp_path, binding):
    definitions = "function charge() end\n"
    if binding == "other_file":
        (tmp_path / "other.lua").write_text(definitions, encoding="utf-8")
        definitions = ""
    parameter = "charge" if binding == "parameter" else ""
    local = "local charge = replacement\n" if binding == "local" else "charge = replacement\n" if binding == "assignment" else ""
    (tmp_path / "app.lua").write_text(f"{definitions}function run({parameter})\n{local}charge()\nend\n", encoding="utf-8")
    document = analyze_repository(tmp_path, AnalysisConfig(language="lua"), deterministic=True)
    call = next(edge for edge in document["edges"] if edge["relation_type"] == "calls")
    assert call["resolution_status"] == "unresolved"
    assert call["detail"]["candidate_target_id"] is None
    node = next(node for node in document["nodes"] if node["id"] == call["target_id"])
    assert node["file"] is None
