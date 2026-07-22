"""Integration tests: compile + execute the commerce battery on real Databricks.

The full ``COMMERCE_CASES`` battery defined in
``tests/integration/_commerce.py`` runs against a live Databricks SQL warehouse.
DuckDB executes the same queries against the same parquet fixtures and acts
as the source of truth — any row-level disagreement is a Databricks dialect
bug.

Opt-in — requires live credentials::

    uv run pytest -m databricks

Required env vars (skipped if missing):

    DATABRICKS_SERVER_HOSTNAME  workspace hostname (e.g. adb-xxxx.azuredatabricks.net)
    DATABRICKS_HTTP_PATH        SQL warehouse HTTP path (/sql/1.0/warehouses/<id>)
    DATABRICKS_TOKEN            personal access token (or DATABRICKS_ACCESS_TOKEN)
    DATABRICKS_CATALOG          Unity Catalog name (default: main)
    DATABRICKS_SCHEMA           schema name (default: orionbelt_test)

Fixture lifecycle: the module-scoped fixture ensures the schema exists,
then for each commerce parquet fixture, seeds a Delta table *only if* the
table is missing or its row count differs from the parquet (cheap rowcount
check). Tables are kept after the run to save compute on the next
invocation. Set ``DATABRICKS_RESEED=1`` to force a drop-and-reload.

Seeding prefers a **bulk** path: stage the parquet into a Unity Catalog
Volume via ``PUT`` and load it with ``read_files`` (one upload + one
statement per table). Row-by-row ``INSERT VALUES`` — dozens of warehouse
round-trips that can outlive the warehouse's auto-stop — is the fallback
when the workspace has no Volume support or the token lacks the privilege.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

databricks_sql = pytest.importorskip("databricks.sql", reason="databricks-sql-connector required")
pd = pytest.importorskip("pandas", reason="pandas required for bulk-load")
pq = pytest.importorskip("pyarrow.parquet", reason="pyarrow required to read parquet")

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
from tests.integration._measure_sweep import (  # noqa: E402
    SWEEP_IDS,
    SWEEP_ITEMS,
    sweep_query,
)

pytestmark = pytest.mark.databricks


# Databricks INSERT VALUES has a statement-size limit; chunk wide tables so
# no single insert exceeds the parser. 500 rows × ~10 cols stays comfortable.
_INSERT_CHUNK_ROWS = 500

# Bulk-seed staging: local dir the connector's PUT is allowed to read, and the
# Unity Catalog volume the parquet is staged into.
_STAGING_DIR = str(parquet_path(COMMERCE_TABLES[0]).parent)
_SEED_VOLUME = "obsl_seed"


_DBX_TYPE_MAP = {
    "int64": "BIGINT",
    "int32": "INT",
    "float64": "DOUBLE",
    "float32": "FLOAT",
    "bool": "BOOLEAN",
}


def _required_env() -> dict[str, str] | None:
    host = os.environ.get("DATABRICKS_SERVER_HOSTNAME")
    http_path = os.environ.get("DATABRICKS_HTTP_PATH")
    token = os.environ.get("DATABRICKS_TOKEN") or os.environ.get("DATABRICKS_ACCESS_TOKEN")
    if not (host and http_path and token):
        return None
    return {
        "server_hostname": host,
        "http_path": http_path,
        "access_token": token,
        "catalog": os.environ.get("DATABRICKS_CATALOG", "main"),
        "schema": os.environ.get("DATABRICKS_SCHEMA", "orionbelt_test"),
    }


def _dbx_type_for(dtype) -> str:
    s = str(dtype)
    if s.startswith("datetime64"):
        return "TIMESTAMP"
    if s == "object":
        return "STRING"
    return _DBX_TYPE_MAP.get(s, "STRING")


def _render_value(v) -> str:
    """Render a Python value as a Databricks SQL literal."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, dt.datetime):
        return f"TIMESTAMP '{v.isoformat(sep=' ')}'"
    if isinstance(v, dt.date):
        return f"DATE '{v.isoformat()}'"
    # String — escape single quotes
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _table_rowcount(cur, qualified: str) -> int | None:
    """Return row count for ``qualified`` table, or None if missing/unreadable."""
    try:
        cur.execute(f"SELECT COUNT(*) FROM {qualified}")
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None


def _parquet_rowcount(table: str) -> int:
    return pq.ParquetFile(parquet_path(table)).metadata.num_rows


def _load_parquet_bulk(cur, catalog: str, schema: str, volume: str, table: str) -> None:
    """Fast path: PUT the parquet into a UC volume, then CTAS via ``read_files``.

    One file upload + one ``CREATE TABLE ... AS SELECT`` per table, versus the
    dozens of ``INSERT`` round-trips the row-by-row path makes. Raises on any
    failure so the caller can fall back to ``_load_parquet``.
    """
    qualified = f"`{catalog}`.`{schema}`.`{table}`"
    volume_file = f"/Volumes/{catalog}/{schema}/{volume}/{table}.parquet"
    cur.execute(f"PUT '{parquet_path(table)}' INTO '{volume_file}' OVERWRITE")
    cur.execute(
        f"CREATE OR REPLACE TABLE {qualified} USING DELTA AS "
        f"SELECT * FROM read_files('{volume_file}', format => 'parquet')"
    )


def _load_parquet(cur, catalog: str, schema: str, table: str) -> None:
    """CREATE TABLE + chunked INSERT VALUES one parquet fixture into Databricks."""
    df = pd.read_parquet(parquet_path(table))
    qualified = f"`{catalog}`.`{schema}`.`{table}`"
    cur.execute(f"DROP TABLE IF EXISTS {qualified}")
    cols_ddl = ", ".join(f"`{c}` {_dbx_type_for(df[c].dtype)}" for c in df.columns)
    cur.execute(f"CREATE TABLE {qualified} ({cols_ddl}) USING DELTA")
    if df.empty:
        return
    quoted_cols = ", ".join(f"`{c}`" for c in df.columns)
    for start in range(0, len(df), _INSERT_CHUNK_ROWS):
        chunk = df.iloc[start : start + _INSERT_CHUNK_ROWS]
        values_sql = ",\n".join(
            "(" + ", ".join(_render_value(v) for v in row) + ")"
            for row in chunk.itertuples(index=False)
        )
        cur.execute(f"INSERT INTO {qualified} ({quoted_cols}) VALUES {values_sql}")


@pytest.fixture(scope="module")
def databricks_setup():
    cfg = _required_env()
    if cfg is None:
        pytest.skip(
            "DATABRICKS_SERVER_HOSTNAME / DATABRICKS_HTTP_PATH / DATABRICKS_TOKEN "
            "env vars not set — skipping live Databricks integration tests"
        )

    try:
        con = databricks_sql.connect(
            server_hostname=cfg["server_hostname"],
            http_path=cfg["http_path"],
            access_token=cfg["access_token"],
            staging_allowed_local_path=_STAGING_DIR,  # lets PUT read the parquet fixtures
        )
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Could not connect to Databricks: {e}")

    cur = con.cursor()
    try:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS `{cfg['catalog']}`.`{cfg['schema']}`")
    except Exception:
        cur.close()
        con.close()
        raise

    # Prefer the bulk (PUT + read_files) path; fall back to row-by-row INSERT if
    # the workspace has no volume support or the token can't create one.
    bulk_volume: str | None = _SEED_VOLUME
    try:
        cur.execute(
            f"CREATE VOLUME IF NOT EXISTS `{cfg['catalog']}`.`{cfg['schema']}`.`{_SEED_VOLUME}`"
        )
    except Exception:  # noqa: BLE001 -- no UC volume → use the INSERT fallback
        bulk_volume = None

    reseed = os.environ.get("DATABRICKS_RESEED", "").lower() in ("1", "true", "yes")
    try:
        for table in COMMERCE_TABLES:
            qualified = f"`{cfg['catalog']}`.`{cfg['schema']}`.`{table}`"
            expected = _parquet_rowcount(table)
            actual = None if reseed else _table_rowcount(cur, qualified)
            if actual == expected:
                continue
            loaded = False
            if bulk_volume is not None:
                try:
                    _load_parquet_bulk(cur, cfg["catalog"], cfg["schema"], bulk_volume, table)
                    loaded = True
                except Exception:  # noqa: BLE001 -- bulk failed → row-by-row fallback
                    bulk_volume = None
            if not loaded:
                _load_parquet(cur, cfg["catalog"], cfg["schema"], table)
    except Exception:
        cur.close()
        con.close()
        raise

    yield con, cfg

    cur.close()
    con.close()


@pytest.fixture(scope="module")
def vendor_model(databricks_setup):
    _con, cfg = databricks_setup
    return load_commerce_model(database=cfg["catalog"], schema=cfg["schema"])


@pytest.fixture(scope="module")
def truth_model():
    return load_commerce_model(database="main", schema="orionbelt_1")


@pytest.fixture(scope="module")
def truth_results(truth_model):
    con = open_duckdb_truth(schema="orionbelt_1")
    try:
        return {
            case.name: fetch_duckdb(con, compile_for(case.query, truth_model, "duckdb"))
            for case in COMMERCE_CASES
        }
    finally:
        con.close()


def _fetch_databricks(con, sql: str) -> list[dict]:
    cur = con.cursor()
    try:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
    finally:
        cur.close()


@pytest.mark.parametrize("case", COMMERCE_CASES, ids=lambda c: c.name)
def test_commerce_case(databricks_setup, vendor_model, truth_results, case: CommerceCase) -> None:
    """Compile for Databricks, execute, compare row-by-row to DuckDB-truth."""
    con, _cfg = databricks_setup
    sql = compile_for(case.query, vendor_model, "databricks")
    actual = _fetch_databricks(con, sql)
    compare_rows(actual, truth_results[case.name], case=case.name)


@pytest.mark.parametrize("kind,name,dims", SWEEP_ITEMS, ids=SWEEP_IDS)
def test_measure_sweep(
    databricks_setup, vendor_model, kind: str, name: str, dims: list[str]
) -> None:
    """Every measure and metric must execute on Databricks (execution only).

    Breadth complement to ``test_commerce_case``: covers the full measure and
    metric surface (incl. synthesised counts), asserting each runs rather than
    comparing rows. Catches Databricks SQL that compiles but the engine rejects
    (e.g. quarter-grain date arithmetic).
    """
    con, _cfg = databricks_setup
    sql = compile_for(sweep_query(name, dims), vendor_model, "databricks")
    rows = _fetch_databricks(con, sql)  # raises on a Databricks execution error
    assert isinstance(rows, list), f"{kind} {name!r} returned no result set"
