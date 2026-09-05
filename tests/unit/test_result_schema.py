"""The declared result schema, shared by every surface.

This mapping decided a column's type on Flight and nowhere else, which is why
MySQL's ``boolean`` still reaches REST as ``1`` and Dremio's ``date`` as
``date64[ms]``. Moving it into core is the step that lets REST and pgwire
reconcile against the declaration too; these tests pin the behaviour so the
move is provably behaviour-neutral and the later surfaces build on a fixed
contract.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from orionbelt.service.result_schema import (
    DECIMAL128_MAX_PRECISION,
    DECIMAL256_MAX_PRECISION,
    decimal_arrow_type,
    declared_result_schema,
    obml_type_to_arrow,
)


class TestObmlTypeToArrow:
    @pytest.mark.parametrize(
        ("obml", "expected"),
        [
            ("string", pa.utf8()),
            ("json", pa.utf8()),
            ("int", pa.int64()),
            ("float", pa.float64()),
            ("boolean", pa.bool_()),
            ("date", pa.date32()),
            ("timestamp", pa.timestamp("us")),
            ("timestamp_tz", pa.timestamp("us", tz="UTC")),
            ("time", pa.utf8()),
            ("time_tz", pa.utf8()),
        ],
    )
    def test_every_obml_type_maps(self, obml: str, expected: pa.DataType) -> None:
        assert obml_type_to_arrow(obml) == expected

    def test_the_full_obml_enum_is_covered(self) -> None:
        """A new OBML type must not silently become utf8."""
        from orionbelt.models.semantic import DataType

        for member in DataType:
            assert obml_type_to_arrow(member.value) != pa.utf8() or member.value in {
                "string",
                "json",
                "time",
                "time_tz",
            }

    def test_unknown_and_empty_default_to_utf8(self) -> None:
        assert obml_type_to_arrow(None) == pa.utf8()
        assert obml_type_to_arrow("no_such_type") == pa.utf8()

    def test_boolean_maps_to_bool_which_is_the_mysql_gap(self) -> None:
        """MySQL returns a declared boolean as int64; this is what it should be."""
        assert obml_type_to_arrow("boolean") == pa.bool_()

    def test_date_maps_to_date32_which_is_the_dremio_gap(self) -> None:
        """Dremio returns date64[ms]; the other seven give date32[day]."""
        assert obml_type_to_arrow("date") == pa.date32()


class TestDecimalArrowType:
    def test_exact_keeps_the_declared_precision(self) -> None:
        assert decimal_arrow_type(18, 2, exact=True) == pa.decimal128(18, 2)

    def test_inexact_uses_the_width_maximum(self) -> None:
        """A sampled precision can under-represent a later row."""
        assert decimal_arrow_type(18, 2) == pa.decimal128(DECIMAL128_MAX_PRECISION, 2)

    def test_widens_to_decimal256_past_38(self) -> None:
        assert decimal_arrow_type(50, 4, exact=True) == pa.decimal256(50, 4)

    def test_degrades_to_float_past_arrow_s_limit(self) -> None:
        """OBML permits a width Arrow cannot hold; schema building must not raise."""
        assert decimal_arrow_type(DECIMAL256_MAX_PRECISION + 1, 2) == pa.float64()

    def test_scale_and_precision_are_floored_sanely(self) -> None:
        assert decimal_arrow_type(0, -1, exact=True) == pa.decimal128(1, 0)


class TestDeclaredResultSchema:
    def test_names_and_types_the_selected_columns(self, sales_model) -> None:
        from orionbelt.models.query import QueryObject

        query = QueryObject.model_validate(
            {"select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}}
        )
        schema = declared_result_schema(query, sales_model)
        assert schema.names == ["Customer Country", "Total Revenue"]
        assert schema.field("Customer Country").type == pa.utf8()

    def test_a_grain_request_is_named_by_its_dimension(self, sales_model) -> None:
        """``At:day`` compiles to ``AS "At"``; the schema has to agree or it
        names a column the result does not have."""
        from orionbelt.models.query import QueryObject

        dim = next(
            (n for n, d in sales_model.dimensions.items() if "date" in str(d.result_type)),
            None,
        )
        if dim is None:
            pytest.skip("fixture has no temporal dimension")
        query = QueryObject.model_validate({"select": {"dimensions": [f"{dim}:month"]}})
        assert declared_result_schema(query, sales_model).names == [dim]
