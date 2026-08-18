from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from connection_map import analysis_context
from connection_map.analysis_context import load_analysis_context
from connection_map.config import AnalysisConfig


def test_compile_commands_are_loaded_without_running_a_compiler(tmp_path: Path) -> None:
    include = tmp_path / "include"
    include.mkdir()
    database = tmp_path / "compile_commands.json"
    database.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "file": "main.cpp",
                    "arguments": ["clang++", "-I", "include", "-DVALUE=1", "-std=c++20", "-c", "main.cpp"],
                }
            ]
        ),
        encoding="utf-8",
    )
    context = load_analysis_context(
        tmp_path,
        AnalysisConfig(language="cpp", context={"compile_commands": "compile_commands.json"}),
    )

    command = context.compilation_database.for_file(tmp_path / "main.cpp", tmp_path)
    assert command is not None
    assert command.include_dirs == (include.resolve(),)
    assert command.defines == ("VALUE=1",)
    assert command.standard == "c++20"
    assert not list(tmp_path.glob(".connection-map-classpath-*"))


def test_classpath_index_reads_sources_and_archives_without_extracting_code(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "External.java").write_text(
        "package demo; public class External { public void ping() {} }\n",
        encoding="utf-8",
    )
    archive = tmp_path / "external.jar"
    with zipfile.ZipFile(archive, "w") as jar:
        jar.writestr("demo/Archived.class", b"not executed")
        jar.writestr("demo/Source.java", "package demo; class Source {}\n")

    context = load_analysis_context(
        tmp_path,
        AnalysisConfig(language="java", context={"source_roots": ["sources"], "classpath": ["external.jar"]}),
    )
    assert context.type_index.has_type("demo.External")
    assert context.type_index.has_type("demo.Archived")
    assert context.type_index.has_type("demo.Source")
    assert context.type_index.has_method("demo.External", "ping")
    assert context.type_index.archives == [str(archive.resolve())]
    assert not list(tmp_path.glob(".connection-map-classpath-*"))


def test_context_rejects_unknown_keys() -> None:
    config = AnalysisConfig(language="python", context={"execute": "never"})
    with pytest.raises(ValueError, match="unsupported keys"):
        config.validate()


def test_classpath_archive_skips_unsafe_members(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.jar"
    with zipfile.ZipFile(archive, "w") as jar:
        jar.writestr("../Evil.java", "package evil; class Evil {}\n")

    context = load_analysis_context(
        tmp_path,
        AnalysisConfig(language="java", context={"classpath": ["unsafe.jar"]}),
    )

    assert not context.type_index.has_type("Evil")
    assert any("unsafe" in diagnostic["message"] for diagnostic in context.diagnostics)


def test_windows_compile_command_preserves_quoted_include_path(monkeypatch) -> None:
    monkeypatch.setattr(analysis_context.os, "name", "nt")
    arguments = analysis_context._command_arguments(
        {"command": r'cl.exe /I"C:\Program Files\SDK\include" /DVALUE=1 main.cpp'}
    )

    assert arguments == (
        "cl.exe",
        r'/IC:\Program Files\SDK\include',
        "/DVALUE=1",
        "main.cpp",
    )


def test_mixed_jvm_and_dotnet_classpath_sources_are_language_scoped(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "JavaOnly.java").write_text("package demo; class JavaOnly {}\n", encoding="utf-8")
    (source_root / "CSharpOnly.cs").write_text("public class CSharpOnly {}\n", encoding="utf-8")

    java = load_analysis_context(
        tmp_path,
        AnalysisConfig(language="java", context={"source_roots": ["sources"]}),
    )
    csharp = load_analysis_context(
        tmp_path,
        AnalysisConfig(language="csharp", context={"source_roots": ["sources"]}),
    )

    assert java.type_index.has_type("demo.JavaOnly")
    assert not java.type_index.has_type("CSharpOnly")
    assert csharp.type_index.has_type("CSharpOnly")
    assert not csharp.type_index.has_type("JavaOnly")
