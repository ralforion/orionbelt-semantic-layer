"""Tier 1 §3.7 — filter additivity.

Over disjoint partition values, ``WHERE x IN (a, b)`` must equal
``WHERE x = a`` + ``WHERE x = b``. Catches WHERE-clause routing bugs:
filters applied to the wrong CFL leg, dropped during sub-query rewrite,
or coerced to the wrong type.

Covers v0 corpus row #12: ``Total Sales`` filtered to two countries.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
)


def _to_decimal(v: Any) -> Decimal:
    if v is None:
        return Decimal(0)
    return v if isinstance(v, Decimal) else Decimal(str(v))


# Two countries known to exist in the bundled DuckDB seed (alphabetical
# slice of the 25-country list). Picked for stability — both have sales.
_COUNTRY_A = "Croatia"
_COUNTRY_B = "Austria"


def test_country_in_filter_additivity(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
) -> None:
    """``Total Sales WHERE country IN (A, B)`` == ``WHERE country=A`` + ``WHERE country=B``."""

    def _run_one(op: FilterOperator, value: Any) -> Decimal:
        rows = run_query(
            QueryObject(
                select=QuerySelect(measures=["Total Sales"]),
                where=[QueryFilter(field="Sales Country Name", op=op, value=value)],
            )
        )
        assert len(rows) == 1, f"Expected one row; got {len(rows)} for {op} {value}"
        return _to_decimal(rows[0]["Total Sales"])

    a_only = _run_one(FilterOperator.EQ, _COUNTRY_A)
    b_only = _run_one(FilterOperator.EQ, _COUNTRY_B)
    both = _run_one(FilterOperator.IN, [_COUNTRY_A, _COUNTRY_B])

    if a_only == 0 and b_only == 0:
        pytest.skip(
            f"Neither {_COUNTRY_A!r} nor {_COUNTRY_B!r} have sales in the seed; "
            "additivity is trivially satisfied (0 == 0)."
        )

    assert both == a_only + b_only, (
        f"Filter additivity failed: IN({_COUNTRY_A}, {_COUNTRY_B})={both}, "
        f"EQ({_COUNTRY_A})={a_only}, EQ({_COUNTRY_B})={b_only}, "
        f"sum={a_only + b_only}, diff={both - (a_only + b_only)}. "
        f"Suggests the IN-list is being parsed as a single string literal, "
        f"or the filter is applied at the wrong CTE level."
    )
