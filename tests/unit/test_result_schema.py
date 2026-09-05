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
    reconciliation_possible,
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

    def test_one_unreconcilable_column_does_not_drop_the_others(self) -> None:
        """Arrow casts a table whole, so a single bad column would otherwise
        cost every other reconciliation on the result."""
        table = pa.table(
            {
                "good": pa.array([1, 0], type=pa.int64()),
                "bad": pa.array([0, 7], type=pa.int64()),
            }
        )
        out, skipped = reconcile_to_declared(table, {"good": pa.bool_(), "bad": pa.bool_()})
        assert out.schema.field("good").type == pa.bool_()
        assert out.schema.field("bad").type == pa.int64()
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


class TestCacheHitWarnings:
    """A hit has to report the warnings its miss did.

    ``execution_result_from_data`` rebuilds a hit with ``raw_rows`` rather than
    an Arrow table - deliberately, because ``table_to_rows`` keeps native dates
    where the executor's row builder serialises them to ISO strings, so
    swapping it changes what a hit returns. It also leaves ``arrow_schema``
    None, so reconciling the *result* is a no-op. The table is reconciled
    instead, before the result is built.
    """

    def test_reconciling_the_rebuilt_result_finds_nothing(self) -> None:
        """The failure mode, pinned so the ordering requirement is explicit."""
        from orionbelt.api.query_cache import execution_result_from_data

        stored = pa.table({"flag": pa.array([0, 1, 7], type=pa.int64())})
        result = execution_result_from_data(stored, execution_time_ms=1.0)
        assert result.arrow_schema is None
        assert result.reconcile_to_declared({"flag": pa.bool_()}) == []

    def test_reconciling_the_table_recovers_the_skip(self) -> None:
        stored = pa.table({"flag": pa.array([0, 1, 7], type=pa.int64())})
        _, skips = reconcile_to_declared(stored, {"flag": pa.bool_()})
        assert [name for name, _ in skips] == ["flag"]

    def test_an_already_reconciled_hit_warns_about_nothing(self) -> None:
        stored = pa.table({"flag": pa.array([False, True], type=pa.bool_())})
        table, skips = reconcile_to_declared(stored, {"flag": pa.bool_()})
        assert skips == []
        assert table.schema.field("flag").type == pa.bool_()

    def test_the_hit_keeps_its_native_row_shape(self) -> None:
        """``table_to_rows`` semantics must survive the reconciliation."""
        import datetime

        from orionbelt.api.query_cache import execution_result_from_data

        stored = pa.table({"d": pa.array([datetime.date(2026, 8, 15)], type=pa.date32())})
        table, _ = reconcile_to_declared(stored, {"d": pa.date32()})
        assert execution_result_from_data(table, execution_time_ms=1.0).rows == [
            [datetime.date(2026, 8, 15)]
        ]


class TestCoalesceAliasMetadata:
    """Casting a coalesce alias is only half of it - the reported type has to
    agree, or the response contradicts its own data."""

    def test_the_type_map_is_query_aware(self, sales_model) -> None:
        from orionbelt.api.query_cache import build_type_map
        from orionbelt.models.query import QueryObject

        dim = next(iter(sales_model.dimensions))
        query = QueryObject.model_validate(
            {"select": {"dimensions": [{"as": "Any", "coalesce": [dim]}]}}
        )
        assert build_type_map(sales_model).get("Any") is None
        assert build_type_map(sales_model, query)["Any"] == build_type_map(sales_model)[dim]

    def test_a_plain_dimension_is_unaffected(self, sales_model) -> None:
        from orionbelt.api.query_cache import build_type_map
        from orionbelt.models.query import QueryObject

        dim = next(iter(sales_model.dimensions))
        query = QueryObject.model_validate({"select": {"dimensions": [dim]}})
        assert build_type_map(sales_model, query) == {
            **build_type_map(sales_model),
        }


class TestReconciliationNeverDowngrades:
    """A ``resultType`` names a family, not the coarsest member of it.

    Casting on any difference took a stored ``decimal128(38, 2)`` down to
    ``float64`` because the measure declares ``resultType: float``, rounding
    123456789012345678.90 to 1.2345678901234568e+17 on a cache hit. An engine
    answering more precisely than the declaration is not drift, and the
    existing decimal-precision contract test is what caught it.
    """

    def test_a_decimal_is_not_narrowed_to_a_declared_float(self) -> None:
        from decimal import Decimal

        table = pa.table(
            {"amount": pa.array([Decimal("123456789012345678.90")], type=pa.decimal128(38, 2))}
        )
        out, skipped = reconcile_to_declared(table, {"amount": pa.float64()})
        assert out.schema.field("amount").type == pa.decimal128(38, 2)
        assert out.column("amount")[0].as_py() == Decimal("123456789012345678.90")
        assert skipped == []

    def test_a_wider_integer_is_not_narrowed(self) -> None:
        table = pa.table({"n": pa.array([2**40], type=pa.int64())})
        out, _ = reconcile_to_declared(table, {"n": pa.int32()})
        assert out.schema.field("n").type == pa.int64()

    def test_a_zoned_timestamp_is_left_to_the_wall_clock_path(self) -> None:
        """ClickHouse's zoned-for-naive case is the same shape but needs the
        clock preserved value by value (#407); a plain cast converts to UTC and
        moves it."""
        import datetime

        table = pa.table(
            {
                "at": pa.array(
                    [datetime.datetime(2026, 8, 15, 13, 45, tzinfo=datetime.UTC)],
                    type=pa.timestamp("us", tz="UTC"),
                )
            }
        )
        out, skipped = reconcile_to_declared(table, {"at": pa.timestamp("us")})
        assert out.schema.field("at").type == pa.timestamp("us", tz="UTC")
        assert skipped == []

    def test_the_two_gaps_this_exists_for_still_reconcile(self) -> None:
        import datetime

        table = pa.table(
            {
                "flag": pa.array([1, 0], type=pa.int64()),
                "d": pa.array(
                    [datetime.date(2026, 8, 15), datetime.date(2026, 8, 16)], type=pa.date64()
                ),
            }
        )
        out, skipped = reconcile_to_declared(table, {"flag": pa.bool_(), "d": pa.date32()})
        assert out.schema.field("flag").type == pa.bool_()
        assert out.schema.field("d").type == pa.date32()
        assert skipped == []


class TestRestOutputDoesNotDependOnCacheHistory:
    """REST and pgwire may differ live; they may not differ through the cache.

    The surface difference is a deliberate boundary - pgwire exposes Postgres
    wire metadata, so changing an int64-ish result into a boolean-like one can
    move an advertised OID and change BI-client behaviour, which needs its own
    testing. But the two share a cache, so an entry pgwire warmed could be
    served to a REST client verbatim, and REST's answer would depend on who
    queried first rather than on REST's own contract.

    A raw-arrow hit is the only path that could leak it, because it ships the
    stored blob without decoding. It now decodes whenever the model declares a
    type reconciliation could act on.
    """

    def _pgwire_entry(self):
        """What pgwire stores: the engine's types, because it opts out."""
        return pa.table({"flag": pa.array([1, 0], type=pa.int64())})

    def test_a_raw_arrow_hit_will_not_pass_through_a_reconcilable_model(self) -> None:
        assert reconciliation_possible({"flag": pa.bool_()}) is True
        assert reconciliation_possible({"d": pa.date32()}) is True

    def test_zero_copy_survives_where_nothing_could_need_it(self) -> None:
        """The gate must not cost the common case its passthrough."""
        assert reconciliation_possible({}) is False
        assert (
            reconciliation_possible({"a": pa.utf8(), "b": pa.float64(), "t": pa.timestamp("us")})
            is False
        )

    def test_rest_reads_the_same_result_whoever_warmed_the_cache(self) -> None:
        declared = {"flag": pa.bool_()}
        from_pgwire, _ = reconcile_to_declared(self._pgwire_entry(), declared)
        rest_written, _ = reconcile_to_declared(self._pgwire_entry(), declared)
        from_rest, _ = reconcile_to_declared(rest_written, declared)
        assert from_pgwire.equals(from_rest)
        assert from_pgwire.column("flag").to_pylist() == [True, False]

    def test_the_unsafe_boolean_warns_either_way(self) -> None:
        """A hit reports what a miss would, whichever surface wrote it."""
        entry = pa.table({"flag": pa.array([0, 1, 7], type=pa.int64())})
        _, skips = reconcile_to_declared(entry, {"flag": pa.bool_()})
        assert [name for name, _ in skips] == ["flag"]


class TestEverySurfaceDeliversTheSameResult:
    """The reason pgwire reconciles rather than staying out of this.

    Left opted out, pgwire's *values* depended on which surface warmed the
    shared entry - `1`/`0` from its own miss, `True`/`False` from a REST one.
    Its OID was stable only because the sidecar drove it; the values were not.
    """

    _SIDECAR = [{"name": "flag", "type": "boolean", "format": None}]

    def _delivered(self, entry: pa.Table) -> tuple[int, list[str]]:
        from orionbelt.api.query_cache import execution_result_from_data
        from orionbelt.pgwire.types import encode_value, oid_for_type_hint

        table, _ = reconcile_to_declared(entry, {"flag": pa.bool_()})
        result = execution_result_from_data(table, execution_time_ms=1.0, columns=self._SIDECAR)
        hint = result.columns[0].type_hint
        return oid_for_type_hint(hint), [encode_value(r[0], hint) for r in result.rows]

    def test_a_declared_boolean_advertises_the_postgres_bool_oid(self) -> None:
        """It fell through to TEXT (25) because the coarse hint folded booleans
        into "string"."""
        from orionbelt.pgwire.types import OID_BOOL

        oid, _ = self._delivered(pa.table({"flag": pa.array([1, 0], type=pa.int64())}))
        assert oid == OID_BOOL

    def test_the_wire_values_are_postgres_booleans(self) -> None:
        _, wire = self._delivered(pa.table({"flag": pa.array([1, 0], type=pa.int64())}))
        assert wire == ["t", "f"]

    def test_the_answer_does_not_depend_on_who_warmed_the_entry(self) -> None:
        from_pgwire = self._delivered(pa.table({"flag": pa.array([1, 0], type=pa.int64())}))
        from_rest = self._delivered(pa.table({"flag": pa.array([True, False], type=pa.bool_())}))
        assert from_pgwire == from_rest


class TestBooleanIsItsOwnCoarseHint:
    """The hint fold that made a declared boolean arrive as TEXT."""

    def test_an_arrow_boolean_is_hinted_boolean(self) -> None:
        from orionbelt.service.db_executor import _arrow_type_to_hint

        assert _arrow_type_to_hint(pa.bool_()) == "boolean"

    def test_a_named_boolean_type_is_hinted_boolean(self) -> None:
        from orionbelt.service.db_executor import coarse_hint_from_type_name

        assert coarse_hint_from_type_name("boolean") == "boolean"
        assert coarse_hint_from_type_name("bool") == "boolean"

    def test_the_other_buckets_are_unmoved(self) -> None:
        """``bool`` shares no substring with them, but the ordering makes that
        independent of what is added to those lists later."""
        from orionbelt.service.db_executor import coarse_hint_from_type_name

        assert coarse_hint_from_type_name("timestamp") == "datetime"
        assert coarse_hint_from_type_name("numeric") == "number"
        assert coarse_hint_from_type_name("bytea") == "binary"
        assert coarse_hint_from_type_name("interval") == "datetime"

    def test_a_boolean_is_not_numeric(self) -> None:
        from orionbelt.service.value_formatting import is_numeric_type_hint

        assert is_numeric_type_hint("boolean") is False


class TestSkippedColumnsReportWhatTheyAre:
    """Metadata for a column reconciliation could not apply.

    Reporting the *declared* type there made the response contradict its own
    rows - `Tier -> boolean` beside values 0, 1, 7 - which is the class of bug
    this whole change exists to remove. The declaration is still carried, as a
    DECLARED_TYPE_NOT_APPLIED warning; the `type` field says what the data is.
    pgwire has always worked this way, since its hint follows the Arrow type
    rather than the model map, so this also makes the two surfaces agree.
    """

    def _columns(self, skipped: frozenset[str]):
        from orionbelt.api.services.query_execution import _columns_and_maps
        from orionbelt.service.db_executor import ColumnMeta

        class _Model:
            dimensions: dict = {}
            measures: dict = {}
            metrics: dict = {}
            settings = None

        cols = [ColumnMeta(name="Tier", type_hint="number")]
        return _columns_and_maps(_Model(), cols, None, skipped)

    def test_a_skipped_column_reports_its_actual_type(self) -> None:
        columns_meta, _, type_map = self._columns(frozenset({"Tier"}))
        assert columns_meta[0].type == "number"
        assert type_map["Tier"] == "number"

    def test_an_applied_column_still_reports_the_declared_type(self) -> None:
        """Only the skip list changes; everything else keeps model semantics."""
        columns_meta, _, _ = self._columns(frozenset())
        assert columns_meta[0].type == "number"  # no model entry, falls back to the hint

    def test_the_sidecar_agrees_with_the_response(self) -> None:
        """A hit rebuilds metadata from the sidecar, so it has to say the same."""
        from orionbelt.api.query_cache import build_result_columns
        from orionbelt.service.db_executor import ColumnMeta, ExecutionResult

        class _Model:
            dimensions: dict = {}
            measures: dict = {}
            metrics: dict = {}
            settings = None

        result = ExecutionResult(
            columns=[ColumnMeta(name="Tier", type_hint="number")],
            raw_rows=[[7]],
            row_count=1,
        )
        columns = build_result_columns(
            _Model(), result, type_map={"Tier": "boolean"}, skipped=frozenset({"Tier"})
        )
        assert columns[0].type == "number"
