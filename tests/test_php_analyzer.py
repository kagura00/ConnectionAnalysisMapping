from __future__ import annotations

import json
from pathlib import Path

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.php_analyzer import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "php_repo"


def _config(*, include_tests: bool = False) -> AnalysisConfig:
    return AnalysisConfig(language="php", include_tests=include_tests)


def test_php_fixture_extracts_definitions_and_relations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "php"
    assert document["meta"]["languages"] == ["php"]
    assert document["meta"]["runtime"]["grammars"] == ["php"]

    nodes = document["nodes"]
    assert any(node["kind"] == "namespace" and node["qualified_name"] == "Demo\\Service" for node in nodes)
    assert any(
        node["kind"] == "interface"
        and node["qualified_name"] == "Demo\\Service\\RunnerContract"
        for node in nodes
    )
    assert any(
        node["kind"] == "class"
        and node["qualified_name"] == "Demo\\Service\\RunnerService"
        for node in nodes
    )
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "Demo\\Service\\RunnerService::run(1)"
        and node["return_behavior"] == "returns_value"
        for node in nodes
    )
    assert any(node["kind"] == "lambda" for node in nodes)

    edges = document["edges"]
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved" for edge in edges)
    assert any(
        edge["relation_type"] == "inherits"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["role"] == "implements"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "uses"
        and edge["resolution_status"] == "resolved"
        and edge["detail"].get("role") == "trait"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["callee"] == "make_helper"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["kind"] == "object_creation"
        for edge in edges
    )


def test_php_test_and_generated_files_are_excluded_by_default() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert not any(node["file"] and node["file"].endswith("RunnerTest.php") for node in document["nodes"])
    assert not any(node["file"] and node["file"].endswith("Generated.generated.php") for node in document["nodes"])
    assert {diagnostic["file"] for diagnostic in document["diagnostics"]} >= {
        "tests/RunnerTest.php",
        "src/Generated.generated.php",
    }


def test_php_include_tests_and_dispatch_are_deterministic() -> None:
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
    assert any(node["file"] and node["file"].endswith("RunnerTest.php") for node in first["nodes"])


def test_php_mixed_dispatch_merges_python_and_php(tmp_path: Path) -> None:
    (tmp_path / "index.php").write_text(
        "<?php\nnamespace Demo;\nfunction start(): void {}\n",
        encoding="utf-8",
    )
    (tmp_path / "script.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    config = AnalysisConfig(language="mixed", languages=["python", "php"])

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="mixed-commit")
    validate_document(document)

    assert document["meta"]["languages"] == ["python", "php"]
    languages = {node.get("extensions", {}).get("language") for node in document["nodes"]}
    assert {"python", "php"} <= languages
    analyzers = {item["name"] for item in document["meta"]["extensions"]["analyzers"]}
    assert "connection-map-php-tree-sitter" in analyzers
