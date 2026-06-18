"""OBSL ``ExecutionResult`` → Postgres wire type mapping.

The executor reports column types as one of four coarse hints —
``number`` / ``string`` / ``datetime`` / ``binary`` — which we
collapse onto a small set of Postgres OIDs.

Numbers are sent on the wire in **binary** format (8-byte IEEE 754
big-endian, ``FLOAT8`` OID). Tableau's JDBC driver, like most
Postgres drivers, parses numerics as binary regardless of
``RowDescription.format_code``; text bytes silently decode as zero.
Everything else (strings, dates, bytea) stays text — they're handled
identically across drivers and text representation is canonical.
"""

from __future__ import annotations

import contextlib
import math
import struct
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal, InvalidOperation
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
    """Pick a Postgres OID for one of the executor's coarse type hints."""

    if type_hint == "number":
        return OID_FLOAT8
    if type_hint == "decimal":
        return OID_NUMERIC
    if type_hint == "datetime":
        return OID_TIMESTAMP
    if type_hint == "binary":
        return OID_BYTEA
    return OID_TEXT


def format_code_for_type_hint(type_hint: str) -> int:
    """Server-default format code. 0 = text, 1 = binary.

    The actual format sent on the wire is governed by
    ``Bind.result_formats`` (in the extended-query protocol) — see
    :func:`encode_value`'s ``format_code`` parameter. This helper is
    only consulted for the simple-Query path and the Parse-time
    pre-execution cache. We default to **text** because:

    * Simple Query is always text per the Postgres protocol;
    * Pre-execution can't know what format Bind will request later;
    * Text is cheap to convert to binary if Bind asks for it.
    """

    return 0


def can_encode_binary(type_hint: str) -> bool:
    """True when ``encode_value`` actually produces a binary payload.

    Today only ``"number"`` (8-byte big-endian IEEE 754 FLOAT8) has
    a binary encoder. Everything else falls through to text.

    Used by the router to compute the *effective* per-column format
    code: a Bind asking for binary on a column we can only emit as
    text must be advertised as text in RowDescription, otherwise
    binary-capable clients misdecode the text bytes per the OID. The
    set is intentionally restricted; widening it requires both an
    encoder (here) and a matching test that round-trips through a real
    Postgres client.
    """

    return type_hint == "number"


def encode_value(
    value: object,
    type_hint: str,
    format_code: int = 0,
    scale: int | None = None,
) -> str | bytes | None:
    """Encode ``value`` for one DataRow slot.

    ``format_code`` 0 = text, 1 = binary; matches the per-column code
    in ``Bind.result_formats``. For ``"number"`` columns:

    * ``format_code=0`` → decimal-string text (e.g. ``"1329.87"``);
    * ``format_code=1`` → 8 bytes big-endian IEEE 754 FLOAT8.

    For ``"decimal"`` columns (NUMERIC OID, issue #116) the value is always
    serialised as fixed-scale text (e.g. ``"574585.00"``, never ``574585.0``
    or scientific notation) so clients render the declared scale. ``scale`` is
    the column's declared scale; when omitted the value's own precision is kept.

    Other type hints currently always serialise as text — binary
    representations for timestamps, bytea, etc. can land alongside
    Step 7 of design/PLAN_postgres_wire.md. Returns ``None`` for SQL
    NULL (the wire framer emits the ``-1`` length sentinel).
    """

    if value is None:
        return None

    if type_hint == "number" and format_code == 1:
        return _encode_float8_binary(value)

    if type_hint == "decimal":
        return _encode_decimal_text(value, scale)

    if isinstance(value, bool):
        return "t" if value else "f"

    if isinstance(value, (int, float, Decimal)):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat(sep=" ")

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, dt_time):
        return value.isoformat()

    if isinstance(value, (bytes, bytearray, memoryview)):
        if type_hint == "binary":
            return "\\x" + bytes(value).hex()
        return bytes(value).decode("utf-8", errors="replace")

    return str(value)


# Backwards-compatibility shim — kept so existing call sites in the
# catalog / canned paths keep working. New code should call
# :func:`encode_value`.
def encode_text_value(value: object, type_hint: str) -> str | bytes | None:
    """Alias for :func:`encode_value` — accepted for legacy call sites."""

    return encode_value(value, type_hint)


def _encode_float8_binary(value: object) -> bytes:
    """Postgres FLOAT8 binary format: 8 bytes IEEE 754 big-endian."""

    if isinstance(value, bool):
        # bool is an int subclass — guard before the numeric branch.
        return struct.pack("!d", 1.0 if value else 0.0)
    if isinstance(value, (int, float)):
        return struct.pack("!d", float(value))
    if isinstance(value, Decimal):
        # Decimal → float loses precision past ~15 significant digits;
        # acceptable for BI display, exact arithmetic stays in the DB.
        try:
            f = float(value)
        except (OverflowError, ValueError):
            f = math.nan
        return struct.pack("!d", f)
    # Last-ditch — try to coerce via str → float.
    try:
        return struct.pack("!d", float(str(value)))
    except (TypeError, ValueError):
        return struct.pack("!d", math.nan)


def _encode_decimal_text(value: object, scale: int | None) -> str:
    """Fixed-scale plain-decimal text for a NUMERIC column (issue #116).

    Always emits plain decimal notation (never scientific) and pads/rounds to
    the declared ``scale`` so ``574585`` renders as ``574585.00`` and a large
    value renders as ``16050258.53`` rather than ``1.605E7``.
    """

    if isinstance(value, bool):
        value = 1 if value else 0
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    if scale is not None and scale >= 0:
        # value wider than the declared scale — emit as-is
        with contextlib.suppress(InvalidOperation):
            dec = dec.quantize(Decimal(1).scaleb(-scale))
    return format(dec, "f")
