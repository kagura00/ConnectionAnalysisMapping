"""Product-specific static SQL relationship analyzer.

SQL is parsed with SQLGlot using an explicit source dialect.  No database
connection, query execution, catalog lookup, or target-repository import is
performed.
"""

from __future__ import annotations

import importlib.metadata
import logging
import platform
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AnalysisConfig, discover_source_files, repository_id
from .contract import validate_document
from .model import GraphBuilder
from .phase3_common import (
    add_relation,
    add_skipped_diagnostics,
    diagnostic,
    unique_id,
)

ANALYZER_NAME = "connection-map-sqlglot"
ANALYZER_VERSION = "0.1.0"

PRODUCTS: dict[str, dict[str, str]] = {
    "mysql": {"dialect": "mysql", "display_name": "MySQL"},
    "postgresql": {"dialect": "postgres", "display_name": "PostgreSQL"},
    "sqlite": {"dialect": "sqlite", "display_name": "SQLite"},
    "sqlserver": {"dialect": "tsql", "display_name": "SQL Server / T-SQL"},
    "oracle": {"dialect": "oracle", "display_name": "Oracle"},
}


class SQLDependencyError(ValueError):
    """Raised when the SQL optional extra is unavailable."""


@contextmanager
def _quiet_sqlglot_warnings():
    """Keep SQLGlot's unsupported-syntax fallback warnings out of CLI output."""

    logger = logging.getLogger("sqlglot")
    previous_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    active_config = config or AnalysisConfig(language="sql")
    active_config.validate()
    if active_config.language not in {"sql", *PRODUCTS}:
        raise ValueError("SQL analyzer requires language = 'sql' or a registered SQL product")
    selected = tuple(item for item in active_config.active_languages() if item in PRODUCTS)
    if not selected:
        raise ValueError("SQL analyzer requires at least one SQL product")
    sqlglot = _load_sqlglot()
    builder = GraphBuilder()
    object_nodes: dict[tuple[str, str], str] = {}
    for product in selected:
        files, skipped = discover_source_files(root.resolve(), active_config, languages={product})
        add_skipped_diagnostics(builder, skipped)
        _analyze_product(
            root.resolve(),
            files,
            builder,
            object_nodes,
            product=product,
            dialect=PRODUCTS[product]["dialect"],
            sqlglot=sqlglot,
        )
    document = _finish_sql_document(
        builder,
        root=root.resolve(),
        config=active_config,
        language=active_config.language,
        languages=list(selected),
        deterministic=deterministic,
        commit_sha=commit_sha,
        sqlglot=sqlglot,
    )
    validate_document(document)
    return document


def _load_sqlglot() -> Any:
    try:
        import sqlglot
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise SQLDependencyError(
            "SQL analysis requires sqlglot; run 'uv sync --extra sql' first"
        ) from exc
    return sqlglot


def _analyze_product(
    root: Path,
    files: list[Path],
    builder: GraphBuilder,
    object_nodes: dict[tuple[str, str], str],
    *,
    product: str,
    dialect: str,
    sqlglot: Any,
) -> None:
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            diagnostic(
                builder,
                code="read_failed",
                message="SQL file could not be read as UTF-8",
                file=relative,
                severity="error",
                details={"sql_product": product},
            )
            continue
        module_id = f"{product}:{relative}:module"
        builder.add_node(
            {
                "id": module_id,
                "kind": "module",
                "qualified_name": relative,
                "display_name": relative,
                "file": relative,
                "span": _span_from_offsets(source, 0, len(source)),
                "parent_id": None,
                "visibility": "public",
                "extensions": {
                    "language": product,
                    "sql_product": product,
                    "sql_dialect": dialect,
                },
            }
        )
        for index, (statement_text, start, end) in enumerate(_split_statements(source, product=product)):
            if not statement_text.strip():
                continue
            statement_span = _span_from_offsets(source, start, end)
            try:
                with _quiet_sqlglot_warnings():
                    expression = sqlglot.parse_one(statement_text, read=dialect)
            except Exception as exc:
                diagnostic(
                    builder,
                    code="parse_error",
                    message=f"{product} SQL statement could not be parsed: {exc}",
                    file=relative,
                    span=statement_span,
                    details={"sql_product": product, "sql_dialect": dialect, "statement_index": index},
                )
                _recover_unparsed_statement(
                    statement_text,
                    statement_id=f"{product}:{relative}:statement:{index}:create",
                    module_id=module_id,
                    relative=relative,
                    statement_span=statement_span,
                    builder=builder,
                    object_nodes=object_nodes,
                    product=product,
                    dialect=dialect,
                    sqlglot=sqlglot,
                )
                continue
            if expression is None:
                continue
            statement_kind = _statement_kind(expression)
            statement_id = f"{product}:{relative}:statement:{index}:{statement_kind}"
            builder.add_node(
                {
                    "id": statement_id,
                    "kind": "unknown",
                    "qualified_name": f"{relative}:statement:{index}:{statement_kind}",
                    "display_name": f"{statement_kind} #{index + 1}",
                    "file": relative,
                    "span": statement_span,
                    "parent_id": module_id,
                    "visibility": "unknown",
                    "signature": statement_text.strip()[:500],
                    "extensions": {
                        "language": product,
                        "sql_product": product,
                        "sql_dialect": dialect,
                        "sql_object_type": statement_kind,
                    },
                }
            )
            add_relation(
                builder,
                source_id=module_id,
                target_id=statement_id,
                relation_type="contains",
                source_span=statement_span,
                detail={"kind": "sql_statement", "statement_type": statement_kind, "sql_product": product},
                edge_prefix=product,
            )
            _extract_statement(
                expression,
                statement_text,
                statement_id,
                module_id,
                relative,
                statement_span,
                builder,
                object_nodes,
                product=product,
                dialect=dialect,
                sqlglot=sqlglot,
            )


def _recover_unparsed_statement(
    statement_text: str,
    *,
    statement_id: str,
    module_id: str,
    relative: str,
    statement_span: dict[str, int],
    builder: GraphBuilder,
    object_nodes: dict[tuple[str, str], str],
    product: str,
    dialect: str,
    sqlglot: Any,
) -> None:
    """Keep useful routine/trigger edges when a vendor body is not SQLGlot-parseable."""

    match = re.search(
        r"\bcreate\s+(?:or\s+replace\s+)?(?P<kind>function|procedure|trigger)\s+"
        r"(?P<name>[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)",
        statement_text,
        re.IGNORECASE,
    )
    if match is None:
        return
    object_kind = match.group("kind").casefold()
    qualified_name = match.group("name")
    builder.add_node(
        {
            "id": statement_id,
            "kind": "unknown",
            "qualified_name": f"{relative}:statement:{statement_id}:create",
            "display_name": "CREATE (recovered)",
            "file": relative,
            "span": statement_span,
            "parent_id": module_id,
            "visibility": "unknown",
            "signature": statement_text.strip()[:500],
            "extensions": {
                "language": product,
                "sql_product": product,
                "sql_dialect": dialect,
                "sql_object_type": "create",
                "parser_recovery": True,
            },
        }
    )
    add_relation(
        builder,
        source_id=module_id,
        target_id=statement_id,
        relation_type="contains",
        source_span=statement_span,
        detail={"kind": "sql_statement", "statement_type": "create", "sql_product": product},
        edge_prefix=product,
    )
    object_id = _object_node(
        builder,
        object_nodes,
        product=product,
        object_kind=object_kind,
        qualified_name=qualified_name,
        display_name=qualified_name.rsplit(".", 1)[-1],
        file=relative,
        span=statement_span,
        parent_id=module_id,
        extensions={"definition": True, "sql_statement": object_kind, "parser_recovery": True},
    )
    add_relation(
        builder,
        source_id=statement_id,
        target_id=object_id,
        relation_type="defines",
        source_span=statement_span,
        detail={"object_kind": object_kind, "name": qualified_name, "sql_product": product},
        edge_prefix=product,
    )
    if object_kind in {"function", "procedure"}:
        _extract_routine_body(
            statement_text,
            object_id,
            builder,
            object_nodes,
            relative,
            statement_span,
            product,
            dialect,
            sqlglot,
        )
    elif object_kind == "trigger":
        table_match = re.search(
            r"\bon\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
            statement_text,
            re.IGNORECASE,
        )
        if table_match:
            table_name = table_match.group(1)
            table_id = _object_node(
                builder,
                object_nodes,
                product=product,
                object_kind="table",
                qualified_name=table_name,
                display_name=table_name.rsplit(".", 1)[-1],
                file=relative,
                span=statement_span,
                parent_id=module_id,
                extensions={"reference": True, "parser_recovery": True},
            )
            add_relation(
                builder,
                source_id=object_id,
                target_id=table_id,
                relation_type="triggers",
                source_span=statement_span,
                detail={"object_kind": "table", "trigger_event": _trigger_event(statement_text), "sql_product": product},
                edge_prefix=product,
            )
        _extract_trigger_routine(statement_text, object_id, builder, object_nodes, relative, statement_span, product)


def _extract_statement(
    expression: Any,
    statement_text: str,
    statement_id: str,
    module_id: str,
    relative: str,
    statement_span: dict[str, int],
    builder: GraphBuilder,
    object_nodes: dict[tuple[str, str], str],
    *,
    product: str,
    dialect: str,
    sqlglot: Any,
) -> None:
    from sqlglot import exp

    ctes: dict[str, str] = {}
    for cte_index, cte in enumerate(expression.find_all(exp.CTE)):
        name = cte.alias_or_name or f"cte_{cte_index + 1}"
        cte_id = _object_node(
            builder,
            object_nodes,
            product=product,
            object_kind="cte",
            qualified_name=f"{relative}:statement:{statement_id}:cte:{name}",
            display_name=name,
            file=relative,
            span=statement_span,
            parent_id=statement_id,
            extensions={"cte": True},
        )
        ctes[name.casefold()] = cte_id
        add_relation(
            builder,
            source_id=statement_id,
            target_id=cte_id,
            relation_type="contains",
            source_span=statement_span,
            detail={"kind": "cte", "name": name, "sql_product": product},
            edge_prefix=product,
        )

    create_kind = _create_kind(expression, statement_text)
    defined_id: str | None = None
    if isinstance(expression, exp.Create):
        defined_name = _create_name(expression)
        if defined_name:
            defined_id = _object_node(
                builder,
                object_nodes,
                product=product,
                object_kind=create_kind or "object",
                qualified_name=defined_name,
                display_name=defined_name.rsplit(".", 1)[-1],
                file=relative,
                span=statement_span,
                parent_id=module_id,
                extensions={"definition": True, "sql_statement": create_kind or "create"},
            )
            add_relation(
                builder,
                source_id=statement_id,
                target_id=defined_id,
                relation_type="defines",
                source_span=statement_span,
                detail={"object_kind": create_kind or "object", "name": defined_name, "sql_product": product},
                edge_prefix=product,
            )
            if create_kind in {"table", "view", "materialized_view"}:
                add_relation(
                    builder,
                    source_id=statement_id,
                    target_id=defined_id,
                    relation_type="writes",
                    source_span=statement_span,
                    detail={"operation": "create", "object_kind": create_kind, "sql_product": product},
                    edge_prefix=product,
                )

    target_table = _dml_target(expression)
    target_id: str | None = None
    if target_table is not None:
        target_name = _qualified_table_name(target_table)
        if target_name:
            target_kind = "table"
            if target_name.casefold() in ctes:
                target_id = ctes[target_name.casefold()]
            else:
                target_id = _object_node(
                    builder,
                    object_nodes,
                    product=product,
                    object_kind=target_kind,
                    qualified_name=target_name,
                    display_name=target_name.rsplit(".", 1)[-1],
                    file=relative,
                    span=statement_span,
                    parent_id=module_id,
                    extensions={"reference": True},
                )
            if expression.key in {"insert", "update", "delete", "merge"}:
                add_relation(
                    builder,
                    source_id=statement_id,
                    target_id=target_id,
                    relation_type="writes",
                    source_span=statement_span,
                    detail={"operation": expression.key, "object_kind": "table", "name": target_name, "sql_product": product},
                    edge_prefix=product,
                )

    table_nodes: dict[str, str] = {}
    created_name = _create_name(expression) if isinstance(expression, exp.Create) else None
    for table in expression.find_all(exp.Table):
        name = _qualified_table_name(table)
        if not name:
            continue
        key = name.casefold()
        if isinstance(expression, exp.Create) and created_name and key == created_name.casefold():
            if defined_id:
                table_nodes[key] = defined_id
            continue
        if key in ctes:
            table_id = ctes[key]
        else:
            table_id = _object_node(
                builder,
                object_nodes,
                product=product,
                object_kind="table",
                qualified_name=name,
                display_name=name.rsplit(".", 1)[-1],
                file=relative,
                span=statement_span,
                parent_id=module_id,
                extensions={"alias": getattr(table, "alias", None) or None},
            )
        table_nodes[key] = table_id
        if table_id == target_id or create_kind == "trigger":
            continue
        relation = "reads"
        detail: dict[str, Any] = {
            "reference": name,
            "object_kind": "cte" if key in ctes else "table",
            "sql_product": product,
            "clause": _table_clause(table, expression),
        }
        if _is_join_table(table, expression):
            relation = "joins"
            detail["join_kind"] = _join_kind(table, expression)
        add_relation(
            builder,
            source_id=statement_id,
            target_id=table_id,
            relation_type=relation,
            source_span=statement_span,
            detail=detail,
            edge_prefix=product,
        )

    for subquery_index, subquery in enumerate(expression.find_all(exp.Subquery)):
        subquery_id = unique_id(
            builder.nodes,
            f"{product}:{relative}:statement:{statement_id}:subquery:{subquery_index}",
            subquery_index,
        )
        builder.add_node(
            {
                "id": subquery_id,
                "kind": "unknown",
                "qualified_name": f"{relative}:subquery:{subquery_index}",
                "display_name": f"subquery #{subquery_index + 1}",
                "file": relative,
                "span": statement_span,
                "parent_id": statement_id,
                "visibility": "unknown",
                "signature": _subquery_signature(subquery, dialect),
                "extensions": {"language": product, "sql_product": product, "sql_object_type": "subquery"},
            }
        )
        add_relation(
            builder,
            source_id=statement_id,
            target_id=subquery_id,
            relation_type="contains",
            source_span=statement_span,
            detail={"kind": "subquery", "sql_product": product},
            edge_prefix=product,
        )

    if isinstance(expression, exp.Create):
        _extract_constraints(expression, target_id or defined_id, statement_id, builder, object_nodes, relative, statement_span, product)
        if create_kind in {"function", "procedure"} and defined_id:
            _extract_routine_body(
                statement_text,
                defined_id,
                builder,
                object_nodes,
                relative,
                statement_span,
                product,
                dialect,
                sqlglot,
            )
        if create_kind == "trigger" and defined_id:
            for table in expression.find_all(exp.Table):
                name = _qualified_table_name(table)
                if not name:
                    continue
                table_id = table_nodes.get(name.casefold()) or _object_node(
                    builder,
                    object_nodes,
                    product=product,
                    object_kind="table",
                    qualified_name=name,
                    display_name=name.rsplit(".", 1)[-1],
                    file=relative,
                    span=statement_span,
                    parent_id=module_id,
                    extensions={"reference": True},
                )
                add_relation(
                    builder,
                    source_id=defined_id,
                    target_id=table_id,
                    relation_type="triggers",
                    source_span=statement_span,
                    detail={"object_kind": "table", "trigger_event": _trigger_event(statement_text), "sql_product": product},
                    edge_prefix=product,
                )
            _extract_trigger_routine(statement_text, defined_id, builder, object_nodes, relative, statement_span, product)


def _object_node(
    builder: GraphBuilder,
    object_nodes: dict[tuple[str, str], str],
    *,
    product: str,
    object_kind: str,
    qualified_name: str,
    display_name: str,
    file: str,
    span: dict[str, int],
    parent_id: str,
    extensions: dict[str, Any] | None = None,
) -> str:
    existing = object_nodes.get((product, f"{object_kind}:{qualified_name.casefold()}"))
    if existing:
        return existing
    node_id = f"{product}:{object_kind}:{qualified_name}"
    node_extensions: dict[str, Any] = {
        "language": product,
        "sql_product": product,
        "sql_object_type": object_kind,
    }
    if extensions:
        node_extensions.update({key: value for key, value in extensions.items() if value is not None})
    builder.add_node(
        {
            "id": node_id,
            "kind": "type",
            "qualified_name": qualified_name,
            "display_name": display_name,
            "file": file,
            "span": span,
            "parent_id": parent_id,
            "visibility": "public",
            "extensions": node_extensions,
        }
    )
    object_nodes[(product, f"{object_kind}:{qualified_name.casefold()}")] = node_id
    return node_id


def _extract_constraints(
    expression: Any,
    source_id: str | None,
    statement_id: str,
    builder: GraphBuilder,
    object_nodes: dict[tuple[str, str], str],
    relative: str,
    span: dict[str, int],
    product: str,
) -> None:
    from sqlglot import exp

    if source_id is None:
        return
    for reference in expression.find_all(exp.Reference):
        target_table = next((candidate for candidate, _ in _walk_ast(reference) if isinstance(candidate, exp.Table)), None)
        if target_table is None:
            continue
        name = _qualified_table_name(target_table)
        if not name:
            continue
        target_id = _object_node(
            builder,
            object_nodes,
            product=product,
            object_kind="table",
            qualified_name=name,
            display_name=name.rsplit(".", 1)[-1],
            file=relative,
            span=span,
            parent_id=statement_id,
            extensions={"reference": True},
        )
        add_relation(
            builder,
            source_id=source_id,
            target_id=target_id,
            relation_type="references",
            source_span=span,
            detail={"kind": "foreign_key", "reference": name, "sql_product": product},
            edge_prefix=product,
        )


def _extract_routine_body(
    statement_text: str,
    routine_id: str,
    builder: GraphBuilder,
    object_nodes: dict[tuple[str, str], str],
    relative: str,
    span: dict[str, int],
    product: str,
    dialect: str,
    sqlglot: Any,
) -> None:
    body = _routine_body(statement_text)
    if not body:
        return
    try:
        with _quiet_sqlglot_warnings():
            expressions = sqlglot.parse(body, read=dialect)
    except Exception:
        expressions = []
    names: set[str] = set()
    relation_by_name: dict[str, set[str]] = {}
    regex_body = _mask_sql_non_code(body)
    regex_names = _regex_table_names(regex_body)
    for expression in expressions:
        from sqlglot import exp

        for table in expression.find_all(exp.Table):
            name = _qualified_table_name(table)
            if not name:
                continue
            names.add(name)
            relation_by_name.setdefault(name, set()).add(
                "writes" if expression.key in {"insert", "update", "delete", "merge"} else "reads"
            )
    if regex_names:
        names.update(regex_names)
        for name, relation_types in _regex_table_relations(regex_body).items():
            relation_by_name.setdefault(name, set()).update(relation_types)
        for name in names:
            relation_by_name.setdefault(name, set()).add("reads")
    if not names:
        names.update(regex_names)
    for name in sorted(names):
        table_id = _object_node(
            builder,
            object_nodes,
            product=product,
            object_kind="table",
            qualified_name=name,
            display_name=name.rsplit(".", 1)[-1],
            file=relative,
            span=span,
            parent_id=routine_id,
            extensions={"routine_body_reference": True},
        )
        for relation_type in sorted(relation_by_name.get(name, {"reads"})):
            add_relation(
                builder,
                source_id=routine_id,
                target_id=table_id,
                relation_type=relation_type,
                source_span=span,
                detail={"clause": "routine_body", "reference": name, "sql_product": product},
                resolution_status="resolved" if expressions else "unresolved",
                confidence=0.85 if expressions else 0.45,
                edge_prefix=product,
            )


def _extract_trigger_routine(
    statement_text: str,
    trigger_id: str,
    builder: GraphBuilder,
    object_nodes: dict[tuple[str, str], str],
    relative: str,
    span: dict[str, int],
    product: str,
) -> None:
    match = re.search(r"\b(?:execute\s+function|execute\s+procedure|call)\s+([\w.$\"`\[\]]+)", statement_text, re.IGNORECASE)
    if not match:
        return
    name = match.group(1).strip('"`[]')
    existing_key = (product, f"function:{name.casefold()}")
    was_known = existing_key in object_nodes
    routine_id = _object_node(
        builder,
        object_nodes,
        product=product,
        object_kind="function",
        qualified_name=name,
        display_name=name.rsplit(".", 1)[-1],
        file=relative,
        span=span,
        parent_id=trigger_id,
        extensions={"trigger_reference": True},
    )
    add_relation(
        builder,
        source_id=trigger_id,
        target_id=routine_id,
        relation_type="triggers",
        source_span=span,
        detail={"object_kind": "function", "reference": name, "sql_product": product},
        resolution_status="resolved" if was_known else "unresolved",
        confidence=1.0 if was_known else 0.6,
        edge_prefix=product,
    )


def _walk_ast(node: Any):
    yield node, ()
    for child in node.iter_expressions():
        yield from _walk_ast(child)


def _dml_target(expression: Any) -> Any | None:
    from sqlglot import exp

    if isinstance(expression, exp.Insert | exp.Update | exp.Delete | exp.Merge):
        target = expression.args.get("this")
        return target if isinstance(target, exp.Table) else None
    return None


def _create_name(expression: Any) -> str | None:
    from sqlglot import exp

    target = expression.args.get("this")
    if target is None:
        return None
    if isinstance(target, exp.Schema):
        target = target.args.get("this")
    inner = target.args.get("this") if hasattr(target, "args") else None
    if isinstance(inner, exp.Table):
        return _qualified_table_name(inner)
    if isinstance(inner, exp.Identifier):
        return str(inner.name or inner.this)
    if hasattr(target, "name") and target.name:
        if hasattr(target, "db") and target.db:
            return f"{target.db}.{target.name}"
        return target.name
    text = target.sql() if hasattr(target, "sql") else str(target)
    return text.strip() or None


def _create_kind(expression: Any, statement_text: str) -> str | None:
    from sqlglot import exp

    if not isinstance(expression, exp.Create):
        return None
    raw = str(expression.args.get("kind") or "").casefold()
    if raw == "table":
        return "table"
    if raw == "view":
        return "materialized_view" if re.search(r"create\s+materialized\s+view", statement_text, re.IGNORECASE) else "view"
    if raw in {"function", "procedure", "trigger", "sequence", "index", "schema"}:
        return raw
    return raw or "object"


def _statement_kind(expression: Any) -> str:
    key = str(getattr(expression, "key", "unknown"))
    return {
        "select": "select",
        "insert": "insert",
        "update": "update",
        "delete": "delete",
        "merge": "merge",
        "create": "create",
        "alter": "alter",
        "drop": "drop",
        "command": "command",
    }.get(key, key or "unknown")


def _qualified_table_name(table: Any) -> str | None:
    name = str(getattr(table, "name", "") or "").strip('"`[]')
    if not name:
        return None
    db = str(getattr(table, "db", "") or "").strip('"`[]')
    catalog = str(getattr(table, "catalog", "") or "").strip('"`[]')
    parts = [part for part in (catalog, db, name) if part]
    return ".".join(parts)


def _subquery_signature(subquery: Any, dialect: str) -> str:
    """Return a bounded display signature even for AST nodes the dialect cannot emit."""

    try:
        return subquery.sql(dialect=dialect)[:500]
    except Exception:
        key = str(getattr(subquery.this, "key", "unknown") or "unknown")
        return f"{key} subquery (serialization unavailable)"


def _is_join_table(table: Any, expression: Any) -> bool:
    from sqlglot import exp

    return any(join.args.get("this") is table for join in expression.find_all(exp.Join))


def _join_kind(table: Any, expression: Any) -> str:
    from sqlglot import exp

    for join in expression.find_all(exp.Join):
        if join.args.get("this") is table:
            return str(join.args.get("side") or join.args.get("kind") or "inner").lower()
    return "inner"


def _table_clause(table: Any, expression: Any) -> str:

    if _is_join_table(table, expression):
        return "join"
    if expression.key in {"insert", "update", "delete", "merge"} and table is expression.args.get("this"):
        return "target"
    return "source"


def _trigger_event(statement_text: str) -> str:
    match = re.search(r"\b(before|after|instead\s+of)\s+([\w\s,]+?)\s+on\b", statement_text, re.IGNORECASE)
    return " ".join(match.groups()).strip() if match else "unknown"


def _routine_body(statement_text: str) -> str:
    dollar = re.search(r"\$[^$]*\$(.*?)\$[^$]*\$", statement_text, re.IGNORECASE | re.DOTALL)
    if dollar:
        return dollar.group(1)
    quoted = re.search(r"\bas\s+['\"](.*?)['\"]", statement_text, re.IGNORECASE | re.DOTALL)
    if quoted:
        return quoted.group(1)
    begin = re.search(r"\bbegin\b", statement_text, re.IGNORECASE)
    end = list(re.finditer(r"\bend\b", statement_text, re.IGNORECASE))
    if begin and end and end[-1].start() > begin.end():
        return statement_text[begin.end() : end[-1].start()]
    return ""


def _mask_sql_non_code(value: str) -> str:
    """Blank SQL comments and string literals while preserving offsets."""

    masked = re.sub(r"--[^\r\n]*", lambda match: " " * len(match.group(0)), value)
    masked = re.sub(r"/\*.*?\*/", lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)), masked, flags=re.DOTALL)
    return re.sub(r"'(?:''|\\.|[^'])*'", lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)), masked)


def _regex_table_names(body: str) -> set[str]:
    names: set[str] = set()
    pattern = r"\b(?:from|join|using|update|insert\s+into|delete\s+from|merge\s+into)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)"
    for match in re.finditer(pattern, body, re.IGNORECASE):
        names.add(match.group(1))
    return names


def _regex_table_relations(body: str) -> dict[str, set[str]]:
    relations: dict[str, set[str]] = {}
    patterns = (
        ("writes", r"\b(?:insert\s+into|update|delete\s+from|merge\s+into)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)"),
        ("reads", r"\b(?:from|join|using)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)"),
    )
    for relation_type, pattern in patterns:
        for match in re.finditer(pattern, body, re.IGNORECASE):
            relations.setdefault(match.group(1), set()).add(relation_type)
    return relations


def _split_statements(source: str, *, product: str | None = None) -> list[tuple[str, int, int]]:
    statements: list[tuple[str, int, int]] = []
    start = 0
    index = 0
    delimiter = ";"
    oracle_block = False
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_comment = False
    while index < len(source):
        char = source[index]
        pair = source[index : index + 2]
        if index == 0 or source[index - 1] in "\r\n":
            directive = re.match(r"[ \t]*delimiter[ \t]+([^\s]+)[ \t]*(?:\r?\n|$)", source[index:], re.IGNORECASE)
            if directive and product == "mysql":
                delimiter = directive.group(1)
                index += directive.end()
                start = index
                continue
            if product == "sqlserver":
                batch = re.match(r"[ \t]*GO(?:[ \t]+\d+)?[ \t]*(?:--[^\r\n]*)?(?:\r?\n|$)", source[index:], re.IGNORECASE)
                if batch:
                    if source[start:index].strip():
                        statements.append((source[start:index], start, index))
                    index += batch.end()
                    start = index
                    oracle_block = False
                    continue
            if product == "oracle":
                batch = re.match(r"[ \t]*/[ \t]*(?:\r?\n|$)", source[index:])
                if batch:
                    if source[start:index].strip():
                        statements.append((source[start:index], start, index))
                    index += batch.end()
                    start = index
                    oracle_block = False
                    continue
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if pair == "*/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if dollar_tag is not None:
            if source.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
            else:
                index += 1
            continue
        if quote is not None:
            if char == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote in {"'", '"', "`"}:
                index += 2
                continue
            index += 1
            continue
        if pair == "--":
            line_comment = True
            index += 2
            continue
        if pair == "/*":
            block_comment = True
            index += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if product == "oracle" and not oracle_block:
            routine_prefix = source[start:index]
            if re.match(
                r"\s*create\s+(?:or\s+replace\s+)?(?:function|procedure|trigger|package)\b",
                routine_prefix,
                re.IGNORECASE,
            ):
                oracle_block = True
        if delimiter and source.startswith(delimiter, index):
            if not (product == "oracle" and oracle_block):
                statements.append((source[start:index], start, index + len(delimiter)))
                index += len(delimiter)
                start = index
                continue
        if product == "oracle" and re.match(r"begin\b", source[index:], re.IGNORECASE):
            oracle_block = True
        if char == "$" and delimiter == ";":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", source[index:])
            if match:
                dollar_tag = match.group(0)
                index += len(dollar_tag)
                continue
        index += 1
    if source[start:].strip():
        statements.append((source[start:], start, len(source)))
    return statements


def _span_from_offsets(source: str, start: int, end: int) -> dict[str, int]:
    prefix = source[:start]
    value = source[start:end]
    start_line = prefix.count("\n") + 1
    end_line = start_line + value.count("\n")
    start_col = len(prefix.rsplit("\n", 1)[-1].encode("utf-8"))
    end_col = len(value.rsplit("\n", 1)[-1].encode("utf-8"))
    return {"start_line": start_line, "start_col": start_col, "end_line": end_line, "end_col": end_col}


def _finish_sql_document(
    builder: GraphBuilder,
    *,
    root: Path,
    config: AnalysisConfig,
    language: str,
    languages: list[str],
    deterministic: bool,
    commit_sha: str | None,
    sqlglot: Any,
) -> dict[str, Any]:
    runtime = {
        "python_version": platform.python_version(),
        "ast_version": f"sqlglot-{importlib.metadata.version('sqlglot')}",
        "parser": "sqlglot",
        "parser_version": importlib.metadata.version("sqlglot"),
        "grammars": [f"sqlglot:{PRODUCTS[product]['dialect']}" for product in languages if product in PRODUCTS],
    }
    meta = {
        "analyzer": {"name": ANALYZER_NAME, "version": ANALYZER_VERSION},
        "language": language,
        "languages": languages,
        "target": {"repository_id": repository_id(root), "relative_root": ".", "commit_sha": commit_sha},
        "runtime": runtime,
        "generated_at": None if deterministic else datetime.now(UTC).isoformat(),
        "deterministic": deterministic,
        "settings": config.to_dict(),
        "extensions": {"sql_products": languages},
    }
    return builder.document(meta)
