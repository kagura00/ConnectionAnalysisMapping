from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.c_family_analyzer import analyze_repository
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document

FIXTURE = Path(__file__).parent / "fixtures" / "c_family_repo"


def _config(language: str = "c-family", languages: list[str] | None = None) -> AnalysisConfig:
    return AnalysisConfig(
        language=language,
        languages=languages or [],
        include_tests=True,
        exclude=[".git/**", ".connection-map/**"],
    )


def test_c_family_fixture_extracts_definitions_and_relations() -> None:
    document = analyze_repository(
        FIXTURE,
        _config("c-family", ["c", "cpp"]),
        deterministic=True,
        commit_sha="fixture-commit",
    )
    validate_document(document)

    assert document["meta"]["language"] == "c-family"
    assert document["meta"]["languages"] == ["c", "cpp"]
    assert document["meta"]["runtime"]["grammars"] == ["c", "cpp"]

    nodes = document["nodes"]
    assert any(
        node["kind"] == "type"
        and node["qualified_name"] == "Item"
        and node["extensions"]["language"] == "c"
        for node in nodes
    )
    assert any(node["kind"] == "namespace" and node["qualified_name"] == "demo" for node in nodes)
    assert any(node["kind"] == "class" and node["qualified_name"] == "demo::Derived" for node in nodes)
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "demo::Derived::value"
        and node["extensions"]["declaration_kind"] == "definition"
        for node in nodes
    )
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "demo::Counter::increment"
        and node["parent_id"]
        and node["extensions"]["declaration_kind"] == "definition"
        for node in nodes
    )

    edges = document["edges"]
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved" for edge in edges)
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"].get("callee") == "c_add"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"].get("callee") == "demo::add"
        for edge in edges
    )
    assert any(edge["relation_type"] == "inherits" and edge["resolution_status"] == "resolved" for edge in edges)
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "external" for edge in edges)
    assert any(item["code"] == "unresolved_call" and item["file"] == "cpp/src/main.cpp" for item in document["diagnostics"])


def test_c_and_cpp_single_language_selection_handles_ambiguous_headers() -> None:
    c_document = analyze_repository(FIXTURE, _config("c"), deterministic=True, commit_sha="fixture-commit")
    cpp_document = analyze_repository(FIXTURE, _config("cpp"), deterministic=True, commit_sha="fixture-commit")

    assert set(c_document["meta"]["languages"]) == {"c"}
    assert {node["extensions"]["language"] for node in c_document["nodes"]} == {"c"}
    assert any(node["file"] == "c/include/common.h" for node in c_document["nodes"])

    assert set(cpp_document["meta"]["languages"]) == {"cpp"}
    assert {node["extensions"]["language"] for node in cpp_document["nodes"]} == {"cpp"}
    assert any(node["file"] == "cpp/include/base.hpp" for node in cpp_document["nodes"])
    assert not any(node["file"].startswith("c/src/") for node in cpp_document["nodes"] if node["file"])


def test_dispatch_and_deterministic_output_cover_c_family() -> None:
    config = _config("c-family", ["c", "cpp"])
    first = dispatch_analysis(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    second = dispatch_analysis(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second,
        ensure_ascii=False,
        sort_keys=True,
    )


def test_mixed_explicit_c_selection_honors_language_with_broad_include(tmp_path: Path) -> None:
    (tmp_path / "main.c").write_text("int c(void) { return 1; }\n", encoding="utf-8")
    (tmp_path / "main.cpp").write_text("int cpp() { return 1; }\n", encoding="utf-8")
    document = dispatch_analysis(
        tmp_path,
        AnalysisConfig(language="mixed", languages=["c"], include=["**/*"], include_tests=True),
        deterministic=True,
        commit_sha="mixed-c-only",
    )
    modules = {node["file"] for node in document["nodes"] if node["kind"] == "module"}
    assert "main.c" in modules
    assert "main.cpp" not in modules


def test_c_family_resolves_safe_parent_relative_includes(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    include_dir = tmp_path / "include"
    source_dir.mkdir()
    include_dir.mkdir()
    (include_dir / "base.hpp").write_text("struct Base {};\n", encoding="utf-8")
    (source_dir / "main.cpp").write_text(
        '#include "../include/base.hpp"\nint main() { return 0; }\n',
        encoding="utf-8",
    )

    document = analyze_repository(tmp_path, _config("cpp"), deterministic=True, commit_sha="parent-include")
    imports = [edge for edge in document["edges"] if edge["relation_type"] == "imports"]

    assert any(
        edge["resolution_status"] == "resolved"
        and edge["target_id"].endswith("include/base.hpp:module")
        for edge in imports
    )


def test_c_family_compile_database_include_order_precedes_repository_fallback(tmp_path: Path) -> None:
    include_dir = tmp_path / "toolchain" / "include"
    include_dir.mkdir(parents=True)
    (tmp_path / "foo.h").write_text("struct RootFoo {};\n", encoding="utf-8")
    (include_dir / "foo.h").write_text("struct BuildFoo {};\n", encoding="utf-8")
    (tmp_path / "main.cpp").write_text('#include <foo.h>\nint main() { return 0; }\n', encoding="utf-8")
    (tmp_path / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": "main.cpp",
                    "arguments": ["clang++", "-I", "toolchain/include", "-c", "main.cpp"],
                }
            ]
        ),
        encoding="utf-8",
    )

    document = analyze_repository(
        tmp_path,
        AnalysisConfig(language="cpp", context={"compile_commands": "compile_commands.json"}),
        deterministic=True,
        commit_sha="include-order",
    )
    imports = [edge for edge in document["edges"] if edge["relation_type"] == "imports"]

    assert any(edge["resolution_status"] == "resolved" and edge["target_id"].endswith("toolchain/include/foo.h:module") for edge in imports)


def test_c_family_fixture_passes_available_native_syntax_checks() -> None:
    gcc = shutil.which("gcc")
    gpp = shutil.which("g++")
    if gcc is None or gpp is None:
        pytest.skip("gcc and g++ are not available")

    subprocess.run(
        [
            gcc,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-fsyntax-only",
            "-I",
            str(FIXTURE / "c" / "include"),
            str(FIXTURE / "c" / "src" / "main.c"),
            str(FIXTURE / "c" / "src" / "common.c"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            gpp,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-fsyntax-only",
            "-I",
            str(FIXTURE / "cpp" / "include"),
            str(FIXTURE / "cpp" / "src" / "main.cpp"),
            str(FIXTURE / "cpp" / "src" / "derived.cpp"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
