"""Commerce-data integration tests: DuckDB executes the full COMMERCE_CASES
battery against the parquet fixtures.

DuckDB is the **truth** every other vendor's results are diffed against. This
file's job is to make sure the truth itself doesn't silently regress: every
case must compile, execute, return rows, and (where the answer is mechanical)
match a frozen expectation derived from the parquet sample.

The other vendor tests (test_<vendor>_commerce_execution.py / the rewritten
test_<vendor>_execution.py files) reuse the same battery via
``tests/integration/_commerce.py`` and compare their rows to whatever this
DuckDB run produces.
"""

from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for execution tests")

from tests.integration._commerce import (  # noqa: E402
    COMMERCE_CASES,
    CommerceCase,
    compile_for,
    fetch_duckdb,
    load_commerce_model,
    open_duckdb_truth,
)


@pytest.fixture(scope="module")
def truth_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the parquet fixtures attached as views."""
    return open_duckdb_truth(schema="orionbelt_1")


@pytest.fixture(scope="module")
def model():
    """Commerce semantic model resolved against the orionbelt_1 schema."""
    return load_commerce_model(database="main", schema="orionbelt_1")


@pytest.mark.parametrize("case", COMMERCE_CASES, ids=lambda c: c.name)
def test_commerce_case_executes(truth_conn, model, case: CommerceCase) -> None:
    """Every battery case compiles + executes against DuckDB and returns rows."""
    sql = compile_for(case.query, model, "duckdb")
    rows = fetch_duckdb(truth_conn, sql)
    assert rows, f"{case.name}: expected at least one row, got 0"

    # Column schema sanity: every dim + measure name from the query appears
    # as a column in the result (the compiled SQL uses these as SELECT aliases).
    cols = set(rows[0].keys())
    for dim in case.query.select.dimensions:
        assert dim in cols, f"{case.name}: dimension {dim!r} missing from result columns"
    for measure in case.query.select.measures:
        assert measure in cols, f"{case.name}: measure {measure!r} missing from result columns"
