"""Vendor-execution fixtures (Phase A — local testcontainers).

Spins up Postgres, MySQL, and ClickHouse containers once per session
and seeds each with the bundled commerce dataset. Each fixture
yields a ``VendorTarget`` — a small dataclass the parametrized
test consumes uniformly regardless of which engine is underneath.

All vendor-exec tests are gated by the ``docker`` pytest marker, so
the regular suite (``pytest`` without `-m docker`) skips them. Run
the full vendor-exec sweep with::

    uv run pytest -m docker tests/integration/drift/vendor_exec/
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

# Each container library is optional — skip the corresponding tests
# rather than failing collection if the import is missing.
testcontainers_postgres = pytest.importorskip(
    "testcontainers.postgres", reason="testcontainers[postgres] required for vendor_exec"
)
testcontainers_mysql = pytest.importorskip(
    "testcontainers.mysql", reason="testcontainers[mysql] required for vendor_exec"
)
testcontainers_clickhouse = pytest.importorskip(
    "testcontainers.clickhouse", reason="testcontainers[clickhouse] required for vendor_exec"
)

from testcontainers.clickhouse import ClickHouseContainer  # noqa: E402
from testcontainers.mysql import MySqlContainer  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from ._seed import (  # noqa: E402
    seed_clickhouse,
    seed_duckdb,
    seed_mysql,
    seed_postgres,
)


@dataclass
class VendorTarget:
    """Single contract every vendor fixture must satisfy.

    ``execute(sql) -> list[dict]`` is the only operation the test runs;
    each fixture wraps its native driver call so the test stays
    driver-agnostic. ``dialect`` is the OBSL dialect name passed to
    the compiler (``"postgres"`` etc.).
    """

    name: str
    dialect: str
    execute: Callable[[str], list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# DuckDB (in-memory, fed by the same seed loader so it's byte-comparable
# to the other vendors — *not* the bundled .duckdb file path)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def vendor_duckdb() -> VendorTarget:
    duckdb_mod = pytest.importorskip("duckdb", reason="duckdb required for duckdb vendor exec")
    conn = duckdb_mod.connect(":memory:")
    seed_duckdb(conn)

    def _execute(sql: str) -> list[dict[str, Any]]:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    try:
        yield VendorTarget(name="duckdb", dialect="duckdb", execute=_execute)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def vendor_postgres() -> VendorTarget:
    psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 required for postgres vendor exec")
    with PostgresContainer("postgres:16") as pg:
        conn = psycopg2.connect(
            host=pg.get_container_host_ip(),
            port=pg.get_exposed_port(5432),
            user=pg.username,
            password=pg.password,
            dbname=pg.dbname,
        )
        # Seed inside an explicit transaction so the bulk inserts commit
        # atomically; flip to autocommit afterwards so a single failing
        # test query doesn't abort the connection state and cascade into
        # ``InFailedSqlTransaction`` errors on every subsequent test.
        conn.autocommit = False
        seed_postgres(conn)
        conn.autocommit = True

        def _execute(sql: str) -> list[dict[str, Any]]:
            # Close cursor after every query so a failure in one test
            # never leaks into the next; psycopg2's per-cursor state is
            # surprisingly sticky when a query raises mid-fetch.
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

        try:
            yield VendorTarget(name="postgres", dialect="postgres", execute=_execute)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def vendor_mysql() -> VendorTarget:
    pymysql = pytest.importorskip("pymysql", reason="pymysql required for mysql vendor exec")
    with MySqlContainer("mysql:8.0") as my:
        # Seed as root so the GRANT step succeeds; the test's actual
        # connection then uses the regular ``test`` user.
        root_conn = pymysql.connect(
            host=my.get_container_host_ip(),
            port=int(my.get_exposed_port(3306)),
            user="root",
            password=my.root_password,
            database=my.dbname,
        )
        try:
            seed_mysql(root_conn, grant_user=my.username)
        finally:
            root_conn.close()

        conn = pymysql.connect(
            host=my.get_container_host_ip(),
            port=int(my.get_exposed_port(3306)),
            user=my.username,
            password=my.password,
            database=my.dbname,
        )

        def _execute(sql: str) -> list[dict[str, Any]]:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

        try:
            yield VendorTarget(name="mysql", dialect="mysql", execute=_execute)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def vendor_clickhouse() -> VendorTarget:
    clickhouse_connect = pytest.importorskip(
        "clickhouse_connect", reason="clickhouse-connect required for clickhouse vendor exec"
    )
    with ClickHouseContainer("clickhouse/clickhouse-server:latest") as ch:
        client = clickhouse_connect.get_client(
            host=ch.get_container_host_ip(),
            port=int(ch.get_exposed_port(8123)),
            username=ch.username,
            password=ch.password,
            database=ch.dbname,
        )
        seed_clickhouse(client)

        def _execute(sql: str) -> list[dict[str, Any]]:
            res = client.query(sql)
            return [dict(zip(res.column_names, row, strict=True)) for row in res.result_rows]

        try:
            yield VendorTarget(name="clickhouse", dialect="clickhouse", execute=_execute)
        finally:
            client.close()
