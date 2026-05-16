"""OBSL ``ExecutionResult`` → Postgres wire type mapping (text format).

Step 2 lives here. The executor reports column types as one of four
coarse hints — ``number`` / ``string`` / ``datetime`` / ``binary`` —
which we collapse onto a small set of Postgres OIDs that every BI tool
handles. Step 7 lands binary-format encoders and finer-grained OIDs
(int4 vs int8, float8 vs numeric, timestamp vs date) once we surface
the underlying Arrow types through ``ColumnMeta``.

Postgres wire text format is described in §52.2 of the PostgreSQL docs.
Roughly: integers and floats use their literal decimal representation;
timestamps use ``YYYY-MM-DD HH:MM:SS[.ffffff][+TZ]``; ``bytea`` uses the
``\\xHEX`` form; booleans use ``t`` / ``f``; NULLs use the wire-level
length-prefix sentinel ``-1`` (handled by ``build_data_row``, not here).
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Final

# OIDs from PostgreSQL's pg_type catalog. We pick the widest variant per
# family so BI tools don't truncate on the edges.
OID_BOOL: Final[int] = 16
OID_BYTEA: Final[int] = 17
OID_INT8: Final[int] = 20
OID_TEXT: Final[int] = 25
OID_FLOAT8: Final[int] = 701
OID_NUMERIC: Final[int] = 1700
OID_DATE: Final[int] = 1082
OID_TIME: Final[int] = 1083
OID_TIMESTAMP: Final[int] = 1114
OID_TIMESTAMPTZ: Final[int] = 1184


def oid_for_type_hint(type_hint: str) -> int:
    """Pick a Postgres OID for one of the executor's coarse type hints.

    The hints come from ``service/db_executor.py``; ``"number"`` covers
    everything from int4 to numeric, so we use NUMERIC's OID — it is the
    only Postgres numeric type that BI tools never narrow.
    """

    if type_hint == "number":
        return OID_NUMERIC
    if type_hint == "datetime":
        return OID_TIMESTAMP
    if type_hint == "binary":
        return OID_BYTEA
    # "string" and anything we don't yet recognise — TEXT is the safe
    # default that every Postgres client accepts as a text-format value.
    return OID_TEXT


def encode_text_value(value: object, type_hint: str) -> str | None:
    """Render ``value`` for transmission in a Postgres text-format DataRow.

    Returns ``None`` when ``value`` is ``None`` so the caller (the wire
    framer) can emit the ``-1`` NULL sentinel.
    """

    if value is None:
        return None

    if isinstance(value, bool):
        return "t" if value else "f"

    if isinstance(value, (int, float, Decimal)):
        return str(value)

    if isinstance(value, datetime):
        # ``isoformat`` uses ``T`` between the date and time; real
        # Postgres uses a space.  BI tools accept both, but matching the
        # canonical form keeps drivers happy.
        formatted = value.isoformat(sep=" ")
        # Postgres uses ``+00`` / ``+05:30`` style offsets — Python emits
        # ``+00:00``; both parse cleanly so we leave it as-is.
        return formatted

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, dt_time):
        return value.isoformat()

    if isinstance(value, (bytes, bytearray, memoryview)):
        if type_hint == "binary":
            return "\\x" + bytes(value).hex()
        # Non-binary column carrying bytes — fall back to UTF-8 decode.
        return bytes(value).decode("utf-8", errors="replace")

    return str(value)
