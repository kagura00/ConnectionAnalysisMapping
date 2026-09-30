import json
from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository
from connection_map.cli import main
from connection_map.config import AnalysisConfig, discover_source_files
from connection_map.query import GraphQuery


@pytest.mark.parametrize("language,languages", [("web", []), ("mixed", ["python", "html", "javascript", "css"])])
def test_inline_code_is_explicitly_partial_in_cli_and_context(tmp_path: Path, language, languages):
    (tmp_path / "index.html").write_text(
        '<style>main {color:red}</style><main></main><script>function start() { fetch("/data"); }</script>',
        encoding="utf-8",
    )
    config = AnalysisConfig(language=language, languages=languages, include_tests=True)
    config_path = tmp_path / "analysis.toml"
    config_path.write_text(config.to_toml(), encoding="utf-8")
    output = tmp_path / "analysis.json"
    assert main(["analyze", "--root", str(tmp_path), "--config", str(config_path),
                 "--output", str(output), "--fail-on-error"]) == 3
    document = json.loads(output.read_text(encoding="utf-8"))
    coverage = document["meta"]["extensions"]["coverage"]
    assert coverage["status"] == "partial" and coverage["error_count"] == 0
    assert len(coverage["extraction_limitations"]) == 2
    assert all("index.html:1" in item for item in coverage["extraction_limitations"])
    focus = next(n["id"] for n in document["nodes"] if n["kind"] == "module")
    context = GraphQuery(document).neighborhood(focus)
    assert context["coverage"]["extraction_limitations"] == coverage["extraction_limitations"]


def test_mixed_objc_and_cpp_headers_have_single_owner(tmp_path: Path):
    (tmp_path / "Widget.h").write_text('@interface Widget\n- (void)run;\n@end\n', encoding="utf-8")
    (tmp_path / "Plain.h").write_text('class Plain { public: void run(); };\n', encoding="utf-8")
    (tmp_path / "Bridge.h").write_text('#import "Widget.h"\n', encoding="utf-8")
    config = AnalysisConfig(language="mixed", languages=["cpp", "objective-c"], include_tests=True)
    document = analyze_repository(tmp_path, config, deterministic=True)
    modules = [n for n in document["nodes"] if n["kind"] == "module" and n.get("file")]
    assert {(n["file"], n["extensions"]["language"]) for n in modules} == {
        ("Widget.h", "objective-c"), ("Bridge.h", "objective-c"), ("Plain.h", "cpp"),
    }
    assert len(modules) == 3
    assert not any(d["severity"] == "error" for d in document["diagnostics"])
    assert "_discovery_languages" not in document["meta"]["settings"]
    assert analyze_repository(tmp_path, config, deterministic=True) == document


def test_flutter_cache_defaults_and_explicit_opt_in(tmp_path: Path):
    sources = {
        "lib/main.dart": "void main() {}",
        "ios/Runner/App.swift": "class App {}",
        "android/app/Main.kt": "class Main",
        ".dart_tool/flutter_build/registrant.dart": "void register() {}",
        "ios/Flutter/ephemeral/debugger.py": "def helper(): pass",
        "windows/flutter/ephemeral/helper.cpp": "void helper() {}",
    }
    for relative, content in sources.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    config = AnalysisConfig(language="mixed", languages=["dart", "swift", "kotlin", "python", "cpp"])
    selected, _ = discover_source_files(tmp_path, config)
    assert {p.relative_to(tmp_path).as_posix() for p in selected} == {
        "lib/main.dart", "ios/Runner/App.swift", "android/app/Main.kt",
    }
    explicit = AnalysisConfig(language=config.language, languages=config.languages, exclude=[".git/**"])
    selected, _ = discover_source_files(tmp_path, explicit)
    assert {p.relative_to(tmp_path).as_posix() for p in selected} == set(sources)
