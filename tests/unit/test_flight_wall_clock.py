"""A zoned column under a naive declaration is a wall clock, not an instant.

OBML declares two timestamp types and the difference is the whole point of the
pair: ``timestamp`` is a wall clock, ``timestamp_tz`` is an instant. Arrow
reconciles a zoned column with a naive declaration by converting to UTC, which
answers the second question when the first was asked.

ClickHouse is the engine that reaches it: it has no zone-free ``DateTime``, so
a declared ``timestamp`` arrives carrying the server's timezone. Measured on a
Berlin server, a stored 13:45 streamed as 11:45 while the same query over REST
answered ``2026-08-15T13:45:00+02:00``.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest

pytest.importorskip("ob_flight", reason="ob-flight-extension not installed")

from ob_flight.converters import rows_to_batch, schema_from_description  # noqa: E402
from ob_flight.server_execution import align_cached_table  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")


def _zoned(*values: dt.datetime) -> pa.Table:
    """A column as ClickHouse hands it back: local time, zone attached."""
    return pa.table({"When": pa.array(values, type=pa.timestamp("ms", tz="Europe/Berlin"))})


def _naive_schema() -> pa.Schema:
    """What Flight advertises for a dimension declaring ``timestamp``."""
    return pa.schema([pa.field("When", pa.timestamp("us"))])


def test_a_naive_declaration_keeps_the_wall_clock() -> None:
    table = _zoned(dt.datetime(2026, 8, 15, 13, 45, tzinfo=BERLIN))
    aligned = align_cached_table(table, _naive_schema())
    assert aligned.column(0)[0].as_py() == dt.datetime(2026, 8, 15, 13, 45)


def test_the_offset_is_read_at_each_value() -> None:
    """Which is why a single offset would not do.

    Berlin is +02:00 in August and +01:00 in January, so one subtraction over
    the column would be right for half of it. Both rows declare 13:45 and both
    have to arrive as 13:45.
    """
    table = _zoned(
        dt.datetime(2026, 8, 15, 13, 45, tzinfo=BERLIN),
        dt.datetime(2026, 1, 15, 13, 45, tzinfo=BERLIN),
    )
    aligned = align_cached_table(table, _naive_schema())
    assert [v.as_py() for v in aligned.column(0)] == [
        dt.datetime(2026, 8, 15, 13, 45),
        dt.datetime(2026, 1, 15, 13, 45),
    ]


def test_a_timestamp_tz_declaration_is_left_alone() -> None:
    """The other half of the pair, and the reason the guard reads the target.

    ``timestamp_tz`` is an instant: 13:45 in Berlin *is* 11:45 UTC, and
    converting it here would answer a different moment.
    """
    table = _zoned(dt.datetime(2026, 8, 15, 13, 45, tzinfo=BERLIN))
    schema = pa.schema([pa.field("When", pa.timestamp("us", tz="UTC"))])
    aligned = align_cached_table(table, schema)
    assert aligned.column(0)[0].as_py() == dt.datetime(2026, 8, 15, 11, 45, tzinfo=dt.UTC)


def test_a_naive_column_is_untouched() -> None:
    """The seven engines that answer naive already: nothing to reinterpret."""
    table = pa.table(
        {"When": pa.array([dt.datetime(2026, 8, 15, 13, 45)], type=pa.timestamp("ms"))}
    )
    aligned = align_cached_table(table, _naive_schema())
    assert aligned.schema.field(0).type == pa.timestamp("us")
    assert aligned.column(0)[0].as_py() == dt.datetime(2026, 8, 15, 13, 45)


def test_the_other_columns_still_align() -> None:
    """The guard runs inside the existing cast, not instead of it."""
    table = pa.table(
        {
            "When": pa.array(
                [dt.datetime(2026, 8, 15, 13, 45, tzinfo=BERLIN)],
                type=pa.timestamp("ms", tz="Europe/Berlin"),
            ),
            "Orders": pa.array([42], type=pa.int8()),
        }
    )
    schema = pa.schema([pa.field("When", pa.timestamp("us")), pa.field("Orders", pa.int64())])
    aligned = align_cached_table(table, schema)
    assert aligned.schema.equals(schema)
    assert aligned.column(0)[0].as_py() == dt.datetime(2026, 8, 15, 13, 45)
    assert aligned.column(1)[0].as_py() == 42


def _streamed(*values: dt.datetime) -> pa.Table:
    """The live path: rows out of a cursor, typed from the description.

    Flight never calls ``fetch_arrow_table``; it fetches rows and builds the
    batches itself, so this is where an aware value becomes an Arrow one.
    """
    description = [("When", None, None, None, None, None, None)]
    rows = [(value,) for value in values]
    schema = schema_from_description(description, sample_rows=rows)
    return pa.Table.from_batches([rows_to_batch(rows, schema)])


def test_an_aware_row_value_keeps_its_zone_through_inference() -> None:
    """Otherwise the conversion happens before anything can read the zone.

    ``_python_type_to_arrow`` typed every ``datetime`` as naive, so pyarrow
    converted 13:45+02:00 to 11:45 while building the batch - and the guard,
    which reads the source's zone, had nothing left to read.
    """
    table = _streamed(dt.datetime(2026, 8, 15, 13, 45, tzinfo=BERLIN))
    assert table.schema.field(0).type == pa.timestamp("us", tz="Europe/Berlin")
    assert table.column(0)[0].as_py() == dt.datetime(2026, 8, 15, 13, 45, tzinfo=BERLIN)


def test_the_live_path_ends_at_the_wall_clock() -> None:
    """Rows in, wall clock out, on both sides of a DST boundary."""
    table = _streamed(
        dt.datetime(2026, 8, 15, 13, 45, tzinfo=BERLIN),
        dt.datetime(2026, 1, 15, 13, 45, tzinfo=BERLIN),
    )
    aligned = align_cached_table(table, _naive_schema())
    assert [v.as_py() for v in aligned.column(0)] == [
        dt.datetime(2026, 8, 15, 13, 45),
        dt.datetime(2026, 1, 15, 13, 45),
    ]


def test_a_naive_row_value_stays_naive() -> None:
    """The seven engines that answer naive rows are untouched by the inference."""
    table = _streamed(dt.datetime(2026, 8, 15, 13, 45))
    assert table.schema.field(0).type == pa.timestamp("us")


def test_a_fixed_offset_is_labelled_as_one() -> None:
    """A driver that reports an offset rather than a zone still keeps it.

    One offset is all such a value knows, so a column spanning a DST change
    would carry the sampled row's offset - which is why the IANA key is
    preferred wherever a driver supplies one.
    """
    value = dt.datetime(2026, 8, 15, 13, 45, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert _streamed(value).schema.field(0).type == pa.timestamp("us", tz="+02:00")
