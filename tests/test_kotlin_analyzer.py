from __future__ import annotations

import json
from pathlib import Path

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.kotlin_analyzer import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "kotlin_repo"


def _config(*, include_tests: bool = False) -> AnalysisConfig:
    return AnalysisConfig(language="kotlin", include_tests=include_tests)


def test_kotlin_fixture_extracts_definitions_and_relations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "kotlin"
    assert document["meta"]["languages"] == ["kotlin"]
    assert document["meta"]["runtime"]["grammars"] == ["kotlin"]

    nodes = document["nodes"]
    assert any(node["kind"] == "namespace" and node["qualified_name"] == "demo.model" for node in nodes)
    assert any(node["kind"] == "interface" and node["qualified_name"] == "demo.model.Worker" for node in nodes)
    assert any(
        node["kind"] == "class"
        and node["qualified_name"] == "demo.model.Service"
        for node in nodes
    )
    assert any(
        node["kind"] == "class"
        and node["qualified_name"] == "demo.model.Service.Companion"
        and node["extensions"]["declaration_form"] == "companion"
        for node in nodes
    )
    assert any(
        node["kind"] == "function"
        and node["qualified_name"] == "demo.app.asyncTop(1)"
        and node["execution_kind"] == "suspend"
        for node in nodes
    )
    assert any(
        node["kind"] == "lambda"
        and node["extensions"]["declaration_form"] == "lambda"
        and node["return_behavior"] == "returns_value"
        for node in nodes
    )
    assert any(
        node["kind"] == "function"
        and node["extensions"].get("receiver_type") == "String"
        and node["qualified_name"] == "demo.lib.String.decorateText(0)"
        for node in nodes
    )

    edges = document["edges"]
    assert any(
        edge["relation_type"] == "imports"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["alias"] == "decorateText"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "inherits"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["reference"] == "Worker"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["callee"] == "Service"
        and edge["detail"]["arity"] == 1
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["callee"] == "decorateText"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "uses"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["reference"] == "Id"
        for edge in edges
    )


def test_kotlin_test_and_generated_files_are_excluded_by_default() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert not any(node["file"] and node["file"].endswith("ServiceTest.kt") for node in document["nodes"])
    assert not any(node["file"] and node["file"].endswith("generated.generated.kt") for node in document["nodes"])
    assert {diagnostic["file"] for diagnostic in document["diagnostics"]} >= {
        "src/test/kotlin/ServiceTest.kt",
        "generated.generated.kt",
    }


def test_kotlin_include_tests_and_dispatch_are_deterministic() -> None:
    config = _config(include_tests=True)
    first = dispatch_analysis(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    second = dispatch_analysis(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    validate_document(first)
    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert any(node["file"] and node["file"].endswith("ServiceTest.kt") for node in first["nodes"])


def test_kotlin_mixed_dispatch_merges_python_and_kotlin(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    (tmp_path / "script.kt").write_text("package demo\nfun start(): Int = 1\n", encoding="utf-8")
    config = AnalysisConfig(language="mixed", languages=["python", "kotlin"])

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="mixed-commit")
    validate_document(document)

    assert document["meta"]["languages"] == ["python", "kotlin"]
    languages = {node.get("extensions", {}).get("language") for node in document["nodes"]}
    assert {"python", "kotlin"} <= languages
    analyzers = {item["name"] for item in document["meta"]["extensions"]["analyzers"]}
    assert "connection-map-kotlin-tree-sitter" in analyzers
