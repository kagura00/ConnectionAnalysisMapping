from __future__ import annotations

from pathlib import Path

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.csharp_analyzer import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "csharp_repo"


def _config(*, include_tests: bool = False) -> AnalysisConfig:
    return AnalysisConfig(language="csharp", include_tests=include_tests)


def test_csharp_fixture_extracts_definitions_and_relations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")

    assert document["meta"]["language"] == "csharp"
    assert document["meta"]["languages"] == ["csharp"]
    assert document["meta"]["runtime"]["grammars"] == ["c_sharp"]
    assert document["meta"]["deterministic"] is True

    nodes = document["nodes"]
    qualified_names = {node["qualified_name"] for node in nodes}
    assert "Demo.Domain.BaseEntity" in qualified_names
    assert "Demo.Domain.IWorker" in qualified_names
    assert "Demo.Domain.Point" in qualified_names
    assert "Demo.Domain.Formatter" in qualified_names
    assert "Demo.Services.Runner.Save(0)" in qualified_names
    assert "Demo.Services.Runner.<init>(0)" in qualified_names

    point = next(node for node in nodes if node["qualified_name"] == "Demo.Domain.Point")
    assert point["kind"] == "type"
    assert point["extensions"]["declaration_form"] == "record_struct"
    constructor = next(node for node in nodes if node["qualified_name"] == "Demo.Services.Helper.<init>(0)")
    assert constructor["extensions"]["constructor"] is True
    assert constructor["execution_kind"] == "sync"

    runner = next(node for node in nodes if node["qualified_name"] == "Demo.Services.Runner")
    namespace = next(node for node in nodes if node["qualified_name"] == "Demo.Services" and node["file"] == "src/Services/Runner.cs")
    assert runner["parent_id"] == namespace["id"]

    relations = document["edges"]
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved" for edge in relations)
    assert any(edge["relation_type"] == "inherits" and edge["resolution_status"] == "resolved" for edge in relations)
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"].get("callee") == "Save"
        for edge in relations
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"].get("callee") == "HelperAlias.<init>"
        for edge in relations
    )
    assert any(edge["relation_type"] == "uses" and edge["resolution_status"] == "resolved" for edge in relations)
    assert any(diagnostic["code"] == "unresolved_call" for diagnostic in document["diagnostics"])


def test_csharp_test_files_are_excluded_by_default() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert not any(node["file"] and "RunnerTests.cs" in node["file"] for node in document["nodes"])
    assert any(diagnostic["file"] == "src/Tests/RunnerTests.cs" for diagnostic in document["diagnostics"])

    included = analyze_repository(FIXTURE, _config(include_tests=True), deterministic=True, commit_sha="fixture-commit")
    assert any(node["file"] and "RunnerTests.cs" in node["file"] for node in included["nodes"])


def test_csharp_dispatch_is_deterministic() -> None:
    first = dispatch_analysis(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    second = dispatch_analysis(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert first == second


def test_mixed_dispatch_merges_python_and_csharp(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "App.cs").write_text(
        "namespace Demo;\npublic class App { public int Run() { return 1; } }\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(language="mixed", languages=["python", "csharp"])
    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="mixed-fixture")

    assert document["meta"]["languages"] == ["python", "csharp"]
    languages = {node.get("extensions", {}).get("language") for node in document["nodes"]}
    assert {"python", "csharp"} <= languages
    analyzers = {item["name"] for item in document["meta"]["extensions"]["analyzers"]}
    assert "connection-map-csharp-tree-sitter" in analyzers
