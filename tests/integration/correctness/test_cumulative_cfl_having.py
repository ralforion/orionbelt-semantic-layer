"""Cumulative metrics beside another fact, and under HAVING.

A cumulative metric next to a measure from an independent fact compiles to a
CFL plan; each column must equal the one its fact gives alone. A HAVING on a
cumulative metric must keep exactly the rows of the unfiltered result that
pass it, both with the time dimension selected and as of one period.

The commerce model has no date dimension both Sales and Returns reach, so the
CFL cases add one over the shared Calendar: ``Calendar Month`` and a
year-to-date over it.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_COMMERCE_MODEL_YAML = (
    Path(__file__).resolve().parents[3] / "examples" / "orionbelt_1_commerce.yaml"
)

Rows = dict[tuple[str, ...], dict[str, Any]]

_CALENDAR_MONTH = """
  Calendar Month:
    dataObject: Calendar
    column: Date
    resultType: date
    timeGrain: month
"""
_CALENDAR_YTD = """
  Calendar YTD Sales:
    type: cumulative
    measure: Total Sales
    timeDimension: Calendar Month
    grainToDate: year
"""
_MARCH_APRIL_2022 = [
    QueryFilter(field="Calendar Month", op=FilterOperator.GTE, value="2022-03-01"),
    QueryFilter(field="Calendar Month", op=FilterOperator.LT, value="2022-05-01"),
]


@pytest.fixture(scope="module")
def run(commerce_db: duckdb.DuckDBPyConnection) -> Callable[..., Rows]:
    """Run a query on the commerce model plus a Calendar-based year-to-date.

    Rows are keyed by their dimension values. ``cfl`` asserts whether the plan
    is a multi-fact one, so a CFL case cannot silently compile to a star.
    """
    source = _COMMERCE_MODEL_YAML.read_text()
    source = source.replace("\ndimensions:\n", "\ndimensions:" + _CALENDAR_MONTH, 1)
    source = source.replace("\nmetrics:\n", "\nmetrics:" + _CALENDAR_YTD, 1)
    raw, source_map = TrackedLoader().load_string(source)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors

    def _run(query: QueryObject, *, cfl: bool = False) -> Rows:
        compiled = CompilationPipeline().compile(query, model, "duckdb")
        assert ("UNION ALL" in compiled.sql) is cfl
        cursor = commerce_db.execute(compiled.sql)
        columns = [c[0] for c in cursor.description]
        width = len(query.select.dimensions)
        return {
            tuple(str(v) for v in row[:width]): dict(zip(columns[width:], row[width:], strict=True))
            for row in cursor.fetchall()
        }

    return _run


def _query(
    dimensions: list[str],
    measures: list[str],
    *,
    where: list[QueryFilter] | None = None,
    having: list[QueryFilter] | None = None,
    as_of: _dt.date | None = None,
) -> QueryObject:
    return QueryObject(
        select=QuerySelect(dimensions=dimensions, measures=measures),
        where=where or [],
        having=having or [],
        asOf=as_of,
    )


def _merged(*parts: Rows) -> Rows:
    keys = set().union(*parts)
    return {k: {m: v for part in parts for m, v in part.get(k, {}).items()} for k in keys}


def test_beside_another_fact_by_period(run: Callable[..., Rows]) -> None:
    month = ["Calendar Month"]
    both = run(
        _query(month, ["Calendar YTD Sales", "Total Returns"], where=_MARCH_APRIL_2022), cfl=True
    )
    ytd = run(_query(month, ["Calendar YTD Sales"], where=_MARCH_APRIL_2022))
    returns = run(_query(month, ["Total Returns"], where=_MARCH_APRIL_2022))
    assert sorted(both) == [("2022-03-01",), ("2022-04-01",)]
    assert both == _merged(ytd, returns)


@pytest.mark.parametrize("time_dimension", ["Sales Month", "Calendar Month"])
def test_beside_another_fact_as_of_one_period(
    run: Callable[..., Rows], time_dimension: str
) -> None:
    """Over a time dimension only Sales reaches, and over the shared Calendar."""
    category = ["Product Category"]
    metric = "YTD Sales" if time_dimension == "Sales Month" else "Calendar YTD Sales"
    as_of = _dt.date(2022, 3, 18)
    both = run(_query(category, [metric, "Total Returns"], as_of=as_of), cfl=True)
    ytd = run(_query(category, [metric], as_of=as_of))
    returns = run(_query(category, ["Total Returns"]))
    assert len(both) == 10
    assert both == _merged(ytd, returns)


def test_having_on_a_cumulative_metric_by_period(run: Callable[..., Rows]) -> None:
    dims = ["Sales Region Name", "Sales Month"]
    where = [
        QueryFilter(field="Sales Month", op=FilterOperator.GTE, value="2022-03-01"),
        QueryFilter(field="Sales Month", op=FilterOperator.LT, value="2022-05-01"),
    ]
    above = [QueryFilter(field="YTD Sales", op=FilterOperator.GT, value=500000)]
    every = run(_query(dims, ["YTD Sales"], where=where))
    kept = run(_query(dims, ["YTD Sales"], where=where, having=above))
    assert kept == {k: v for k, v in every.items() if v["YTD Sales"] > 500000}
    assert 0 < len(kept) < len(every)


def test_having_on_a_cumulative_metric_as_of_one_period(run: Callable[..., Rows]) -> None:
    region = ["Sales Region Name"]
    as_of = _dt.date(2022, 3, 18)
    above = [QueryFilter(field="YTD Sales", op=FilterOperator.GT, value=400000)]
    every = run(_query(region, ["YTD Sales"], as_of=as_of))
    kept = run(_query(region, ["YTD Sales"], as_of=as_of, having=above))
    assert kept == {k: v for k, v in every.items() if v["YTD Sales"] > 400000}
    assert 0 < len(kept) < len(every)
