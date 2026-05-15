"""Integration tests: compile + execute the commerce battery on real BigQuery.

The full ``COMMERCE_CASES`` battery defined in
``tests/integration/_commerce.py`` runs against a live BigQuery dataset.
DuckDB executes the same queries against the same parquet fixtures and acts
as the source of truth — any row-level disagreement is a BigQuery dialect
bug.

Opt-in — requires live credentials::

    uv run pytest -m bigquery

Required env vars (skipped if missing):

    BIGQUERY_PROJECT      GCP project ID
    BIGQUERY_DATASET      dataset name (default: orionbelt_test)
    BIGQUERY_LOCATION     dataset location (default: US)
    GOOGLE_APPLICATION_CREDENTIALS  service-account JSON (ADC also OK)

Fixture lifecycle: the module-scoped fixture creates the dataset if absent,
then for each commerce parquet fixture, loads it into a BigQuery table via
``client.load_table_from_file()`` (Parquet auto-detect) *only if* the table
is missing or its row count differs from the parquet. Tables are kept after
the run to save load-job compute on the next invocation. Set
``BIGQUERY_RESEED=1`` to force a reload regardless of current state.
"""

from __future__ import annotations

import os

import pytest

bigquery = pytest.importorskip("google.cloud.bigquery", reason="google-cloud-bigquery required")
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

pytestmark = pytest.mark.bigquery


def _required_env() -> dict[str, str] | None:
    project = os.environ.get("BIGQUERY_PROJECT")
    if not project:
        return None
    return {
        "project": project,
        "dataset": os.environ.get("BIGQUERY_DATASET", "orionbelt_test"),
        "location": os.environ.get("BIGQUERY_LOCATION", "US"),
    }


def _bq_rowcount(client, table_ref: str) -> int | None:
    """Return row count for ``table_ref`` via ``client.get_table``, or None if missing."""
    try:
        return int(client.get_table(table_ref).num_rows)
    except Exception:  # noqa: BLE001
        return None


def _parquet_rowcount(table: str) -> int:
    return pq.ParquetFile(parquet_path(table)).metadata.num_rows


def _load_parquet_to_table(client, project: str, dataset: str, table: str) -> None:
    """Bulk-load one parquet fixture into a BigQuery table (replace mode)."""
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        # The commerce model expects uppercase physical table names per the
        # OBML ``code:`` field — but the YAML ships lowercase codes so the
        # demo DuckDB matches. We load with the exact case from the model.
    )
    table_ref = f"{project}.{dataset}.{table}"
    with parquet_path(table).open("rb") as fh:
        job = client.load_table_from_file(fh, table_ref, job_config=job_config)
    job.result()  # wait for completion; raises on error


@pytest.fixture(scope="module")
def bigquery_setup():
    """Open client, create dataset + load all commerce parquet tables, tear down on exit."""
    cfg = _required_env()
    if cfg is None:
        pytest.skip("BIGQUERY_PROJECT env var not set — skipping live BigQuery integration tests")

    try:
        client = bigquery.Client(project=cfg["project"], location=cfg["location"])
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Could not initialise BigQuery client: {e}")

    dataset = bigquery.Dataset(f"{cfg['project']}.{cfg['dataset']}")
    dataset.location = cfg["location"]
    client.create_dataset(dataset, exists_ok=True)

    reseed = os.environ.get("BIGQUERY_RESEED", "").lower() in ("1", "true", "yes")
    try:
        for table in COMMERCE_TABLES:
            table_ref = f"{cfg['project']}.{cfg['dataset']}.{table}"
            expected = _parquet_rowcount(table)
            actual = None if reseed else _bq_rowcount(client, table_ref)
            if actual == expected:
                continue
            _load_parquet_to_table(client, cfg["project"], cfg["dataset"], table)
    except Exception:
        client.close()
        raise

    yield client, cfg

    client.close()


@pytest.fixture(scope="module")
def vendor_model(bigquery_setup):
    _client, cfg = bigquery_setup
    return load_commerce_model(database=cfg["project"], schema=cfg["dataset"])


@pytest.fixture(scope="module")
def truth_model():
    return load_commerce_model(database="main", schema="orionbelt_1")


@pytest.fixture(scope="module")
def truth_results(truth_model):
    """Pre-compute expected rows for every battery case once per session."""
    con = open_duckdb_truth(schema="orionbelt_1")
    try:
        return {
            case.name: fetch_duckdb(con, compile_for(case.query, truth_model, "duckdb"))
            for case in COMMERCE_CASES
        }
    finally:
        con.close()


def _fetch_bigquery(client, sql: str) -> list[dict]:
    rows = client.query(sql).result()
    return [dict(row.items()) for row in rows]


@pytest.mark.parametrize("case", COMMERCE_CASES, ids=lambda c: c.name)
def test_commerce_case(bigquery_setup, vendor_model, truth_results, case: CommerceCase) -> None:
    """Compile for BigQuery, execute, compare row-by-row to DuckDB-truth."""
    client, _cfg = bigquery_setup
    sql = compile_for(case.query, vendor_model, "bigquery")
    actual = _fetch_bigquery(client, sql)
    compare_rows(actual, truth_results[case.name], case=case.name)
