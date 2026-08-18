from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository as dispatch_analysis
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.java_analyzer import analyze_repository

FIXTURE = Path(__file__).parent / "fixtures" / "java_repo"
PROBE = Path(__file__).parent / "tools" / "java" / "CompilerApiProbe.java"


def _config(*, include_tests: bool = True) -> AnalysisConfig:
    return AnalysisConfig(
        language="java",
        include_tests=include_tests,
        exclude=[".git/**", ".connection-map/**"],
    )


def test_java_fixture_extracts_definitions_and_relations() -> None:
    document = analyze_repository(FIXTURE, _config(), deterministic=True, commit_sha="fixture-commit")
    validate_document(document)

    assert document["meta"]["language"] == "java"
    assert document["meta"]["languages"] == ["java"]
    assert document["meta"]["runtime"]["grammars"] == ["java"]

    nodes = document["nodes"]
    assert any(node["kind"] == "namespace" and node["qualified_name"] == "demo" for node in nodes)
    assert any(node["kind"] == "class" and node["qualified_name"] == "demo.Derived" for node in nodes)
    assert any(node["kind"] == "interface" and node["qualified_name"] == "demo.Marker" for node in nodes)
    assert any(node["kind"] == "type" and node["qualified_name"] == "demo.Kind" for node in nodes)
    assert any(node["kind"] == "type" and node["qualified_name"] == "demo.Point" for node in nodes)
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "demo.Derived.local(0)"
        and node["return_behavior"] == "returns_value"
        for node in nodes
    )
    assert any(
        node["kind"] == "method"
        and node["qualified_name"] == "demo.Derived.<init>(1)"
        and node["extensions"]["constructor"] is True
        for node in nodes
    )
    assert any(node["kind"] == "method" and node["qualified_name"] == "demo.support.Helper.help(1)" for node in nodes)

    edges = document["edges"]
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved" for edge in edges)
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "external" for edge in edges)
    assert any(edge["relation_type"] == "inherits" and edge["resolution_status"] == "resolved" for edge in edges)
    assert any(
        edge["relation_type"] == "inherits"
        and edge["resolution_status"] == "external"
        and edge["detail"]["reference"] == "Runnable"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["callee"] == "local"
        for edge in edges
    )
    assert any(
        edge["relation_type"] == "calls"
        and edge["resolution_status"] == "resolved"
        and edge["detail"]["callee"] == "help"
        for edge in edges
    )
    assert not document["diagnostics"]


def test_java_test_files_are_excluded_by_default() -> None:
    document = analyze_repository(FIXTURE, _config(include_tests=False), deterministic=True, commit_sha="fixture-commit")
    assert not any(node["file"] and "DerivedTest.java" in node["file"] for node in document["nodes"])
    assert any(diagnostic["file"] == "src/test/java/demo/DerivedTest.java" for diagnostic in document["diagnostics"])


def test_java_dispatch_is_deterministic() -> None:
    config = _config()
    first = dispatch_analysis(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    second = dispatch_analysis(FIXTURE, config, deterministic=True, commit_sha="fixture-commit")
    validate_document(first)
    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second,
        ensure_ascii=False,
        sort_keys=True,
    )


def test_java_fixture_matches_javac_compiler_tree_api(tmp_path: Path) -> None:
    javac = shutil.which("javac")
    java = shutil.which("java")
    if javac is None or java is None:
        pytest.skip("javac and java are not available")

    classes = tmp_path / "classes"
    classes.mkdir()
    source_files = sorted(str(path) for path in FIXTURE.rglob("*.java"))
    subprocess.run(
        [javac, "-proc:none", "-d", str(classes), *source_files],
        check=True,
        capture_output=True,
        text=True,
    )
    probe_classes = tmp_path / "probe-classes"
    probe_classes.mkdir()
    subprocess.run(
        [javac, "-proc:none", "-d", str(probe_classes), str(PROBE)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [java, "-cp", str(probe_classes), "CompilerApiProbe", str(FIXTURE)],
        check=True,
        capture_output=True,
        text=True,
    )
    names = set(result.stdout.splitlines())
    assert {"Base", "Derived", "Marker", "Kind", "Point", "Helper"} <= names
    assert {"baseValue", "run", "local", "values", "mark", "help"} <= names


def test_java_classpath_method_context_creates_external_target(tmp_path: Path) -> None:
    source_root = tmp_path / "classpath"
    (source_root / "demo").mkdir(parents=True)
    (source_root / "demo" / "External.java").write_text(
        "package demo; public class External { public static void ping() {} }\n",
        encoding="utf-8",
    )
    (tmp_path / "Main.java").write_text(
        "class Main { void run() { External.ping(); } }\n",
        encoding="utf-8",
    )
    config = AnalysisConfig(
        language="java",
        include_tests=True,
        exclude=[".git/**", ".connection-map/**", "classpath/**"],
        context={"source_roots": ["classpath"]},
    )

    document = analyze_repository(tmp_path, config, deterministic=True, commit_sha="classpath")

    targets = {
        node["id"]: node
        for node in document["nodes"]
        if node.get("extensions", {}).get("context_source") == "classpath"
    }
    assert any(node["qualified_name"] == "call:External.ping/0" for node in targets.values())
    assert any(
        edge["relation_type"] == "calls" and edge["target_id"] in targets
        for edge in document["edges"]
    )
