"""The semi-additive recipe from the model-format guide (DuckDB, commerce).

A closing balance is a ``last`` measure reaggregated with ``sum`` per member,
an opening balance the same with ``first``, and an average daily total a
``sum`` measure reaggregated with ``avg`` per day. The commerce sales stand in
for snapshots: a client's last sale amount in the period is its "balance".
The references are written straight against the tables, ties going to the
greater value for ``last`` and the lesser for ``first``, as the measures do.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

_KEY = {"column": {"dataObject": "Sales", "column": "Sales Date"}}
_AMOUNT = [{"dataObject": "Sales", "column": "Sales Amount"}]

_FROM = """
FROM orionbelt_1.sales s
JOIN orionbelt_1.clients cl ON s.salesclient = cl.clientid
JOIN orionbelt_1.countries co ON cl.clientcountryid = co.countryid
WHERE s.salesamount IS NOT NULL AND s.salesdate IS NOT NULL
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["measures"]["Last Sale"] = {"columns": _AMOUNT, "aggregation": "last", "withinGroup": _KEY}
    raw["measures"]["First Sale"] = {
        "columns": _AMOUNT,
        "aggregation": "first",
        "withinGroup": _KEY,
    }
    per_client = {"type": "reaggregate", "per": ["Client Name"], "aggregation": "sum"}
    raw["metrics"]["Closing Total"] = {**per_client, "measure": "Last Sale"}
    raw["metrics"]["Opening Total"] = {**per_client, "measure": "First Sale"}
    raw["metrics"]["Avg Daily Sales"] = {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Date:day"],
        "aggregation": "avg",
    }
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture(scope="module")
def run(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection
) -> Callable[..., list[dict[str, Any]]]:
    pipeline = CompilationPipeline()

    def _run(dimensions: list[str], measures: list[str]) -> list[dict[str, Any]]:
        q = QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures))
        return _rows_as_dicts(commerce_db, pipeline.compile(q, model, "duckdb").sql)

    return _run


def _reference(db: duckdb.DuckDBPyConnection, by_year: bool) -> dict[Any, tuple[Any, Any]]:
    """(closing, opening) per country, or per country and year: each client's
    last / first amount in the group, summed over the clients."""
    year, year_out = (", year(s.salesdate) AS yr", ", yr") if by_year else ("", "")
    sql = f"""
    SELECT country{year_out}, SUM(last_amount), SUM(first_amount)
    FROM (
        SELECT co.countryname AS country{year},
               cl.clientid,
               arg_max(s.salesamount, (s.salesdate, s.salesamount)) AS last_amount,
               arg_min(s.salesamount, (s.salesdate, s.salesamount)) AS first_amount
        {_FROM}
        GROUP BY ALL
    )
    GROUP BY ALL
    """
    out: dict[Any, tuple[Any, Any]] = {}
    for row in db.sql(sql).fetchall():
        *key, closing, opening = row
        out[tuple(key)] = (closing, opening)
    return out


def _close(got: Any, want: Any) -> bool:
    return abs(Decimal(str(got)) - Decimal(str(want))) < Decimal("0.01")


@pytest.mark.parametrize("by_year", [False, True], ids=["by country", "by country and year"])
def test_closing_and_opening_balances(
    run: Callable, commerce_db: duckdb.DuckDBPyConnection, by_year: bool
) -> None:
    want = _reference(commerce_db, by_year)
    dims = ["Country Name", "Sales Year"] if by_year else ["Country Name"]
    rows = run(dims, ["Closing Total", "Opening Total"])
    assert len(rows) == len(want)
    for r in rows:
        key = (r["Country Name"], r["Sales Year"].year) if by_year else (r["Country Name"],)
        closing, opening = want[key]
        assert _close(r["Closing Total"], closing), r
        assert _close(r["Opening Total"], opening), r


def test_average_daily_total(run: Callable, commerce_db: duckdb.DuckDBPyConnection) -> None:
    """The other common form: per country, the average over the days with
    sales of each day's total."""
    want = dict(
        commerce_db.sql(
            f"""
            SELECT country, AVG(day_total) FROM (
                SELECT co.countryname AS country, s.salesdate, SUM(s.salesamount) AS day_total
                {_FROM}
                GROUP BY ALL
            ) GROUP BY ALL
            """
        ).fetchall()
    )
    rows = run(["Country Name"], ["Avg Daily Sales"])
    assert len(rows) == len(want)
    for r in rows:
        assert _close(r["Avg Daily Sales"], want[r["Country Name"]]), r
