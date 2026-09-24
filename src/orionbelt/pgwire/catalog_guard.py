"""What the pgwire catalog connection is allowed to run.

Statements that are not semantic queries - catalog probes, BI metadata
browsing, the temp-table cycle Tableau runs to check a connection - go to the
in-memory catalog DuckDB rather than the OBSQL translator. That connection
serves two things, and this module admits nothing else:

* **the catalog**: ``pg_catalog`` and ``information_schema``, including the
  ``pg_*`` names clients use without a schema;
* **the OBSL objects** of each loaded model: the synthetic ``model`` table and
  its metadata views.

Plus ``"#..."`` temp tables, which a BI connect check creates, fills, reads and
drops. A statement is checked as a whole: one statement, of an allowed kind,
reading only allowed relations. What cannot be parsed cannot be checked, so it
is refused rather than guessed at.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

#: Relations each model schema holds (see ``CatalogEmulator.refresh``).
OBSL_OBJECTS: frozenset[str] = frozenset(
    {
        "model",
        "dimensions",
        "measures",
        "metrics",
        "_dimensions_metadata",
        "_measures_metadata",
        "_metrics_metadata",
    }
)

_CATALOG_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema"})

#: Databases a relation may name: the branded catalog database and DuckDB's own.
_DATABASES: frozenset[str] = frozenset({"", "orionbelt", "memory", "system", "temp"})

#: Table-valued functions a catalog probe may read from, besides ``pg_*``.
_TABLE_FUNCTIONS: frozenset[str] = frozenset({"unnest", "generate_series"})

_READS = (exp.Select, exp.SetOperation)


def _is_temp_name(name: str) -> bool:
    return name.startswith("#")


def _parse(sql: str) -> exp.Expr | None:
    """The single statement *sql* holds, or ``None`` if it is not exactly one."""
    for dialect in ("postgres", "duckdb"):
        try:
            statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
        except sqlglot.errors.ParseError:
            continue
        return statements[0] if len(statements) == 1 else None
    return None


def _function_allowed(node: exp.Expr) -> bool:
    if isinstance(node, exp.ExplodingGenerateSeries):
        return True
    if isinstance(node, exp.Anonymous):
        name = node.name.lower()
    elif isinstance(node, exp.Func):
        name = node.sql_name().lower()
    else:
        return False
    return name in _TABLE_FUNCTIONS or name.startswith(("pg_", "_pg_"))


def _relation_allowed(table: exp.Table, model_schemas: set[str], ctes: set[str]) -> bool:
    if not isinstance(table.this, exp.Identifier):
        return table.this is not None and _function_allowed(table.this)
    name = table.name
    schema = table.db.lower()
    if table.catalog.lower() not in _DATABASES:
        return False
    if _is_temp_name(name):
        return not schema
    if not schema:
        return name in ctes or name.lower().startswith("pg_") or name.lower() in OBSL_OBJECTS
    if schema in _CATALOG_SCHEMAS:
        return True
    return schema in model_schemas and name.lower() in OBSL_OBJECTS


def _writes_only_temp_tables(stmt: exp.Expr) -> bool:
    """``CREATE``, ``INSERT`` and ``DROP`` are the BI connect check, on a temp table."""
    if isinstance(stmt, exp.Create | exp.Drop) and (stmt.args.get("kind") or "").upper() != "TABLE":
        return False
    # ``DROP`` lists its tables (it may name several); the others name one.
    targets = (stmt.args.get("tables") or []) if isinstance(stmt, exp.Drop) else [stmt.this]
    unwrapped = [t.this if isinstance(t, exp.Schema) else t for t in targets]
    return bool(unwrapped) and all(
        isinstance(t, exp.Table) and _is_temp_name(t.name) and not t.db for t in unwrapped
    )


def catalog_rejection(sql: str, model_schemas: set[str]) -> str | None:
    """Why the catalog connection must not run *sql*, or ``None`` when it may.

    *model_schemas* are the schemas holding a loaded model's OBSL objects.
    """
    stmt = _parse(sql)
    if stmt is None:
        return "only a single statement the catalog can check is accepted"
    if isinstance(stmt, exp.Create | exp.Insert | exp.Drop):
        if not _writes_only_temp_tables(stmt):
            return 'only temporary tables named "#..." may be created, filled or dropped'
    elif not isinstance(stmt, _READS):
        return f"{stmt.key.upper()} statements are not accepted"
    ctes = {cte.alias for cte in stmt.find_all(exp.CTE)}
    for table in stmt.find_all(exp.Table):
        if not _relation_allowed(table, model_schemas, ctes):
            return f"'{table.sql(dialect='postgres')}' is not part of the catalog or an OBSL model"
    return None
