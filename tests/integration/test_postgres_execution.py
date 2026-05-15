"""Integration tests: compile + execute the commerce battery on real PostgreSQL.

The full ``COMMERCE_CASES`` battery defined in
``tests/integration/_commerce.py`` runs against a PostgreSQL container.
DuckDB executes the same queries against the same parquet fixtures and acts
as the source of truth — any row-level disagreement is a Postgres dialect
bug.

Opt-in — requires Docker::

    uv run pytest -m docker

Skipped automatically when:
- testcontainers / psycopg2 / pandas / pyarrow are not installed
- the Docker daemon is not reachable
"""

from __future__ import annotations

import pytest

testcontainers_postgres = pytest.importorskip(
    "testcontainers.postgres", reason="testcontainers[postgres] required"
)
psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2-binary required")
pd = pytest.importorskip("pandas", reason="pandas required for bulk-load")
pytest.importorskip("pyarrow", reason="pyarrow required to read parquet")

from psycopg2.extras import RealDictCursor, execute_values  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from tests.integration._commerce import (  # noqa: E402
    COMMERCE_CASES,
    COMMERCE_TABLES,
    CommerceCase,
    compare_rows,
    compile_for,
    fetch_duckdb,
    load_commerce_model,
    open_duckdb_truth,
    parquet_path,
)

pytestmark = pytest.mark.docker


_SCHEMA = "orionbelt_1"


_PG_TYPE_MAP = {
    "object": "TEXT",
    "string": "TEXT",
    "int64": "BIGINT",
    "int32": "INTEGER",
    "float64": "DOUBLE PRECISION",
    "float32": "REAL",
    "bool": "BOOLEAN",
    "datetime64[ns]": "TIMESTAMP",
}


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _pg_type_for(dtype) -> str:
    s = str(dtype)
    if s.startswith("datetime64"):
        return "TIMESTAMP"
    if s == "object":
        return "TEXT"
    return _PG_TYPE_MAP.get(s, "TEXT")


def _load_parquet(cur, schema: str, table: str) -> None:
    """CREATE TABLE + INSERT one parquet fixture via psycopg2.execute_values."""
    df = pd.read_parquet(parquet_path(table))
    # Convert pandas date-only columns (which pyarrow surfaces as object[date])
    # to ISO strings; psycopg2 handles those natively as DATE.
    cols_ddl = ", ".join(f'"{c}" {_pg_type_for(df[c].dtype)}' for c in df.columns)
    cur.execute(f'CREATE TABLE "{schema}"."{table}" ({cols_ddl})')
    if df.empty:
        return
    quoted_cols = ", ".join(f'"{c}"' for c in df.columns)
    # df.itertuples gives Python tuples; psycopg2 + execute_values is fast and
    # handles None/dates/decimals correctly without needing per-column casting.
    rows = [tuple(None if pd.isna(v) else v for v in row) for row in df.itertuples(index=False)]
    execute_values(
        cur,
        f'INSERT INTO "{schema}"."{table}" ({quoted_cols}) VALUES %s',
        rows,
    )


@pytest.fixture(scope="module")
def postgres_setup():
    if not _docker_available():
        pytest.skip("Docker is not running")

    with PostgresContainer("postgres:16-alpine") as pg:
        conn = psycopg2.connect(
            host=pg.get_container_host_ip(),
            port=pg.get_exposed_port(5432),
            dbname=pg.dbname,
            user=pg.username,
            password=pg.password,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f'CREATE SCHEMA "{_SCHEMA}"')
        for table in COMMERCE_TABLES:
            _load_parquet(cur, _SCHEMA, table)
        cur.close()
        yield conn
        conn.close()


@pytest.fixture(scope="module")
def vendor_model():
    return load_commerce_model(database="postgres", schema=_SCHEMA)


@pytest.fixture(scope="module")
def truth_model():
    return load_commerce_model(database="main", schema=_SCHEMA)


@pytest.fixture(scope="module")
def truth_results(truth_model):
    con = open_duckdb_truth(schema=_SCHEMA)
    try:
        return {
            case.name: fetch_duckdb(con, compile_for(case.query, truth_model, "duckdb"))
            for case in COMMERCE_CASES
        }
    finally:
        con.close()


def _fetch_postgres(conn, sql: str) -> list[dict]:
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(sql)
        return [dict(row) for row in cur.fetchall()]
    finally:
        cur.close()


@pytest.mark.parametrize("case", COMMERCE_CASES, ids=lambda c: c.name)
def test_commerce_case(postgres_setup, vendor_model, truth_results, case: CommerceCase) -> None:
    sql = compile_for(case.query, vendor_model, "postgres")
    actual = _fetch_postgres(postgres_setup, sql)
    compare_rows(actual, truth_results[case.name], case=case.name)
