"""A time filter picks the periods shown; it does not cut what they read.

Year-to-date for March reads January and February, and a month-over-month
change for March reads February, whether or not the query shows them. So a
query filtered to March-April returns exactly the March and April rows of the
same query without the filter. Both sides are produced by OBSL; the unfiltered
side has no filter to get wrong.

Covers corpus rows 17 (cumulative) and 18 (period-over-period).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect

_WINDOW = [
    QueryFilter(field="Sales Month", op=FilterOperator.GTE, value="2021-03-01"),
    QueryFilter(field="Sales Month", op=FilterOperator.LT, value="2021-05-01"),
]


def _by_month(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r["Sales Month"])[:10]: r for r in rows}


def _assert_filtered_rows_equal_unfiltered(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
    metric: str,
    march: Decimal,
) -> None:
    select = QuerySelect(dimensions=["Sales Month"], measures=["Total Sales", metric])
    filtered = _by_month(run_query(QueryObject(select=select, where=_WINDOW)))
    unfiltered = _by_month(run_query(QueryObject(select=select)))

    assert sorted(filtered) == ["2021-03-01", "2021-04-01"]
    for month, row in filtered.items():
        assert row == unfiltered[month], month
    assert filtered["2021-03-01"][metric] == march


def test_ytd_sales_under_a_time_filter(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #17. March read 302,261.62 before: YTD started at the filter."""
    _assert_filtered_rows_equal_unfiltered(run_query, "YTD Sales", Decimal("1022556.94"))


def test_mom_change_under_a_time_filter(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #18. March read NULL before: there was no February to compare with."""
    _assert_filtered_rows_equal_unfiltered(run_query, "Sales MoM Change", Decimal("-136811.32"))
