"""``current_timestamp()``, ``current_time()`` and ``current_date()`` read the
clock in the model's ``queryTimezone`` on every engine.

Asia/Kathmandu is UTC+05:45, so a value read in UTC, in the session's zone or
with a sign or offset wrong is off by hours or by 45 minutes, far outside the
tolerance a clock difference between this host and the engine needs.

Gated by the ``docker`` pytest marker::

    uv run pytest -m docker tests/integration/drift/vendor_exec/test_current_clock_exec.py
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.ast.nodes import FunctionCall
from orionbelt.dialect.registry import DialectRegistry

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

ZONE = "Asia/Kathmandu"
TOLERANCE = timedelta(minutes=5)


def _wall_clock(value: Any) -> datetime:
    """A timestamp as the wall clock in ``ZONE``: an aware one is read there."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    assert isinstance(value, datetime), repr(value)
    if value.tzinfo is not None:
        value = value.astimezone(ZoneInfo(ZONE)).replace(tzinfo=None)
    return value


def _seconds_of_day(value: Any) -> float:
    """A time of day as seconds after midnight, whatever the driver returned."""
    if isinstance(value, timedelta):  # MySQL's TIME through PyMySQL
        return value.total_seconds()
    if isinstance(value, str):  # ClickHouse and Databricks: text
        value = time.fromisoformat(value)
    assert isinstance(value, time), repr(value)
    return value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1e6


def _assert_clock(vendor: VendorTarget) -> None:
    engine = DialectRegistry.get(vendor.dialect)
    engine.today_zone = ZONE

    def call(name: str) -> str:
        return engine.compile_expr(FunctionCall(name=name, args=[]))

    sql = (
        f"SELECT {call('current_timestamp')} AS ts, {call('current_time')} AS t, "
        f"{call('current_date')} AS d"
    )
    # Snowflake names an unquoted alias in upper case.
    row = {name.lower(): value for name, value in vendor.execute(sql)[0].items()}
    now = datetime.now(ZoneInfo(ZONE)).replace(tzinfo=None)

    stamp = _wall_clock(row["ts"])
    assert abs(stamp - now) < TOLERANCE, f"{vendor.name}: {stamp} is not {now} ({sql})"

    clock = _seconds_of_day(row["t"])
    expected = now.hour * 3600 + now.minute * 60 + now.second
    gap = abs(clock - expected) % 86400
    assert min(gap, 86400 - gap) < TOLERANCE.total_seconds(), (
        f"{vendor.name}: time {row['t']!r} is not {now.time()} ({sql})"
    )

    today = row["d"]
    today = today.date() if isinstance(today, datetime) else today
    if isinstance(today, str):
        today = date.fromisoformat(today[:10])
    # The three are read in one zone, so they name one day (bar a run across
    # midnight between the two reads).
    assert today in {stamp.date(), (stamp - TOLERANCE).date()}, f"{vendor.name}: {today}"


def _assert_session_clock(vendor: VendorTarget) -> None:
    """Without ``queryTimezone`` the clock is the session's, whichever zone that
    is: a wall clock no further from UTC's than a zone can be."""
    engine = DialectRegistry.get(vendor.dialect)
    stamp_sql = engine.compile_expr(FunctionCall(name="current_timestamp", args=[]))
    time_sql = engine.compile_expr(FunctionCall(name="current_time", args=[]))
    row = vendor.execute(f"SELECT {stamp_sql} AS ts, {time_sql} AS t")[0]
    stamp, clock = row.values()
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp)
    assert isinstance(stamp, datetime), repr(stamp)
    utc = datetime.now(UTC)
    if stamp.tzinfo is None:
        utc = utc.replace(tzinfo=None)
    assert abs(stamp - utc) < timedelta(hours=14, minutes=5), f"{vendor.name}: {stamp}"
    _seconds_of_day(clock)


def test_duckdb_clock(vendor_duckdb: VendorTarget) -> None:
    _assert_clock(vendor_duckdb)


def test_postgres_clock(vendor_postgres: VendorTarget) -> None:
    _assert_clock(vendor_postgres)


def test_mysql_clock(vendor_mysql: VendorTarget) -> None:
    _assert_clock(vendor_mysql)


def test_clickhouse_clock(vendor_clickhouse: VendorTarget) -> None:
    _assert_clock(vendor_clickhouse)


def test_snowflake_clock(vendor_snowflake: VendorTarget) -> None:
    _assert_clock(vendor_snowflake)


def test_bigquery_clock(vendor_bigquery: VendorTarget) -> None:
    _assert_clock(vendor_bigquery)


def test_databricks_clock(vendor_databricks: VendorTarget) -> None:
    _assert_clock(vendor_databricks)


def test_dremio_clock(vendor_dremio: VendorTarget) -> None:
    _assert_clock(vendor_dremio)


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
def test_session_clock(fixture: str, request: pytest.FixtureRequest) -> None:
    _assert_session_clock(request.getfixturevalue(fixture))
