"""Unit tests for pgwire/types.py — OID mapping and text encoding."""

from __future__ import annotations

from datetime import UTC, date, datetime
from datetime import time as dt_time
from decimal import Decimal

import pytest

from orionbelt.pgwire import types as pgtypes


def test_oid_for_known_hints() -> None:
    # Numbers advertise FLOAT8 OID; the wire format is text, but JDBC
    # parses text FLOAT8 correctly via ``Double.parseDouble``.
    assert pgtypes.oid_for_type_hint("number") == pgtypes.OID_FLOAT8
    assert pgtypes.oid_for_type_hint("string") == pgtypes.OID_TEXT
    assert pgtypes.oid_for_type_hint("datetime") == pgtypes.OID_TIMESTAMP
    assert pgtypes.oid_for_type_hint("binary") == pgtypes.OID_BYTEA


def test_format_code_default_is_text_for_everything() -> None:
    """Server-default is text (0) for every hint. The actual wire format
    is decided per-column by Bind.result_formats; this helper is only
    used for the simple-Query path and Parse-time preexec where there
    is no client-requested format yet.
    """
    for hint in ("number", "string", "datetime", "binary", "unknown"):
        assert pgtypes.format_code_for_type_hint(hint) == 0


def test_oid_for_unknown_hint_falls_back_to_text() -> None:
    assert pgtypes.oid_for_type_hint("something-new") == pgtypes.OID_TEXT


def test_encode_none_returns_none() -> None:
    assert pgtypes.encode_text_value(None, "string") is None
    assert pgtypes.encode_text_value(None, "number") is None


def test_encode_bool_emits_postgres_letters() -> None:
    assert pgtypes.encode_text_value(True, "string") == "t"
    assert pgtypes.encode_text_value(False, "string") == "f"


def test_encode_numeric_types_text() -> None:
    # ``format_code=0`` (default) → decimal-string text.
    assert pgtypes.encode_value(42, "number", 0) == "42"
    assert pgtypes.encode_value(3.14, "number", 0) == "3.14"
    assert pgtypes.encode_value(Decimal("12345.6789"), "number", 0) == "12345.6789"


def test_encode_numeric_types_binary() -> None:
    """``format_code=1`` → 8-byte big-endian IEEE 754 FLOAT8.

    pgjdbc puts FLOAT8 in its ``binaryTransferEnable`` set, so it
    requests ``result_formats=[…, 1, …]`` in Bind for numeric columns.
    The server must honour that or pgjdbc throws
    ``ArrayIndexOutOfBoundsException`` trying to read 8 bytes from a
    7-byte text payload.
    """
    import struct as _struct

    for v in (42, 3.14, Decimal("12345.6789")):
        encoded = pgtypes.encode_value(v, "number", 1)
        assert isinstance(encoded, bytes), f"{v!r} → {encoded!r}"
        assert len(encoded) == 8
        decoded = _struct.unpack("!d", encoded)[0]
        assert abs(decoded - float(v)) < 1e-9


def test_encode_datetime_uses_space_separator() -> None:
    ts = datetime(2024, 5, 16, 12, 34, 56, 789000)
    encoded = pgtypes.encode_text_value(ts, "datetime")
    assert encoded == "2024-05-16 12:34:56.789000"


def test_encode_datetime_with_timezone_preserves_offset() -> None:
    ts = datetime(2024, 5, 16, 12, 34, 56, tzinfo=UTC)
    encoded = pgtypes.encode_text_value(ts, "datetime")
    assert encoded is not None
    assert encoded.startswith("2024-05-16 12:34:56")
    assert encoded.endswith("+00:00")


def test_encode_date() -> None:
    assert pgtypes.encode_text_value(date(2024, 1, 31), "datetime") == "2024-01-31"


def test_encode_time() -> None:
    assert pgtypes.encode_text_value(dt_time(9, 5, 0), "string") == "09:05:00"


def test_encode_binary_uses_hex_prefix() -> None:
    assert pgtypes.encode_text_value(b"\x00\xff", "binary") == "\\x00ff"


def test_encode_string_falls_back_to_str() -> None:
    class _Custom:
        def __str__(self) -> str:
            return "custom-repr"

    assert pgtypes.encode_text_value(_Custom(), "string") == "custom-repr"


def test_decimal_hint_reports_numeric_oid() -> None:
    # Decimals advertise NUMERIC (not FLOAT8) so clients keep the scale and
    # don't render scientific notation / strip trailing zeros (issue #116).
    assert pgtypes.oid_for_type_hint("decimal") == pgtypes.OID_NUMERIC
    # Binary NUMERIC is Postgres's exact base-10000 format, never the lossy
    # float8 path: 0.1 keeps its digits instead of becoming 0.1000000000000000055.
    assert pgtypes.can_encode_binary("decimal") is True
    assert pgtypes.encode_value(Decimal("0.1"), "decimal", format_code=1) == bytes.fromhex(
        "0001ffff0000000103e8"
    )


def test_encode_decimal_fixed_scale() -> None:
    # Pads/rounds to the declared scale, always plain notation.
    assert pgtypes.encode_value(574585.0, "decimal", 0, 2) == "574585.00"
    assert pgtypes.encode_value(Decimal("574585"), "decimal", 0, 2) == "574585.00"
    assert pgtypes.encode_value(-16050258.53, "decimal", 0, 2) == "-16050258.53"
    # Large magnitude must not come out as ``1.605...E7``.
    assert "E" not in pgtypes.encode_value(16050258.53, "decimal", 0, 2).upper()
    # A Decimal wider than float precision must survive exactly, not round to
    # ``123456789012345680.00`` (issue #136).
    assert (
        pgtypes.encode_value(Decimal("123456789012345678.90"), "decimal", 0, 2)
        == "123456789012345678.90"
    )


def test_encode_decimal_without_scale_keeps_value() -> None:
    assert pgtypes.encode_value(Decimal("12.340"), "decimal", 0, None) == "12.340"


def test_text_array_hint_maps_to_text_array_oid() -> None:
    assert pgtypes.oid_for_type_hint("text_array") == pgtypes.OID_TEXT_ARRAY == 1009


def test_encode_text_array_literal() -> None:
    assert pgtypes.encode_value([], "text_array") == "{}"
    assert (
        pgtypes.encode_value(["fillfactor=70", "a b", "", None, "null", 'q"\\'], "text_array")
        == '{fillfactor=70,"a b","",NULL,"null","q\\"\\\\"}'
    )


@pytest.mark.parametrize(
    ("items", "pg_hex"),
    [
        # Postgres 16: ``encode(array_send(x::text[]), 'hex')``.
        ([], "000000000000000000000019"),
        (["x"], "00000001000000000000001900000001000000010000000178"),
        (
            ["fillfactor=70", "a b", None, ""],
            "00000001000000010000001900000004000000010000000d66696c6c666163746f723d3730"
            "00000003612062ffffffff00000000",
        ),
    ],
)
def test_text_array_binary_matches_postgres(items: list[str | None], pg_hex: str) -> None:
    """pgjdbc requests binary for text[] (OID 1009) on server-prepared statements."""
    assert pgtypes.can_encode_binary("text_array")
    assert pgtypes.encode_value(items, "text_array", format_code=1) == bytes.fromhex(pg_hex)


# Reference bytes from Postgres 16 itself: ``encode(numeric_send(x), 'hex')``
# and ``encode(timestamp_send(x), 'hex')``.
@pytest.mark.parametrize(
    ("value", "pg_hex"),
    [
        (Decimal("0"), "0000000000000000"),
        (Decimal("0.00"), "0000000000000002"),
        (Decimal("-1"), "00010000400000000001"),
        (Decimal("9284889.34"), "000300010000000203a013190d48"),
        (Decimal("574585.00"), "0002000100000002003911e9"),
        (Decimal("0.00012"), "0002ffff00000005000107d0"),
        (Decimal("-0.5"), "0001ffff400000011388"),
        (
            Decimal("12345678901234567890.123456789"),
            "000800040000000904d2162e23340d801ed204d2162e2328",
        ),
        (Decimal("100000000"), "00010002000000000001"),
        (Decimal("1E-10"), "0001fffd0000000a0064"),
        (Decimal("NaN"), "00000000c0000000"),
    ],
)
def test_numeric_binary_matches_postgres(value: Decimal, pg_hex: str) -> None:
    assert pgtypes.encode_value(value, "decimal", format_code=1) == bytes.fromhex(pg_hex)


def test_numeric_binary_keeps_the_declared_scale() -> None:
    """Same scale as the text path: 574585 at scale 2 is ``574585.00``."""
    assert pgtypes.encode_value(574585, "decimal", format_code=1, scale=2) == bytes.fromhex(
        "0002000100000002003911e9"
    )


@pytest.mark.parametrize(
    ("value", "pg_hex"),
    [
        (datetime(2000, 1, 1), "0000000000000000"),
        (datetime(2026, 10, 5, 21, 58, 30, 123456), "0003001dc047efc0"),
        (datetime(1970, 1, 1), "fffca2fec4c82000"),
        (datetime(1999, 12, 31, 23, 59, 59, 999999), "ffffffffffffffff"),
        (date(2024, 2, 29), "0002b578b58c6000"),
    ],
)
def test_timestamp_binary_matches_postgres(value: object, pg_hex: str) -> None:
    assert pgtypes.encode_value(value, "datetime", format_code=1) == bytes.fromhex(pg_hex)


def test_timestamp_binary_keeps_the_wall_clock_of_an_aware_value() -> None:
    aware = datetime(2026, 10, 5, 21, 58, 30, 123456, tzinfo=UTC)
    naive = datetime(2026, 10, 5, 21, 58, 30, 123456)
    assert pgtypes.encode_value(aware, "datetime", 1) == pgtypes.encode_value(naive, "datetime", 1)


def test_interval_has_no_binary_form_and_stays_text() -> None:
    from datetime import timedelta

    assert pgtypes.encode_value(timedelta(days=1), "datetime", format_code=1) == "1 day, 0:00:00"


def test_bool_and_bytea_binary() -> None:
    assert pgtypes.encode_value(True, "boolean", format_code=1) == b"\x01"
    assert pgtypes.encode_value(b"\x00\xff", "binary", format_code=1) == b"\x00\xff"


def test_text_binary_is_its_utf8_bytes() -> None:
    """psycopg 3 applies the first column's format to the whole row (#511)."""
    assert pgtypes.can_encode_binary("string")
    assert pgtypes.encode_value("Zürich", "string", format_code=1) == "Zürich".encode()
    assert pgtypes.encode_value(42, "string", format_code=1) == b"42"
