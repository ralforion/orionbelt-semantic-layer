"""Vendor-execution seed loader.

Extracts the bundled commerce DuckDB seed once at session start and
provides per-vendor loaders that materialise the same data inside a
testcontainer (Postgres / MySQL / ClickHouse) or an in-memory DuckDB
under the ``orionbelt_1`` schema/database. The OBSL-emitted SQL
references ``orionbelt_1.<table>`` directly, so the schema name must
match the model's ``schema:`` field.

Type fidelity is intentionally loose at the source side — the bundled
DuckDB stores numeric columns as ``DOUBLE`` and the OBSL compiler
applies ``CAST(... AS DECIMAL(p, s))`` at query time per measure. The
seed mirrors that: ``DOUBLE`` everywhere, casts happen in the
generated SQL.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[4]
COMMERCE_DUCKDB = REPO_ROOT / "examples" / "orionbelt_1_commerce.duckdb"
SCHEMA = "orionbelt_1"

# ----------------------------------------------------------------------
# Source extraction (run once per session, cheap — ~25k rows total)
# ----------------------------------------------------------------------


def _read_source() -> dict[str, dict[str, Any]]:
    """Return ``{table_name: {"columns": [(name, duckdb_type)], "rows": [(...)]}}``."""
    if not COMMERCE_DUCKDB.exists():
        raise FileNotFoundError(
            f"Bundled DuckDB seed not found at {COMMERCE_DUCKDB}. "
            "Run scripts/build_demo_duckdb.py to generate it."
        )
    conn = duckdb.connect(database=str(COMMERCE_DUCKDB), read_only=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{SCHEMA}' ORDER BY table_name"
            ).fetchall()
        ]
        out: dict[str, dict[str, Any]] = {}
        for tbl in tables:
            cols = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_schema = '{SCHEMA}' AND table_name = '{tbl}' "
                "ORDER BY ordinal_position"
            ).fetchall()
            rows = conn.execute(f'SELECT * FROM "{SCHEMA}"."{tbl}"').fetchall()
            out[tbl] = {"columns": cols, "rows": rows}
        return out
    finally:
        conn.close()


_CACHE: dict[str, dict[str, Any]] | None = None


def get_source() -> dict[str, dict[str, Any]]:
    """Memoised ``_read_source`` — keeps the seed DuckDB read once per process."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _read_source()
    return _CACHE


# ----------------------------------------------------------------------
# Per-dialect type translation
# ----------------------------------------------------------------------


# Source ``DOUBLE`` columns in the bundled seed are all clean 2-dp money
# values. Loading them into the target as ``DECIMAL(18, 2)`` makes every
# engine's SUM/AVG/CAST exact and identical, eliminating cross-vendor
# float drift on monetary aggregates. The OBSL compiler already casts
# measure outputs to ``DECIMAL(18, 2)`` per the model's declared type,
# so the source-column choice never propagates to query output — but it
# does prevent IEEE-754 last-bit drift inside the engine's accumulator.
_PG_TYPES = {"VARCHAR": "TEXT", "DATE": "DATE", "DOUBLE": "DECIMAL(18, 2)"}
_MYSQL_TYPES = {"VARCHAR": "VARCHAR(255)", "DATE": "DATE", "DOUBLE": "DECIMAL(18, 2)"}
_CH_TYPES = {"VARCHAR": "String", "DATE": "Date", "DOUBLE": "Decimal(18, 2)"}
_DUCKDB_TYPES = {"VARCHAR": "VARCHAR", "DATE": "DATE", "DOUBLE": "DECIMAL(18, 2)"}


def _columns_clause(columns: list[tuple[str, str]], type_map: dict[str, str]) -> str:
    return ", ".join(f'"{name}" {type_map[dtype]}' for name, dtype in columns)


def _ch_columns_clause(columns: list[tuple[str, str]]) -> str:
    return ", ".join(f"`{name}` {_CH_TYPES[dtype]}" for name, dtype in columns)


# ----------------------------------------------------------------------
# Row → SQL literal rendering (kept separate from the connection so the
# same logic feeds a pre-prepared INSERT or a streaming bulk loader).
# ----------------------------------------------------------------------


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, date):
        return f"'{v.isoformat()}'"
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _values_batches(rows: list[tuple[Any, ...]], batch_size: int) -> list[str]:
    """Yield ``(...), (...)`` strings, ``batch_size`` rows per chunk."""
    out: list[str] = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        out.append(", ".join("(" + ", ".join(_lit(v) for v in row) + ")" for row in chunk))
    return out


# ----------------------------------------------------------------------
# Per-vendor seeders
# ----------------------------------------------------------------------


def seed_postgres(conn: Any) -> None:
    """Create ``orionbelt_1`` schema + tables and bulk-load all rows."""
    src = get_source()
    cur = conn.cursor()
    cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    cur.execute(f'CREATE SCHEMA "{SCHEMA}"')
    for tbl, payload in src.items():
        cur.execute(
            f'CREATE TABLE "{SCHEMA}"."{tbl}" ({_columns_clause(payload["columns"], _PG_TYPES)})'
        )
        for batch in _values_batches(payload["rows"], batch_size=500):
            cur.execute(f'INSERT INTO "{SCHEMA}"."{tbl}" VALUES {batch}')
    conn.commit()


def seed_mysql(conn: Any, *, grant_user: str | None = None) -> None:
    """Create ``orionbelt_1`` database (= MySQL schema) + tables and load rows.

    The testcontainer's default user is permissioned only for the
    container's default database; granting it on ``orionbelt_1`` lets
    the same connection reach the seeded tables. ``grant_user`` should
    be the connection's MySQL user — set to ``None`` to skip grant
    (e.g. when seeding as ``root``).
    """
    src = get_source()
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{SCHEMA}`")
    cur.execute(f"CREATE DATABASE `{SCHEMA}`")
    if grant_user:
        cur.execute(f"GRANT ALL PRIVILEGES ON `{SCHEMA}`.* TO '{grant_user}'@'%'")
        cur.execute("FLUSH PRIVILEGES")
    for tbl, payload in src.items():
        cols_sql = ", ".join(
            f"`{name}` {_MYSQL_TYPES[dtype]}" for name, dtype in payload["columns"]
        )
        cur.execute(f"CREATE TABLE `{SCHEMA}`.`{tbl}` ({cols_sql})")
        for batch in _values_batches(payload["rows"], batch_size=500):
            cur.execute(f"INSERT INTO `{SCHEMA}`.`{tbl}` VALUES {batch}")
    conn.commit()


def seed_clickhouse(client: Any) -> None:
    """ClickHouse: CREATE DATABASE + MergeTree tables; INSERT in chunks."""
    src = get_source()
    client.command(f"DROP DATABASE IF EXISTS `{SCHEMA}`")
    client.command(f"CREATE DATABASE `{SCHEMA}`")
    for tbl, payload in src.items():
        # Pick the first column as the ORDER BY key — IDs are first by
        # convention in this schema, and any column suffices for our
        # query workload (no real-world ordering matters here).
        order_key = payload["columns"][0][0]
        client.command(
            f"CREATE TABLE `{SCHEMA}`.`{tbl}` ({_ch_columns_clause(payload['columns'])}) "
            f"ENGINE = MergeTree() ORDER BY `{order_key}`"
        )
        for batch in _values_batches(payload["rows"], batch_size=500):
            client.command(f"INSERT INTO `{SCHEMA}`.`{tbl}` VALUES {batch}")


def seed_duckdb(conn: Any) -> None:
    """In-memory DuckDB seed — same SQL path as the testcontainer engines.

    Useful as a "control" target: the same loader code feeds DuckDB,
    Postgres, MySQL, and ClickHouse, so a divergence in one vendor's
    rows is unambiguously attributable to that engine, not to the
    seed loader.
    """
    src = get_source()
    conn.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    conn.execute(f'CREATE SCHEMA "{SCHEMA}"')
    for tbl, payload in src.items():
        cols_sql = _columns_clause(payload["columns"], _DUCKDB_TYPES)
        conn.execute(f'CREATE TABLE "{SCHEMA}"."{tbl}" ({cols_sql})')
        for batch in _values_batches(payload["rows"], batch_size=500):
            conn.execute(f'INSERT INTO "{SCHEMA}"."{tbl}" VALUES {batch}')
