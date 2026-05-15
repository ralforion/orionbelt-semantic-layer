"""Integration tests: compile + execute the commerce battery on real Snowflake.

The full ``COMMERCE_CASES`` battery defined in
``tests/integration/_commerce.py`` runs against a live Snowflake account.
DuckDB executes the same queries against the same parquet fixtures and acts
as the source of truth — any row-level disagreement is a Snowflake dialect
bug.

Opt-in — requires live credentials::

    uv run pytest -m snowflake

Required env vars (skipped if missing):

    SNOWFLAKE_ACCOUNT     account identifier (e.g. DBAJBIQ-IH76647)
    SNOWFLAKE_USER        login name
    SNOWFLAKE_PASSWORD    password
    SNOWFLAKE_WAREHOUSE   warehouse name (default: COMPUTE_WH)
    SNOWFLAKE_DATABASE    database name (default: ORIONBELT)
    SNOWFLAKE_SCHEMA      schema name (default: PUBLIC)
    SNOWFLAKE_ROLE        role name (optional)

Fixture lifecycle: the module-scoped fixture creates the schema if absent,
then for each commerce parquet fixture, seeds a Snowflake table via the
stage + COPY INTO flow (``write_pandas``) *only if* the table is missing
or its row count differs from the parquet. Tables are kept after the run
to save warehouse compute on the next invocation. Set ``SNOWFLAKE_RESEED=1``
to force a reload regardless of current state.
"""

from __future__ import annotations

import os

import pytest

snowflake_connector = pytest.importorskip(
    "snowflake.connector", reason="snowflake-connector-python required"
)
pytest.importorskip(
    "snowflake.connector.pandas_tools",
    reason="snowflake-connector-python[pandas] required for write_pandas",
)
pd = pytest.importorskip("pandas", reason="pandas required for write_pandas")
pq = pytest.importorskip("pyarrow.parquet", reason="pyarrow required to read parquet")

from snowflake.connector.pandas_tools import write_pandas  # noqa: E402

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

pytestmark = pytest.mark.snowflake


def _required_env() -> dict[str, str] | None:
    required = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")
    if any(not os.environ.get(name) for name in required):
        return None
    return {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "password": os.environ["SNOWFLAKE_PASSWORD"],
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "ORIONBELT"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
        "role": os.environ.get("SNOWFLAKE_ROLE", ""),
    }


def _sf_rowcount(con, schema: str, table: str) -> int | None:
    """Return row count for the Snowflake table, or None if missing/unreadable."""
    cur = con.cursor()
    try:
        cur.execute(f'SELECT COUNT(*) FROM "{schema.upper()}"."{table.upper()}"')
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        cur.close()


def _parquet_rowcount(table: str) -> int:
    return pq.ParquetFile(parquet_path(table)).metadata.num_rows


def _load_parquet(con, schema: str, table: str) -> None:
    """Bulk-load one parquet fixture into a Snowflake table (replace mode).

    ``write_pandas`` uploads via an internal stage + COPY INTO. The
    ``auto_create_table=True`` lets Snowflake infer the schema from the
    DataFrame dtypes, which lines up with the parquet schema produced by
    ``tests/fixtures/commerce/_export.py``.
    """
    df = pd.read_parquet(parquet_path(table))
    df.columns = [c.upper() for c in df.columns]  # Snowflake quotes lowercase ⇒ case-sensitive
    write_pandas(
        con,
        df,
        table_name=table.upper(),
        schema=schema.upper(),
        auto_create_table=True,
        overwrite=True,
    )


@pytest.fixture(scope="module")
def snowflake_setup():
    cfg = _required_env()
    if cfg is None:
        pytest.skip(
            "SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / SNOWFLAKE_PASSWORD env vars "
            "not set — skipping live Snowflake integration tests"
        )

    connect_kwargs = {
        "account": cfg["account"],
        "user": cfg["user"],
        "password": cfg["password"],
        "warehouse": cfg["warehouse"],
        "database": cfg["database"],
        "schema": cfg["schema"],
    }
    if cfg["role"]:
        connect_kwargs["role"] = cfg["role"]

    con = snowflake_connector.connect(**connect_kwargs)
    cur = con.cursor()
    try:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{cfg["schema"].upper()}"')
    finally:
        cur.close()

    reseed = os.environ.get("SNOWFLAKE_RESEED", "").lower() in ("1", "true", "yes")
    try:
        for table in COMMERCE_TABLES:
            expected = _parquet_rowcount(table)
            actual = None if reseed else _sf_rowcount(con, cfg["schema"], table)
            if actual == expected:
                continue
            _load_parquet(con, cfg["schema"], table)
    except Exception:
        con.close()
        raise

    yield con, cfg

    con.close()


@pytest.fixture(scope="module")
def vendor_model(snowflake_setup):
    """Commerce model rewritten with uppercase schema/code identifiers.

    Snowflake quotes lowercase as case-sensitive lowercase, but write_pandas
    creates tables with uppercase names by default, so the model's lowercase
    ``code: sales`` would compile to ``"sales"`` and miss the actual ``SALES``
    table. Uppercase everything to match.
    """
    _con, cfg = snowflake_setup
    model = load_commerce_model(database=cfg["database"], schema=cfg["schema"].upper())
    # Uppercase the physical names so the compiled SQL targets the actual tables.
    for obj in model.data_objects.values():
        obj.code = obj.code.upper()
        for col in obj.columns.values():
            col.code = col.code.upper()
    return model


@pytest.fixture(scope="module")
def truth_model():
    return load_commerce_model(database="main", schema="orionbelt_1")


@pytest.fixture(scope="module")
def truth_results(truth_model):
    """Pre-compute expected rows once per session via the parquet-backed DuckDB."""
    con = open_duckdb_truth(schema="orionbelt_1")
    try:
        return {
            case.name: fetch_duckdb(con, compile_for(case.query, truth_model, "duckdb"))
            for case in COMMERCE_CASES
        }
    finally:
        con.close()


def _fetch_snowflake(con, sql: str) -> list[dict]:
    cur = con.cursor()
    try:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
    finally:
        cur.close()


@pytest.mark.parametrize("case", COMMERCE_CASES, ids=lambda c: c.name)
def test_commerce_case(snowflake_setup, vendor_model, truth_results, case: CommerceCase) -> None:
    """Compile for Snowflake, execute, compare row-by-row to DuckDB-truth."""
    con, _cfg = snowflake_setup
    sql = compile_for(case.query, vendor_model, "snowflake")
    actual = _fetch_snowflake(con, sql)
    compare_rows(actual, truth_results[case.name], case=case.name)
