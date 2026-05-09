"""Tier 1 §3.4 — pandas baseline.

For queries whose semantics are awkward to express as a single hand-SQL
file (cumulative window, rolling average, period-over-period lag join),
the reference computation is built in pandas — sharing no code with the
OBSL compiler — and then compared to OBSL's compiled output.

Per plan §10.1, the comparison rule is:
  * Sums use ``Decimal`` exact equality.
  * Ratios / averages may use ``pytest.approx``.

Important precision note: ``Sales.salesamount`` in the bundled DuckDB
seed is stored as DOUBLE. OBSL's cumulative wrapper emits an unwrapped
``SUM(salesamount)`` inside the CTE (no ``CAST`` to the declared
``decimal(18, 2)`` dataType), so the running sum carries last-bit float
noise. The baseline below mirrors that arithmetic, so the cross-check is
still meaningful for join-path / grain / window-definition bugs but does
not assert the declared ``dataType`` precision. A precision-hardening
check is a separate concern (see plan §10.4).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")
pd = pytest.importorskip("pandas", reason="pandas required for §3.4 baseline tests")

from orionbelt.models.query import QueryObject, QuerySelect  # noqa: E402

# Tolerances for float comparisons. ``rel=1e-9`` accommodates last-bit
# float noise from accumulated SUM/AVG; anything larger would mask a real
# arithmetic bug.
_FLOAT_REL = 1e-9
_FLOAT_ABS = 1e-6


def _to_date(v: Any) -> _dt.date:
    """Coerce ``date`` / ``datetime`` / pandas Timestamp to plain ``date``.

    OBSL returns ``date_trunc`` columns as ``datetime.date`` (DuckDB's
    DATE) or ``datetime.datetime`` (TIMESTAMP) depending on context.
    Pandas' ``date_range`` produces ``Timestamp``. Normalize both sides
    so dict-key comparisons are stable.
    """
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    # pandas Timestamp inherits from datetime.datetime, so the above
    # branch handles it; this catches any numpy datetime64 leftovers.
    return pd.Timestamp(v).date()


# ---------------------------------------------------------------------------
# Corpus #10 — Cumulative Sales by Sales Month
# ---------------------------------------------------------------------------


def test_cumulative_sales_by_month(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
    commerce_db: duckdb.DuckDBPyConnection,
) -> None:
    """OBSL ``Cumulative Sales`` == running sum of monthly Total Sales."""
    base = commerce_db.execute(
        """
        SELECT date_trunc('month', salesdate) AS month,
               SUM(salesamount) AS total
        FROM orionbelt_1.sales
        GROUP BY date_trunc('month', salesdate)
        ORDER BY date_trunc('month', salesdate)
        """
    ).fetchdf()

    base["expected"] = base["total"].cumsum()
    expected = {_to_date(row.month): row.expected for row in base.itertuples(index=False)}

    obsl_rows = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Month"],
                measures=["Cumulative Sales"],
            )
        )
    )
    obsl = {_to_date(r["Sales Month"]): r["Cumulative Sales"] for r in obsl_rows}

    assert set(obsl) == set(expected), (
        f"Month sets differ: only-in-OBSL={set(obsl) - set(expected)}, "
        f"only-in-baseline={set(expected) - set(obsl)}"
    )
    for month in sorted(expected):
        assert float(obsl[month]) == pytest.approx(
            float(expected[month]), rel=_FLOAT_REL, abs=_FLOAT_ABS
        ), (
            f"Cumulative Sales for {month}: OBSL={obsl[month]}, "
            f"pandas baseline={expected[month]}. A mismatch here suggests a "
            f"window-definition bug (wrong frame), wrong order, or that "
            f"some monthly rows are missing from the cumulative_base CTE."
        )


# ---------------------------------------------------------------------------
# Corpus #11 — Rolling 30 Day Sales by Sales Date
# ---------------------------------------------------------------------------


def test_rolling_30_day_sales(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
    commerce_db: duckdb.DuckDBPyConnection,
) -> None:
    """OBSL ``Rolling 30 Day Sales`` == 30-row rolling AVG of daily totals.

    Note: OBSL emits ``ROWS BETWEEN 29 PRECEDING AND CURRENT ROW`` — a
    *row*-based window, not a 30-day calendar window. So the baseline must
    also use ``rolling(30)`` over the densely-grouped daily rows (one row
    per day-with-sales), not over a calendar spine.
    """
    base = commerce_db.execute(
        """
        SELECT date_trunc('day', salesdate) AS day,
               SUM(salesamount) AS total
        FROM orionbelt_1.sales
        GROUP BY date_trunc('day', salesdate)
        ORDER BY date_trunc('day', salesdate)
        """
    ).fetchdf()

    base["expected"] = base["total"].rolling(window=30, min_periods=1).mean()
    expected = {_to_date(row.day): row.expected for row in base.itertuples(index=False)}

    obsl_rows = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Date"],
                measures=["Rolling 30 Day Sales"],
            )
        )
    )
    obsl = {_to_date(r["Sales Date"]): r["Rolling 30 Day Sales"] for r in obsl_rows}

    assert set(obsl) == set(expected), (
        f"Day sets differ: only-in-OBSL={set(obsl) - set(expected)}, "
        f"only-in-baseline={set(expected) - set(obsl)}"
    )
    for day in sorted(expected):
        assert float(obsl[day]) == pytest.approx(
            float(expected[day]), rel=_FLOAT_REL, abs=_FLOAT_ABS
        ), (
            f"Rolling 30 Day Sales for {day}: OBSL={obsl[day]}, "
            f"pandas baseline={expected[day]}. A mismatch here suggests a "
            f"window-frame bug (e.g. ``30 PRECEDING`` interpreted as 30 "
            f"days rather than 30 rows) or wrong sort order."
        )


# ---------------------------------------------------------------------------
# Corpus #13 — Sales YoY Growth (lag-12-months percent change)
# ---------------------------------------------------------------------------


def test_sales_yoy_growth(
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
    commerce_db: duckdb.DuckDBPyConnection,
) -> None:
    """OBSL ``Sales YoY Growth`` == (current / lag(12 months) - 1).

    OBSL builds a calendar spine of months from min/max ``salesdate``, then
    lag-joins each month to itself shifted -1 year. The baseline does the
    same with ``pd.date_range`` + ``.shift(12)``.

    Months with no prior-year value should be NULL on both sides; months
    where prior-year was zero should also be NULL (DIV/0 → NULLIF in OBSL,
    NaN in pandas — both treated as None for comparison).
    """
    base = commerce_db.execute(
        """
        SELECT date_trunc('month', salesdate) AS month,
               SUM(salesamount) AS total
        FROM orionbelt_1.sales
        GROUP BY date_trunc('month', salesdate)
        """
    ).fetchdf()

    if base.empty:
        pytest.skip("Seed has no Sales rows.")

    spine = pd.DataFrame(
        {"month": pd.date_range(base["month"].min(), base["month"].max(), freq="MS")}
    )
    df = spine.merge(base, on="month", how="left")
    df["prev"] = df["total"].shift(12)
    # Replicate NULLIF(prev, 0): division-by-zero or missing-prev → None.
    df["yoy"] = df.apply(
        lambda r: (
            (r["total"] / r["prev"] - 1)
            if (r["prev"] is not None and not pd.isna(r["prev"]) and r["prev"] != 0)
            else None
        ),
        axis=1,
    )
    expected = {_to_date(row.month): row.yoy for row in df.itertuples(index=False)}

    obsl_rows = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Month"],
                measures=["Sales YoY Growth"],
            )
        )
    )
    obsl = {_to_date(r["Sales Month"]): r["Sales YoY Growth"] for r in obsl_rows}

    assert set(obsl) == set(expected), (
        f"Month sets differ: only-in-OBSL={set(obsl) - set(expected)}, "
        f"only-in-baseline={set(expected) - set(obsl)}"
    )
    for month in sorted(expected):
        a = obsl[month]
        b = expected[month]
        a_none = a is None
        b_none = b is None or (isinstance(b, float) and pd.isna(b))
        if a_none and b_none:
            continue
        assert not a_none and not b_none, (
            f"YoY Growth for {month}: OBSL={a}, baseline={b}. One is null, "
            f"the other isn't — likely a date-spine or lag-join bug."
        )
        assert float(a) == pytest.approx(float(b), rel=_FLOAT_REL, abs=_FLOAT_ABS), (
            f"YoY Growth for {month}: OBSL={a}, baseline={b}. Suggests a "
            f"period-over-period offset bug or wrong percent-change formula."
        )
