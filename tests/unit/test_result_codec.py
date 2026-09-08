"""Tests for the Arrow IPC + gzip cache codec (``orionbelt.cache.result_codec``).

See ``design/PLAN_arrow_cache.md``. The codec stores ONLY row data as an
uncompressed Arrow IPC stream, gzip'd at the blob level. No response envelope is
baked in — metadata is rebuilt fresh on every read.
"""

from __future__ import annotations

import gzip
from typing import Any

import pytest

pa = pytest.importorskip("pyarrow", reason="pyarrow required for the result codec")

from orionbelt.cache import result_codec  # noqa: E402

_COLUMN_NAMES = ["Country", "Revenue"]
_ROWS = [["US", 1234.5], ["UK", 6789.0]]


def _rows(table: Any) -> list[list[Any]]:
    """A decoded table as list-of-lists.

    Local because production no longer has one: a cache hit is table-backed
    and materialises through ``ExecutionResult.rows``, the same serialiser a
    miss runs. ``result_codec.table_to_rows`` existed only to read the rows a
    cache entry stored *pre-serialised*, and nothing stores those any more.
    """
    names = table.column_names
    return [[row.get(n) for n in names] for row in table.to_pylist()]


def test_encode_decode_round_trip() -> None:
    payload = result_codec.encode_data(_COLUMN_NAMES, _ROWS)
    table = result_codec.decode_data(payload)

    assert table.column_names == _COLUMN_NAMES
    assert table.num_rows == 2
    assert _rows(table) == _ROWS


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
    assert _rows(decoded) == _ROWS


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
    assert _rows(table) == []


def test_zero_columns_round_trips() -> None:
    payload = result_codec.encode_data([], [])
    table = result_codec.decode_data(payload)
    assert table.column_names == []
    assert _rows(table) == []


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


def test_decoded_rows_keep_schema_order() -> None:
    table = result_codec.build_result_table(["x", "y"], [[1, 2], [3, 4]])
    assert _rows(table) == [[1, 2], [3, 4]]


# ---------------------------------------------------------------------------
# The driver's table is what gets stored
# ---------------------------------------------------------------------------
#
# These replace a block that tested a schema *hint*: ``build_result_table``
# used to take the driver's Arrow schema and offer each column's type to
# values that had already been serialised, falling back to inference when the
# offer was refused. That existed because the writer only had rows. It now has
# the table, so the width is not offered - it is what is stored.


def _driver_table() -> Any:
    """One column of every type the two encoders used to disagree about."""
    import datetime as dt
    from decimal import Decimal

    return pa.table(
        {
            "Amount": pa.array([Decimal("1.50"), None], pa.decimal128(18, 2)),
            "Orders": pa.array([1, None], pa.int64()),
            "Ordered At": pa.array([dt.datetime(2024, 1, 2, 3, 4, 5), None], pa.timestamp("us")),
            "Shipped On": pa.array([dt.date(2024, 1, 2), None], pa.date32()),
            "Blob": pa.array([b"\x00\x01", None], pa.binary()),
            "Empty": pa.array([None, None], pa.int64()),
        }
    )


def test_the_stored_blob_is_the_drivers_table() -> None:
    """Every type survives, including the three that used to be flattened.

    ``timestamp`` and ``date`` were stored as ISO ``string`` and ``binary`` as
    base64 ``string``, because the writer encoded already-serialised rows.
    Flight, which stored its table verbatim, wrote native types into the same
    key - so whoever wrote first decided what the other read.
    """
    decoded = result_codec.decode_data(result_codec.encode_table(_driver_table()))

    assert decoded.schema.field("Amount").type == pa.decimal128(18, 2)
    assert decoded.schema.field("Orders").type == pa.int64()
    assert decoded.schema.field("Ordered At").type == pa.timestamp("us")
    assert decoded.schema.field("Shipped On").type == pa.date32()
    assert decoded.schema.field("Blob").type == pa.binary()
    assert decoded.schema.field("Empty").type == pa.int64(), "an empty column kept its type"


def test_a_declared_width_is_stable_across_result_sets() -> None:
    """The failure the old schema hint existed to prevent, now structural.

    Inference reads the values present, so the same ``decimal(18, 2)`` column
    came back a different width for a different filter and a consumer that
    read the schema once was wrong about the next result.
    """
    from decimal import Decimal

    schema = pa.schema([pa.field("Amount", pa.decimal128(18, 2))])
    narrow = pa.table({"Amount": pa.array([Decimal("1.50")], pa.decimal128(18, 2))})
    wide = pa.table({"Amount": pa.array([Decimal("12345.67")], pa.decimal128(18, 2))})
    empty = pa.table({"Amount": pa.array([], pa.decimal128(18, 2))})

    for table in (narrow, wide, empty):
        decoded = result_codec.decode_data(result_codec.encode_table(table))
        assert decoded.schema.field("Amount").type == schema.field("Amount").type


def test_an_adbc_opaque_numeric_survives_the_round_trip() -> None:
    """PostgreSQL NUMERIC under ADBC, which carries no width to read.

    It used to be inferred - ``decimal128`` where the column had values and
    ``null`` where it did not - because the extension type never reached the
    blob. It does now, and the executor parses its cells back to ``Decimal``
    on the way out exactly as it does on a miss.
    """
    opaque = pa.opaque(pa.string(), "numeric", "PostgreSQL")
    table = pa.table({"Amount": pa.array(["1.50", None], pa.string()).cast(opaque)})

    decoded = result_codec.decode_data(result_codec.encode_table(table))
    assert decoded.schema.field("Amount").type == opaque
    assert decoded.column("Amount").to_pylist() == ["1.50", None]


def test_the_fallback_still_infers_from_values() -> None:
    """``encode_data`` is what a result with no Arrow table gets, and it is
    honest about being inference: no schema is offered because none exists."""
    from decimal import Decimal

    table = result_codec.build_result_table(["Amount"], [[Decimal("1.50")]])
    assert pa.types.is_decimal(table.schema.field("Amount").type)
    assert _rows(result_codec.decode_data(result_codec.encode_data(["x"], [[1]]))) == [[1]]
