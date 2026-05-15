"""Tests for Arrow conversion utilities."""

from __future__ import annotations

from unittest.mock import MagicMock

import pyarrow as pa

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, STRING
from ob_flight.converters import (
    cursor_to_batches,
    pep249_type_to_arrow,
    rows_to_batch,
    schema_from_description,
)


class TestPep249TypeToArrow:
    def test_number(self):
        assert pep249_type_to_arrow(NUMBER) == pa.float64()

    def test_string(self):
        assert pep249_type_to_arrow(STRING) == pa.utf8()

    def test_datetime(self):
        assert pep249_type_to_arrow(DATETIME) == pa.timestamp("us")

    def test_binary(self):
        assert pep249_type_to_arrow(BINARY) == pa.binary()

    def test_unknown_fallback(self):
        assert pep249_type_to_arrow("UNKNOWN") == pa.utf8()


class TestSchemaFromDescription:
    def test_basic(self):
        desc = (
            ("id", NUMBER, None, None, None, None, None),
            ("name", STRING, None, None, None, None, None),
            ("created", DATETIME, None, None, None, None, None),
        )
        schema = schema_from_description(desc)
        assert len(schema) == 3
        assert schema.field(0).name == "id"
        assert schema.field(0).type == pa.float64()
        assert schema.field(1).name == "name"
        assert schema.field(1).type == pa.utf8()
        assert schema.field(2).name == "created"
        assert schema.field(2).type == pa.timestamp("us")

    def test_single_column(self):
        desc = (("value", NUMBER, None, None, None, None, None),)
        schema = schema_from_description(desc)
        assert len(schema) == 1


class TestRowsToBatch:
    def test_basic(self):
        schema = pa.schema([pa.field("x", pa.float64()), pa.field("y", pa.utf8())])
        rows = [(1.0, "a"), (2.0, "b"), (3.0, "c")]
        batch = rows_to_batch(rows, schema)
        assert batch.num_rows == 3
        assert batch.num_columns == 2
        assert batch.column("x").to_pylist() == [1.0, 2.0, 3.0]
        assert batch.column("y").to_pylist() == ["a", "b", "c"]

    def test_empty_rows(self):
        schema = pa.schema([pa.field("x", pa.float64())])
        batch = rows_to_batch([], schema)
        assert batch.num_rows == 0


class TestCursorToBatches:
    def test_single_batch(self):
        schema = pa.schema([pa.field("n", pa.float64())])
        cursor = MagicMock()
        cursor.fetchmany.side_effect = [
            [(1.0,), (2.0,), (3.0,)],
            [],
        ]
        batches = cursor_to_batches(cursor, schema, batch_size=10)
        assert len(batches) == 1
        assert batches[0].num_rows == 3

    def test_multiple_batches(self):
        schema = pa.schema([pa.field("n", pa.float64())])
        cursor = MagicMock()
        cursor.fetchmany.side_effect = [
            [(1.0,), (2.0,)],
            [(3.0,)],
            [],
        ]
        batches = cursor_to_batches(cursor, schema, batch_size=2)
        assert len(batches) == 2
        assert batches[0].num_rows == 2
        assert batches[1].num_rows == 1

    def test_empty_cursor(self):
        schema = pa.schema([pa.field("n", pa.float64())])
        cursor = MagicMock()
        cursor.fetchmany.return_value = []
        batches = cursor_to_batches(cursor, schema)
        assert len(batches) == 0
