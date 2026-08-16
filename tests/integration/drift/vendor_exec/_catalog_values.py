"""Compare a catalog example's documented value with what an engine returned.

Shared by the vendor-exec matrix and the Dremio suite, which reaches its engine
over a different transport and therefore sees different Python types for the
same answer: a DATE arrives as ``datetime.date`` from a DB-API driver and as an
ISO string from Dremio's REST API. What the catalog pins is the value, so the
comparison is by value in each expected type's own terms rather than by equality
on whatever object the transport produced.
"""

from __future__ import annotations

import re
from typing import Any

NUMERIC_TOLERANCE = 1e-9
"""Relative tolerance for a numeric catalog value.

Not laxity about the answer: the numeric entries are floating point, and an
engine is free to deliver 2.35 as ``Decimal('2.3500')`` or the base change
behind ``log(2, 8)`` as 2.9999999999999996. It is tight enough that a real
disagreement (2 against 3 for ``round(2.5)``) still fails.
"""

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def matches_date(expected: str, actual: Any) -> bool:
    """Whether a date-valued entry returned the documented calendar day.

    ``date_trunc`` comes back as a DATE on ClickHouse, Snowflake and MySQL and
    as a TIMESTAMP at midnight on DuckDB and Postgres. The catalog pins the
    instant, not which of the two an engine chose, so the day is compared and a
    time component is required to be midnight rather than ignored.
    """
    if hasattr(actual, "isoformat"):
        if actual.isoformat()[:10] != expected:
            return False
        time = getattr(actual, "time", None)
        return time is None or time().isoformat().startswith("00:00:00")
    if isinstance(actual, str):
        if not actual.startswith(expected):
            return False
        remainder = actual[len(expected) :].strip("T ")
        return remainder in ("", "00:00:00") or remainder.startswith("00:00:00")
    return False


def matches(expected: str | int | float | bool | None, actual: Any) -> bool:
    """Whether *actual* is the documented value, across transport type mappings.

    Booleans come back as ``1``/``0`` from MySQL and ClickHouse, numbers as
    ``Decimal`` or ``float`` depending on the driver, dates as objects or ISO
    strings, and strings are strings everywhere.
    """
    if expected is None:
        return actual is None
    if actual is None:
        return False
    if isinstance(expected, bool):
        return bool(actual) is expected
    if isinstance(expected, (int, float)):
        return abs(float(actual) - expected) <= NUMERIC_TOLERANCE * max(1.0, abs(expected))
    if isinstance(expected, str) and _ISO_DATE.fullmatch(expected):
        return matches_date(expected, actual)
    return str(actual) == str(expected)
