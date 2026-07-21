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

    def test_driver_precision_scale_beats_narrow_first_batch(self):
        # A NUMERIC(40, 2) whose first batch is all small values must still be
        # typed wide enough (decimal256) from the driver-reported precision/scale
        # so a later 40-precision row doesn't overflow the reused schema (#136).
        from decimal import Decimal

        desc = (("amt", NUMBER, None, None, 40, 2, None),)  # description[4]=40, [5]=2
        schema = schema_from_description(desc, sample_rows=[(Decimal("1.23"),)])
        assert schema.field(0).type == pa.decimal256(76, 2)
        wide = Decimal("1" * 38 + ".50")  # precision 40 — arrives in a later batch
        assert rows_to_batch([(wide,)], schema).column(0).to_pylist()[0] == wide

    def test_driver_precision_over_76_degrades_to_float(self):
        # A driver-reported precision beyond decimal256's limit degrades to
        # float64 instead of raising during schema construction.
        from decimal import Decimal

        desc = (("amt", NUMBER, None, None, 100, 2, None),)
        schema = schema_from_description(desc, sample_rows=[(Decimal("1.23"),)])
        assert schema.field(0).type == pa.float64()

    def test_all_null_first_batch_uses_driver_precision_scale(self):
        # A NUMERIC(40, 2) whose first batch is entirely NULL (UNION ALL padding)
        # must still be typed decimal from the driver's precision/scale, so a
        # later Decimal isn't degraded to float64 (issue #136 P2).
        from decimal import Decimal

        desc = (("amt", NUMBER, None, None, 40, 2, None),)
        schema = schema_from_description(desc, sample_rows=[(None,)])
        assert schema.field(0).type == pa.decimal256(76, 2)
        wide = Decimal("1" * 38 + ".50")
        assert rows_to_batch([(wide,)], schema).column(0).to_pylist()[0] == wide

    def test_non_decimal_sampled_values_are_not_forced_to_decimal(self):
        # A numeric column reporting precision/scale but yielding int values
        # stays int64 — the driver width only applies to real decimal columns.
        desc = (("id", NUMBER, None, None, 10, 0, None),)
        schema = schema_from_description(desc, sample_rows=[(5,)])
        assert schema.field(0).type == pa.int64()


class TestDecimalArrowType:
    def test_within_decimal128_uses_max_headroom(self):
        # Inferred (non-exact): max precision of the chosen width so a later,
        # wider row can't overflow.
        assert decimal_arrow_type(20, 2) == pa.decimal128(38, 2)
        assert decimal_arrow_type(1, 0) == pa.decimal128(38, 0)

    def test_precision_over_38_uses_decimal256(self):
        # Precision beyond decimal128's 38 must widen to decimal256, not
        # overflow decimal128(38, s) (issue #136 P2a).
        assert decimal_arrow_type(40, 2) == pa.decimal256(76, 2)

    def test_precision_over_76_falls_back_to_float(self):
        # Beyond decimal256's limit — no Arrow decimal can hold it (P2b).
        assert decimal_arrow_type(100, 2) == pa.float64()
        assert decimal_arrow_type(100, 2, exact=True) == pa.float64()

    def test_exact_keeps_declared_precision(self):
        # Advertised schema from an authoritative declared type keeps precision.
        assert decimal_arrow_type(18, 2, exact=True) == pa.decimal128(18, 2)
        assert decimal_arrow_type(40, 2, exact=True) == pa.decimal256(40, 2)

    def test_high_precision_roundtrips_exactly(self):
        from decimal import Decimal

        val = Decimal("123456789012345678.90")
        schema = pa.schema([pa.field("x", decimal_arrow_type(20, 2))])
        batch = rows_to_batch([(val,)], schema)
        assert batch.column(0).to_pylist()[0] == val

    def test_sampled_precision_over_38_infers_decimal256(self):
        # A sampled value needing precision 40 must not be typed decimal128(38,2)
        # (which would raise in rows_to_batch) — P2a regression.
        from decimal import Decimal

        wide = Decimal("1" * 38 + ".50")  # 38 integer digits + scale 2
        desc = (("amt", NUMBER, None, None, None, None, None),)
        schema = schema_from_description(desc, sample_rows=[(wide,)])
        assert schema.field(0).type == pa.decimal256(76, 2)
        # And it actually builds + round-trips exactly.
        batch = rows_to_batch([(wide,)], schema)
        assert batch.column(0).to_pylist()[0] == wide


class TestRowsToBatch:
    def test_decimal_into_float_column_is_coerced(self):
        # The >76-precision fallback advertises float64; pyarrow can't build a
        # float array straight from Decimals, so rows_to_batch coerces them so
        # delivery degrades lossily instead of raising.
        from decimal import Decimal

        schema = pa.schema([pa.field("x", pa.float64())])
        batch = rows_to_batch([(Decimal("1.5"),), (None,)], schema)
        assert batch.column(0).to_pylist() == [1.5, None]

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
