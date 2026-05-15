"""Integration tests: compile + execute the commerce battery on real ClickHouse.

The full ``COMMERCE_CASES`` battery defined in
``tests/integration/_commerce.py`` runs against a ClickHouse container.
DuckDB executes the same queries against the same parquet fixtures and acts
as the source of truth — any row-level disagreement is a ClickHouse dialect
bug.

Opt-in — requires Docker::

    uv run pytest -m docker

Skipped automatically when:
- testcontainers / clickhouse-connect / pandas / pyarrow are not installed
- the Docker daemon is not reachable
"""

from __future__ import annotations

import pytest

testcontainers_clickhouse = pytest.importorskip(
    "testcontainers.clickhouse", reason="testcontainers[clickhouse] required"
)
clickhouse_connect = pytest.importorskip("clickhouse_connect", reason="clickhouse-connect required")
pd = pytest.importorskip("pandas", reason="pandas required for bulk-load")
pytest.importorskip("pyarrow", reason="pyarrow required to read parquet")

from testcontainers.clickhouse import ClickHouseContainer  # noqa: E402

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


# ClickHouse maps OBML ``schema`` → CH ``database``. We use a single named
# database matching the commerce model so compiled SQL (``orionbelt_1.sales``)
# resolves cleanly.
_SCHEMA = "orionbelt_1"


_CH_TYPE_MAP = {
    "int64": "Nullable(Int64)",
    "int32": "Nullable(Int32)",
    "float64": "Nullable(Float64)",
    "float32": "Nullable(Float32)",
    "bool": "Nullable(UInt8)",
}


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _ch_type_for(dtype) -> str:
    s = str(dtype)
    if s.startswith("datetime64"):
        return "Nullable(DateTime)"
    if s == "object":
        return "Nullable(String)"
    return _CH_TYPE_MAP.get(s, "Nullable(String)")


def _load_parquet(client, schema: str, table: str) -> None:
    """CREATE TABLE + insert_df one parquet fixture via clickhouse-connect."""
    df = pd.read_parquet(parquet_path(table))
    # Convert date-only object columns (pyarrow surfaces those as object[date])
    # so CH can store them as Nullable(Date).
    for col in df.columns:
        if df[col].dtype == "object" and len(df) and hasattr(df[col].iloc[0], "isoformat"):
            df[col] = pd.to_datetime(df[col])

    cols_ddl = ", ".join(f"`{c}` {_ch_type_for(df[c].dtype)}" for c in df.columns)
    client.command(
        f"CREATE TABLE `{schema}`.`{table}` ({cols_ddl}) ENGINE = MergeTree() ORDER BY tuple()"
    )
    if df.empty:
        return
    client.insert_df(f"`{schema}`.`{table}`", df)


@pytest.fixture(scope="module")
def ch_setup():
    if not _docker_available():
        pytest.skip("Docker is not running")

    with ClickHouseContainer("clickhouse/clickhouse-server:latest") as ch:
        client = clickhouse_connect.get_client(
            host=ch.get_container_host_ip(),
            port=int(ch.get_exposed_port(8123)),
            username=ch.username,
            password=ch.password,
        )
        client.command(f"CREATE DATABASE `{_SCHEMA}`")
        for table in COMMERCE_TABLES:
            _load_parquet(client, _SCHEMA, table)
        yield client
        client.close()


@pytest.fixture(scope="module")
def vendor_model():
    return load_commerce_model(database="default", schema=_SCHEMA)


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


def _fetch_clickhouse(client, sql: str) -> list[dict]:
    result = client.query(sql)
    return [dict(zip(result.column_names, row, strict=False)) for row in result.result_rows]


@pytest.mark.parametrize("case", COMMERCE_CASES, ids=lambda c: c.name)
def test_commerce_case(ch_setup, vendor_model, truth_results, case: CommerceCase) -> None:
    sql = compile_for(case.query, vendor_model, "clickhouse")
    actual = _fetch_clickhouse(ch_setup, sql)
    compare_rows(actual, truth_results[case.name], case=case.name)
