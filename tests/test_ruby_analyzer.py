from __future__ import annotations

import json
from pathlib import Path

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.ruby_analyzer import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "ruby_repo"


def _config(*, include_tests: bool = False) -> AnalysisConfig:
    return AnalysisConfig(language="ruby", include_tests=include_tests)


def test_ruby_fixture_extracts_definitions_and_relations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "ruby"
    assert document["meta"]["languages"] == ["ruby"]
    assert document["meta"]["runtime"]["grammars"] == ["ruby"]
    assert document["meta"]["extensions"]["module_reopen_count"] >= 2

    nodes = document["nodes"]
    assert any(node["kind"] == "namespace" and node["qualified_name"] == "Demo" for node in nodes)
    assert any(node["kind"] == "class" and node["qualified_name"] == "Demo::Service" for node in nodes)
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "Demo::Service#run(1)"
        and node["return_behavior"] == "returns_value"
        for node in nodes
    )
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "Demo::Service.build(1)"
        and node["extensions"]["singleton"] is True
        for node in nodes
    )
    assert any(
        node["qualified_name"] == "Demo::Inline::Item#shorthand(0)"
        and node["return_behavior"] == "returns_value"
        for node in nodes
    )
    assert any(
        node["kind"] == "lambda"
        and node["extensions"]["block"] is True
        and node["extensions"]["parameter_count"] == 1
        for node in nodes
    )

    edges = document["edges"]
    assert any(
        edge["relation_type"] == "imports"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["path"] == "support"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "imports"
        and edge["resolution_status"] == "external"
        and edge["detail"]["path"] == "set"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "inherits"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["reference"] == "Base"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "uses"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["role"] == "include"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "uses"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["role"] == "extend"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["callee"] == "save"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"].get("role") == "object_creation"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "unresolved"
        and edge["detail"]["callee"] == "yield"
        for edge in edges
    )


def test_ruby_test_and_generated_files_are_excluded_by_default() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert not any(node["file"] and node["file"].endswith("service_spec.rb") for node in document["nodes"])
    assert not any(node["file"] and node["file"].endswith("generated.generated.rb") for node in document["nodes"])
    assert {diagnostic["file"] for diagnostic in document["diagnostics"]} >= {
        "spec/service_spec.rb",
        "lib/demo/generated.generated.rb",
    }


def test_ruby_include_tests_and_dispatch_are_deterministic() -> None:
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
    assert any(node["file"] and node["file"].endswith("service_spec.rb") for node in first["nodes"])


def test_ruby_mixed_dispatch_merges_python_and_ruby(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    (tmp_path / "script.rb").write_text("def start\n  1\nend\n", encoding="utf-8")
    config = AnalysisConfig(language="mixed", languages=["python", "ruby"])

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="mixed-commit")
    validate_document(document)

    assert document["meta"]["languages"] == ["python", "ruby"]
    languages = {node.get("extensions", {}).get("language") for node in document["nodes"]}
    assert {"python", "ruby"} <= languages
    analyzers = {item["name"] for item in document["meta"]["extensions"]["analyzers"]}
    assert "connection-map-ruby-tree-sitter" in analyzers
