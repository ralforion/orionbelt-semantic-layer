"""Unit tests for pgwire/types.py — OID mapping and text encoding."""

from __future__ import annotations

from datetime import UTC, date, datetime
from datetime import time as dt_time
from decimal import Decimal

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
    # NUMERIC stays text-only — no float8 binary path.
    assert pgtypes.can_encode_binary("decimal") is False


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
