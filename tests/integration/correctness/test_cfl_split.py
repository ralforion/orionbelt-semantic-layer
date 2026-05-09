"""Tier 1 §3.6 — CFL split.

A cross-fact query (CFL — measures from independent fact tables) must
produce, per dimension key, the same per-measure values as the equivalent
single-fact queries. Catches CFL leg corruption: a wrong UNION ALL leg,
a dropped measure column on one side, or a NULL-padding mistake.

Covers v0 corpus rows:
    4. ``Total Sales`` + ``Total Returns`` (CFL with shared dim path)
    5. ``Total Sales`` + ``Total Purchases`` (CFL, no shared dim — ungrouped)
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from orionbelt.models.query import QueryObject, QuerySelect


def _to_decimal(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    return v if isinstance(v, Decimal) else Decimal(str(v))


def test_sales_returns_cfl_split_by_country(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #4: ``Total Sales`` + ``Total Returns`` by ``Sales Country Name``.

    Each measure in the combined CFL output must equal the single-measure
    query for the same dimension grain. A divergence means one CFL leg has
    a bad join, a wrong filter scope, or a NULL-padded column drifted.
    """
    combined = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"],
                measures=["Total Sales", "Total Returns"],
            )
        )
    )
    sales_only = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"],
                measures=["Total Sales"],
            )
        )
    )
    returns_only = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"],
                measures=["Total Returns"],
            )
        )
    )

    sales_by_country = {r["Sales Country Name"]: _to_decimal(r["Total Sales"]) for r in sales_only}
    returns_by_country = {
        r["Sales Country Name"]: _to_decimal(r["Total Returns"]) for r in returns_only
    }
    combined_by_country = {r["Sales Country Name"]: r for r in combined}

    # Combined output must cover at least every country that has either
    # sales or returns. CFL's UNION ALL preserves both sides — a missing
    # row indicates a leg dropped a country entirely.
    union_keys = set(sales_by_country) | set(returns_by_country)
    assert set(combined_by_country) == union_keys, (
        f"Country sets differ: only-in-combined="
        f"{set(combined_by_country) - union_keys}, "
        f"missing-from-combined={union_keys - set(combined_by_country)}"
    )

    for country, row in combined_by_country.items():
        expected_sales = sales_by_country.get(country, Decimal(0))
        expected_returns = returns_by_country.get(country, Decimal(0))
        actual_sales = _to_decimal(row["Total Sales"])
        actual_returns = _to_decimal(row["Total Returns"])

        assert actual_sales == expected_sales, (
            f"Country {country!r} Total Sales: combined={actual_sales}, "
            f"single={expected_sales}. The Sales leg of the CFL UNION ALL "
            f"appears to have a different filter scope or join cardinality."
        )
        assert actual_returns == expected_returns, (
            f"Country {country!r} Total Returns: combined={actual_returns}, "
            f"single={expected_returns}. The Returns leg appears to have a "
            f"different filter scope or join cardinality."
        )


def test_sales_purchases_cfl_split_ungrouped(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #5: ``Total Sales`` + ``Total Purchases`` (CFL, no shared dim, ungrouped).

    Sales and Purchases share no dim path — the CFL planner unions them as
    independent legs with NULL padding. The combined output should be a
    single row whose values match the per-measure single-fact totals.
    """
    combined = run_query(
        QueryObject(select=QuerySelect(measures=["Total Sales", "Total Purchases"]))
    )
    sales_only = run_query(QueryObject(select=QuerySelect(measures=["Total Sales"])))
    purchases_only = run_query(QueryObject(select=QuerySelect(measures=["Total Purchases"])))

    assert len(combined) == 1, (
        f"Ungrouped CFL should return one row; got {len(combined)}. "
        f"Possibly a NULL-padding bug producing one row per leg."
    )
    assert len(sales_only) == 1
    assert len(purchases_only) == 1

    expected_sales = _to_decimal(sales_only[0]["Total Sales"])
    expected_purchases = _to_decimal(purchases_only[0]["Total Purchases"])
    actual_sales = _to_decimal(combined[0]["Total Sales"])
    actual_purchases = _to_decimal(combined[0]["Total Purchases"])

    assert actual_sales == expected_sales, (
        f"Total Sales: combined CFL={actual_sales}, single={expected_sales}. "
        f"The Sales leg may be NULL where it shouldn't be, or summed across "
        f"the wrong rows."
    )
    assert actual_purchases == expected_purchases, (
        f"Total Purchases: combined CFL={actual_purchases}, "
        f"single={expected_purchases}. Same diagnosis as above for Purchases."
    )
