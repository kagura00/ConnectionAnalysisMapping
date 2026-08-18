from __future__ import annotations

import json
from pathlib import Path

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.swift_analyzer import _recover_parser_source, analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "swift_repo"


def _config(*, include_tests: bool = False) -> AnalysisConfig:
    return AnalysisConfig(language="swift", include_tests=include_tests)


def test_swift_fixture_extracts_forms_and_relations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "swift"
    assert document["meta"]["languages"] == ["swift"]
    assert document["meta"]["runtime"]["grammars"] == ["swift"]

    nodes = document["nodes"]
    assert any(node["kind"] == "interface" and node["qualified_name"] == "Worker" for node in nodes)
    assert any(
        node["kind"] == "class"
        and node["qualified_name"] == "Service"
        and node["extensions"]["declaration_form"] == "class"
        for node in nodes
    )
    assert any(
        node["kind"] == "type"
        and node["qualified_name"] == "Item"
        and node["extensions"]["declaration_form"] == "struct"
        for node in nodes
    )
    assert any(
        node["kind"] == "class"
        and node["qualified_name"] == "Store"
        and node["extensions"]["declaration_form"] == "actor"
        for node in nodes
    )
    assert any(
        node["kind"] == "namespace"
        and node["extensions"]["declaration_form"] == "extension"
        for node in nodes
    )
    assert any(
        node["kind"] == "method"
        and node["display_name"] == "work"
        and node["execution_kind"] == "async"
        and node["extensions"]["throws_kind"] == "throws"
        for node in nodes
    )
    assert any(node["kind"] == "lambda" for node in nodes)

    edges = document["edges"]
    assert any(edge["relation_type"] == "imports" and edge["detail"]["module"] == "Foundation" for edge in edges)
    assert any(
        edge["relation_type"] == "inherits"
        and edge["detail"]["reference"] == "Base"
        and edge["detail"]["role"] == "superclass"
        and edge["resolution_status"] == "resolved"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "inherits"
        and edge["detail"]["reference"] == "Worker"
        and edge["detail"]["role"] == "conforms"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "uses"
        and edge["detail"]["role"] == "extension"
        and edge["resolution_status"] == "resolved"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["detail"]["callee"] == "decorated"
        and edge["resolution_status"] == "resolved"
        for edge in edges
    )


def test_swift_test_generated_and_package_files_are_excluded_by_default() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert not any(node["file"] and node["file"].endswith("ServiceTests.swift") for node in document["nodes"])
    assert not any(node["file"] and node["file"].endswith("Generated.generated.swift") for node in document["nodes"])
    assert not any(node["file"] == "Package.swift" for node in document["nodes"])
    assert {
        diagnostic["file"]
        for diagnostic in document["diagnostics"]
    } >= {"Tests/ServiceTests.swift", "Generated.generated.swift", "Package.swift"}


def test_swift_include_tests_and_dispatch_are_deterministic() -> None:
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
    assert any(node["file"] and node["file"].endswith("ServiceTests.swift") for node in first["nodes"])


def test_swift_mixed_dispatch_merges_python_and_swift(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    (tmp_path / "script.swift").write_text("func start() -> Int { return 1 }\n", encoding="utf-8")
    config = AnalysisConfig(language="mixed", languages=["python", "swift"])

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="mixed-commit")
    validate_document(document)

    assert document["meta"]["languages"] == ["python", "swift"]
    languages = {node.get("extensions", {}).get("language") for node in document["nodes"]}
    assert {"python", "swift"} <= languages
    analyzers = {item["name"] for item in document["meta"]["extensions"]["analyzers"]}
    assert "connection-map-swift-tree-sitter" in analyzers


def test_swift_parser_recovery_keeps_conditional_declarations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    recovery_file = "Sources/Demo/Recovery.swift"

    recovery_diagnostics = [
        diagnostic
        for diagnostic in document["diagnostics"]
        if diagnostic["file"] == recovery_file
    ]
    assert any(diagnostic["code"] == "parser_recovery" for diagnostic in recovery_diagnostics)
    assert not any(diagnostic["code"] == "parse_error" for diagnostic in recovery_diagnostics)

    names = {
        node["display_name"]
        for node in document["nodes"]
        if node["file"] == recovery_file
    }
    assert {"foundationOnly", "fallbackOnly", "attributedRecovery"} <= names


def test_swift_parser_recovery_does_not_mask_directives_inside_strings() -> None:
    source = (
        b'// #if comment\r\n'
        b'let plain = "#if plain-string"\r\n'
        b'let raw = #"""\r\n'
        b'#if raw-string\r\n'
        b'"""#\r\n'
        b'let text = """\r\n'
        b'line \\\r\n'
        b'next\r\n'
        b'"""\r\n'
    )
    recovered = _recover_parser_source(source)
    assert len(recovered) == len(source)
    assert recovered == source.replace(b"\\\r\n", b" \r\n")
    assert b"#if raw-string" in recovered
