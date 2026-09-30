from __future__ import annotations

from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository


def call_from(document, file):
    nodes = {node["id"]: node for node in document["nodes"]}
    edge = next(edge for edge in document["edges"]
                if edge["relation_type"] == "calls" and nodes[edge["source_id"]]["file"] == file)
    return edge, nodes[edge["target_id"]]


@pytest.mark.parametrize("caller", ["from facade import public\npublic()\n", "import facade as db\ndb.public()\n"])
def test_reexports_follow_alias_chain_and_record_import_evidence(tmp_path: Path, caller: str):
    (tmp_path / "implementation.py").write_text("def implementation(): pass\n", encoding="utf-8")
    (tmp_path / "middle.py").write_text("from implementation import implementation as alias\n", encoding="utf-8")
    (tmp_path / "facade.py").write_text("from middle import alias as public\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(caller, encoding="utf-8")
    document = analyze_repository(tmp_path, deterministic=True)
    edge, target = call_from(document, "main.py")
    assert edge["resolution_status"] == "resolved"
    assert target["file"] == "implementation.py"
    evidence = edge["detail"]["resolution_evidence"]
    assert evidence["strategy"] == "module_reexport"
    assert [item["file"] for item in evidence["imports"]] == ["facade.py", "middle.py"]
    assert all(item["span"]["start_line"] == 1 for item in evidence["imports"])


def test_package_facade_supports_relative_import_and_submodule(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("from package import child\nfrom .child import helper\n", encoding="utf-8")
    (package / "child.py").write_text("def helper(): pass\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from package import helper\nhelper()\n", encoding="utf-8")
    document = analyze_repository(tmp_path, deterministic=True)
    edge, target = call_from(document, "main.py")
    assert edge["resolution_status"] == "resolved"
    assert target["file"] == "package/child.py"
    assert any(edge["relation_type"] == "imports" and edge["resolution_status"] == "resolved"
               and edge["target_id"] == "python:package/child.py:module" for edge in document["edges"])


@pytest.mark.parametrize("facade", [
    "from implementation import helper\nhelper = replacement\n",
    "if flag:\n    from implementation import helper\n",
    "from other import helper\n",  # cyclic exports, below
])
def test_ambiguous_and_cyclic_exports_remain_unresolved(tmp_path: Path, facade: str):
    (tmp_path / "implementation.py").write_text("def helper(): pass\n", encoding="utf-8")
    (tmp_path / "facade.py").write_text(facade, encoding="utf-8")
    (tmp_path / "other.py").write_text("from facade import helper\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("import facade\nfacade.helper()\n", encoding="utf-8")
    document = analyze_repository(tmp_path, deterministic=True)
    edge, target = call_from(document, "main.py")
    assert edge["resolution_status"] == "unresolved"
    assert target["kind"] == "unknown"


@pytest.mark.parametrize("caller", [
    "from facade import helper\nhelper = replacement\nhelper()\n",
    "from facade import helper\ndef run(helper):\n    helper()\n",
    "import facade\ndef run(facade):\n    facade.helper()\n",
])
def test_reexport_does_not_bypass_local_or_module_shadowing(tmp_path: Path, caller: str):
    (tmp_path / "implementation.py").write_text("def helper(): pass\n", encoding="utf-8")
    (tmp_path / "facade.py").write_text("from implementation import helper\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(caller, encoding="utf-8")
    edge, _ = call_from(analyze_repository(tmp_path), "main.py")
    assert edge["resolution_status"] == "unresolved"
