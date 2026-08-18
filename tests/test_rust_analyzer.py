from __future__ import annotations

import json
from pathlib import Path

from connection_map.analysis_context import RustBuildProfile, rust_cfg_expression_matches
from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.rust_analyzer import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "rust_repo"


def _config(*, include_tests: bool = False) -> AnalysisConfig:
    return AnalysisConfig(language="rust", include_tests=include_tests)


def test_rust_fixture_extracts_definitions_and_relations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "rust"
    assert document["meta"]["languages"] == ["rust"]
    assert document["meta"]["runtime"]["grammars"] == ["rust"]
    assert document["meta"]["extensions"]["crate_name"] == "rust_fixture"

    nodes = document["nodes"]
    assert any(node["kind"] == "namespace" and node["qualified_name"] == "crate::nested" for node in nodes)
    assert any(
        node["kind"] == "type"
        and node["qualified_name"] == "crate::domain::Base"
        and node["extensions"]["declaration_form"] == "struct"
        for node in nodes
    )
    assert any(
        node["kind"] == "interface" and node["qualified_name"] == "crate::domain::Worker"
        for node in nodes
    )
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "crate::service::Runner::work(0)"
        and node["return_behavior"] == "no_explicit_return"
        for node in nodes
    )
    assert any(node["kind"] == "lambda" for node in nodes)
    assert any(
        node["kind"] == "function"
        and node["qualified_name"] == "crate::local_macro"
        and node["extensions"]["declaration_form"] == "macro"
        for node in nodes
    )

    edges = document["edges"]
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved" for edge in edges)
    assert any(
        edge["relation_type"] == "imports"
        and edge["resolution_status"] == "external"
        and edge["detail"]["path"] == "std::fmt::Display"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "inherits"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["role"] == "supertrait"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "inherits"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["role"] == "implements"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"].get("callee") == "save"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"].get("macro") == "local_macro"
        for edge in edges
    )


def test_rust_test_generated_and_cfg_metadata() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    assert not any(node["file"] and node["file"].endswith("runner_test.rs") for node in document["nodes"])
    assert not any(node["file"] and node["file"].endswith("generated.generated.rs") for node in document["nodes"])
    assert {diagnostic["file"] for diagnostic in document["diagnostics"]} >= {
        "src/runner_test.rs",
        "src/generated.generated.rs",
    }
    lib_module = next(node for node in document["nodes"] if node["file"] == "src/lib.rs" and node["kind"] == "module")
    assert lib_module["extensions"]["cfg_attributes"] == [
        '#![cfg_attr(feature = "demo", allow(dead_code))]'
    ]


def test_rust_include_tests_and_dispatch_are_deterministic() -> None:
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
    assert any(node["file"] and node["file"].endswith("runner_test.rs") for node in first["nodes"])


def test_rust_mixed_dispatch_merges_python_and_go(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"mixed-rust\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    (tmp_path / "main.rs").write_text("fn start() { helper(); }\nfn helper() {}\n", encoding="utf-8")
    (tmp_path / "script.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    config = AnalysisConfig(language="mixed", languages=["python", "rust"])

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="mixed-commit")
    validate_document(document)

    assert document["meta"]["languages"] == ["python", "rust"]
    languages = {node.get("extensions", {}).get("language") for node in document["nodes"]}
    assert {"python", "rust"} <= languages
    analyzers = {item["name"] for item in document["meta"]["extensions"]["analyzers"]}
    assert "connection-map-rust-tree-sitter" in analyzers


def test_rust_build_profile_filters_cfg_items(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"cfg-profile\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    (tmp_path / "lib.rs").write_text(
        "#[cfg(feature = \"enabled\")]\nfn enabled() {}\n\n"
        "#[cfg(feature = \"missing\")]\nfn missing() {}\n\n"
        "#[cfg(all(unix, not(feature = \"windows_only\")))]\nfn unix_only() {}\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(
        language="rust",
        context={"rust_features": ["enabled"], "rust_target": "x86_64-unknown-linux-gnu", "rust_all_cfg": False},
    )

    document = analyze_repository(tmp_path, config, deterministic=True, commit_sha="cfg-profile")

    names = {node["display_name"] for node in document["nodes"]}
    assert "enabled" in names
    assert "unix_only" in names
    assert "missing" not in names


def test_rust_unknown_target_cfg_is_kept_conservatively() -> None:
    profile = RustBuildProfile(all_cfg=False)

    assert rust_cfg_expression_matches("unix", profile) is True
    assert rust_cfg_expression_matches('feature = "missing"', profile) is False
