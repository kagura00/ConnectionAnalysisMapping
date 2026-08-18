from __future__ import annotations

import json
from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.bundle import split_analysis_file, validate_bundle
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.language_registry import LANGUAGE_SPECS, concrete_languages


def _write_mixed_fixture(root: Path) -> None:
    (root / "main.py").write_text(
        "def boot():\n    return 1\n",
        encoding="utf-8",
    )
    (root / "index.html").write_text(
        '<main id="app"><script src="app.js"></script></main>\n',
        encoding="utf-8",
    )
    (root / "style.css").write_text(
        '#app { color: red; }\n',
        encoding="utf-8",
    )
    (root / "app.js").write_text(
        'export function start() { document.querySelector("#app"); }\n',
        encoding="utf-8",
    )
    (root / "types.ts").write_text(
        "export interface Options { enabled: boolean; }\n"
        "export function typedStart(): Options { return { enabled: true }; }\n",
        encoding="utf-8",
    )


def _mixed_config() -> AnalysisConfig:
    return AnalysisConfig(
        language="mixed",
        languages=["python", "html", "css", "javascript", "typescript"],
        include_tests=True,
        exclude=[".git/**", ".connection-map/**", "**/node_modules/**"],
    )


def test_mixed_dispatch_merges_all_selected_languages(tmp_path: Path) -> None:
    _write_mixed_fixture(tmp_path)

    document = dispatch_analysis(tmp_path, _mixed_config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "mixed"
    assert document["meta"]["languages"] == ["python", "html", "css", "javascript", "typescript"]
    assert {item["name"] for item in document["meta"]["extensions"]["analyzers"]} == {
        "connection-map-python-ast",
        "connection-map-web-tree-sitter",
    }
    source_languages = {
        (node.get("extensions") or {}).get("language")
        for node in document["nodes"]
        if node.get("file")
    }
    assert source_languages == {"python", "html", "css", "javascript", "typescript"}
    assert any(node["display_name"] == "boot" and node["extensions"]["language"] == "python" for node in document["nodes"])
    assert any(node["display_name"] == "typedStart" and node["extensions"]["language"] == "typescript" for node in document["nodes"])


def test_mixed_analysis_is_deterministic_and_bundle_search_keeps_language(tmp_path: Path) -> None:
    _write_mixed_fixture(tmp_path)
    config = _mixed_config()
    first = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="fixture-commit")
    second = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="fixture-commit")
    assert first == second

    analysis_path = tmp_path / "analysis.json"
    analysis_path.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    bundle_path = tmp_path / "bundle"
    index = split_analysis_file(analysis_path, bundle_path, node_chunk_size=2, search_chunk_size=2)
    validate_bundle(bundle_path)

    records = []
    for entry in index["search"]["record_chunks"]:
        records.extend(json.loads((bundle_path / entry["path"]).read_text(encoding="utf-8")))
    typed_record = next(record for record in records if record["display_name"] == "typedStart")
    assert typed_record["language"] == "typescript"


def test_language_presets_reject_duplicates_and_out_of_preset_languages() -> None:
    assert concrete_languages("mixed") == ("python", "html", "css", "javascript", "typescript")
    all_languages = tuple(name for name, spec in LANGUAGE_SPECS.items() if not spec.is_preset)
    assert concrete_languages("all") == all_languages
    all_config = AnalysisConfig(language="all")
    assert all_config.active_languages() == all_languages
    assert "**/*.g.dart" in all_config.exclude
    with pytest.raises(ValueError, match="duplicates"):
        concrete_languages("mixed", ["python", "python"])
    with pytest.raises(ValueError, match="only supports"):
        concrete_languages("web", ["python"])


def test_mixed_dispatch_merges_python_and_c_family(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def boot():\n    return 1\n", encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int add(int left, int right) { return left + right; }\n"
        "int main(void) { return add(1, 2); }\n",
        encoding="utf-8",
    )
    (tmp_path / "main.cpp").write_text(
        "namespace demo { int add(int left, int right) { return left + right; } }\n"
        "int main() { return demo::add(1, 2); }\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(
        language="mixed",
        languages=["python", "c", "cpp"],
        include_tests=True,
        exclude=[".git/**", ".connection-map/**"],
    )

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["languages"] == ["python", "c", "cpp"]
    assert {item["name"] for item in document["meta"]["extensions"]["analyzers"]} == {
        "connection-map-python-ast",
        "connection-map-c-family-tree-sitter",
    }
    assert {node["extensions"]["language"] for node in document["nodes"] if node.get("file")} == {
        "python",
        "c",
        "cpp",
    }


def test_mixed_dispatch_propagates_read_only_build_context(tmp_path: Path) -> None:
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (tmp_path / "compile_commands.json").write_text(
        json.dumps([{"directory": str(tmp_path), "file": "main.cpp", "arguments": ["c++", "-c", "main.cpp"]}]),
        encoding="utf-8",
    )
    config = AnalysisConfig(
        language="mixed",
        languages=["cpp"],
        context={"compile_commands": "compile_commands.json"},
        exclude=[".git/**", ".connection-map/**"],
    )

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="fixture-commit")

    context = document["meta"]["runtime"]["build_context"]["analyzers"][0]
    assert context["compile_commands"].endswith("compile_commands.json")
    assert context["compile_command_files"] >= 1


def test_mixed_dispatch_merges_python_and_java(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def boot():\n    return 1\n", encoding="utf-8")
    java_root = tmp_path / "src" / "main" / "java" / "demo"
    java_root.mkdir(parents=True)
    (java_root / "App.java").write_text(
        "package demo;\n"
        "public class App { public int run() { return 1; } }\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(
        language="mixed",
        languages=["python", "java"],
        include_tests=True,
        exclude=[".git/**", ".connection-map/**"],
    )

    document = dispatch_analysis(tmp_path, config, deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["languages"] == ["python", "java"]
    assert {item["name"] for item in document["meta"]["extensions"]["analyzers"]} == {
        "connection-map-python-ast",
        "connection-map-java-tree-sitter",
    }
    assert {node["extensions"]["language"] for node in document["nodes"] if node.get("file")} == {
        "python",
        "java",
    }
