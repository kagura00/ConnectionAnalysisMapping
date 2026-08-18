from __future__ import annotations

import json
from pathlib import Path

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.go_analyzer import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "go_repo"


def _config(*, include_tests: bool = False) -> AnalysisConfig:
    return AnalysisConfig(language="go", include_tests=include_tests)


def test_go_fixture_extracts_definitions_and_relations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "go"
    assert document["meta"]["languages"] == ["go"]
    assert document["meta"]["runtime"]["grammars"] == ["go"]
    assert document["meta"]["extensions"]["module_path"] == "example.com/go-fixture"

    nodes = document["nodes"]
    assert any(
        node["kind"] == "namespace" and node["qualified_name"] == "example.com/go-fixture/src/service"
        for node in nodes
    )
    assert any(
        node["kind"] == "type"
        and node["qualified_name"] == "example.com/go-fixture/src/service.Runner"
        and node["extensions"]["declaration_form"] == "struct"
        for node in nodes
    )
    assert any(
        node["kind"] == "interface"
        and node["qualified_name"] == "example.com/go-fixture/src/domain.Worker"
        for node in nodes
    )
    assert not any(
        node["kind"] == "method"
        and node["qualified_name"] == "example.com/go-fixture/src/service.Runner.Work(1)"
        and node["return_behavior"] == "returns_none"
        for node in nodes
    )
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "example.com/go-fixture/src/service.Runner.Work(1)"
        and node["return_behavior"] == "returns_value"
        for node in nodes
    )
    assert any(node["kind"] == "lambda" for node in nodes)

    edges = document["edges"]
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved" for edge in edges)
    assert any(
        edge["relation_type"] == "imports"
        and edge["resolution_status"] == "external"
        and edge["detail"]["path"] == "fmt"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "inherits"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["reference"] == "domain.Embedded"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["callee"] == "Save"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["callee"] == "New"
        for edge in edges
    )


def test_go_test_and_generated_files_are_excluded_by_default() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert not any(node["file"] and node["file"].endswith("runner_test.go") for node in document["nodes"])
    assert not any(node["file"] and node["file"].endswith("generated.gen.go") for node in document["nodes"])
    assert {diagnostic["file"] for diagnostic in document["diagnostics"]} >= {
        "src/service/runner_test.go",
        "src/service/generated.gen.go",
    }
    build_module = next(
        node
        for node in document["nodes"]
        if node["kind"] == "module" and node["file"] == "src/service/build_linux.go"
    )
    assert build_module["extensions"]["build_constraints"] == ["linux"]


def test_go_include_tests_and_dispatch_are_deterministic() -> None:
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
    assert any(node["display_name"] == "TestRunner" for node in first["nodes"])
    assert any("#service_test" in node["qualified_name"] for node in first["nodes"] if node["kind"] == "namespace")


def test_go_mixed_dispatch_merges_python_and_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/mixed\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "main.go").write_text(
        "package main\n\nfunc Start() { Helper() }\nfunc Helper() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "script.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    config = AnalysisConfig(language="mixed", languages=["python", "go"])

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="mixed-commit")
    validate_document(document)

    assert document["meta"]["languages"] == ["python", "go"]
    languages = {node.get("extensions", {}).get("language") for node in document["nodes"]}
    assert {"python", "go"} <= languages
    analyzers = {item["name"] for item in document["meta"]["extensions"]["analyzers"]}
    assert "connection-map-go-tree-sitter" in analyzers


def test_go_build_profile_filters_platform_files_and_constraints(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/build-profile\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "windows.go").write_text(
        "//go:build windows\n\npackage main\n\nfunc WindowsOnly() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "linux.go").write_text(
        "//go:build linux && amd64\n\npackage main\n\nfunc LinuxOnly() {}\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(language="go", context={"go_os": "linux", "go_arch": "amd64"})

    document = analyze_repository(tmp_path, config, deterministic=True, commit_sha="build-profile")

    assert any(node["display_name"] == "LinuxOnly" for node in document["nodes"])
    assert not any(node["display_name"] == "WindowsOnly" for node in document["nodes"])
    assert any(diagnostic["code"] == "build_condition_excluded" and diagnostic["file"] == "windows.go" for diagnostic in document["diagnostics"])


def test_go_build_profile_does_not_short_circuit_compound_constraints(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/compound\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "impossible.go").write_text(
        "//go:build windows && linux\n\npackage main\n\nfunc Impossible() {}\n",
        encoding="utf-8",
    )

    document = analyze_repository(
        tmp_path,
        AnalysisConfig(language="go", context={"go_os": "windows"}),
        deterministic=True,
        commit_sha="compound-profile",
    )

    assert not any(node["display_name"] == "Impossible" for node in document["nodes"])
