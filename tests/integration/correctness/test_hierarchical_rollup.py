"""Tier 1 §3.2 — hierarchical rollup.

Coarser grain = sum of finer grain. This catches time-grain composition
bugs in the compiler (e.g. ``Sales Year`` and ``Sales Month`` resolving to
different ``DATE_TRUNC`` semantics, or a CFL leg dropping rows for one
grain but not the other).

Covers v0 corpus row #3: ``Total Sales by Sales Year`` vs ``Total Sales by
Sales Year + Sales Month``.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from orionbelt.models.query import QueryObject, QuerySelect

from .conftest import assert_decimal_equal


def test_year_equals_sum_of_year_month(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #3: ``Total Sales`` by Sales Year == Σ by (Sales Year, Sales Month)."""
    by_year = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Year"],
                measures=["Total Sales"],
            )
        )
    )
    by_year_month = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Year", "Sales Month"],
                measures=["Total Sales"],
            )
        )
    )

    assert by_year, "by-year query returned no rows"
    assert by_year_month, "by-year-month query returned no rows"

    # Group the finer-grain rows by year. ``Sales Year`` is a date (truncated
    # to year) so its values match exactly between both queries.
    fine_by_year: dict[Any, Decimal] = {}
    for r in by_year_month:
        key = r["Sales Year"]
        v = r["Total Sales"]
        if v is None:
            continue
        d = v if isinstance(v, Decimal) else Decimal(str(v))
        fine_by_year[key] = fine_by_year.get(key, Decimal(0)) + d

    coarse_by_year = {
        r["Sales Year"]: (
            r["Total Sales"]
            if isinstance(r["Total Sales"], Decimal)
            else Decimal(str(r["Total Sales"]))
        )
        for r in by_year
        if r["Total Sales"] is not None
    }

    # Same set of years on both sides — otherwise the planner is dropping
    # rows at one grain.
    assert set(coarse_by_year) == set(fine_by_year), (
        f"Year sets differ between grains: "
        f"only-in-year={set(coarse_by_year) - set(fine_by_year)}, "
        f"only-in-year-month={set(fine_by_year) - set(coarse_by_year)}"
    )

    for year, coarse in coarse_by_year.items():
        fine = fine_by_year[year]
        assert_decimal_equal(
            coarse,
            fine,
            msg=(
                f"Year {year}: by-year total ({coarse}) does not equal sum "
                f"across months ({fine}). Difference={coarse - fine}. "
                f"Suggests a time-grain composition bug — likely DATE_TRUNC "
                f"is being applied differently for year vs month, or a row "
                f"is being assigned to the wrong month."
            ),
        )
