"""A rolling window counts calendar periods, not rows.

``Rolling 30 Day Sales`` for a day is the average of the daily sales of that day
and the 29 before it, and ``Peak Daily Sales 30D`` their maximum. The seed has
21 days without sales, and over rows a window reached past each gap to a day
outside the 30. The expected values here come from a different path: the plain
daily ``Total Sales``, windowed in Python.

Covers corpus row 19.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect

_DAYS = 30
_FEBRUARY = [
    QueryFilter(field="Sales Date", op=FilterOperator.GTE, value="2021-02-01"),
    QueryFilter(field="Sales Date", op=FilterOperator.LT, value="2021-03-01"),
]


def _by_day(rows: list[dict[str, Any]]) -> dict[dt.date, dict[str, Any]]:
    return {r["Sales Date"]: r for r in rows}


def test_rolling_windows_read_the_calendar_days(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    select = QuerySelect(
        dimensions=["Sales Date"],
        measures=["Total Sales", "Rolling 30 Day Sales", "Peak Daily Sales 30D"],
    )
    days = _by_day(run_query(QueryObject(select=select)))
    calendar = (max(days) - min(days)).days + 1
    assert calendar - len(days) == 21, "the seed's days without sales"

    for day, row in days.items():
        window = [
            days[day - dt.timedelta(back)]["Total Sales"]
            for back in range(_DAYS)
            if day - dt.timedelta(back) in days
        ]
        average = row["Rolling 30 Day Sales"]
        scale = Decimal(1).scaleb(average.as_tuple().exponent)
        expected = (sum(window) / len(window)).quantize(scale, rounding=ROUND_HALF_UP)
        assert average == expected, day
        assert row["Peak Daily Sales 30D"] == max(window), day


def test_rolling_windows_under_a_time_filter(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #19. February has a day without sales, and its windows read January."""
    select = QuerySelect(
        dimensions=["Sales Date"],
        measures=["Total Sales", "Rolling 30 Day Sales", "Peak Daily Sales 30D"],
    )
    filtered = _by_day(run_query(QueryObject(select=select, where=_FEBRUARY)))
    unfiltered = _by_day(run_query(QueryObject(select=select)))

    assert dt.date(2021, 2, 20) not in filtered
    assert len(filtered) == 27
    for day, row in filtered.items():
        assert row == unfiltered[day], day
