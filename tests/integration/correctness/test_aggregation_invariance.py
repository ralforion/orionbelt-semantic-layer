"""Tier 1 §3.1 — aggregation invariance.

A grouped aggregation summed back over its dim must equal the ungrouped
total. Both sides are produced by OBSL itself, but at different aggregation
grains — so a grain-leak bug (e.g. CFL legs double-counting, or a dimension
reaching the wrong fact) shows up as a mismatch.

Covers v0 corpus rows:
    1. ``Total Sales`` (no dims)              — vs ``Total Sales by Country``
    14. ``Total Sales`` filtered to 2025      — same invariance under WHERE
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
)

from .conftest import assert_decimal_equal


def _sum_decimal(rows: list[dict[str, Any]], col: str) -> Decimal:
    total = Decimal(0)
    for r in rows:
        v = r[col]
        if v is None:
            continue
        total += v if isinstance(v, Decimal) else Decimal(str(v))
    return total


def test_total_sales_equals_sum_by_country(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #1: ``Total Sales`` ungrouped == sum of grouped."""
    ungrouped = run_query(QueryObject(select=QuerySelect(measures=["Total Sales"])))
    by_country = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"],
                measures=["Total Sales"],
            )
        )
    )

    assert len(ungrouped) == 1, "ungrouped query should return exactly one row"
    assert len(by_country) > 1, "by-country query should return multiple rows"

    total = ungrouped[0]["Total Sales"]
    rolled = _sum_decimal(by_country, "Total Sales")

    assert_decimal_equal(
        total,
        rolled,
        msg=(
            f"Total Sales (ungrouped={total}) does not match the sum across "
            f"Sales Country Name (rolled={rolled}). "
            f"Suggests a grain-leak bug — likely the country dimension is "
            f"reaching Sales via a join path that multiplies rows, or some "
            f"sales rows are dropped because the country join is INNER."
        ),
    )


def test_total_sales_invariance_under_year_filter(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #14: ``Total Sales`` ungrouped under filter == sum of grouped under same filter.

    Uses the calendar-year filter on ``Sales Year`` (which compiles to a
    ``DATE_TRUNC('year', salesdate)`` predicate). A WHERE-clause routing bug
    in the planner — applying the filter to the wrong CFL leg or to the
    wrong table alias — would surface as a mismatch here.
    """
    where = [
        QueryFilter(
            field="Sales Year",
            op=FilterOperator.EQ,
            value="2025-01-01",
        )
    ]

    ungrouped = run_query(QueryObject(select=QuerySelect(measures=["Total Sales"]), where=where))
    by_country = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"],
                measures=["Total Sales"],
            ),
            where=where,
        )
    )

    assert len(ungrouped) == 1
    total = ungrouped[0]["Total Sales"]
    rolled = _sum_decimal(by_country, "Total Sales")

    # If 2025 has no data in the seed both sides will be NULL/zero — that
    # still satisfies invariance (0 == 0) but is a weak test, so flag it.
    if total in (None, Decimal(0)) and rolled == Decimal(0):
        import pytest

        pytest.skip("Seed has no Sales rows for 2025 — invariance is trivially satisfied.")

    assert_decimal_equal(
        total,
        rolled,
        msg=(
            f"Filtered Total Sales (ungrouped={total}) does not match sum "
            f"across Sales Country Name (rolled={rolled}) for Sales Year=2025. "
            f"Suggests a WHERE-routing bug — the filter is applied to one "
            f"side but not the other."
        ),
    )
