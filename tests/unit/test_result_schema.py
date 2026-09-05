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
    declared_arrow_types,
    declared_result_schema,
    obml_type_to_arrow,
    reconcile_to_declared,
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


class TestDeclaredArrowTypes:
    def test_maps_dimensions_and_measures_by_label(self, sales_model) -> None:
        types = declared_arrow_types(sales_model)
        assert types
        for label, dim in sales_model.dimensions.items():
            rt = getattr(getattr(dim, "result_type", None), "value", None)
            if rt:
                assert types[label] == obml_type_to_arrow(rt)

    def test_a_column_the_model_does_not_name_is_absent(self, sales_model) -> None:
        """Raw ``select.fields`` projections and GROUPING() flags are left alone."""
        assert "no_such_column" not in declared_arrow_types(sales_model)


class TestReconcileToDeclared:
    """The MySQL boolean and Dremio date cases, and the line around them."""

    def test_mysql_boolean_becomes_a_boolean(self) -> None:
        table = pa.table({"flag": pa.array([1, 0, None], type=pa.int64())})
        out, skipped = reconcile_to_declared(table, {"flag": pa.bool_()})
        assert out.column("flag").to_pylist() == [True, False, None]
        assert skipped == []

    def test_a_value_that_is_not_a_boolean_is_left_alone(self) -> None:
        """Arrow would map 7 to True. The model declared a boolean; a 7 says
        the column is not one yet, and asserting otherwise invents content."""
        table = pa.table({"flag": pa.array([0, 1, 7], type=pa.int64())})
        out, skipped = reconcile_to_declared(table, {"flag": pa.bool_()})
        assert out.column("flag").to_pylist() == [0, 1, 7]
        assert len(skipped) == 1
        assert skipped[0][0] == "flag"
        assert "other than 0 and 1" in skipped[0][1]

    def test_an_all_null_integer_column_reconciles(self) -> None:
        """Nothing contradicts the declaration, so the declaration stands."""
        table = pa.table({"flag": pa.array([None, None], type=pa.int64())})
        out, _ = reconcile_to_declared(table, {"flag": pa.bool_()})
        assert out.schema.field("flag").type == pa.bool_()

    def test_dremio_date64_narrows_to_date32(self) -> None:
        import datetime

        table = pa.table({"d": pa.array([datetime.date(2026, 8, 15)], type=pa.date64())})
        out, skipped = reconcile_to_declared(table, {"d": pa.date32()})
        assert out.schema.field("d").type == pa.date32()
        assert out.column("d").to_pylist() == [datetime.date(2026, 8, 15)]
        assert skipped == []

    def test_a_column_the_model_does_not_name_is_untouched(self) -> None:
        table = pa.table({"raw": pa.array([1, 2], type=pa.int64())})
        out, skipped = reconcile_to_declared(table, {})
        assert out.schema.field("raw").type == pa.int64()
        assert skipped == []

    def test_a_matching_column_is_a_no_op(self) -> None:
        table = pa.table({"n": pa.array([1], type=pa.int64())})
        out, skipped = reconcile_to_declared(table, {"n": pa.int64()})
        assert out is table
        assert skipped == []

    def test_one_impossible_cast_does_not_drop_the_others(self) -> None:
        """Arrow casts a table whole, so a single bad column would otherwise
        cost every other reconciliation on the result."""
        table = pa.table(
            {
                "good": pa.array([1, 0], type=pa.int64()),
                "bad": pa.array(["x", "y"], type=pa.string()),
            }
        )
        out, skipped = reconcile_to_declared(table, {"good": pa.bool_(), "bad": pa.timestamp("us")})
        assert out.schema.field("good").type == pa.bool_()
        assert out.schema.field("bad").type == pa.string()
        assert [name for name, _ in skipped] == ["bad"]


class TestExecutionResultReconciliation:
    def test_reconcile_updates_schema_and_hints(self) -> None:
        from orionbelt.service.db_executor import ColumnMeta, ExecutionResult

        table = pa.table({"flag": pa.array([1, 0], type=pa.int64())})
        result = ExecutionResult(
            columns=[ColumnMeta(name="flag", type_hint="number")],
            arrow_table=table,
            row_count=2,
        )
        assert result.reconcile_to_declared({"flag": pa.bool_()}) == []
        assert result.arrow_schema.field("flag").type == pa.bool_()
        assert result.rows == [[True], [False]]

    def test_a_pep249_result_has_nothing_to_reconcile(self) -> None:
        """No Arrow types to compare, and the coarse hint cannot tell a boolean
        from a string. Since #412 this path is the exception."""
        from orionbelt.service.db_executor import ColumnMeta, ExecutionResult

        result = ExecutionResult(
            columns=[ColumnMeta(name="flag", type_hint="number")],
            raw_rows=[[1]],
            row_count=1,
        )
        assert result.reconcile_to_declared({"flag": pa.bool_()}) == []
        assert result.rows == [[1]]


class TestReconciliationOrdering:
    """Reconciliation has to happen before anything reads ``rows``.

    ``rows`` materialises the Arrow table and frees it, so a later
    ``reconcile_to_declared`` finds nothing to cast and returns silently. The
    cacheable-miss path read ``rows`` to write the cache entry *before* the
    response builder reconciled, so with the cache enabled REST returned - and
    stored - the engine's types, and every existing test passed because they
    exercised the uncached path.
    """

    def _result(self):
        from orionbelt.service.db_executor import ColumnMeta, ExecutionResult

        return ExecutionResult(
            columns=[ColumnMeta(name="flag", type_hint="number")],
            arrow_table=pa.table({"flag": pa.array([1, 0], type=pa.int64())}),
            row_count=2,
        )

    def test_reconciling_after_rows_is_a_no_op(self) -> None:
        """The failure mode, pinned so the ordering requirement is explicit."""
        result = self._result()
        _ = result.rows
        assert result.reconcile_to_declared({"flag": pa.bool_()}) == []
        assert result.arrow_schema.field("flag").type == pa.int64()

    def test_reconciling_before_rows_reaches_the_rows(self) -> None:
        result = self._result()
        result.reconcile_to_declared({"flag": pa.bool_()})
        assert result.arrow_schema.field("flag").type == pa.bool_()
        assert result.rows == [[True], [False]]

    def test_the_cache_write_sees_the_reconciled_values(self) -> None:
        """``try_cache_set`` is handed ``rows`` and ``arrow_schema``; both have
        to be the declared ones or the entry outlives the fix."""
        result = self._result()
        result.reconcile_to_declared({"flag": pa.bool_()})
        assert result.arrow_schema.field("flag").type == pa.bool_()
        assert result.rows == [[True], [False]]


class TestCoalesceAliases:
    """A coalesce entry outputs its ``as`` alias, which is not a model
    dimension - so a model-only map never mentions it and the column keeps
    whatever the engine returned."""

    def test_a_coalesce_alias_is_absent_without_the_query(self, sales_model) -> None:
        assert "Any Country" not in declared_arrow_types(sales_model)

    def test_the_query_supplies_the_alias(self, sales_model) -> None:
        from orionbelt.models.query import QueryObject

        dim = next(iter(sales_model.dimensions))
        query = QueryObject.model_validate(
            {"select": {"dimensions": [{"as": "Any Country", "coalesce": [dim]}]}}
        )
        types = declared_arrow_types(sales_model, query)
        assert "Any Country" in types
        assert types["Any Country"] == types[dim]

    def test_a_plain_name_is_unaffected(self, sales_model) -> None:
        from orionbelt.models.query import QueryObject

        dim = next(iter(sales_model.dimensions))
        query = QueryObject.model_validate({"select": {"dimensions": [dim]}})
        assert (
            declared_arrow_types(sales_model, query)[dim] == declared_arrow_types(sales_model)[dim]
        )
