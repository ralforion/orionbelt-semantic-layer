"""Tests for Arrow conversion utilities."""

from __future__ import annotations

from unittest.mock import MagicMock

import pyarrow as pa

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, STRING
from ob_flight.converters import (
    cursor_to_batches,
    decimal_arrow_type,
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

    def test_decimal_sample_infers_decimal_not_float(self):
        # A Decimal sample value must produce an Arrow decimal so high-precision
        # NUMERIC survives instead of rounding through float64 (issue #136).
        from decimal import Decimal

        desc = (("amt", NUMBER, None, None, None, None, None),)
        rows = [(Decimal("123456789012345678.90"),)]
        schema = schema_from_description(desc, sample_rows=rows)
        assert schema.field(0).type == pa.decimal128(38, 2)

    def test_decimal_scale_covers_widest_sample(self):
        # The Arrow scale is taken from the widest sampled value.
        from decimal import Decimal

        desc = (("amt", NUMBER, None, None, None, None, None),)
        rows = [(Decimal("1.5"),), (Decimal("1.500"),), (Decimal("1.50"),)]
        schema = schema_from_description(desc, sample_rows=rows)
        assert schema.field(0).type == pa.decimal128(38, 3)

    def test_all_null_decimal_column_falls_back_to_type_code(self):
        # No sample value → PEP 249 type code path (unchanged float64).
        desc = (("amt", NUMBER, None, None, None, None, None),)
        schema = schema_from_description(desc, sample_rows=[(None,)])
        assert schema.field(0).type == pa.float64()


class TestDecimalArrowType:
    def test_common_scales_use_decimal128(self):
        assert decimal_arrow_type(2) == pa.decimal128(38, 2)
        assert decimal_arrow_type(0) == pa.decimal128(38, 0)

    def test_large_scale_uses_decimal256(self):
        assert decimal_arrow_type(40) == pa.decimal256(76, 40)

    def test_pathological_scale_falls_back_to_float(self):
        assert decimal_arrow_type(200) == pa.float64()

    def test_high_precision_roundtrips_exactly(self):
        from decimal import Decimal

        val = Decimal("123456789012345678.90")
        schema = pa.schema([pa.field("x", decimal_arrow_type(2))])
        batch = rows_to_batch([(val,)], schema)
        assert batch.column(0).to_pylist()[0] == val


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
