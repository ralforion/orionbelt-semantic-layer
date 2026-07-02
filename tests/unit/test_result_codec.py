"""Tests for the Arrow IPC + gzip cache codec (``orionbelt.cache.result_codec``).

See ``design/PLAN_arrow_cache.md``. The codec stores an uncompressed Arrow IPC
stream (envelope packed into schema metadata) gzip'd at the blob level.
"""

from __future__ import annotations

import gzip

import pytest

pa = pytest.importorskip("pyarrow", reason="pyarrow required for the result codec")

from orionbelt.cache import result_codec  # noqa: E402

_SAMPLE = dict(
    columns=[
        {"name": "Country", "type": "string", "format": None},
        {"name": "Revenue", "type": "decimal(18, 2)", "format": "#,##0.00"},
    ],
    rows=[["US", 1234.5], ["UK", 6789.0]],
    sql="SELECT country, SUM(revenue) FROM sales GROUP BY country",
    dialect="duckdb",
    explain=None,
    warnings=[],
    sql_valid=True,
    execution_time_ms=12.5,
    timezone="UTC",
    resolved={"fact_tables": ["SALES"], "dimensions": ["Country"], "measures": ["Revenue"]},
    physical_tables=["WH.PUBLIC.SALES"],
)


def test_encode_decode_round_trip() -> None:
    payload = result_codec.encode(**_SAMPLE)
    env = result_codec.decode(payload)

    assert env.columns == _SAMPLE["columns"]
    assert env.rows == _SAMPLE["rows"]
    assert env.sql == _SAMPLE["sql"]
    assert env.dialect == "duckdb"
    assert env.explain is None
    assert env.warnings == []
    assert env.sql_valid is True
    assert env.execution_time_ms == 12.5
    assert env.timezone == "UTC"
    assert env.resolved == _SAMPLE["resolved"]
    assert env.physical_tables == ["WH.PUBLIC.SALES"]
    assert env.row_count == 2


def test_payload_is_gzip() -> None:
    """The blob is gzip'd at the transport/storage layer (§3)."""
    payload = result_codec.encode(**_SAMPLE)
    assert payload[:2] == b"\x1f\x8b"  # gzip magic


def test_inner_stream_is_uncompressed_arrow_ipc() -> None:
    """Un-gzipping yields a plain, universally-readable IPC stream with the
    envelope in schema metadata and no Arrow-level buffer compression (§4)."""
    payload = result_codec.encode(**_SAMPLE)
    raw = gzip.decompress(payload)

    with pa.ipc.open_stream(pa.BufferReader(raw)) as reader:
        table = reader.read_all()

    assert table.num_rows == 2
    assert table.column_names == ["Country", "Revenue"]
    md = table.schema.metadata or {}
    assert b"obsl_sql" in md
    assert b"obsl_columns" in md


def test_empty_rows_round_trips() -> None:
    sample = dict(_SAMPLE, rows=[])
    env = result_codec.decode(result_codec.encode(**sample))
    assert env.rows == []
    assert env.row_count == 0
    assert [c["name"] for c in env.columns] == ["Country", "Revenue"]


def test_zero_columns_round_trips() -> None:
    sample = dict(_SAMPLE, columns=[], rows=[])
    env = result_codec.decode(result_codec.encode(**sample))
    assert env.columns == []
    assert env.rows == []


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


def test_decode_table_reads_encode_output() -> None:
    """The Flight surface's ``decode_table`` reads what REST's ``encode`` wrote —
    one blob format shared across surfaces (single-entry cache)."""
    payload = result_codec.encode(**_SAMPLE)
    table = result_codec.decode_table(payload)
    assert table.column_names == ["Country", "Revenue"]
    assert table.to_pylist() == [
        {"Country": "US", "Revenue": 1234.5},
        {"Country": "UK", "Revenue": 6789.0},
    ]


def test_decode_survives_missing_metadata() -> None:
    """A bare IPC stream (no obsl_ envelope) decodes to sensible defaults."""
    table = result_codec.build_result_table(["x"], [[1]])
    raw = result_codec.to_ipc_stream(table)
    env = result_codec.decode(gzip.compress(raw))
    assert env.sql == ""
    assert env.columns == []
    # Falls back to the table's own column names / row shape.
    assert env.rows == [[1]]
    assert env.row_count == 1
