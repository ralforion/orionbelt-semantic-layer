"""Post-generation SQL validation and pretty-printing using sqlglot."""

from __future__ import annotations

from functools import lru_cache

import sqlglot
from sqlglot.errors import SqlglotError

# Map OrionBelt dialect names to sqlglot dialect identifiers.
# Dremio uses Calcite-based ANSI SQL; Trino is the closest sqlglot dialect.
_DIALECT_MAP: dict[str, str] = {
    "bigquery": "bigquery",
    "clickhouse": "clickhouse",
    "databricks": "databricks",
    "dremio": "trino",
    "duckdb": "duckdb",
    "mysql": "mysql",
    "postgres": "postgres",
    "snowflake": "snowflake",
}


def validate_sql(sql: str, dialect_name: str) -> list[str]:
    """Parse SQL with sqlglot for the given dialect.

    Returns a list of error messages (empty if valid).
    Validation is non-blocking — callers should treat errors as warnings.
    """
    sg_dialect = _DIALECT_MAP.get(dialect_name)
    if sg_dialect is None:
        return [f"Unknown dialect '{dialect_name}' — skipping SQL validation"]

    # Parse only. ``transpile`` would also generate SQL back, but generation
    # runs at ``unsupported_level=WARN`` and only logs, so it never adds a
    # ``SqlglotError`` a parse does not raise; it cost ~15% of validation.
    errors: list[str] = []
    try:
        sqlglot.parse(sql, read=sg_dialect)
    except SqlglotError as exc:
        errors.append(str(exc))
    return errors


def format_sql(sql: str, dialect_name: str) -> str:
    """Pretty-print SQL with sqlglot, one expression per line.

    Falls back to the original SQL string on unknown dialect or parse error,
    matching the non-blocking philosophy of :func:`validate_sql`.
    """
    sg_dialect = _DIALECT_MAP.get(dialect_name)
    if sg_dialect is None:
        return sql
    return _pretty(sql, sg_dialect)


# Every REST response formats its SQL, and a repeated query (a compilation
# cache hit, a dashboard refresh) formats the same string again: ~1.2 ms of
# parse + generate on the TPC-DS example. Formatting is a pure function of two
# strings, so the result is memoized. Bounded by entry count; an entry is the
# SQL and its formatted form, a few KB.
@lru_cache(maxsize=512)
def _pretty(sql: str, sg_dialect: str) -> str:
    try:
        return sqlglot.transpile(sql, read=sg_dialect, write=sg_dialect, pretty=True)[0]
    except SqlglotError:
        return sql
