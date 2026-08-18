from __future__ import annotations

from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.web_analyzer import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "web_repo"


def _config() -> AnalysisConfig:
    return AnalysisConfig(
        language="web",
        include_tests=True,
        exclude=[".git/**", ".connection-map/**", "**/node_modules/**"],
    )


def test_web_fixture_obeys_contract_and_extracts_cross_language_relationships() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "web"
    assert set(document["meta"]["languages"]) == {"html", "css", "javascript", "typescript"}
    assert document["meta"]["runtime"]["parser"] == "tree-sitter-language-pack"
    node_by_qualified = {node["qualified_name"]: node for node in document["nodes"]}
    kinds = {node["kind"] for node in document["nodes"]}
    assert {"module", "class", "method", "function", "interface", "type", "element", "style_rule"} <= kinds
    assert any(node["file"] == "index.html" and node["kind"] == "element" for node in document["nodes"])
    assert any(node["file"] == "style.css" and node["kind"] == "style_rule" for node in document["nodes"])
    assert any("App.run" in name for name in node_by_qualified)
    assert node_by_qualified[next(name for name in node_by_qualified if name.endswith("App.run"))]["return_behavior"] == "returns_value"
    assert any(node["display_name"] == "callback" and node["kind"] == "function" for node in document["nodes"])
    assert any(
        edge["relation_type"] == "exports"
        and edge["target_id"].endswith(":callback:function")
        for edge in document["edges"]
    )

    relation_types = {edge["relation_type"] for edge in document["edges"]}
    assert {"contains", "imports", "dynamic_imports", "exports", "calls", "inherits", "references", "handles", "styles"} <= relation_types
    assert any(edge["relation_type"] == "imports" and edge["detail"].get("kind") == "script" for edge in document["edges"])
    assert any(edge["relation_type"] == "references" and edge["resolution_status"] == "resolved" for edge in document["edges"])
    assert any(edge["relation_type"] == "styles" and edge["resolution_status"] == "resolved" for edge in document["edges"])


def test_web_analysis_is_deterministic() -> None:
    config = _config()
    first = analyze_repository(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    second = analyze_repository(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    assert first == second


def test_single_language_configuration_keeps_unresolved_web_references_visible(tmp_path: Path) -> None:
    (tmp_path / "main.ts").write_text('document.querySelector("#missing");\n', encoding="utf-8")
    config = AnalysisConfig(language="typescript")
    document = analyze_repository(tmp_path, config, deterministic=True, commit_sha="fixture-commit")
    validate_document(document)
    assert document["meta"]["language"] == "typescript"
    assert any(item["code"] == "unresolved_dom_reference" for item in document["diagnostics"])
    assert any(
        edge["relation_type"] == "references" and edge["resolution_status"] == "unresolved"
        for edge in document["edges"]
    )


@pytest.mark.parametrize(
    ("language", "suffixes"),
    [
        ("javascript", (".js", ".mjs", ".cjs", ".jsx")),
        ("typescript", (".ts", ".mts", ".cts", ".tsx")),
    ],
)
def test_js_ts_module_suffixes_are_analyzed(tmp_path: Path, language: str, suffixes: tuple[str, ...]) -> None:
    for suffix in suffixes:
        (tmp_path / f"entry{suffix}").write_text("export function entry() { return 1; }\n", encoding="utf-8")

    document = dispatch_analysis(
        tmp_path,
        AnalysisConfig(language=language),
        deterministic=True,
        commit_sha="suffix-fixture",
    )
    validate_document(document)
    module_files = {node["file"] for node in document["nodes"] if node["kind"] == "module"}
    assert module_files == {f"entry{suffix}" for suffix in suffixes}


@pytest.mark.parametrize(
    ("language", "suffixes"),
    [
        ("html", {".html"}),
        ("css", {".css"}),
        ("javascript", {".js"}),
        ("typescript", {".ts"}),
    ],
)
def test_dispatcher_supports_each_concrete_web_language(language: str, suffixes: set[str]) -> None:
    document = dispatch_analysis(
        FIXTURE,
        AnalysisConfig(language=language),
        deterministic=True,
        commit_sha="fixture-commit",
    )
    validate_document(document)
    assert document["meta"]["language"] == language
    module_files = {
        node["file"]
        for node in document["nodes"]
        if node["kind"] == "module"
    }
    assert module_files
    assert {Path(file).suffix for file in module_files} <= suffixes


def test_html_and_css_bare_relative_references_are_resolved(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        '<script src="app.ts"></script>\n<link rel="stylesheet" href="style.css">\n',
        encoding="utf-8",
    )
    (tmp_path / "app.ts").write_text("export function boot() {}\n", encoding="utf-8")
    (tmp_path / "style.css").write_text('@import "base.css";\nbody { color: red; }\n', encoding="utf-8")
    (tmp_path / "base.css").write_text("body { margin: 0; }\n", encoding="utf-8")

    document = analyze_repository(
        tmp_path,
        AnalysisConfig(language="web"),
        deterministic=True,
        commit_sha="fixture-commit",
    )
    validate_document(document)
    resolved_imports = [
        edge
        for edge in document["edges"]
        if edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved"
    ]
    assert len(resolved_imports) == 3


def test_typescript_js_specifiers_and_re_exports_follow_ts_sources(tmp_path: Path) -> None:
    (tmp_path / "entry.ts").write_text(
        'import { run } from "./service.js";\nexport { run } from "./service.js";\nrun();\n',
        encoding="utf-8",
    )
    (tmp_path / "service.ts").write_text("export function run() { return 1; }\n", encoding="utf-8")

    document = analyze_repository(tmp_path, AnalysisConfig(language="typescript"), deterministic=True, commit_sha="ts-resolution")
    service_module = next(node for node in document["nodes"] if node["kind"] == "module" and node["file"] == "service.ts")
    cross_module = [
        edge for edge in document["edges"]
        if edge["source_id"] == "typescript:entry.ts:module" and edge["target_id"] == service_module["id"]
    ]

    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved" for edge in cross_module)
    assert any(edge["relation_type"] == "exports" and edge["detail"].get("kind") == "re_export" for edge in cross_module)


def test_web_relative_escape_is_not_resolved_inside_the_repository(tmp_path: Path) -> None:
    (tmp_path / "target.ts").write_text("export function target() {}\n", encoding="utf-8")
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "entry.ts").write_text('import "../../target.js";\n', encoding="utf-8")

    document = analyze_repository(tmp_path, AnalysisConfig(language="typescript"), deterministic=True, commit_sha="path-boundary")
    target_module = next(node for node in document["nodes"] if node["kind"] == "module" and node["file"] == "target.ts")
    assert not any(
        edge["relation_type"] == "imports"
        and edge["resolution_status"] == "resolved"
        and edge["target_id"] == target_module["id"]
        for edge in document["edges"]
    )
