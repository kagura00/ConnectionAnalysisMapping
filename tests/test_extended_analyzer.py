from __future__ import annotations

from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.language_registry import (
    concrete_languages,
    get_language_spec,
    language_for_path,
    supported_languages,
)

SAMPLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "vbnet": (
        "Program.vb",
        "Namespace Demo\nPublic Class Service\nPublic Sub Run()\nHelper()\nEnd Sub\nPrivate Sub Helper()\nEnd Sub\nEnd Class\nEnd Namespace\n",
        ("Service", "Run", "Helper"),
    ),
    "vba": (
        "Module.bas",
        "Attribute VB_Name = \"Module1\"\nPublic Sub Run()\nHelper\nEnd Sub\nPrivate Sub Helper()\nEnd Sub\n",
        ("Module1", "Run", "Helper"),
    ),
    "lua": (
        "app.lua",
        "local M = {}\nfunction M.run() return helper() end\nfunction helper() end\nreturn M\n",
        ("M.run", "helper"),
    ),
    "haskell": (
        "Main.hs",
        "module Main where\nimport Data.List\nhelper :: Int -> Int\nhelper x = x + 1\nmain = helper 1\n",
        ("helper", "main"),
    ),
    "perl": (
        "main.pl",
        "package Demo;\nuse strict;\nsub helper { return 1; }\nsub start { helper(); }\n",
        ("Demo", "helper", "start"),
    ),
    "matlab": (
        "main.m",
        "function y = helper(x)\ny = x + 1;\nend\nfunction start()\nhelper(1);\nend\n",
        ("helper", "start"),
    ),
    "cobol": (
        "main.cob",
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. DEMO.\n       PROCEDURE DIVISION.\n       MAIN SECTION.\n       RUN.\n           PERFORM HELPER.\n       HELPER.\n           GOBACK.\n",
        ("DEMO", "MAIN", "RUN", "HELPER"),
    ),
    "fortran": (
        "main.f90",
        "module demo\ncontains\nsubroutine helper(x)\nend subroutine helper\nsubroutine run(x)\ncall helper(x)\nend subroutine run\nend module demo\n",
        ("demo", "helper", "run"),
    ),
    "r": (
        "main.r",
        "helper <- function(x) x + 1\nstart <- function() helper(1)\n",
        ("helper", "start"),
    ),
    "objective-c": (
        "Worker.m",
        "@interface Worker : NSObject\n- (void)run;\n@end\n@implementation Worker\n- (void)run { [self helper]; }\n- (void)helper {}\n@end\n",
        ("Worker", "run", "helper"),
    ),
    "cuda": (
        "kernel.cu",
        "__global__ void kernel() {}\n__device__ int helper(int x) { return x; }\nint main() { helper(1); return 0; }\n",
        ("kernel", "helper", "main"),
    ),
    "groovy": (
        "Service.groovy",
        "package demo\nclass Service {\n  def run() { helper() }\n  def helper() {}\n}\n",
        ("demo", "Service", "run", "helper"),
    ),
    "fsharp": (
        "Demo.fs",
        "module Demo\nlet Helper() = 1\nlet Start() = Helper()\n",
        ("Demo", "Helper", "Start"),
    ),
    "assembly": (
        "main.asm",
        ".text\n_start:\n  call helper\n  ret\nhelper:\n  ret\n",
        ("_start", "helper"),
    ),
    "hcl": (
        "main.tf",
        "variable \"name\" {}\nmodule \"child\" { source = \"./child\" }\nresource \"aws_instance\" \"web\" { ami = var.name }\n",
        ("name", "child", "aws_instance.web"),
    ),
    "gdscript": (
        "player.gd",
        "extends Node\nclass_name Player\nfunc _ready():\n    helper()\nfunc helper():\n    pass\n",
        ("Player", "_ready", "helper"),
    ),
    "elixir": (
        "demo.ex",
        "defmodule Demo do\n  def helper(x), do: x + 1\n  def start, do: helper(1)\nend\n",
        ("Demo", "helper", "start"),
    ),
    "zig": (
        "main.zig",
        "const User = struct { value: i32 };\nfn helper(x: i32) i32 { return x; }\npub fn main() void { _ = helper(1); }\n",
        ("User", "helper", "main"),
    ),
    "julia": (
        "main.jl",
        "module Demo\nstruct User\n value::Int\nend\nfunction helper(x)\n x + 1\nend\nfunction start()\n helper(1)\nend\nend\n",
        ("Demo", "User", "helper", "start"),
    ),
    "pascal": (
        "demo.pas",
        "unit Demo;\ninterface\nuses SysUtils;\ntype TService = class\nend;\nimplementation\nprocedure Helper; begin end;\nprocedure TService.Run; begin Helper; end;\nend.\n",
        ("Demo", "TService", "Helper", "TService.Run"),
    ),
    "erlang": (
        "demo.erl",
        "-module(demo).\n-export([start/0]).\nhelper() -> ok.\nstart() -> helper().\n",
        ("demo", "helper", "start"),
    ),
}


@pytest.mark.parametrize("language", list(SAMPLES))
def test_extended_language_extracts_contract_nodes_and_is_deterministic(
    tmp_path: Path, language: str
) -> None:
    filename, source, expected_names = SAMPLES[language]
    (tmp_path / filename).write_text(source, encoding="utf-8")
    config = AnalysisConfig(language=language, include_tests=True)

    first = analyze_repository(tmp_path, config, deterministic=True, commit_sha="extended-fixture")
    second = analyze_repository(tmp_path, config, deterministic=True, commit_sha="extended-fixture")
    validate_document(first)
    assert first == second
    assert first["meta"]["languages"] == [language]
    assert any(node.get("extensions", {}).get("language") == language for node in first["nodes"])
    if language not in {"vba", "vbnet"}:
        assert not any(item["code"] == "parser_unavailable" for item in first["diagnostics"])
    names = {node["display_name"] for node in first["nodes"]}
    for expected_name in expected_names:
        assert expected_name in names, (language, expected_name, first["nodes"])


def test_individual_languages_and_js_ts_suffixes_are_registered() -> None:
    assert language_for_path("app.jsx", {"javascript"}) == "javascript"
    assert {".js", ".mjs", ".cjs", ".jsx"} <= set(get_language_spec("javascript").extensions)
    assert {".ts", ".mts", ".cts", ".tsx"} <= set(get_language_spec("typescript").extensions)
    for language in ("python", "html", "css", "javascript", "typescript", "lua", "zig"):
        assert concrete_languages(language) == (language,)
    assert "extended" not in supported_languages()
    with pytest.raises(ValueError, match="language must be one of"):
        concrete_languages("extended")


def test_extended_languages_merge_with_python_and_keep_language_labels(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    (tmp_path / "main.lua").write_text("function start() end\n", encoding="utf-8")
    document = analyze_repository(
        tmp_path,
        AnalysisConfig(language="mixed", languages=["python", "lua"], include_tests=True),
        deterministic=True,
        commit_sha="mixed-extended-fixture",
    )
    validate_document(document)
    assert document["meta"]["languages"] == ["python", "lua"]
    languages = {node.get("extensions", {}).get("language") for node in document["nodes"]}
    assert {"python", "lua"} <= languages


def test_extended_calls_ignore_comments_and_strings_and_mark_profile_edges(tmp_path: Path) -> None:
    (tmp_path / "app.lua").write_text(
        "function helper() end\n"
        "function start()\n"
        "  -- helper()\n"
        "  local text = \"helper()\"\n"
        "  helper()\n"
        "end\n",
        encoding="utf-8",
    )
    document = analyze_repository(tmp_path, AnalysisConfig(language="lua"), deterministic=True, commit_sha="negative-call")
    calls = [edge for edge in document["edges"] if edge["relation_type"] == "calls"]
    assert len(calls) == 1
    assert calls[0]["resolution_status"] == "resolved"
    assert calls[0]["provenance"] == "unknown"
    assert calls[0]["detail"]["expression"] == "helper(...)"


def test_extended_keeps_same_target_calls_at_distinct_source_spans(tmp_path: Path) -> None:
    (tmp_path / "app.lua").write_text(
        "function helper() end\n"
        "function start()\n"
        "  helper()\n"
        "  helper()\n"
        "end\n",
        encoding="utf-8",
    )
    document = analyze_repository(tmp_path, AnalysisConfig(language="lua"), deterministic=True, commit_sha="duplicate-fixture")
    calls = [edge for edge in document["edges"] if edge["relation_type"] == "calls"]

    assert len(calls) == 2
    assert {edge["source_span"]["start_line"] for edge in calls} == {3, 4}


def test_extended_scope_and_span_use_safe_boundaries_and_utf8_columns(tmp_path: Path) -> None:
    (tmp_path / "app.lua").write_text(
        "function helper() end\n"
        "function start()\n"
        "  local text = \"é\"; helper()\n"
        "end\n"
        "helper()\n",
        encoding="utf-8",
    )
    document = analyze_repository(tmp_path, AnalysisConfig(language="lua"), deterministic=True, commit_sha="scope-span")
    calls = [edge for edge in document["edges"] if edge["relation_type"] == "calls"]
    start = next(node for node in document["nodes"] if node["display_name"] == "start")
    module = next(node for node in document["nodes"] if node["kind"] == "module")
    inner_call = next(edge for edge in calls if edge["source_id"] == start["id"])
    module_call = next(edge for edge in calls if edge["source_id"] == module["id"])
    prefix = '  local text = "é"; '

    assert inner_call["source_span"]["start_col"] == len(prefix.encode("utf-8"))
    assert module_call["source_id"] == module["id"]


def test_haskell_function_application_is_a_call_relationship(tmp_path: Path) -> None:
    (tmp_path / "Main.hs").write_text(
        "module Main where\n{- helper 2 -}\nhelper x = x + 1\nmain = helper 1\nmessage = \"helper 3\"\n",
        encoding="utf-8",
    )
    document = analyze_repository(tmp_path, AnalysisConfig(language="haskell"), deterministic=True, commit_sha="haskell-call")
    calls = [edge for edge in document["edges"] if edge["relation_type"] == "calls"]
    assert any(
        edge["resolution_status"] == "resolved" and edge["detail"]["expression"] == "helper(...)"
        for edge in calls
    )


def test_extended_resolves_dotted_haskell_module_references(tmp_path: Path) -> None:
    (tmp_path / "Main.hs").write_text(
        "module Main where\nimport Data.List\nmain = sort [2, 1]\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    (data_dir / "List.hs").write_text("module Data.List where\nsort = id\n", encoding="utf-8")

    document = analyze_repository(
        tmp_path,
        AnalysisConfig(language="haskell"),
        deterministic=True,
        commit_sha="haskell-module-resolution",
    )
    imports = [edge for edge in document["edges"] if edge["relation_type"] == "imports"]

    assert any(
        edge["resolution_status"] == "resolved"
        and edge["target_id"].endswith("Data/List.hs:module")
        for edge in imports
    )


def test_cobol_fixed_format_comment_does_not_create_a_call(tmp_path: Path) -> None:
    (tmp_path / "main.cob").write_text(
        "123456* PERFORM HELPER\n"
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. DEMO.\n"
        "       PROCEDURE DIVISION.\n"
        "       MAIN.\n"
        "           PERFORM HELPER.\n"
        "       HELPER.\n"
        "           GOBACK.\n",
        encoding="utf-8",
    )
    document = analyze_repository(tmp_path, AnalysisConfig(language="cobol"), deterministic=True, commit_sha="cobol-comment")
    calls = [edge for edge in document["edges"] if edge["relation_type"] == "calls"]
    assert len(calls) == 1
    assert calls[0]["resolution_status"] == "resolved"


def test_matlab_and_objective_c_m_suffixes_are_disambiguated(tmp_path: Path) -> None:
    matlab = tmp_path / "analysis.m"
    objective_c = tmp_path / "Worker.m"
    matlab.write_text("function value = analysis(x)\nvalue = x;\nend\n", encoding="utf-8")
    objective_c.write_text(
        "@interface Worker : NSObject\n@end\n@implementation Worker\n@end\n",
        encoding="utf-8",
    )

    assert language_for_path(matlab, {"matlab", "objective-c"}) == "matlab"
    assert language_for_path(objective_c, {"matlab", "objective-c"}) == "objective-c"


def test_objective_c_multi_selector_call_keeps_a_method_connection(tmp_path: Path) -> None:
    (tmp_path / "Worker.m").write_text(
        "@interface Worker : NSObject\n"
        "- (void)helper:(id)value withValue:(id)other;\n"
        "- (void)run;\n"
        "@end\n"
        "@implementation Worker\n"
        "- (void)run { [self helper:@\"x\" withValue:@\"y\"]; }\n"
        "- (void)helper:(id)value withValue:(id)other {}\n"
        "@end\n",
        encoding="utf-8",
    )

    document = analyze_repository(tmp_path, AnalysisConfig(language="objective-c"), deterministic=True)
    calls = [edge for edge in document["edges"] if edge["relation_type"] == "calls"]
    assert any(edge["resolution_status"] == "resolved" for edge in calls)


def test_objective_c_distinguishes_multi_selector_declarations(tmp_path: Path) -> None:
    (tmp_path / "Worker.m").write_text(
        "@implementation Worker\n"
        "- (void)helper:(id)value;\n"
        "- (void)helper:(id)value withValue:(id)other {}\n"
        "@end\n",
        encoding="utf-8",
    )

    document = analyze_repository(tmp_path, AnalysisConfig(language="objective-c"), deterministic=True)
    method_names = {node["display_name"] for node in document["nodes"] if node["kind"] == "method"}

    assert {"helper:", "helper:withValue:"} <= method_names
