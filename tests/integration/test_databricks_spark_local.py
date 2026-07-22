"""Execute the Databricks dialect on a local PySpark session.

Databricks SQL *is* Spark SQL — Databricks Runtime runs Apache Spark, and the
analytical SQL OBSL compiles for the ``databricks`` dialect (date functions,
window functions, CTEs, ``SEQUENCE``/``EXPLODE``, ``add_months``) is core
Spark. Databricks' additions (Photon, Unity Catalog, ``read_files`` / Volumes,
Delta SQL) are execution/storage features that never appear in the query
dialect, so a local PySpark session with ANSI mode on (Databricks' default) is
a faithful executor for the "does this SQL parse and run" bug class.

This gives the Databricks dialect real execution coverage without a live
Databricks warehouse — the credit-gated `-m databricks` suite is the only path
that exercises the true Databricks SQL warehouse + Delta, but this catches the
same class of dialect bugs the other vendor sweeps found.

Opt-in via the ``spark`` marker (needs ``pyspark`` and a JDK)::

    uv pip install 'pyspark>=3.5,<4.0'   # or: pip install -e '.[spark]'
    uv run pytest -m spark

Skips cleanly if pyspark is missing or no JDK is available.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

pyspark = pytest.importorskip("pyspark", reason="pyspark required for local Spark execution")

from pyspark.sql import SparkSession  # noqa: E402

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

pytestmark = pytest.mark.spark

# Distinct DB name (not ``orionbelt_1``) so a run never clobbers a developer's
# own Spark database, and an explicit LOCATION keeps its storage inside a temp
# dir rather than the repo's default ``./spark-warehouse``.
SCHEMA = "orionbelt_spark_test"

# Artifacts Spark may still drop in the CWD despite the temp warehouse. We only
# delete ones that did not exist before the fixture ran, never a pre-existing one.
_CWD_STRAYS = ("spark-warehouse", "metastore_db", "derby.log")


@pytest.fixture(scope="module")
def spark_session():
    """A local Spark session (ANSI mode) seeded with the commerce parquet.

    The database's storage is pinned to a temp dir via an explicit ``LOCATION``
    (``spark.sql.warehouse.dir`` alone is unreliable for managed-table paths),
    the database is dropped on teardown, and any stray CWD artifacts the JVM
    still creates are removed — but only if this fixture created them. Skips if
    a JDK is not available to start the JVM.
    """
    warehouse = tempfile.mkdtemp(prefix="obsl-spark-")
    cwd = Path.cwd()
    pre_existing = {name for name in _CWD_STRAYS if (cwd / name).exists()}

    def _cleanup_cwd() -> None:
        for name in _CWD_STRAYS:
            if name in pre_existing:
                continue
            stray = cwd / name
            if stray.is_dir():
                shutil.rmtree(stray, ignore_errors=True)
            elif stray.exists():
                stray.unlink()

    try:
        spark = (
            SparkSession.builder.appName("obsl-databricks-local")
            .master("local[2]")
            # Match the Databricks SQL warehouse default so ANSI-sensitive
            # behaviour (casts, arithmetic) lines up with the real engine.
            .config("spark.sql.ansi.enabled", "true")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.sql.warehouse.dir", warehouse)
            .config("spark.driver.extraJavaOptions", f"-Dderby.system.home={warehouse}")
            .getOrCreate()
        )
    except Exception as exc:  # noqa: BLE001 -- no JDK / JVM start failure → skip
        shutil.rmtree(warehouse, ignore_errors=True)
        _cleanup_cwd()
        pytest.skip(f"could not start a local Spark session (needs a JDK): {exc}")

    spark.sparkContext.setLogLevel("ERROR")
    db_location = (Path(warehouse) / f"{SCHEMA}.db").as_uri()
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {SCHEMA} LOCATION '{db_location}'")
    for table in COMMERCE_TABLES:
        (
            spark.read.parquet(str(parquet_path(table)))
            .write.mode("overwrite")
            .saveAsTable(f"{SCHEMA}.{table}")
        )
    try:
        yield spark
    finally:
        # Best-effort; the temp-dir removal below covers the storage regardless.
        with contextlib.suppress(Exception):
            spark.sql(f"DROP DATABASE IF EXISTS {SCHEMA} CASCADE")
        spark.stop()
        shutil.rmtree(warehouse, ignore_errors=True)
        _cleanup_cwd()


@pytest.fixture(scope="module")
def vendor_model():
    # Spark's default catalog is ``spark_catalog``; tables live in the SCHEMA db.
    return load_commerce_model(database="spark_catalog", schema=SCHEMA)


@pytest.fixture(scope="module")
def truth_results():
    """DuckDB-truth rows for the battery, from the same parquet fixtures."""
    truth_model = load_commerce_model(database="main", schema=SCHEMA)
    con = open_duckdb_truth(schema=SCHEMA)
    try:
        return {
            case.name: fetch_duckdb(con, compile_for(case.query, truth_model, "duckdb"))
            for case in COMMERCE_CASES
        }
    finally:
        con.close()


def _fetch(spark, sql: str) -> list[dict[str, Any]]:
    return [row.asDict() for row in spark.sql(sql).collect()]  # collect() raises on error


@pytest.mark.parametrize("kind,name,dims", SWEEP_ITEMS, ids=SWEEP_IDS)
def test_measure_sweep(spark_session, vendor_model, kind: str, name: str, dims: list[str]) -> None:
    """Every measure and metric must execute on Spark (Databricks dialect)."""
    sql = compile_for(sweep_query(name, dims), vendor_model, "databricks")
    rows = _fetch(spark_session, sql)
    assert isinstance(rows, list), f"{kind} {name!r} returned no result set"


@pytest.mark.parametrize("case", COMMERCE_CASES, ids=lambda c: c.name)
def test_commerce_case(spark_session, vendor_model, truth_results, case: CommerceCase) -> None:
    """Compile for Databricks, execute on Spark, compare row-by-row to DuckDB-truth."""
    sql = compile_for(case.query, vendor_model, "databricks")
    actual = _fetch(spark_session, sql)
    compare_rows(actual, truth_results[case.name], case=case.name)
