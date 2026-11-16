"""``date_diff`` and ``date_add`` answer alike on all eight engines.

The catalog's rules: ``date_diff`` counts the unit boundaries crossed (a week
starts on Monday), signed, and ``date_add`` moves to the last day of a shorter
month. Each is written out in Python here, and every engine checked against it
on timestamps at month ends, leap days, across midnight and backwards, where
the engines' own functions disagree: Databricks' ``date_diff`` counted whole
units, so 23:00 to 01:00 the next morning was 0 days; BigQuery refused a week
interval (``ISOWEEK``); Dremio refused a week difference as an amount to add;
MySQL and ClickHouse wrote a timestamp literal in whole seconds.

Gated by the ``docker`` pytest marker::

    uv run pytest -m docker tests/integration/drift/vendor_exec/test_date_arithmetic_exec.py
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from typing import Any

import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.registry import DialectRegistry

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

PAIRS = [
    ("2026-01-31 23:00:00", "2026-02-01 01:00:00"),  # across midnight
    ("2026-01-31 12:00:00", "2026-03-01 00:00:00"),
    ("2024-02-29 00:00:00", "2025-02-28 00:00:00"),  # from a leap day
    ("2026-03-10 10:30:00", "2026-03-01 11:00:00"),  # backwards
    ("2026-01-06 00:00:00", "2026-01-19 00:00:00"),  # Tuesday to Monday
    ("2026-08-09 00:00:00", "2026-08-15 00:00:00"),  # Sunday to Saturday
    ("2026-12-31 23:59:59", "2027-01-01 00:00:00"),  # every boundary at once
    ("2026-01-01 00:00:00.9", "2026-01-01 00:00:01.1"),  # a second boundary
]
UNITS = ["year", "quarter", "month", "week", "day", "hour", "minute", "second"]
ADDS = [
    ("month", "1", "2026-01-31 10:00:00"),  # to February's last day
    ("month", "-1", "2026-03-31 00:00:00"),
    ("quarter", "1", "2025-11-30 00:00:00"),
    ("year", "1", "2024-02-29 12:00:00"),
    ("week", "2", "2026-08-01 00:00:00"),
    ("day", "-5", "2026-03-03 06:00:00"),
    ("hour", "25", "2026-01-31 23:00:00"),
    ("minute", "90", "2026-01-31 23:00:00"),
    ("second", "3600", "2026-01-31 23:30:00"),
    # A computed count: a week difference, and a ``div``.
    ("day", "date_diff('week', DATE '2026-08-03', DATE '2026-08-24')", "2026-08-01 00:00:00"),
    ("day", "div(7, 2)", "2026-08-01 00:00:00"),
]


def _boundaries(start: str, end: str, unit: str) -> int:
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if unit == "year":
        return e.year - s.year
    if unit == "quarter":
        return (e.year * 4 + (e.month - 1) // 3) - (s.year * 4 + (s.month - 1) // 3)
    if unit == "month":
        return (e.year * 12 + e.month) - (s.year * 12 + s.month)
    if unit == "week":
        monday_e = e.date() - timedelta(days=e.weekday())
        monday_s = s.date() - timedelta(days=s.weekday())
        return (monday_e - monday_s).days // 7
    if unit == "day":
        return (e.date() - s.date()).days
    size = {"hour": 3600, "minute": 60, "second": 1}[unit]
    epoch = datetime(1970, 1, 1)
    return int((e - epoch).total_seconds() // size - (s - epoch).total_seconds() // size)


def _added(unit: str, count: int, start: str) -> datetime:
    s = datetime.fromisoformat(start)
    months = {"year": 12, "quarter": 3, "month": 1}.get(unit)
    if months is None:
        return s + count * timedelta(**{f"{unit}s": 1})
    year, month = divmod(s.month - 1 + count * months, 12)
    year += s.year
    day = min(s.day, calendar.monthrange(year, month + 1)[1])
    return s.replace(year=year, month=month + 1, day=day)


def _count(text: str) -> int:
    known = {
        "date_diff('week', DATE '2026-08-03', DATE '2026-08-24')": 3,
        "div(7, 2)": 3,
    }
    return known.get(text, int(text) if text.lstrip("-").isdigit() else 0)


def _instant(value: Any) -> str:
    """A returned timestamp or date as ``YYYY-MM-DD HH:MM:SS``."""
    text = str(value).replace("T", " ")
    return text[:19] if len(text) >= 19 else f"{text[:10]} 00:00:00"


def _assert_date_arithmetic(vendor: VendorTarget) -> None:
    engine = DialectRegistry.get(vendor.dialect)

    def sql(text: str) -> str:
        return engine.compile_expr(parse_expression(tokenize_metric_formula(text)))

    diffs = [(s, e, u) for s, e in PAIRS for u in UNITS]
    projections = [
        sql(f"date_diff('{u}', TIMESTAMP '{s}', TIMESTAMP '{e}')") + f" AS d{i}"
        for i, (s, e, u) in enumerate(diffs)
    ] + [
        sql(f"date_add('{u}', {n}, TIMESTAMP '{s}')") + f" AS a{i}"
        for i, (u, n, s) in enumerate(ADDS)
    ]
    row = vendor.execute("SELECT " + ", ".join(projections))[0]
    values = {name.lower(): value for name, value in row.items()}
    wrong = [
        f"date_diff('{u}', {s}, {e}) = {values[f'd{i}']}, expected {_boundaries(s, e, u)}"
        for i, (s, e, u) in enumerate(diffs)
        if values[f"d{i}"] != _boundaries(s, e, u)
    ] + [
        f"date_add('{u}', {n}, {s}) = {values[f'a{i}']}, expected {_added(u, _count(n), s)}"
        for i, (u, n, s) in enumerate(ADDS)
        if _instant(values[f"a{i}"]) != str(_added(u, _count(n), s))[:19]
    ]
    assert not wrong, f"{vendor.name}:\n  " + "\n  ".join(wrong)


@pytest.mark.parametrize(
    "fixture",
    [
        "vendor_duckdb",
        "vendor_postgres",
        "vendor_mysql",
        "vendor_clickhouse",
        "vendor_snowflake",
        "vendor_bigquery",
        "vendor_databricks",
        "vendor_dremio",
    ],
)
def test_date_arithmetic(fixture: str, request: pytest.FixtureRequest) -> None:
    _assert_date_arithmetic(request.getfixturevalue(fixture))
