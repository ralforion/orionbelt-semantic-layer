"""Post-generation SQL validation using sqlglot."""

from __future__ import annotations

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

    errors: list[str] = []
    try:
        sqlglot.transpile(sql, read=sg_dialect)
    except SqlglotError as exc:
        errors.append(str(exc))
    return errors
