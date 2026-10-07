"""A cumulative metric without its time dimension is evaluated as of one period.

The value per group is the one the row for that period shows when the query
selects the time dimension: a year-to-date tile reads the last row of the
by-month table. Both sides are produced by OBSL; the by-period side is the
look-back and calendar-window behaviour the earlier corpus rows pin.

Covers corpus rows 20 (an explicit ``asOf``) and 21 (as of the latest period
under a time filter).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from typing import Any

from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect

_REGION = "Sales Region Name"
_BY_MONTH = ["YTD Sales", "Cumulative Sales"]
_BY_DAY = ["MTD Sales", "Rolling 30 Day Sales", "Peak Daily Sales 30D"]


def _rows_at(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
    time_dim: str,
    metrics: list[str],
    period: str,
    where: list[QueryFilter] | None = None,
) -> dict[str, dict[str, Any]]:
    """Each region's row for *period* of the query that selects *time_dim*."""
    rows = run_query(
        QueryObject(
            select=QuerySelect(dimensions=[_REGION, time_dim], measures=metrics),
            where=where or [],
        )
    )
    return {r[_REGION]: {m: r[m] for m in metrics} for r in rows if str(r[time_dim])[:10] == period}


def _as_of(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
    metrics: list[str],
    as_of: _dt.date | None = None,
    where: list[QueryFilter] | None = None,
) -> dict[str, dict[str, Any]]:
    rows = run_query(
        QueryObject(
            select=QuerySelect(dimensions=[_REGION], measures=metrics),
            where=where or [],
            asOf=as_of,
        )
    )
    return {r[_REGION]: {m: r[m] for m in metrics} for r in rows}


def test_as_of_a_date_equals_the_row_for_its_period(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #20. 2021-03-18 is March for the monthly metrics, that day for the daily ones."""
    as_of = _dt.date(2021, 3, 18)
    monthly = _rows_at(run_query, "Sales Month", _BY_MONTH, "2021-03-01")
    daily = _rows_at(run_query, "Sales Date", _BY_DAY, "2021-03-18")
    assert monthly and daily

    assert _as_of(run_query, _BY_MONTH, as_of) == monthly
    tile = _as_of(run_query, _BY_DAY, as_of)
    # A region without a sale on the day has no row there, but its month-to-date
    # and 30-day window still read the days before it.
    for region, row in daily.items():
        assert tile[region] == row, region


def test_as_of_a_day_without_sales_reads_the_days_before_it(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Nothing sold on 2021-03-17, so month-to-date is the 16th's."""
    (sixteenth,) = [
        r["MTD Sales"]
        for r in run_query(
            QueryObject(select=QuerySelect(dimensions=["Sales Date"], measures=["MTD Sales"]))
        )
        if str(r["Sales Date"]) == "2021-03-16"
    ]
    (tile,) = run_query(
        QueryObject(select=QuerySelect(measures=["MTD Sales"]), asOf=_dt.date(2021, 3, 17))
    )
    assert tile["MTD Sales"] == sixteenth


def test_without_as_of_the_latest_period_with_data(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
    commerce_db: Any,
) -> None:
    (latest,) = commerce_db.execute(
        "SELECT date_trunc('month', MAX(salesdate)) FROM orionbelt_1.sales"
    ).fetchone()
    monthly = _rows_at(run_query, "Sales Month", _BY_MONTH, str(latest)[:10])
    assert _as_of(run_query, _BY_MONTH) == monthly


def test_a_time_filter_picks_the_period_not_the_history(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #21. Filtered to before July 2021, year-to-date is June's, read from January."""
    first_half = [QueryFilter(field="Sales Month", op=FilterOperator.LT, value="2021-07-01")]
    june = _rows_at(run_query, "Sales Month", _BY_MONTH, "2021-06-01")
    assert _as_of(run_query, _BY_MONTH, where=first_half) == june
