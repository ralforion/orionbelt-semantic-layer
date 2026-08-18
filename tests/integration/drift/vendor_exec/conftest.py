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

import os
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
    SCHEMA as SEED_SCHEMA,
)
from ._seed import (  # noqa: E402
    seed_clickhouse,
    seed_duckdb,
    seed_mysql,
    seed_postgres,
)

# Row count of the bundled commerce ``sales`` table. A cloud vendor whose
# seed does not match this is stale, and every comparison against the DuckDB
# golden would fail for that reason rather than a real dialect difference.
EXPECTED_SALES_ROWS = 10000


def _require_seed(
    vendor: str,
    execute: Callable[[str], list[dict[str, Any]]],
    sales_ref: str,
) -> None:
    """Skip unless *vendor* carries a current commerce seed.

    Cloud vendors are seeded once, out of band, so a missing or stale schema
    is reported here with the command that fixes it rather than surfacing as a
    row mismatch in every test.
    """
    try:
        row = execute(f"SELECT COUNT(*) AS n FROM {sales_ref}")[0]
    except Exception as exc:  # noqa: BLE001 — any failure here means "not seeded"
        pytest.skip(
            f"{vendor} schema '{SEED_SCHEMA}' is not loaded ({exc}). Seed it with:\n"
            f"  uv run python scripts/seed_cloud_vendor.py {vendor}"
        )
    count = next(iter(row.values()))
    if count != EXPECTED_SALES_ROWS:
        raise AssertionError(
            f"{vendor} '{SEED_SCHEMA}'.sales has {count} rows, expected "
            f"{EXPECTED_SALES_ROWS}. Re-seed with: "
            f"uv run python scripts/seed_cloud_vendor.py {vendor}"
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
    # How this engine spells "these literal values, as a table" (#330). The
    # corpus seed is the right source for almost everything, but an aggregate
    # that only misbehaves past a double mantissa needs values the seed does
    # not contain, and every engine spells an inline row set differently.
    # Defaults to the UNION ALL form, which six of the eight accept.
    literal_rows: Callable[[str, str, list[int | None]], str] | None = None

    def rows_of(self, alias: str, column: str, values: list[int | None]) -> str:
        """A FROM-able source of one integer column holding *values*."""
        if self.literal_rows is not None:
            return self.literal_rows(alias, column, values)
        return _union_all_rows(alias, column, values, self.dialect)


def _cast_int(value: int | None, dialect: str) -> str:
    """A 64-bit integer literal, spelled for *dialect*."""
    # The 64-bit integer type is spelled differently per engine, and the NULL
    # case has to use the same spelling or the UNION legs disagree.
    type_name = {
        "clickhouse": "Nullable(Int64)",
        "bigquery": "INT64",
        "mysql": "SIGNED",
    }.get(dialect, "BIGINT")
    return f"CAST({'NULL' if value is None else value} AS {type_name})"


def _union_all_rows(alias: str, column: str, values: list[int | None], dialect: str) -> str:
    legs = " UNION ALL ".join(f"SELECT {_cast_int(v, dialect)} AS {column}" for v in values)
    return f"({legs}) {alias}"


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


# ---------------------------------------------------------------------------
# Snowflake (live account — no container exists for it)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def vendor_snowflake() -> VendorTarget:
    """Live Snowflake, seeded out of band from ``seed_sql/snowflake/``.

    The cloud vendors have no testcontainer, so the seed is applied once by
    ``scripts/seed_cloud_vendor.py`` rather than per session: re-loading 26k
    rows over the network on every run would dominate the suite, and the data
    is static. The fixture asserts the schema is present and current rather
    than creating it, so a stale or missing seed fails loudly here instead of
    surfacing as a row-count mismatch in every test.

    Skipped unless ``SNOWFLAKE_ACCOUNT`` and friends are set.
    """
    connector = pytest.importorskip(
        "snowflake.connector",
        reason="snowflake-connector-python required for snowflake vendor exec",
    )
    missing = [
        name
        for name in (
            "SNOWFLAKE_ACCOUNT",
            "SNOWFLAKE_USER",
            "SNOWFLAKE_PASSWORD",
            "SNOWFLAKE_DATABASE",
        )
        if not os.environ.get(name)
    ]
    if missing:
        pytest.skip(f"Snowflake credentials not configured: {', '.join(missing)}")

    conn = connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ["SNOWFLAKE_DATABASE"],
    )

    def _execute(sql: str) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    try:
        _require_seed("snowflake", _execute, f'"{SEED_SCHEMA}"."sales"')
        yield VendorTarget(name="snowflake", dialect="snowflake", execute=_execute)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BigQuery (live project — no container exists for it)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def vendor_bigquery() -> VendorTarget:
    """Live BigQuery, seeded out of band from ``seed_sql/bigquery/``.

    ``orionbelt_1`` is the dataset; the project comes from the client, so the
    model's two-part ``dataset.table`` reference resolves without a
    ``database:`` field. Skipped unless ``BIGQUERY_PROJECT`` is set.
    """
    bigquery = pytest.importorskip(
        "google.cloud.bigquery", reason="google-cloud-bigquery required for bigquery vendor exec"
    )
    project = os.environ.get("BIGQUERY_PROJECT")
    if not project:
        pytest.skip("BIGQUERY_PROJECT not set")

    client = bigquery.Client(project=project, location=os.environ.get("BIGQUERY_LOCATION"))

    def _execute(sql: str) -> list[dict[str, Any]]:
        return [dict(row) for row in client.query(sql).result()]

    try:
        _require_seed("bigquery", _execute, f"`{SEED_SCHEMA}`.`sales`")
        yield VendorTarget(name="bigquery", dialect="bigquery", execute=_execute)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Databricks (live workspace — no container exists for it)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def vendor_databricks() -> VendorTarget:
    """Live Databricks SQL warehouse, seeded out of band from ``seed_sql/databricks/``.

    ``orionbelt_1`` is the schema and the catalog comes from the connection,
    so the model's two-part reference resolves as-is. Skipped unless the
    workspace variables are set, and again if the warehouse cannot be reached
    — a stopped warehouse answers ``BAD_REQUEST: Cannot create the resource``
    rather than a connection error, so that is treated as unavailable too.
    """
    dbsql = pytest.importorskip(
        "databricks.sql", reason="databricks-sql-connector required for databricks vendor exec"
    )
    host = os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    http_path = os.environ.get("DATABRICKS_HTTP_PATH")
    token = os.environ.get("DATABRICKS_TOKEN") or os.environ.get("DATABRICKS_ACCESS_TOKEN")
    if not (host and http_path and token):
        pytest.skip("Databricks workspace variables not configured")

    try:
        conn = dbsql.connect(
            server_hostname=host,
            http_path=http_path,
            access_token=token,
            catalog=os.environ.get("DATABRICKS_CATALOG", "main"),
        )
    except Exception as exc:  # noqa: BLE001 — an unreachable warehouse is a skip, not a failure
        pytest.skip(f"Could not connect to Databricks: {exc}")

    def _execute(sql: str) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    try:
        _require_seed("databricks", _execute, f"`{SEED_SCHEMA}`.`sales`")
        yield VendorTarget(name="databricks", dialect="databricks", execute=_execute)
    finally:
        conn.close()
