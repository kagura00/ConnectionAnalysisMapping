from pathlib import Path

from connection_map.analyzer import analyze_repository
from connection_map.config import AnalysisConfig
from connection_map.query import GraphQuery


def analyze(root, sources):
    for relative, text in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return analyze_repository(root, AnalysisConfig(language="dart", include_tests=True), deterministic=True)


def node(document, name, file="main.dart"):
    return next(n for n in document["nodes"] if n["display_name"] == name and n.get("file") == file)


def calls(document, source):
    return sorted(
        (e for e in document["edges"] if e["relation_type"] == "calls" and e["source_id"] == source["id"]),
        key=lambda e: (e["source_span"]["start_line"], e["source_span"]["start_col"]),
    )


def test_dart_method_body_and_receiver_are_not_confused(tmp_path: Path):
    document = analyze(tmp_path, {
        "main.dart": """class Queue {
  dynamic channel;
  Future<void> push(int item) async {
    await drain();
    channel.close();
    this.drain();
  }
  Future<void> drain() async => print('drain');
}
class Other { void drain() {} }
""",
        "database.dart": "class Database { void close() {} }",
    })
    push = node(document, "push")
    assert push["execution_kind"] == "async"
    assert push["span"]["start_line"] == 3 and push["span"]["end_line"] == 7
    edges = calls(document, push)
    assert len(edges) == 3
    by_expression = {e["detail"]["expression"]: e for e in edges}
    for expression in ("drain(...)", "this.drain(...)"):
        edge = by_expression[expression]
        assert edge["resolution_status"] == "resolved"
        target = next(n for n in document["nodes"] if n["id"] == edge["target_id"])
        assert target["qualified_name"] == "main.dart:Queue.drain"
        assert edge["detail"]["resolution_evidence"]
    assert by_expression["channel.close(...)"]["resolution_status"] == "unresolved"
    assert by_expression["channel.close(...)"]["target_id"] != node(document, "close", "database.dart")["id"]
    context = GraphQuery(document).neighborhood(push["id"], direction="out", relations=["calls"])
    assert len(context["edges"]) == 3


def test_dart_top_level_nested_functions_and_case_sensitive_names(tmp_path: Path):
    document = analyze(tmp_path, {"main.dart": """void helper() {}
void Helper() {}
Future<void> main() async {
  void inner() { helper(); }
  inner(); Helper();
}
void after() { helper(); }
"""})
    main = node(document, "main")
    inner = node(document, "inner")
    assert inner["kind"] == "function" and inner["parent_id"] == main["id"]
    assert main["execution_kind"] == "async" and main["span"]["end_line"] == 6
    assert {e["target_id"] for e in calls(document, main)} == {inner["id"], node(document, "Helper")["id"]}
    assert calls(document, inner)[0]["target_id"] == node(document, "helper")["id"]
    assert calls(document, node(document, "after"))[0]["target_id"] == node(document, "helper")["id"]


def test_dart_parameters_locals_and_closures_shadow_names(tmp_path: Path):
    document = analyze(tmp_path, {"main.dart": """void helper() {}
void parameter(void Function() helper) { helper(); }
void variable() { final helper = () {}; helper(); }
void closure() { [1].map((helper) => helper()); }
void siblingBlocks() {
  { final helper = () {}; helper(); }
  helper();
}
"""})
    for name in ("parameter", "variable", "closure"):
        assert all(e["resolution_status"] == "unresolved" for e in calls(document, node(document, name)))
    edges = calls(document, node(document, "siblingBlocks"))
    assert [e["resolution_status"] for e in edges] == ["unresolved", "resolved"]
    assert edges[-1]["target_id"] == node(document, "helper")["id"]


def test_dart_imports_require_explicit_scope_and_honor_combinators(tmp_path: Path):
    document = analyze(tmp_path, {
        "lib/main.dart": """import './helpers.dart' as tools show work;
import '../common.dart' show finish;
void run() { tools.work(); finish(); tools.hidden(); work(); orphan(); }
void shadow(dynamic tools) { tools.work(); }
""",
        "lib/helpers.dart": "void work() {}\nvoid hidden() {}",
        "common.dart": "void finish() {}",
        "orphan.dart": "void orphan() {}",
    })
    edges = {e["detail"]["expression"]: e for e in calls(document, node(document, "run", "lib/main.dart"))}
    assert edges["tools.work(...)"]["target_id"] == node(document, "work", "lib/helpers.dart")["id"]
    assert edges["finish(...)"]["target_id"] == node(document, "finish", "common.dart")["id"]
    for expression in ("tools.hidden(...)", "work(...)", "orphan(...)"):
        assert edges[expression]["resolution_status"] == "unresolved"
    assert calls(document, node(document, "shadow", "lib/main.dart"))[0]["resolution_status"] == "unresolved"
    assert sum(e["relation_type"] == "imports" and e["resolution_status"] == "resolved" for e in document["edges"]) == 2


def test_dart_conditional_and_ambiguous_imports_stay_unresolved(tmp_path: Path):
    document = analyze(tmp_path, {
        "main.dart": """import 'a.dart' if (dart.library.io) 'b.dart' as selected;
import 'a.dart';
import 'b.dart';
void run() { selected.work(); work(); }
""",
        "a.dart": "void work() {}", "b.dart": "void work() {}",
    })
    assert all(e["resolution_status"] == "unresolved" for e in calls(document, node(document, "run")))


def test_dart_annotations_constructors_accessors_and_static_members(tmp_path: Path):
    document = analyze(tmp_path, {"main.dart": """void helper() {}
@Deprecated('sample')
class Service {
  Service.named() { helper(); }
  int get value => compute();
  static int compute() => 1;
  void run() {}
}
void main() { Service.compute(); Service.named(); Service.run(); }
"""})
    assert node(document, "Service")["kind"] == "class"
    assert not any(n.get("file") and n["display_name"] == "Deprecated" for n in document["nodes"])
    constructor = node(document, "Service.named")
    assert constructor["span"]["end_line"] == 4
    assert calls(document, constructor)[0]["target_id"] == node(document, "helper")["id"]
    assert calls(document, node(document, "value"))[0]["target_id"] == node(document, "compute")["id"]
    edges = {e["detail"]["expression"]: e for e in calls(document, node(document, "main"))}
    assert edges["Service.compute(...)"]["resolution_status"] == "resolved"
    assert edges["Service.named(...)"]["target_id"] == constructor["id"]
    assert edges["Service.run(...)"]["resolution_status"] == "unresolved"


def test_dart_unknown_and_chained_receivers_never_use_global_names(tmp_path: Path):
    document = analyze(tmp_path, {"main.dart": """void close() {}
void main(dynamic receiver) {
 receiver?.close(); receiver..close(); receiver.make().close();
}
"""})
    edges = calls(document, node(document, "main"))
    assert len(edges) == 4
    assert all(e["resolution_status"] == "unresolved" for e in edges)


def test_dart_nested_function_in_class_is_not_a_method(tmp_path: Path):
    document = analyze(tmp_path, {"main.dart": """class Task {
 void run() { void local() { print('ok'); } local(); }
}
"""})
    local = node(document, "local")
    assert local["kind"] == "function" and not local["extensions"]["member"]
    assert calls(document, node(document, "run"))[0]["target_id"] == local["id"]


def test_dart_comments_and_loop_catch_pattern_bindings(tmp_path: Path):
    document = analyze(tmp_path, {"main.dart": """void helper() {}
void run() /* gap */ { helper(); }
void loop() { for (final helper in []) { helper(); } helper(); }
void catching() { try {} catch (helper) { helper(); } helper(); }
void pattern() { var (helper, x) = (1, 2); helper(); }
"""})
    assert calls(document, node(document, "run"))[0]["target_id"] == node(document, "helper")["id"]
    for name in ("loop", "catching"):
        assert [e["resolution_status"] for e in calls(document, node(document, name))] == ["unresolved", "resolved"]
    assert calls(document, node(document, "pattern"))[0]["resolution_status"] == "unresolved"


def test_dart_factory_result_and_reexports_are_not_guessed(tmp_path: Path):
    document = analyze(tmp_path, {
        "main.dart": """import 'barrel.dart';
import 'direct.dart';
class Service { factory Service() = Other; void run() {} }
class Other implements Service { void run() {} }
void main() { Service().run(); work(); }
""",
        "barrel.dart": "export 'direct.dart';",
        "direct.dart": "void work() {}",
    })
    edges = calls(document, node(document, "main"))
    assert len(edges) == 3
    assert sum(e["resolution_status"] == "resolved" for e in edges) == 1
    assert next(e for e in edges if e["resolution_status"] == "resolved")["target_id"] == node(document, "Service")["id"]
