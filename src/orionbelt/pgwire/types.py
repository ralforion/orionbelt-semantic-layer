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
OID_TEXT_ARRAY: Final[int] = 1009
OID_FLOAT8: Final[int] = 701
OID_NUMERIC: Final[int] = 1700
OID_DATE: Final[int] = 1082
OID_TIME: Final[int] = 1083
OID_TIMESTAMP: Final[int] = 1114
OID_TIMESTAMPTZ: Final[int] = 1184


def oid_for_type_hint(type_hint: str) -> int:
    """Pick a Postgres OID for one of the executor's coarse type hints."""

    if type_hint == "boolean":
        return OID_BOOL
    if type_hint == "number":
        return OID_FLOAT8
    if type_hint == "decimal":
        return OID_NUMERIC
    if type_hint == "datetime":
        return OID_TIMESTAMP
    if type_hint == "binary":
        return OID_BYTEA
    if type_hint == "text_array":
        return OID_TEXT_ARRAY
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

    Binary encoders exist for every OID the server announces: TEXT
    (``"string"``, the UTF-8 bytes, the same as its text form), FLOAT8
    (``"number"``), NUMERIC (``"decimal"``), TIMESTAMP (``"datetime"``),
    BOOL (``"boolean"``), BYTEA (``"binary"``) and TEXT[]
    (``"text_array"``). Honouring every binary
    request matters twice over: a client that described a statement up
    front (pgjdbc's server-prepared statements) holds that description and
    ignores a later RowDescription's format codes, and psycopg 3 applies
    the first column's format to the whole row, so one text column among
    binary ones breaks decoding.

    Used by the router to compute the *effective* per-column format
    code: a Bind asking for binary on a column we can only emit as
    text must be advertised as text in RowDescription, otherwise
    binary-capable clients misdecode the text bytes per the OID. The
    set is intentionally restricted; widening it requires both an
    encoder (here) and a matching test that round-trips through a real
    Postgres client.
    """

    return type_hint in _BINARY_HINTS


_BINARY_HINTS: Final[frozenset[str]] = frozenset(
    {"string", "number", "decimal", "datetime", "boolean", "binary", "text_array"}
)


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

    if format_code == 1:
        binary = _encode_binary(value, type_hint, scale)
        if binary is not None:
            return binary

    if type_hint == "decimal":
        return _encode_decimal_text(value, scale)

    if type_hint == "text_array" and isinstance(value, (list, tuple)):
        return _encode_text_array(value)

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


def _encode_text_array(items: list[object] | tuple[object, ...]) -> str:
    """Postgres text-format array literal: ``{a,"b c",NULL}``.

    Elements are quoted when Postgres would quote them (empty, the word
    NULL, or containing a delimiter, brace, quote, backslash or
    whitespace), with ``"`` and ``\\`` backslash-escaped. An empty list is
    ``{}``.
    """

    parts: list[str] = []
    for item in items:
        if item is None:
            parts.append("NULL")
            continue
        text = str(item)
        if text == "" or text.upper() == "NULL" or any(c in '{},"\\' or c.isspace() for c in text):
            text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        parts.append(text)
    return "{" + ",".join(parts) + "}"


def _encode_binary(value: object, type_hint: str, scale: int | None) -> bytes | None:
    """Binary wire format for ``type_hint``'s OID, or None when there is none.

    None (text fallback) only for a value the OID cannot hold in binary:
    an interval in a ``"datetime"`` column, which has no TIMESTAMP form.
    """

    if type_hint == "number":
        return _encode_float8_binary(value)
    if type_hint == "decimal":
        return _encode_numeric_binary(_encode_decimal_text(value, scale))
    if type_hint == "datetime":
        return _encode_timestamp_binary(value)
    if type_hint == "boolean":
        return b"\x01" if value else b"\x00"
    if type_hint == "binary" and isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if type_hint == "text_array" and isinstance(value, (list, tuple)):
        return _encode_text_array_binary(value)
    if type_hint in ("string", "binary"):
        text = encode_value(value, "string")
        return text.encode("utf-8") if isinstance(text, str) else text
    return None


def _encode_text_array_binary(items: list[object] | tuple[object, ...]) -> bytes:
    """Postgres TEXT[] binary format (``array_send``).

    ``ndim, has_nulls, element OID`` (int32 each); for a non-empty array one
    dimension ``(length, lower bound 1)``; then each element as an int32
    length (-1 for NULL) and its UTF-8 bytes. An empty array has ``ndim`` 0.
    """

    if not items:
        return struct.pack("!iiI", 0, 0, OID_TEXT)
    has_nulls = any(item is None for item in items)
    out = [struct.pack("!iiIii", 1, int(has_nulls), OID_TEXT, len(items), 1)]
    for item in items:
        if item is None:
            out.append(struct.pack("!i", -1))
            continue
        data = str(item).encode("utf-8")
        out.append(struct.pack("!i", len(data)) + data)
    return b"".join(out)


#: NUMERIC binary sign words.
_NUMERIC_POS = 0x0000
_NUMERIC_NEG = 0x4000
_NUMERIC_NAN = 0xC000


def _encode_numeric_binary(text: str) -> bytes:
    """Postgres NUMERIC binary format from a plain decimal string.

    ``ndigits, weight, sign, dscale`` (four int16) followed by ``ndigits``
    base-10000 digits; ``weight`` is the power of 10000 of the first digit.
    Leading and trailing zero digits are dropped, as Postgres does; ``dscale``
    keeps the display scale. ``text`` is :func:`_encode_decimal_text`'s output,
    so the binary value carries exactly the scale the text path would.
    """

    try:
        dec = Decimal(text)
    except InvalidOperation:
        return struct.pack("!hhHH", 0, 0, _NUMERIC_NAN, 0)
    sign, digit_tuple, exponent = dec.as_tuple()
    if not isinstance(exponent, int):  # NaN / Infinity
        return struct.pack("!hhHH", 0, 0, _NUMERIC_NAN, 0)
    dscale = max(0, -exponent)
    digits = "".join(map(str, digit_tuple))
    if exponent >= 0:
        int_part, frac_part = digits + "0" * exponent, ""
    else:
        frac_len = -exponent
        int_part = digits[:-frac_len] if len(digits) > frac_len else ""
        frac_part = digits[-frac_len:].rjust(frac_len, "0")
    int_part = int_part.lstrip("0")
    int_part = int_part.rjust(-(-len(int_part) // 4) * 4, "0")
    frac_part = frac_part.ljust(-(-len(frac_part) // 4) * 4, "0")
    groups = [int(int_part[i : i + 4]) for i in range(0, len(int_part), 4)]
    weight = len(groups) - 1
    groups += [int(frac_part[i : i + 4]) for i in range(0, len(frac_part), 4)]
    while groups and groups[0] == 0:
        groups.pop(0)
        weight -= 1
    while groups and groups[-1] == 0:
        groups.pop()
    if not groups:
        weight = 0
    header = struct.pack(
        "!hhHH", len(groups), weight, _NUMERIC_NEG if sign else _NUMERIC_POS, dscale
    )
    return header + struct.pack(f"!{len(groups)}h", *groups)


_PG_EPOCH = datetime(2000, 1, 1)


def _encode_timestamp_binary(value: object) -> bytes | None:
    """Postgres TIMESTAMP binary format: int64 microseconds since 2000-01-01.

    A TIMESTAMP carries the wall clock, so an aware value keeps its local
    time, as the text path renders it. A date is its midnight. Anything
    that is not a point in time (an interval) has no binary form here.
    """

    if isinstance(value, datetime):
        moment = value.replace(tzinfo=None)
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            return None
    else:
        return None
    delta = moment - _PG_EPOCH
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return struct.pack("!q", micros)


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
