from __future__ import annotations

from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository
from connection_map.config import AnalysisConfig
from connection_map.query import GraphQuery


def _analyze(root: Path, files: dict[str, str], language: str = "cpp") -> dict:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return analyze_repository(root, AnalysisConfig(language=language, include_tests=True),
                              deterministic=True, commit_sha="native-bindings-test")


def _calls(document: dict, callee: str | None = None) -> list[tuple[dict, dict, dict]]:
    nodes = {node["id"]: node for node in document["nodes"]}
    return [(edge, nodes[edge["source_id"]], nodes[edge["target_id"]]) for edge in document["edges"]
            if edge["relation_type"] == "calls" and (callee is None or edge["detail"]["callee"] == callee)]


@pytest.mark.parametrize("language", ["c", "cpp"])
def test_function_pointer_parameter_shadows_global_function(tmp_path: Path, language: str) -> None:
    suffix = "c" if language == "c" else "cpp"
    document = _analyze(tmp_path, {f"sample.{suffix}": "int step(int x) { return x + 1; }\n"
        "int invoke(int (*step)(int)) { return step(4); }\n"}, language)
    calls = _calls(document, "step")
    assert len(calls) == 1
    assert calls[0][0]["resolution_status"] == "unresolved"
    assert calls[0][2]["kind"] == "unknown"


def test_local_block_binding_does_not_escape_its_scope(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step() { return 1; }\n"
        "int invoke() { step(); { int (*step)() = nullptr; step(); } return step(); }\n"})
    calls = sorted(_calls(document, "step"), key=lambda row: row[0]["source_span"]["start_col"])
    assert [row[0]["resolution_status"] for row in calls] == ["resolved", "unresolved", "resolved"]


@pytest.mark.parametrize("body", [
    "for (int (*step)() = nullptr; step; ) { step(); break; }",
    "for (auto step : callbacks) { step(); }",
    "try {} catch (Callback step) { step(); }",
    "auto fn = [](int (*step)()) { return step(); };",
    "auto fn = [step = callbacks[0]]() { return step(); };",
    "auto [step, other] = pair; step();",
])
def test_loop_catch_lambda_and_structured_bindings_are_unknown(tmp_path: Path, body: str) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "struct Callback { int operator()(); }; Callback callbacks[1];\n"
        "struct Pair { Callback first; Callback second; }; Pair pair;\nint step() { return 1; }\n"
        "void invoke() { " + body + " }\n"})
    calls = _calls(document, "step")
    assert len(calls) == 1
    assert calls[0][0]["resolution_status"] == "unresolved"


def test_cpp_absolute_call_bypasses_parameter_shadow(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step() { return 1; }\n"
        "int invoke(int (*step)()) { return ::step(); }\n"})
    calls = _calls(document, "::step")
    assert len(calls) == 1
    assert calls[0][0]["resolution_status"] == "resolved"
    assert calls[0][2]["qualified_name"] == "step"


def test_external_qualified_call_does_not_match_another_namespace(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "#include <cmath>\n"
        "namespace project { double sqrt(double x) { return x; } }\n"
        "double invoke() { return std::sqrt(4.0); }\n"})
    calls = _calls(document, "std::sqrt")
    assert len(calls) == 1
    assert calls[0][0]["resolution_status"] == "unresolved"
    assert calls[0][2]["kind"] == "unknown"


def test_same_file_static_names_are_isolated_between_translation_units(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"a.c": "static int step(void) { return 1; }\n"
        "int first(void) { return step(); }\n", "b.c": "static int step(void) { return 2; }\n"
        "int second(void) { return step(); }\n"}, "c")
    calls = _calls(document, "step")
    assert len(calls) == 2
    assert all(edge["resolution_status"] == "resolved" and caller["file"] == target["file"]
               for edge, caller, target in calls)


def test_other_file_static_definition_does_not_replace_visible_external_prototype(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"a.c": "static int step(void) { return 1; }\n",
        "b.c": "int step(void);\nint invoke(void) { return step(); }\n"}, "c")
    calls = _calls(document, "step")
    assert len(calls) == 1
    assert calls[0][2]["file"] == "b.c"
    assert calls[0][2]["extensions"]["declaration_kind"] == "prototype"


def test_unseen_other_file_definition_is_not_sufficient_binding_evidence(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"a.c": "int step(void) { return 1; }\n",
        "b.c": "int invoke(void) { return step(); }\n"}, "c")
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_visible_header_prototype_links_to_definition_with_evidence(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"include/helper.h": "int step(int);\n",
        "step.c": "#include \"include/helper.h\"\nint step(int value) { return value; }\n",
        "invoke.c": "#include \"include/helper.h\"\nint invoke(void) { return step(1); }\n"}, "c")
    edge, _, target = _calls(document, "step")[0]
    assert edge["resolution_status"] == "resolved"
    assert target["file"] == "step.c"
    assert edge["detail"]["resolution_evidence"]["basis"] == "visible_lexical_declaration"
    assert any(item["file"] == "include/helper.h"
               for item in edge["detail"]["resolution_evidence"]["visible_declarations"])
    context = GraphQuery(document).neighborhood(edge["source_id"], direction="out", relations=["calls"], root=tmp_path)
    assert context["freshness"]["status"] == "current"
    assert context["edges"][0]["detail"]["resolution_evidence"] == edge["detail"]["resolution_evidence"]


def test_anonymous_namespace_function_is_visible_locally_only(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"a.cpp": "namespace { int step() { return 1; } }\n"
        "int local() { return step(); }\n", "b.cpp": "int step();\nint external() { return step(); }\n"})
    calls = _calls(document, "step")
    assert len(calls) == 2
    assert all(caller["file"] == target["file"] for _, caller, target in calls)


def test_overload_prototype_not_discarded_in_favor_of_different_definition(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step(int value) { return value; }\n"
        "double step(double);\nint invoke() { return step(1); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_object_initialization_is_not_a_callable_and_true_declarations_remain(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "#include <string>\nstruct Item {};\n"
        "int invoke(const wchar_t* input) { const std::wstring path(input); int ordinary(int); "
        "Item vex(Item()); return static_cast<int>(path.size()); }\n"})
    functions = {n["display_name"] for n in document["nodes"] if n["kind"] in {"function", "method"}}
    assert "path" not in functions
    assert {"ordinary", "vex"} <= functions
    assert not _calls(document, "Item")


def test_constructor_initialized_variable_shadows_unrelated_function(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "struct Callback { explicit Callback(int); int operator()(); };\n"
        "int step() { return 1; }\nint invoke(int seed) { Callback step(seed); return step(); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"
    assert not any(n["qualified_name"] == "invoke::step" and n["kind"] == "function" for n in document["nodes"])


def test_return_pointer_and_reference_function_prototypes_remain(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int* pointer(); int& reference();\n"
        "int invoke() { return *pointer() + reference(); }\n"})
    calls = _calls(document)
    assert len(calls) == 2
    assert all(edge["resolution_status"] == "resolved" for edge, _, _ in calls)


def test_compact_nested_namespaces_keep_identity_and_containment(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "namespace first::inner { int step() { return 1; } }\n"
        "namespace second::inner { int step() { return 2; } }\n"
        "int invoke() { return first::inner::step() + second::inner::step(); }\n"})
    nodes = {n["id"]: n for n in document["nodes"]}
    targets = {target["qualified_name"] for edge, _, target in _calls(document) if edge["resolution_status"] == "resolved"}
    assert targets == {"first::inner::step", "second::inner::step"}
    namespaces = {n["qualified_name"]: n for n in nodes.values() if n["kind"] == "namespace"}
    assert set(namespaces) == {"first", "first::inner", "second", "second::inner"}
    assert namespaces["first::inner"]["parent_id"] == namespaces["first"]["id"]


def test_relative_qualified_call_uses_lexical_namespace(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "namespace outer { namespace inner { int step() { return 1; } }\n"
        "int invoke() { return inner::step(); } }\n"})
    assert _calls(document, "inner::step")[0][2]["qualified_name"] == "outer::inner::step"


def test_using_directive_does_not_produce_a_global_name_guess(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step() { return 1; }\n"
        "namespace external { int step(); }\nusing namespace external;\n"
        "int invoke() { return step(); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_out_of_class_member_pointer_shadows_global_function(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"worker.h": "struct Worker { int (*step)(); int run(); };\n",
        "sample.cpp": '#include "worker.h"\nint step() { return 1; }\n'
        "int Worker::run() { return step(); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_out_of_class_method_uses_declared_member_scope(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"worker.h": "struct Worker { int step(); int run(); };\n",
        "sample.cpp": '#include "worker.h"\nint step() { return 1; }\n'
        "int Worker::step() { return 2; }\nint Worker::run() { return step(); }\n"})
    edge, _, target = _calls(document, "step")[0]
    assert edge["resolution_status"] == "resolved"
    assert target["qualified_name"] == "Worker::step"
    assert target["file"] == "sample.cpp"
    assert target["extensions"]["declaration_kind"] == "definition"


def test_known_base_method_resolves_in_out_of_class_definition(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"worker.h": "struct Base { int step(); };\n"
        "struct Worker : Base { int run(); };\n",
        "sample.cpp": '#include "worker.h"\nint step() { return 1; }\n'
        "int Base::step() { return 2; }\nint Worker::run() { return step(); }\n"})
    edge, _, target = _calls(document, "step")[0]
    assert edge["resolution_status"] == "resolved"
    assert target["qualified_name"] == "Base::step"


def test_inherited_member_pointer_shadows_global_function(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"worker.h": "struct Base { int (*step)(); };\n"
        "struct Worker : Base { int run(); };\n",
        "sample.cpp": '#include "worker.h"\nint step() { return 1; }\n'
        "int Worker::run() { return step(); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_const_method_overloads_are_not_collapsed_to_one_prototype(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "struct Worker { int step(); int step() const;\n"
        "int run() const { return step(); } };\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_transitive_inheritance_resolves_base_method_instead_of_global(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"worker.h": "struct GrandBase { int step(); };\n"
        "struct Parent : GrandBase {};\nstruct Child : Parent { int probe(); };\n",
        "sample.cpp": '#include "worker.h"\nint step() { return 1; }\n'
        "int GrandBase::step() { return 2; }\nint Child::probe() { return step(); }\n"})
    edge, _, target = _calls(document, "step")[0]
    assert edge["resolution_status"] == "resolved"
    assert target["qualified_name"] == "GrandBase::step"
    assert any(item["file"] == "worker.h" for item in edge["detail"]["resolution_evidence"]["visible_declarations"])


@pytest.mark.parametrize("bases", ["std::string", "Known, std::string"])
def test_unselected_base_blocks_global_or_known_base_guess(tmp_path: Path, bases: str) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "#include <string>\nint size() { return 99; }\n"
        "struct Known { int size() { return 2; } };\nstruct Child : " + bases
        + " { auto probe() { return size(); } };\n"})
    edge, caller, target = _calls(document, "size")[0]
    assert edge["resolution_status"] == "unresolved"
    assert target["kind"] == "unknown"
    context = GraphQuery(document).neighborhood(caller["id"], direction="out", relations=["calls"],
                                              resolution="resolved", root=tmp_path)
    assert context["edges"] == []


def test_own_member_hides_unknown_and_transitive_base_members(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "#include <string>\nint size() { return 99; }\n"
        "struct Base { int size; };\nstruct Parent : Base {};\nstruct Child : Parent, std::string {\n"
        "int size() { return 2; }\nint probe() { return size(); } };\n"})
    edge, _, target = _calls(document, "size")[0]
    assert edge["resolution_status"] == "resolved"
    assert target["qualified_name"] == "Child::size"


def test_complete_base_search_allows_real_global_fallback(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step() { return 1; }\nstruct GrandBase {};\n"
        "struct Parent : GrandBase {};\nstruct Child : Parent { int probe() { return step(); } };\n"})
    edge, _, target = _calls(document, "step")[0]
    assert edge["resolution_status"] == "resolved"
    assert target["qualified_name"] == "step"


def test_two_inherited_paths_are_not_collapsed_to_one_target(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step() { return 1; }\n"
        "struct Base { int step() { return 2; } };\nstruct Left : virtual Base {};\n"
        "struct Right : virtual Base {};\nstruct Child : Left, Right { int probe() { return step(); } };\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_uninstantiated_template_base_does_not_supply_a_method_target(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step() { return 1; }\n"
        "template<class T> struct Base { int step() { return 2; } };\n"
        "template<> struct Base<int> { int step() { return 3; } };\n"
        "struct Child : Base<int> { int probe() { return step(); } };\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


@pytest.mark.parametrize("alias", ["using step = int;", "typedef int other, step;"])
def test_type_cast_is_not_a_call_and_alias_does_not_escape_block(tmp_path: Path, alias: str) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step(int value) { return value + 100; }\n"
        "int probe() { int result = step(1); { " + alias + " result += step(3); } return result + step(5); }\n"})
    calls = _calls(document, "step")
    assert len(calls) == 2
    assert all(edge["resolution_status"] == "resolved" and target["qualified_name"] == "step"
               for edge, _, target in calls)


def test_class_alias_in_header_is_visible_in_out_of_class_body(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"worker.h": "struct Base { using step = int; };\n"
        "struct Parent : Base {};\nstruct Child : Parent { int probe(); };\n",
        "sample.cpp": '#include "worker.h"\nint step(int value) { return value + 100; }\n'
        "int Child::probe() { return step(3) + ::step(4); }\n"})
    assert not _calls(document, "step")
    assert _calls(document, "::step")[0][0]["resolution_status"] == "resolved"


def test_namespace_alias_in_header_does_not_hide_other_namespace_function(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"types.h": "namespace first { using step = int; }\n",
        "sample.cpp": '#include "types.h"\nnamespace first { int probe() { return step(3); } }\n'
        "namespace second { int step(int value) { return value; } int probe() { return step(3); } }\n"})
    calls = _calls(document, "step")
    assert len(calls) == 1
    assert calls[0][0]["resolution_status"] == "resolved"
    assert calls[0][2]["qualified_name"] == "second::step"


def test_value_parameter_hides_outer_type_alias_and_remains_indirect_call(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "using step = int;\n"
        "int probe(int (*step)(int)) { return step(3); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_type_conversion_keeps_real_calls_inside_its_arguments(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step(int value) { return value + 100; }\n"
        "int value() { return 3; }\nint probe() { using step = int; return step(value()); }\n"})
    assert not _calls(document, "step")
    edge, _, target = _calls(document, "value")[0]
    assert edge["resolution_status"] == "resolved"
    assert target["qualified_name"] == "value"


def test_local_function_declaration_hides_outer_type_alias(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "using step = int;\n"
        "int probe() { int step(int); return step(3); }\n"})
    edge, _, target = _calls(document, "step")[0]
    assert edge["resolution_status"] == "resolved"
    assert target["qualified_name"] == "probe::step"


@pytest.mark.parametrize("parameter, invocation", [("int (*step)(int)", "step(3)"), ("auto step", "step(3)")])
def test_non_type_template_parameter_is_indirect_and_does_not_escape(tmp_path: Path, parameter: str,
                                                                   invocation: str) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step(int value) { return value + 1; }\n"
        "int other(int value) { return value + 100; }\ntemplate<" + parameter + "> int probe() { return "
        + invocation + "; }\nint use() { return probe<other>() + step(4); }\n"})
    calls = _calls(document, "step")
    by_caller = {caller["qualified_name"]: (edge, target) for edge, caller, target in calls}
    assert by_caller["probe"][0]["resolution_status"] == "unresolved"
    assert by_caller["probe"][1]["kind"] == "unknown"
    assert by_caller["use"][0]["resolution_status"] == "resolved"
    assert by_caller["use"][1]["qualified_name"] == "step"


@pytest.mark.parametrize("body", ["int probe() { return step(3); }",
    "int probe(); }; template<auto step> int Box<step>::probe() { return step(3);"])
def test_class_template_callback_is_visible_in_method_body(tmp_path: Path, body: str) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step(int value) { return value; }\n"
        "template<auto step> struct Box { " + body + " };\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_parameter_name_inside_template_pointer_signature_does_not_leak(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step(int value) { return value; }\n"
        "template<int (*callback)(int step)> int probe() { return step(3) + callback(3); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "resolved"
    assert _calls(document, "callback")[0][0]["resolution_status"] == "unresolved"


def test_template_type_conversion_is_not_a_function_call(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step(int value) { return value + 100; }\n"
        "template<typename step> int probe() { return step(3); }\n"})
    assert not _calls(document, "step")


def test_default_template_pointer_signature_name_does_not_leak(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step(int value) { return value; }\n"
        "template<int (*callback)(int step) = nullptr> int probe() { return step(3); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "resolved"


def test_non_type_template_parameter_pack_remains_indirect(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step() { return 1; }\n"
        "template<auto... step> int probe() { return (step() + ...); }\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"


def test_dependent_base_name_is_not_bound_to_same_named_global_class(tmp_path: Path) -> None:
    document = _analyze(tmp_path, {"sample.cpp": "int step() { return 1; }\n"
        "struct T { int step() { return 2; } };\n"
        "template<class T> struct Child : T { int probe() { return step(); } };\n"})
    assert _calls(document, "step")[0][0]["resolution_status"] == "unresolved"
    inherits = [edge for edge in document["edges"] if edge["relation_type"] == "inherits"]
    assert len(inherits) == 1
    assert inherits[0]["resolution_status"] == "unresolved"
