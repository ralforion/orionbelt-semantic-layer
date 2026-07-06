"""Tier 1 §3.5 — metric algebra.

Where a metric is defined as algebra over base measures, the test recomputes
the algebra independently. A bug in expression evaluation (sqlglot rewrite,
NULLIF handling, decimal vs float casting, operator precedence) shows up
as a mismatch between the OBSL-emitted metric and the manual algebra.

Per plan §10.1, ratios may use ``pytest.approx`` (these all are ratios).

Covers v0 corpus rows:
    8. ``Average Sale``  = Total Sales / NULLIF(Sales Count, 0)
    9. ``Return Rate``   = Total Returns / NULLIF(Total Sales, 0)
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pytest

from orionbelt.models.query import QueryObject, QuerySelect


def _to_decimal(v: Any) -> Decimal:
    if v is None:
        raise AssertionError("Expected a non-null value, got None")
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _quantize(value: Decimal, scale: int) -> Decimal:
    """Round to ``scale`` decimal places — matches OBSL's CAST to decimal(p, s).

    OBSL emits ``CAST(... AS DECIMAL(p, s))`` for declared metric dataTypes.
    DuckDB's CAST uses banker's rounding (ROUND_HALF_EVEN), so we mirror it.
    """
    quant = Decimal(10) ** -scale
    return value.quantize(quant, rounding=ROUND_HALF_EVEN)


def test_average_sale_equals_total_sales_over_order_count(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #8: ``Average Sale`` == ``Total Sales`` / ``Sales Count``."""
    base = run_query(QueryObject(select=QuerySelect(measures=["Total Sales", "Sales Count"])))
    metric = run_query(QueryObject(select=QuerySelect(measures=["Average Sale"])))

    assert len(base) == 1 and len(metric) == 1

    total_sales = _to_decimal(base[0]["Total Sales"])
    order_count = _to_decimal(base[0]["Sales Count"])
    # Average Sale is declared decimal(18, 2) — quantize to 2 dp to match
    # OBSL's CAST. Without this, the Decimal division retains 30+ digits
    # of precision and disagrees with OBSL's truncated value.
    expected = _quantize(total_sales / order_count, scale=2)

    actual = _to_decimal(metric[0]["Average Sale"])
    assert actual == expected, (
        f"Average Sale: OBSL={actual}, manual={expected} "
        f"(Total Sales={total_sales}, Sales Count={order_count}). "
        f"Suggests an expression-evaluation bug — possibly sqlglot rewrite "
        f"of `/` or NULLIF, or a wrong cast of Sales Count."
    )


def test_return_rate_equals_returns_over_sales(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """Corpus #9: ``Return Rate`` == ``Total Returns`` / ``Total Sales``."""
    base = run_query(QueryObject(select=QuerySelect(measures=["Total Sales", "Total Returns"])))
    metric = run_query(QueryObject(select=QuerySelect(measures=["Return Rate"])))

    assert len(base) == 1 and len(metric) == 1

    total_sales = _to_decimal(base[0]["Total Sales"])
    total_returns = _to_decimal(base[0]["Total Returns"])
    if total_sales == 0:
        pytest.skip("Total Sales == 0 in seed; ratio is undefined.")
    # Return Rate is declared decimal(18, 4) — quantize to 4 dp.
    expected = _quantize(total_returns / total_sales, scale=4)

    actual = _to_decimal(metric[0]["Return Rate"])
    assert actual == expected, (
        f"Return Rate: OBSL={actual}, manual={expected} "
        f"(Total Returns={total_returns}, Total Sales={total_sales}). "
        f"Suggests an expression-evaluation bug or a wrong CFL leg."
    )
