"""Tests for the Arrow IPC + gzip cache codec (``orionbelt.cache.result_codec``).

See ``design/PLAN_arrow_cache.md``. The codec stores ONLY row data as an
uncompressed Arrow IPC stream, gzip'd at the blob level. No response envelope is
baked in — metadata is rebuilt fresh on every read.
"""

from __future__ import annotations

import gzip

import pytest

pa = pytest.importorskip("pyarrow", reason="pyarrow required for the result codec")

from orionbelt.cache import result_codec  # noqa: E402

_COLUMN_NAMES = ["Country", "Revenue"]
_ROWS = [["US", 1234.5], ["UK", 6789.0]]


def test_encode_decode_round_trip() -> None:
    payload = result_codec.encode_data(_COLUMN_NAMES, _ROWS)
    table = result_codec.decode_data(payload)

    assert table.column_names == _COLUMN_NAMES
    assert table.num_rows == 2
    assert result_codec.table_to_rows(table) == _ROWS


def test_encode_table_preserves_schema() -> None:
    """``encode_table`` keeps the caller's exact Arrow types, unlike
    ``encode_data`` (which re-infers from values). An empty typed table must
    survive the round-trip with its schema intact — the case Flight relies on so
    a cache hit doesn't stream ``null``-typed columns for an empty result."""
    table = pa.table(
        {
            "id": pa.array([], type=pa.int64()),
            "amount": pa.array([], type=pa.float64()),
            "ts": pa.array([], type=pa.timestamp("us")),
            "name": pa.array([], type=pa.utf8()),
        }
    )
    decoded = result_codec.decode_data(result_codec.encode_table(table))

    assert decoded.num_rows == 0
    assert decoded.schema.field("id").type == pa.int64()
    assert decoded.schema.field("amount").type == pa.float64()
    assert decoded.schema.field("ts").type == pa.timestamp("us")
    assert decoded.schema.field("name").type == pa.utf8()


def test_encode_table_shares_byte_format_with_encode_data() -> None:
    """Both writers produce a blob ``decode_data`` reads, so any surface reads
    any other's entry regardless of which encoder wrote it."""
    table = result_codec.build_result_table(_COLUMN_NAMES, _ROWS)
    payload = result_codec.encode_table(table)

    assert payload[:2] == b"\x1f\x8b"  # gzip magic, same container as encode_data
    decoded = result_codec.decode_data(payload)
    assert decoded.column_names == _COLUMN_NAMES
    assert result_codec.table_to_rows(decoded) == _ROWS


def test_payload_is_gzip() -> None:
    """The blob is gzip'd at the transport/storage layer (§3)."""
    payload = result_codec.encode_data(_COLUMN_NAMES, _ROWS)
    assert payload[:2] == b"\x1f\x8b"  # gzip magic


def test_blob_holds_only_data_no_envelope_metadata() -> None:
    """The stored blob carries pure data — no ``obsl_`` envelope in the schema."""
    payload = result_codec.encode_data(_COLUMN_NAMES, _ROWS)
    table = result_codec.decode_data(payload)
    md = table.schema.metadata or {}
    assert not any(key.startswith(b"obsl_") for key in md)


def test_inner_stream_is_uncompressed_arrow_ipc() -> None:
    """Un-gzipping yields a plain, universally-readable IPC stream with no
    Arrow-level buffer compression (§4)."""
    payload = result_codec.encode_data(_COLUMN_NAMES, _ROWS)
    raw = gzip.decompress(payload)

    with pa.ipc.open_stream(pa.BufferReader(raw)) as reader:
        table = reader.read_all()

    assert table.num_rows == 2
    assert table.column_names == ["Country", "Revenue"]


def test_empty_rows_round_trips() -> None:
    payload = result_codec.encode_data(_COLUMN_NAMES, [])
    table = result_codec.decode_data(payload)
    assert table.num_rows == 0
    assert table.column_names == ["Country", "Revenue"]
    assert result_codec.table_to_rows(table) == []


def test_zero_columns_round_trips() -> None:
    payload = result_codec.encode_data([], [])
    table = result_codec.decode_data(payload)
    assert table.column_names == []
    assert result_codec.table_to_rows(table) == []


def test_build_result_table_pads_short_rows() -> None:
    table = result_codec.build_result_table(["a", "b", "c"], [[1], [2, 3]])
    assert table.column_names == ["a", "b", "c"]
    assert table.to_pylist() == [
        {"a": 1, "b": None, "c": None},
        {"a": 2, "b": 3, "c": None},
    ]


def test_to_ipc_stream_is_readable_by_pyarrow() -> None:
    table = result_codec.build_result_table(["x"], [[1], [2], [3]])
    raw = result_codec.to_ipc_stream(table)
    with pa.ipc.open_stream(pa.BufferReader(raw)) as reader:
        got = reader.read_all()
    assert got.to_pylist() == [{"x": 1}, {"x": 2}, {"x": 3}]


def test_decode_data_is_shared_across_surfaces() -> None:
    """``decode_data`` reads what ``encode_data`` wrote — one blob format shared
    across REST / pgwire / Flight (single-entry cache)."""
    payload = result_codec.encode_data(_COLUMN_NAMES, _ROWS)
    table = result_codec.decode_data(payload)
    assert table.column_names == ["Country", "Revenue"]
    assert table.to_pylist() == [
        {"Country": "US", "Revenue": 1234.5},
        {"Country": "UK", "Revenue": 6789.0},
    ]


def test_table_to_rows_preserves_schema_order() -> None:
    table = result_codec.build_result_table(["x", "y"], [[1, 2], [3, 4]])
    assert result_codec.table_to_rows(table) == [[1, 2], [3, 4]]


# ---------------------------------------------------------------------------
# The driver schema decides the column types
# ---------------------------------------------------------------------------

_DECIMAL_SCHEMA = pa.schema(
    [
        pa.field("Amount", pa.decimal128(18, 2)),
        pa.field("Orders", pa.int64()),
        pa.field("Ordered At", pa.timestamp("us")),
        pa.field("Shipped", pa.bool_()),
    ]
)
_DECIMAL_NAMES = ["Amount", "Orders", "Ordered At", "Shipped"]


def test_declared_decimal_width_survives_narrow_values() -> None:
    """A ``decimal(18, 2)`` measure holding 1.50 stays ``decimal128(18, 2)``.

    Inference reads the digits that happen to be present and answered
    ``decimal128(3, 2)``, so the blob advertised a narrower column than the
    model declares.
    """
    from decimal import Decimal

    rows = [[Decimal("1.50"), 3, "2026-08-15T13:45:00", True]]
    table = result_codec.build_result_table(_DECIMAL_NAMES, rows, _DECIMAL_SCHEMA)

    assert table.schema.field("Amount").type == pa.decimal128(18, 2)
    assert table.to_pylist()[0]["Amount"] == Decimal("1.50")


def test_declared_width_is_stable_across_result_sets() -> None:
    """The same column answers the same Arrow type whatever rows it returns.

    This is the property, not the width itself: a consumer that read the schema
    from one result was wrong about the next, which is the failure #393 fixed on
    MySQL and #407 on Snowflake.
    """
    from decimal import Decimal

    narrow = result_codec.build_result_table(
        _DECIMAL_NAMES, [[Decimal("1.50"), 3, "2026-08-15T13:45:00", True]], _DECIMAL_SCHEMA
    )
    wide = result_codec.build_result_table(
        _DECIMAL_NAMES, [[Decimal("123456.78"), 4, "2026-08-15T14:45:00", False]], _DECIMAL_SCHEMA
    )
    empty = result_codec.build_result_table(_DECIMAL_NAMES, [], _DECIMAL_SCHEMA)

    assert narrow.schema == wide.schema == empty.schema


def test_temporals_still_serialize_as_strings() -> None:
    """The declared type is mapped through serialisation first, so a
    ``timestamp[us]`` column keeps the ISO string the executor produced rather
    than a declaration no serialised row could satisfy."""
    rows = [[None, None, "2026-08-15T13:45:00", None]]
    table = result_codec.build_result_table(_DECIMAL_NAMES, rows, _DECIMAL_SCHEMA)

    assert table.schema.field("Ordered At").type == pa.string()
    assert table.to_pylist()[0]["Ordered At"] == "2026-08-15T13:45:00"


def test_values_that_outgrow_the_declaration_fall_back_to_inference() -> None:
    """A declared type is offered, never forced: pyarrow raises rather than
    coercing, and the column is inferred instead of failing the encode."""
    from decimal import Decimal

    schema = pa.schema([pa.field("Amount", pa.decimal128(18, 2))])
    table = result_codec.build_result_table(["Amount"], [[Decimal("1.5678")]], schema)

    assert table.to_pylist() == [{"Amount": Decimal("1.5678")}]


def test_string_backed_numeric_falls_back_to_decimal() -> None:
    """PostgreSQL NUMERIC arrives as a string-backed extension type whose cells
    reach the codec as ``Decimal``. The offered ``string`` is refused, so the
    column is inferred as a decimal - what it was before the schema decided
    types, and the value is unchanged either way."""
    from decimal import Decimal

    schema = pa.schema([pa.field("Amount", pa.string())])
    table = result_codec.build_result_table(["Amount"], [[Decimal("1.50")]], schema)

    assert pa.types.is_decimal(table.schema.field("Amount").type)
    assert table.to_pylist() == [{"Amount": Decimal("1.50")}]


def test_types_are_inferred_without_a_schema() -> None:
    """The PEP 249 path has no Arrow schema, and the ``format_values`` arrow
    response deliberately passes none. Both keep value inference."""
    from decimal import Decimal

    table = result_codec.build_result_table(["Amount"], [[Decimal("1.50")]])

    assert table.schema.field("Amount").type == pa.decimal128(3, 2)


def test_declared_types_survive_the_cache_round_trip() -> None:
    """What the schema decided is what a cache hit reads back."""
    from decimal import Decimal

    rows = [[Decimal("1.50"), 3, "2026-08-15T13:45:00", True]]
    decoded = result_codec.decode_data(
        result_codec.encode_data(_DECIMAL_NAMES, rows, _DECIMAL_SCHEMA)
    )

    assert decoded.schema.field("Amount").type == pa.decimal128(18, 2)
    assert decoded.schema.field("Orders").type == pa.int64()
    assert result_codec.table_to_rows(decoded) == rows
