from __future__ import annotations

from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository
from connection_map.config import AnalysisConfig
from connection_map.contract import validate_document
from connection_map.language_registry import concrete_languages, language_for_path, supported_languages
from connection_map.sql_analyzer import _subquery_signature

FIXTURE = Path(__file__).parent / "fixtures" / "phase3_repo"


def _config(language: str) -> AnalysisConfig:
    return AnalysisConfig(
        language=language,
        include_tests=True,
        exclude=[".git/**", ".connection-map/**"],
    )


def test_phase3_languages_are_registered_and_sql_is_product_specific() -> None:
    assert {"bash", "posix-shell", "powershell", "dart", "scala", "mysql", "postgresql", "sqlite", "sqlserver", "oracle"} <= set(supported_languages())
    assert concrete_languages("sql") == ("mysql", "postgresql", "sqlite", "sqlserver", "oracle")


def test_sql_product_suffix_is_more_specific_than_generic_sql() -> None:
    assert language_for_path("migrations/create.mysql.sql", ["postgresql", "mysql"]) == "mysql"
    assert language_for_path("migrations/create.sql", ["postgresql", "mysql"]) == "mysql"


def test_bash_fixture_extracts_source_function_pipeline_and_variables() -> None:
    document = analyze_repository(FIXTURE, _config("bash"), deterministic=True, commit_sha="phase3-fixture")
    validate_document(document)
    assert {node["display_name"] for node in document["nodes"]} >= {"greet", "helper", "echo", "jq"}
    relation_types = {edge["relation_type"] for edge in document["edges"]}
    assert {"contains", "imports", "calls", "handles", "uses"} <= relation_types


def test_shell_preset_keeps_bash_and_posix_shell_results(tmp_path: Path) -> None:
    (tmp_path / "script.sh").write_text("helper() { return 0; }\nmain() { helper; }\n", encoding="utf-8")
    document = analyze_repository(
        tmp_path,
        AnalysisConfig(language="shell", include_tests=True),
        deterministic=True,
        commit_sha="shell-preset",
    )

    validate_document(document)
    source_languages = {
        node.get("extensions", {}).get("language")
        for node in document["nodes"]
        if node.get("file") == "script.sh"
    }
    assert {"bash", "posix-shell"} <= source_languages


def test_powershell_fixture_extracts_functions_and_module_import() -> None:
    document = analyze_repository(FIXTURE, _config("powershell"), deterministic=True, commit_sha="phase3-fixture")
    validate_document(document)
    assert {node["display_name"] for node in document["nodes"]} >= {"Get-Thing", "Get-Helper"}
    assert any(edge["relation_type"] == "imports" for edge in document["edges"])
    assert any(edge["relation_type"] == "calls" for edge in document["edges"])


def test_dart_fixture_extracts_class_method_import_and_calls() -> None:
    document = analyze_repository(FIXTURE, _config("dart"), deterministic=True, commit_sha="phase3-fixture")
    validate_document(document)
    assert {node["display_name"] for node in document["nodes"]} >= {"Service", "run", "main"}
    assert any(edge["relation_type"] == "imports" for edge in document["edges"])
    assert any(edge["relation_type"] == "calls" for edge in document["edges"])


def test_dart_generated_bindings_are_skipped_by_default(tmp_path: Path) -> None:
    (tmp_path / "main.dart").write_text("void main() { helper(); }\nvoid helper() {}\n", encoding="utf-8")
    generated = tmp_path / "lib" / "generated"
    generated.mkdir(parents=True)
    (generated / "native_bindings.dart").write_text("void generatedFunction() {}\n", encoding="utf-8")
    (generated / "ordinary.dart").write_text("void ordinaryGeneratedFunction() {}\n", encoding="utf-8")
    document = analyze_repository(
        tmp_path,
        AnalysisConfig(language="dart", include_tests=True),
        deterministic=True,
        commit_sha="phase3-fixture",
    )
    validate_document(document)
    analyzed_files = {node.get("file") for node in document["nodes"] if node.get("file")}
    assert "main.dart" in analyzed_files
    assert "lib/generated/native_bindings.dart" not in analyzed_files
    assert "lib/generated/ordinary.dart" not in analyzed_files


def test_scala_fixture_extracts_package_types_inheritance_and_calls() -> None:
    document = analyze_repository(FIXTURE, _config("scala"), deterministic=True, commit_sha="phase3-fixture")
    validate_document(document)
    assert {node["display_name"] for node in document["nodes"]} >= {"demo", "App", "Service", "run", "helper"}
    relation_types = {edge["relation_type"] for edge in document["edges"]}
    assert {"contains", "imports", "inherits", "calls"} <= relation_types


@pytest.mark.parametrize("product", ["mysql", "postgresql", "sqlite", "sqlserver", "oracle"])
def test_sql_products_extract_nontrivial_data_relationships(product: str) -> None:
    document = analyze_repository(FIXTURE, _config(product), deterministic=True, commit_sha="phase3-fixture")
    validate_document(document)
    assert {node["extensions"].get("sql_product") for node in document["nodes"]} == {product}
    assert {node["display_name"] for node in document["nodes"]} >= {"teams", "users", "active_users", "recent"}
    relation_types = {edge["relation_type"] for edge in document["edges"]}
    assert {"contains", "reads", "writes", "joins", "defines", "references"} <= relation_types


def test_postgresql_fixture_extracts_routine_and_trigger_relations(tmp_path: Path) -> None:
    (tmp_path / "routine.sql").write_text(
        "CREATE TABLE users (id INT PRIMARY KEY);\n"
        "CREATE FUNCTION find_user(x INT) RETURNS INT AS $$ SELECT id FROM users WHERE id=x; $$ LANGUAGE SQL;\n"
        "CREATE TRIGGER users_after AFTER INSERT ON users EXECUTE FUNCTION find_user(1);\n",
        encoding="utf-8",
    )
    document = analyze_repository(tmp_path, _config("postgresql"), deterministic=True, commit_sha="phase3-fixture")
    validate_document(document)
    assert {node["display_name"] for node in document["nodes"]} >= {"find_user", "users_after"}
    assert any(edge["relation_type"] == "reads" and edge["source_id"].endswith(":function:find_user") for edge in document["edges"])
    assert sum(edge["relation_type"] == "triggers" for edge in document["edges"]) >= 2


def test_sql_preset_analyzes_each_product_with_separate_ids(tmp_path: Path) -> None:
    (tmp_path / "query.sql").write_text(
        "SELECT u.id FROM users u JOIN teams t ON t.id = u.team_id;\n",
        encoding="utf-8",
    )
    document = analyze_repository(
        tmp_path,
        AnalysisConfig(language="sql", include_tests=True),
        deterministic=True,
        commit_sha="phase3-fixture",
    )
    validate_document(document)
    products = ("mysql", "postgresql", "sqlite", "sqlserver", "oracle")
    assert document["meta"]["languages"] == list(products)
    assert {
        node["id"].split(":", 1)[0]
        for node in document["nodes"]
        if node.get("extensions", {}).get("sql_object_type") == "table"
    } == set(products)
    assert all(
        f"sqlglot:{product}" in document["meta"]["runtime"]["grammars"]
        for product in ("mysql", "postgres", "sqlite", "tsql", "oracle")
    )


def test_sql_product_blocks_capture_routine_writes_and_oracle_variables_not_tables(tmp_path: Path) -> None:
    mysql_root = tmp_path / "mysql"
    mysql_root.mkdir()
    (mysql_root / "routine.sql").write_text(
        "DELIMITER //\n"
        "CREATE PROCEDURE save_user()\n"
        "BEGIN\n"
        "  INSERT INTO users(id) VALUES (1);\n"
        "  SELECT id FROM users;\n"
        "END//\n"
        "DELIMITER ;\n",
        encoding="utf-8",
    )
    mysql_document = analyze_repository(
        mysql_root,
        _config("mysql"),
        deterministic=True,
        commit_sha="phase3-fixture",
    )
    validate_document(mysql_document)
    routine_id = next(node["id"] for node in mysql_document["nodes"] if node["display_name"] == "save_user")
    routine_relations = {
        edge["relation_type"]
        for edge in mysql_document["edges"]
        if edge["source_id"] == routine_id
    }
    assert {"reads", "writes"} <= routine_relations

    oracle_root = tmp_path / "oracle"
    oracle_root.mkdir()
    (oracle_root / "routine.sql").write_text(
        "CREATE OR REPLACE FUNCTION find_user(x NUMBER) RETURN NUMBER IS\n"
        "  y NUMBER;\n"
        "BEGIN\n"
        "  SELECT id INTO y FROM users WHERE id = x;\n"
        "  RETURN y;\n"
        "END;\n"
        "/\n",
        encoding="utf-8",
    )
    oracle_document = analyze_repository(
        oracle_root,
        _config("oracle"),
        deterministic=True,
        commit_sha="phase3-fixture",
    )
    validate_document(oracle_document)
    assert {node["display_name"] for node in oracle_document["nodes"]} >= {"find_user", "users"}
    assert "x" not in {node["display_name"] for node in oracle_document["nodes"]}


def test_sql_routine_literals_are_not_reported_as_table_references(tmp_path: Path) -> None:
    (tmp_path / "routine.sql").write_text(
        "CREATE FUNCTION run_job() RETURNS void AS $$\n"
        "BEGIN\n"
        "  SELECT * FROM real_table;\n"
        "  SELECT 'from phantom_table';\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n",
        encoding="utf-8",
    )

    document = analyze_repository(
        tmp_path,
        _config("postgresql"),
        deterministic=True,
    )
    table_names = {
        node["display_name"]
        for node in document["nodes"]
        if node.get("extensions", {}).get("sql_object_type") == "table"
    }

    assert "real_table" in table_names
    assert "phantom_table" not in table_names


def test_sql_subquery_signature_recovers_when_dialect_generator_rejects_ast() -> None:
    class BrokenSubquery:
        this = type("MergeNode", (), {"key": "merge"})()

        def sql(self, *, dialect: str) -> str:
            raise AttributeError("dialect generator does not support Merge")

    assert _subquery_signature(BrokenSubquery(), "tsql") == "merge subquery (serialization unavailable)"


def test_sqlglot_unsupported_syntax_fallback_does_not_pollute_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "unsupported.sql").write_text(
        "CREATE STATISTICS stats_users ON dbo.users (id);\n",
        encoding="utf-8",
    )
    document = analyze_repository(tmp_path, _config("sqlserver"), deterministic=True, commit_sha="phase3-fixture")
    validate_document(document)
    assert capsys.readouterr().err == ""


def test_phase3_mixed_analysis_is_deterministic_and_keeps_product_labels() -> None:
    config = AnalysisConfig(
        language="mixed",
        languages=["python", "bash", "powershell", "dart", "scala", "postgresql"],
        include_tests=True,
        exclude=[".git/**", ".connection-map/**"],
    )
    first = analyze_repository(FIXTURE, config, deterministic=True, commit_sha="phase3-fixture")
    second = analyze_repository(FIXTURE, config, deterministic=True, commit_sha="phase3-fixture")
    validate_document(first)
    assert first == second
    assert first["meta"]["languages"] == config.languages
    source_languages = {node.get("extensions", {}).get("language") for node in first["nodes"] if node.get("file")}
    assert {"bash", "powershell", "dart", "scala", "postgresql"} <= source_languages
