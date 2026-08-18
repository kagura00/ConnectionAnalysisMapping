"""Validate the extended language adapters against a fresh, non-golden corpus.

The corpus is deliberately created in a temporary directory at runtime.  It is
not the development fixture and no source from the generated corpus is ever
imported, compiled, or executed.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from connection_map.analyzer import analyze_repository
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document

SAMPLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "vbnet": (
        "Fresh.vb",
        "Namespace Fresh\nPublic Class Worker\nPublic Sub Execute()\nAssist()\nEnd Sub\nPrivate Sub Assist()\nEnd Sub\nEnd Class\nEnd Namespace\n",
        ("Worker", "Execute", "Assist"),
    ),
    "vba": (
        "Fresh.bas",
        'Attribute VB_Name = "FreshModule"\nPublic Sub Execute()\nAssist\nEnd Sub\nPrivate Sub Assist()\nEnd Sub\n',
        ("FreshModule", "Execute", "Assist"),
    ),
    "lua": (
        "fresh.lua",
        "local M = {}\nfunction M.execute() return assist() end\nfunction assist() end\nreturn M\n",
        ("M.execute", "assist"),
    ),
    "haskell": (
        "Fresh.hs",
        "module Fresh where\nassist :: Int -> Int\nassist x = x + 2\nexecute = assist 2\n",
        ("assist", "execute"),
    ),
    "perl": (
        "Fresh.pl",
        "package Fresh;\nsub assist { return 2; }\nsub execute { assist(); }\n",
        ("Fresh", "assist", "execute"),
    ),
    "matlab": (
        "fresh.m",
        "function y = assist(x)\ny = x + 2;\nend\nfunction execute()\nassist(2);\nend\n",
        ("assist", "execute"),
    ),
    "cobol": (
        "fresh.cob",
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. FRESH.\n       PROCEDURE DIVISION.\n       MAIN SECTION.\n       EXECUTE.\n           PERFORM ASSIST.\n       ASSIST.\n           GOBACK.\n",
        ("FRESH", "MAIN", "EXECUTE", "ASSIST"),
    ),
    "fortran": (
        "fresh.f90",
        "module fresh\ncontains\nsubroutine assist(x)\nend subroutine assist\nsubroutine execute(x)\ncall assist(x)\nend subroutine execute\nend module fresh\n",
        ("fresh", "assist", "execute"),
    ),
    "r": (
        "fresh.r",
        "assist <- function(x) x + 2\nexecute <- function() assist(2)\n",
        ("assist", "execute"),
    ),
    "objective-c": (
        "Fresh.m",
        "@interface FreshWorker : NSObject\n- (void)execute;\n@end\n@implementation FreshWorker\n- (void)execute { [self assist]; }\n- (void)assist {}\n@end\n",
        ("FreshWorker", "execute", "assist"),
    ),
    "cuda": (
        "fresh.cu",
        "__global__ void launch() {}\n__device__ int assist(int x) { return x; }\nint execute() { assist(2); return 0; }\n",
        ("launch", "assist", "execute"),
    ),
    "groovy": (
        "Fresh.groovy",
        "package fresh\nclass Worker {\n  def execute() { assist() }\n  def assist() {}\n}\n",
        ("fresh", "Worker", "execute", "assist"),
    ),
    "fsharp": (
        "Fresh.fs",
        "module Fresh\nlet Assist() = 2\nlet Execute() = Assist()\n",
        ("Fresh", "Assist", "Execute"),
    ),
    "assembly": (
        "fresh.asm",
        ".text\nentry:\n  call assist\n  ret\nassist:\n  ret\n",
        ("entry", "assist"),
    ),
    "hcl": (
        "fresh.tf",
        'variable "region" {}\nresource "service" "api" { region = var.region }\n',
        ("region", "service.api"),
    ),
    "gdscript": (
        "fresh.gd",
        "class_name FreshPlayer\nfunc _ready():\n    assist()\nfunc assist():\n    pass\n",
        ("FreshPlayer", "_ready", "assist"),
    ),
    "elixir": (
        "fresh.ex",
        "defmodule Fresh do\n  def assist, do: 2\n  def execute, do: assist()\nend\n",
        ("Fresh", "assist", "execute"),
    ),
    "zig": (
        "fresh.zig",
        "const Worker = struct { value: i32 };\nfn assist(x: i32) i32 { return x; }\npub fn execute() void { _ = assist(2); }\n",
        ("Worker", "assist", "execute"),
    ),
    "julia": (
        "fresh.jl",
        "module Fresh\nstruct Worker\n value::Int\nend\nfunction assist(x)\n x + 2\nend\nfunction execute()\n assist(2)\nend\nend\n",
        ("Fresh", "Worker", "assist", "execute"),
    ),
    "pascal": (
        "Fresh.pas",
        "unit Fresh;\ninterface\ntype TWorker = class\nend;\nimplementation\nprocedure Assist; begin end;\nprocedure TWorker.Execute; begin Assist; end;\nend.\n",
        ("Fresh", "TWorker", "Assist", "TWorker.Execute"),
    ),
    "erlang": (
        "fresh.erl",
        "-module(fresh).\n-export([execute/0]).\nassist() -> ok.\nexecute() -> assist().\n",
        ("fresh", "assist", "execute"),
    ),
}


def _validate_one(root: Path, language: str, sample: tuple[str, str, tuple[str, ...]]) -> dict[str, object]:
    filename, source, expected_names = sample
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(source, encoding="utf-8")
    config = AnalysisConfig(language=language, include_tests=True)
    first = analyze_repository(root, config, deterministic=True, commit_sha="fresh-extended-corpus")
    second = analyze_repository(root, config, deterministic=True, commit_sha="fresh-extended-corpus")
    validate_document(first)
    if first != second:
        raise AssertionError(f"non-deterministic result for {language}")
    names = {node.get("display_name") for node in first["nodes"]}
    missing_names = sorted(set(expected_names) - names)
    blocked = [
        diagnostic
        for diagnostic in first["diagnostics"]
        if diagnostic.get("code") in {"parse_error", "parser_recovery"}
    ]
    parser_unavailable = [
        diagnostic
        for diagnostic in first["diagnostics"]
        if diagnostic.get("code") == "parser_unavailable"
    ]
    if missing_names or blocked or (language != "vba" and parser_unavailable):
        raise AssertionError(
            {
                "language": language,
                "missing_names": missing_names,
                "blocked": blocked,
                "parser_unavailable": parser_unavailable,
            }
        )
    return {
        "language": language,
        "file": filename,
        "nodes": len(first["nodes"]),
        "edges": len(first["edges"]),
        "diagnostics": len(first["diagnostics"]),
        "resolved_edges": sum(edge.get("resolution_status") == "resolved" for edge in first["edges"]),
        "parser_unavailable": len(parser_unavailable),
        "deterministic": True,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="connection-map-extended-validation-") as temporary:
        root = Path(temporary)
        cases = [_validate_one(root / language, language, sample) for language, sample in SAMPLES.items()]
        all_document = analyze_repository(
            root,
            AnalysisConfig(language="mixed", languages=list(SAMPLES)),
            deterministic=True,
            commit_sha="fresh-extended-corpus",
        )
        validate_document(all_document)
        result = {
            "corpus": "fresh-runtime-generated",
            "languages": list(SAMPLES),
            "cases": cases,
            "all_languages": {
                "selected": all_document["meta"]["languages"],
                "modules": sum(node.get("kind") == "module" for node in all_document["nodes"]),
                "nodes": len(all_document["nodes"]),
                "edges": len(all_document["edges"]),
                "diagnostics": len(all_document["diagnostics"]),
                "blocked_diagnostics": [
                    diagnostic.get("code")
                    for diagnostic in all_document["diagnostics"]
                    if diagnostic.get("code") in {"parse_error", "parser_recovery"}
                ],
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
