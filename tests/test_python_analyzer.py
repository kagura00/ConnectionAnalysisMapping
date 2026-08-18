from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.python_analyzer import analyze_repository
from connection_map.scaffold import initialize_target
from connection_map.server import serve_analysis

FIXTURE = Path(__file__).parent / "fixtures" / "basic_repo"


def _config() -> AnalysisConfig:
    return AnalysisConfig(
        include_tests=False,
        generated=["generated/**"],
    )


def test_fixture_obeys_contract_and_extracts_core_relationships() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    node_by_name = {node["qualified_name"]: node for node in document["nodes"]}
    assert node_by_name["Service"]["kind"] == "class"
    assert node_by_name["Service.run"]["kind"] == "method"
    assert node_by_name["Service.run"]["return_behavior"] == "returns_value"
    assert node_by_name["Service.no_return"]["return_behavior"] == "no_explicit_return"
    assert node_by_name["mixed"]["return_behavior"] == "mixed"
    assert node_by_name["builtin_call"]["return_behavior"] == "returns_value"
    assert any(node["kind"] == "lambda" for node in document["nodes"])

    factory_edge = next(
        edge
        for edge in document["edges"]
        if edge["detail"].get("expression") == "make_base().base_method()"
    )
    target = node_by_name["Base.base_method"]
    assert factory_edge["target_id"] == target["id"]
    assert factory_edge["resolution_status"] == "resolved"
    assert factory_edge["detail"]["resolution_evidence"]["strategy"] == "return_annotation"
    assert factory_edge["detail"]["resolution_evidence"]["return_annotation"] == "Base"

    constructor_edge = next(
        edge
        for edge in document["edges"]
        if edge["detail"].get("expression") == "Base().base_method()"
    )
    assert constructor_edge["target_id"] == target["id"]
    assert constructor_edge["resolution_status"] == "resolved"

    relation_types = {edge["relation_type"] for edge in document["edges"]}
    assert {"contains", "imports", "dynamic_imports", "calls", "inherits"} <= relation_types
    assert any(item["code"] == "parse_error" for item in document["diagnostics"])
    assert any(item["code"] == "unresolved_call" for item in document["diagnostics"])
    assert not any(
        item["code"] == "unresolved_call" and "len" in item["message"]
        for item in document["diagnostics"]
    )
    assert any(item["code"] == "generated_file" and item["file"] == "generated/auto.py" for item in document["diagnostics"])
    assert any(item["code"] == "excluded_file" and item["file"] == "tests/test_excluded.py" for item in document["diagnostics"])


def test_deterministic_analysis_is_byte_stable() -> None:
    first = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    second = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_tests_can_be_enabled() -> None:
    config = _config()
    config.include_tests = True
    document = analyze_repository(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    assert any(node["file"] == "tests/test_excluded.py" for node in document["nodes"])


def test_local_argument_shadowing_does_not_resolve_to_module_function(tmp_path: Path) -> None:
    (tmp_path / "shadow.py").write_text(
        "def helper():\n"
        "    return 1\n\n"
        "def caller(helper):\n"
        "    helper()\n",
        encoding="utf-8",
    )
    document = analyze_repository(tmp_path, AnalysisConfig(), deterministic=True, commit_sha="shadow-fixture")
    caller = next(node for node in document["nodes"] if node.get("display_name") == "caller")
    helper = next(node for node in document["nodes"] if node.get("display_name") == "helper" and node["kind"] == "function")
    call = next(edge for edge in document["edges"] if edge["relation_type"] == "calls" and edge["source_id"] == caller["id"])

    assert call["target_id"] != helper["id"]
    assert call["resolution_status"] == "unresolved"


def test_checked_in_example_obeys_contract() -> None:
    example_path = Path(__file__).parents[1] / "examples" / "analysis-v1.json"
    validate_document(json.loads(example_path.read_text(encoding="utf-8")))


def test_init_creates_scaffold_without_overwriting_existing_files(tmp_path: Path) -> None:
    first = initialize_target(tmp_path)
    assert first.created
    assert (tmp_path / ".connection-map" / "layout" / "manual-v1.json").is_file()
    config_path = tmp_path / ".connection-map" / "config.toml"
    config_path.write_text("user-edit\n", encoding="utf-8")

    second = initialize_target(tmp_path)
    assert config_path in second.skipped
    assert config_path.read_text(encoding="utf-8") == "user-edit\n"


def test_init_scaffold_includes_phase3_source_suffixes(tmp_path: Path) -> None:
    initialize_target(tmp_path)
    config = (tmp_path / ".connection-map" / "config.toml").read_text(encoding="utf-8")
    for pattern in ("**/*.sh", "**/*.ps1", "**/*.dart", "**/*.scala", "**/*.sql"):
        assert pattern in config


def test_web_assets_are_packaged_sources() -> None:
    web_directory = Path(__file__).parents[1] / "src" / "connection_map" / "web"
    assert (web_directory / "index.html").is_file()
    assert (web_directory / "app.js").is_file()
    assert (web_directory / "style.css").is_file()


def test_serve_rejects_invalid_graph_before_starting(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{}\n", encoding="utf-8")
    try:
        serve_analysis(invalid_path, port=0)
    except ValueError as error:
        assert "invalid analysis JSON" in str(error)
    else:  # pragma: no cover - guard against accidentally starting a server
        raise AssertionError("serve_analysis accepted an invalid graph")


def test_synthetic_performance_graph_is_contract_valid() -> None:
    project_root = Path(__file__).parents[1]
    output_path = project_root / ".tmp" / "test-graph.json"
    subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "generate_synthetic_graph.py"),
            "--nodes",
            "100",
            "--edges-per-node",
            "2",
            "--seed",
            "7",
            "--output",
            str(output_path),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    document = json.loads(output_path.read_text(encoding="utf-8"))
    validate_document(document)
    assert document["meta"]["counts"]["nodes"] == 100
    assert document["meta"]["counts"]["edges"] > 0
