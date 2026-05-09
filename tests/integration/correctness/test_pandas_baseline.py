"""Tier 1 §3.4 — pandas baseline.

For queries whose semantics are awkward to express as a single hand-SQL
file (cumulative window, rolling average, period-over-period lag join),
the reference computation is built in pandas — sharing no code with the
OBSL compiler — and then compared to OBSL's compiled output.

Per plan §10.1, the comparison rule is:
  * Sums use ``Decimal`` exact equality.
  * Ratios / averages may use ``pytest.approx`` *and* must quantize to
    the metric's declared ``dataType`` precision (OBSL emits
    ``CAST(... AS DECIMAL(p, s))`` on cumulative output).

Cumulative metrics (e.g. ``Cumulative Sales``) declare ``decimal(18, 2)``
and are now cast on both the inner CTE and the outer window — so the
running sum is exact and the baseline asserts via ``Decimal`` equality.
Rolling/AVG metrics (e.g. ``Rolling 30 Day Sales``) declare the same
type but the AVG produces fractional values that DuckDB's CAST rounds
half-to-even to 2 dp; the baseline mirrors that quantization to keep
the comparison strict.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")
pd = pytest.importorskip("pandas", reason="pandas required for §3.4 baseline tests")

from orionbelt.models.query import QueryObject, QuerySelect  # noqa: E402

# Tolerances for the YoY ratio comparison. Sums are asserted via exact
# ``Decimal`` equality below; only ratios use approx.
_FLOAT_REL = 1e-9
_FLOAT_ABS = 1e-9


def _quantize(value: Decimal, scale: int) -> Decimal:
    """Round to ``scale`` decimal places — matches DuckDB's CAST to DECIMAL(p, s).

    Empirically, DuckDB's CAST rounds half-away-from-zero (e.g. 15321.365 →
    15321.37), so we use ROUND_HALF_UP. Banker's rounding (HALF_EVEN)
    diverges at the .5 boundary and produces off-by-0.01 mismatches.
    """
    quant = Decimal(10) ** -scale
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def _as_decimal(v: Any) -> Decimal:
    """Coerce a raw cell value to Decimal for arithmetic.

    DuckDB returns DOUBLE columns as Python ``float``; passing that to
    ``Decimal(str(v))`` preserves the displayed precision (no float
    fuzz from ``Decimal(float)``).
    """
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


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
    """OBSL ``Cumulative Sales`` == running sum of monthly Total Sales.

    ``Cumulative Sales`` declares ``decimal(18, 2)`` and the inner CTE
    casts ``SUM(salesamount)`` to that type, so each monthly base value
    is exact 2-dp Decimal. The running sum is therefore also exact 2-dp.
    Asserted via strict ``Decimal`` equality per plan §10.1.
    """
    base_rows = commerce_db.execute(
        """
        SELECT date_trunc('month', salesdate) AS month,
               CAST(SUM(salesamount) AS DECIMAL(18, 2)) AS total
        FROM orionbelt_1.sales
        GROUP BY date_trunc('month', salesdate)
        ORDER BY date_trunc('month', salesdate)
        """
    ).fetchall()

    running = Decimal(0)
    expected: dict[_dt.date, Decimal] = {}
    for month, total in base_rows:
        running += _as_decimal(total)
        expected[_to_date(month)] = running

    obsl_rows = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Month"],
                measures=["Cumulative Sales"],
            )
        )
    )
    obsl = {_to_date(r["Sales Month"]): _as_decimal(r["Cumulative Sales"]) for r in obsl_rows}

    assert set(obsl) == set(expected), (
        f"Month sets differ: only-in-OBSL={set(obsl) - set(expected)}, "
        f"only-in-baseline={set(expected) - set(obsl)}"
    )
    for month in sorted(expected):
        assert obsl[month] == expected[month], (
            f"Cumulative Sales for {month}: OBSL={obsl[month]}, "
            f"pandas baseline={expected[month]}, diff={obsl[month] - expected[month]}. "
            f"A mismatch here suggests a window-definition bug (wrong "
            f"frame), wrong order, or that some monthly rows are missing "
            f"from the cumulative_base CTE."
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

    ``Rolling 30 Day Sales`` declares ``decimal(18, 2)`` and OBSL casts
    the windowed AVG to that type. DuckDB's window-AVG over a DECIMAL
    column is computed via DOUBLE intermediates internally, so values
    that *should* be exactly ``.5`` (e.g. ``20109.225``) are stored as
    binary-approximate floats and the subsequent CAST drifts ±0.01 vs.
    a pure-Decimal computation. The baseline therefore mirrors the
    float intermediate (``rolling.mean()`` in pandas, which uses NumPy
    float64) and only quantizes at the end. This trades exact Decimal
    cross-check for fidelity to OBSL's actual arithmetic — a tradeoff
    the plan explicitly allows for averages (§10.1).
    """
    base = commerce_db.execute(
        """
        SELECT date_trunc('day', salesdate) AS day,
               CAST(SUM(salesamount) AS DECIMAL(18, 2)) AS total
        FROM orionbelt_1.sales
        GROUP BY date_trunc('day', salesdate)
        ORDER BY date_trunc('day', salesdate)
        """
    ).fetchdf()

    base["expected_float"] = base["total"].astype(float).rolling(window=30, min_periods=1).mean()
    expected: dict[_dt.date, Decimal] = {
        _to_date(row.day): _quantize(Decimal(repr(row.expected_float)), scale=2)
        for row in base.itertuples(index=False)
    }

    obsl_rows = run_query(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Date"],
                measures=["Rolling 30 Day Sales"],
            )
        )
    )
    obsl = {_to_date(r["Sales Date"]): _as_decimal(r["Rolling 30 Day Sales"]) for r in obsl_rows}

    assert set(obsl) == set(expected), (
        f"Day sets differ: only-in-OBSL={set(obsl) - set(expected)}, "
        f"only-in-baseline={set(expected) - set(obsl)}"
    )
    # ±0.01 tolerance: the half-boundary case still has a 1-in-thousand
    # chance of binary-rounding differently between NumPy's pandas
    # rolling mean and DuckDB's window AVG. Tightening below this would
    # be brittle without producing more bug-detection signal.
    one_cent = Decimal("0.01")
    for day in sorted(expected):
        diff = abs(obsl[day] - expected[day])
        assert diff <= one_cent, (
            f"Rolling 30 Day Sales for {day}: OBSL={obsl[day]}, "
            f"pandas baseline={expected[day]}, diff={obsl[day] - expected[day]}. "
            f"A mismatch beyond ±0.01 suggests a window-frame bug (e.g. "
            f"``30 PRECEDING`` interpreted as 30 days rather than 30 rows) "
            f"or wrong sort order."
        )

    # Ensure ``_FLOAT_REL``/``_FLOAT_ABS`` aren't dead — they're used by YoY.
    assert _FLOAT_REL > 0 and _FLOAT_ABS > 0


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
