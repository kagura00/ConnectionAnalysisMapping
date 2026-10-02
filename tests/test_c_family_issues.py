from __future__ import annotations

import json
from pathlib import Path

from connection_map.c_family_analyzer import analyze_repository
from connection_map.cli import main
from connection_map.config import AnalysisConfig
from connection_map.evidence import coverage_summary


def _analyze(root: Path, files: dict[str, str]) -> dict:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.encode("utf-8"))
    return analyze_repository(
        root,
        AnalysisConfig(language="cpp", include_tests=True),
        deterministic=True,
        commit_sha="c-family-issues-test",
    )


def _nodes(document: dict) -> list[dict]:
    return [node for node in document["nodes"] if node.get("file")]


def _parent(document: dict, node: dict) -> dict | None:
    return next((item for item in document["nodes"] if item["id"] == node.get("parent_id")), None)


def test_parse_diagnostics_localize_error_missing_token_and_explain_fragments(tmp_path: Path) -> None:
    document = _analyze(
        tmp_path,
        {
            "broken.inl": (
                "namespace fixture {\r\n"
                "int before() { return 1; }\r\n"
                "int broken() { return 2; @ }\r\n"
                "}\r\n"
            ),
            "eof.ipp": "int partial() { return 3;",
        },
    )

    summaries = {
        item["file"]: item for item in document["diagnostics"] if item["code"] == "parse_error"
    }
    assert set(summaries) == {"broken.inl", "eof.ipp"}
    assert summaries["broken.inl"]["span"] is None
    fragment = summaries["broken.inl"]["details"]
    assert fragment["analysis_kind"] == "standalone_include_fragment"
    assert "function" in fragment["extracted_kind_counts"]
    assert fragment["details_emitted"] == 1
    assert fragment["details_omitted"] == 0

    error = next(
        item for item in document["diagnostics"]
        if item["code"] == "parse_error_node" and item["file"] == "broken.inl"
    )
    assert error["span"] == {"start_line": 3, "start_col": 25, "end_line": 3, "end_col": 26}
    missing = next(
        item for item in document["diagnostics"]
        if item["code"] == "parse_missing_token" and item["file"] == "eof.ipp"
    )
    assert missing["details"]["node_type"] == "}"
    assert "partial" in missing["details"]["context_excerpt"]
    assert "partial" in missing["message"]
    assert len(missing["details"]["context_excerpt"]) <= 120
    assert missing["span"]["start_line"] == missing["span"]["end_line"] == 1
    assert missing["span"]["start_col"] == missing["span"]["end_col"]

    coverage = coverage_summary(document)
    assert coverage["status"] == "partial"
    assert coverage["files_with_errors"] == ["broken.inl", "eof.ipp"]


def test_parse_diagnostic_details_are_bounded_and_report_omitted_count(tmp_path: Path) -> None:
    source = "".join(f"int broken_{index}() {{ @; }}\n" for index in range(40))
    document = _analyze(tmp_path, {"many.cpp": source})

    summary = next(item for item in document["diagnostics"] if item["code"] == "parse_error")
    local = [
        item for item in document["diagnostics"]
        if item["file"] == "many.cpp" and item["code"] in {"parse_error_node", "parse_missing_token"}
    ]
    details = summary["details"]
    assert len(local) <= details["detail_limit"] == 24
    assert details["details_emitted"] == len(local)
    assert details["details_omitted"] == (
        details["error_node_count"] + details["missing_token_count"] - len(local)
    )
    assert details["details_omitted"] > 0


def test_fail_on_error_still_writes_partial_graph_and_returns_three(tmp_path: Path, capsys) -> None:
    (tmp_path / "broken.cpp").write_text("int broken() { @; }\n", encoding="utf-8")
    config = tmp_path / "cpp.toml"
    config.write_text(
        '[analysis]\nlanguage = "cpp"\nlanguages = ["cpp"]\ninclude = ["**/*.cpp"]\n',
        encoding="utf-8",
    )
    output = tmp_path / "partial.json"

    code = main([
        "analyze", "--root", str(tmp_path), "--config", str(config), "--output", str(output),
        "--deterministic", "--fail-on-error",
    ])

    assert code == 3
    assert output.is_file()
    written = json.loads(output.read_text(encoding="utf-8"))
    assert coverage_summary(written)["status"] == "partial"
    assert "partial" in capsys.readouterr().err


def test_friend_callables_have_semantic_owners_and_hidden_friends_stay_hidden(tmp_path: Path) -> None:
    document = _analyze(
        tmp_path,
        {
            "sample.cpp": (
                "namespace demo {\n"
                "struct Target { void relative(); void absolute(); };\n"
                "class Grant {\n"
                "  int stored();\n"
                "  friend void hidden(Grant&);\n"
                "  friend void inline_friend() { stored(); auto nested = [] { stored(); }; }\n"
                "  friend void outside_friend(Grant&);\n"
                "  friend void Target::relative();\n"
                "  friend void ::demo::Target::absolute();\n"
                "  friend class Ally;\n"
                "};\n"
                "void caller() { hidden(Grant{}); }\n"
                "void outside_friend(Grant&) { stored(); }\n"
                "}\n"
            )
        },
    )
    nodes = _nodes(document)
    friend_nodes = [node for node in nodes if node.get("extensions", {}).get("friend_declaration")]

    hidden = next(node for node in friend_nodes if node["display_name"] == "hidden")
    assert hidden["kind"] == "function"
    assert hidden["qualified_name"] == "demo::hidden"
    assert _parent(document, hidden)["qualified_name"] == "demo"
    assert hidden["extensions"]["ordinary_lookup"] == "hidden"
    assert hidden["extensions"]["friend_of"] == "demo::Grant"

    relative = next(node for node in friend_nodes if node["display_name"] == "relative")
    absolute = next(node for node in friend_nodes if node["display_name"] == "absolute")
    for node, name in ((relative, "demo::Target::relative"), (absolute, "demo::Target::absolute")):
        assert node["kind"] == "method"
        assert node["qualified_name"] == name
        assert _parent(document, node)["qualified_name"] == "demo::Target"

    assert not any(node["display_name"] == "Ally" and node["kind"] in {"function", "method"} for node in nodes)
    hidden_calls = [
        edge for edge in document["edges"]
        if edge["relation_type"] == "calls" and edge.get("detail", {}).get("callee") == "hidden"
    ]
    assert len(hidden_calls) == 1
    assert hidden_calls[0]["resolution_status"] != "resolved"
    assert hidden_calls[0]["target_id"] != hidden["id"]

    stored_calls = [
        edge for edge in document["edges"]
        if edge["relation_type"] == "calls" and edge.get("detail", {}).get("callee") == "stored"
    ]
    assert len(stored_calls) == 3
    resolved = [edge for edge in stored_calls if edge["resolution_status"] == "resolved"]
    assert len(resolved) == 1
    target = next(node for node in nodes if node["id"] == resolved[0]["target_id"])
    assert target["qualified_name"] == "demo::Grant::stored"


def test_visible_namespace_prototype_exposes_only_matching_hidden_friend_definition(tmp_path: Path) -> None:
    document = _analyze(
        tmp_path,
        {
            "friend.cpp": (
                "namespace demo {\n"
                "class Grant {\n"
                "  friend int exposed(Grant&) { return 1; }\n"
                "  friend double exposed(double value) { return value; }\n"
                "};\n"
                "int exposed(Grant&);\n"
                "int caller(Grant& value) { return exposed(value); }\n"
                "}\n"
            )
        },
    )
    nodes = _nodes(document)
    friends = [
        node for node in nodes
        if node.get("display_name") == "exposed" and node.get("extensions", {}).get("friend_declaration")
    ]
    int_friend = next(node for node in friends if node["signature"].startswith("int exposed(Grant&"))
    double_friend = next(node for node in friends if node["signature"].startswith("double exposed(double"))
    assert int_friend["extensions"]["friend_of"] == double_friend["extensions"]["friend_of"] == "demo::Grant"

    calls = [
        edge for edge in document["edges"]
        if edge["relation_type"] == "calls" and edge.get("detail", {}).get("callee") in {"exposed", "demo::exposed"}
    ]
    assert len(calls) == 1
    assert calls[0]["resolution_status"] == "resolved"
    assert calls[0]["target_id"] == int_friend["id"]
    assert calls[0]["target_id"] != double_friend["id"]


def test_friend_header_prototype_binds_separate_namespace_definition_and_caller(tmp_path: Path) -> None:
    document = _analyze(
        tmp_path,
        {
            "api.hpp": (
                "namespace demo {\n"
                "class Grant { friend int exposed(Grant&); };\n"
                "int exposed(Grant&);\n"
                "}\n"
            ),
            "implementation.cpp": (
                '#include "api.hpp"\n'
                "namespace demo { int exposed(Grant&) { return 1; } }\n"
            ),
            "caller.cpp": (
                '#include "api.hpp"\n'
                "int caller(demo::Grant& value) { return demo::exposed(value); }\n"
            ),
        },
    )
    nodes = _nodes(document)
    friend = next(
        node for node in nodes
        if node["file"] == "api.hpp" and node.get("extensions", {}).get("friend_declaration")
    )
    assert friend["kind"] == "function"
    assert friend["qualified_name"] == "demo::exposed"
    assert _parent(document, friend)["qualified_name"] == "demo"
    assert friend["extensions"]["friend_of"] == "demo::Grant"

    definition = next(
        node for node in nodes
        if node["file"] == "implementation.cpp" and node["qualified_name"] == "demo::exposed"
    )
    assert definition["kind"] == "function"
    assert _parent(document, definition)["qualified_name"] == "demo"
    call = next(
        edge for edge in document["edges"]
        if edge["relation_type"] == "calls" and edge.get("detail", {}).get("callee") in {"exposed", "demo::exposed"}
    )
    assert call["resolution_status"] == "resolved"
    assert call["target_id"] == definition["id"]


def test_internal_hidden_friend_definition_does_not_cross_anonymous_namespaces(tmp_path: Path) -> None:
    document = _analyze(
        tmp_path,
        {
            "internal.cpp": (
                "namespace {\n"
                "struct Grant { friend int exposed(Grant&) { return 1; } };\n"
                "int exposed(Grant&);\n"
                "int local_caller(Grant& value) { return exposed(value); }\n"
                "}\n"
            ),
            "caller.cpp": (
                "namespace {\n"
                "struct Grant;\n"
                "int exposed(Grant&);\n"
                "int other_caller(Grant& value) { return exposed(value); }\n"
                "}\n"
            ),
        },
    )
    nodes = _nodes(document)
    internal_friend = next(
        node for node in nodes
        if node["file"] == "internal.cpp" and node.get("extensions", {}).get("friend_declaration")
    )
    assert internal_friend["qualified_name"].endswith("::exposed")
    assert internal_friend["extensions"]["friend_of"].endswith("::Grant")

    calls = [
        edge for edge in document["edges"]
        if edge["relation_type"] == "calls" and edge.get("detail", {}).get("callee") in {"exposed", "demo::exposed"}
    ]
    source_files = {
        edge["source_id"]: next(node for node in nodes if node["id"] == edge["source_id"])["file"]
        for edge in calls
    }
    by_file = {source_files[edge["source_id"]]: edge for edge in calls}
    assert set(by_file) == {"internal.cpp", "caller.cpp"}
    assert by_file["internal.cpp"]["target_id"] == internal_friend["id"]
    other_prototype = next(
        node for node in nodes
        if node["file"] == "caller.cpp" and node["display_name"] == "exposed"
        and node.get("extensions", {}).get("declaration_kind") == "prototype"
        and not node.get("extensions", {}).get("friend_declaration")
    )
    assert by_file["caller.cpp"]["target_id"] == other_prototype["id"]
    assert by_file["caller.cpp"]["target_id"] != internal_friend["id"]


def test_qualified_friend_with_unknown_owner_is_reported_as_uncertain(tmp_path: Path) -> None:
    document = _analyze(
        tmp_path,
        {"sample.cpp": "namespace demo { class Grant { friend void Missing::unknown(); }; }\n"},
    )

    assert not any(node["display_name"] == "unknown" for node in _nodes(document))
    diagnostic = next(item for item in document["diagnostics"] if item["code"] == "friend_scope_unresolved")
    assert diagnostic["details"]["owner_resolution"] == "uncertain"
    assert diagnostic["span"]["start_line"] == 1


def test_friend_member_type_aliases_stay_uncertain_instead_of_becoming_owners(tmp_path: Path) -> None:
    for spelling, declaration in (("typedef", "typedef Actual Alias;"), ("using", "using Alias = Actual;")):
        document = _analyze(
            tmp_path / spelling,
            {"alias.cpp": (
                "struct Actual { void member(); };\n"
                + declaration + "\n"
                "struct Grant { friend void Alias::member(); };\n"
            )},
        )
        nodes = _nodes(document)
        assert any(node["qualified_name"] == "Actual::member" for node in nodes)
        assert not any(node["qualified_name"] == "Alias::member" for node in nodes)
        assert not any(node.get("extensions", {}).get("friend_declaration") for node in nodes)
        diagnostic = next(item for item in document["diagnostics"] if item["code"] == "friend_scope_unresolved")
        assert diagnostic["details"]["owner_resolution"] == "uncertain"
        assert diagnostic["details"]["name"] == "Alias::member"


def test_repeated_compact_namespace_keeps_header_friend_and_definition_together(tmp_path: Path) -> None:
    document = _analyze(
        tmp_path,
        {
            "api.hpp": (
                "namespace demo::inner {\n"
                "struct Grant { friend int exposed(Grant&); };\n"
                "int exposed(Grant&);\n"
                "}\n"
            ),
            "implementation.cpp": (
                '#include "api.hpp"\n'
                "namespace demo::inner { int exposed(Grant&) { return 1; } }\n"
            ),
            "caller.cpp": (
                '#include "api.hpp"\n'
                "int caller(demo::inner::Grant& value) { return demo::inner::exposed(value); }\n"
            ),
        },
    )
    nodes = _nodes(document)
    declarations = [node for node in nodes if node["display_name"] == "exposed"]
    assert len(declarations) == 3
    assert {node["qualified_name"] for node in declarations} == {"demo::inner::exposed"}
    assert all(node["kind"] == "function" for node in declarations)
    assert all(_parent(document, node)["qualified_name"] == "demo::inner" for node in declarations)
    friend = next(node for node in declarations if node.get("extensions", {}).get("friend_declaration"))
    assert friend["extensions"]["friend_of"] == "demo::inner::Grant"
    definition = next(node for node in declarations if node["file"] == "implementation.cpp")
    calls = [edge for edge in document["edges"] if edge["relation_type"] == "calls"]
    assert len(calls) == 1
    assert calls[0]["resolution_status"] == "resolved"
    assert calls[0]["target_id"] == definition["id"]
