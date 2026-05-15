"""Tests for the natural-SQL → QueryObject translator.

Spec: design/PLAN_flight_natural_sql.md
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.sql_translator import SQLTranslationError, translate_sql_to_query
from orionbelt.models.query import FilterOperator, Grouping
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def model() -> SemanticModel:
    loader = TrackedLoader()
    raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
    resolver = ReferenceResolver()
    sm, result = resolver.resolve(raw, source_map)
    assert result.valid
    return sm


# --- happy path ---------------------------------------------------------------


def test_select_dim_and_measure(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM sample_model',
        model,
    )
    assert q.select.dimensions == ["Customer Country"]
    assert q.select.measures == ["Total Revenue"]
    assert q.grouping is None


def test_select_metric_counts_as_measure(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Revenue per Order" FROM sample_model',
        model,
    )
    assert q.select.measures == ["Revenue per Order"]


def test_case_insensitive_labels(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "customer country", "TOTAL REVENUE" FROM sample_model',
        model,
    )
    assert q.select.dimensions == ["Customer Country"]
    assert q.select.measures == ["Total Revenue"]


def test_where_on_dim_routes_to_where(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WHERE "Customer Country" = \'US\'',
        model,
    )
    assert len(q.where) == 1
    assert len(q.having) == 0
    f = q.where[0]
    assert isinstance(f.value, str) and f.value == "US"
    assert f.field == "Customer Country"
    assert f.op == FilterOperator.EQUALS


def test_where_on_measure_routes_to_having(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WHERE "Total Revenue" > 1000',
        model,
    )
    assert len(q.where) == 0
    assert len(q.having) == 1
    f = q.having[0]
    assert f.field == "Total Revenue"
    assert f.op == FilterOperator.GT
    assert f.value == 1000


def test_having_passes_through(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m HAVING "Total Revenue" > 1000',
        model,
    )
    assert len(q.having) == 1


def test_in_predicate(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m '
        "WHERE \"Customer Country\" IN ('US', 'CA', 'MX')",
        model,
    )
    f = q.where[0]
    assert f.op == FilterOperator.IN_LIST
    assert f.value == ["US", "CA", "MX"]


def test_is_null_predicate(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WHERE "Customer Country" IS NULL',
        model,
    )
    f = q.where[0]
    assert f.op == FilterOperator.IS_NULL


def test_like_predicate(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WHERE "Customer Country" LIKE \'U%\'',
        model,
    )
    f = q.where[0]
    assert f.op == FilterOperator.LIKE
    assert f.value == "U%"


def test_order_by_alias(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m ORDER BY "Total Revenue" DESC',
        model,
    )
    assert len(q.order_by) == 1
    assert q.order_by[0].field == "Total Revenue"
    assert q.order_by[0].direction.value == "desc"


def test_order_by_position(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m ORDER BY 2 DESC',
        model,
    )
    assert q.order_by[0].field == "Total Revenue"
    assert q.order_by[0].direction.value == "desc"


def test_limit(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country" FROM m LIMIT 50',
        model,
    )
    assert q.limit == 50


def test_group_by_ignored(model: SemanticModel) -> None:
    """Explicit GROUP BY in Semantic QL is silently accepted (no error)."""
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m GROUP BY "Customer Country"',
        model,
    )
    assert q.select.dimensions == ["Customer Country"]
    assert q.grouping is None


# --- raw mode (qualified columns) --------------------------------------------


def test_raw_mode_qualified_columns(model: SemanticModel) -> None:
    """Every SELECT item is `"DataObject"."column"` → raw mode."""
    q = translate_sql_to_query(
        'SELECT "Customers"."Customer ID", "Customers"."Country" FROM sample_model',
        model,
    )
    assert q.select.is_raw
    assert q.select.fields == ["Customers.Customer ID", "Customers.Country"]
    assert q.select.dimensions == []
    assert q.select.measures == []


def test_raw_mode_with_where_and_limit(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customers"."Customer ID" FROM m WHERE "Customers"."Country" = \'US\' LIMIT 50',
        model,
    )
    assert q.select.is_raw
    assert q.limit == 50
    assert len(q.where) == 1
    assert q.where[0].field == "Customers.Country"
    assert q.where[0].op == FilterOperator.EQUALS
    assert q.where[0].value == "US"


def test_raw_mode_with_distinct(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT DISTINCT "Customers"."Country" FROM m',
        model,
    )
    assert q.select.is_raw
    assert q.select.distinct is True


def test_raw_mode_order_by_qualified_and_position(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customers"."Customer ID", "Customers"."Country" FROM m '
        'ORDER BY "Customers"."Country" DESC, 1 ASC',
        model,
    )
    assert q.select.is_raw
    assert q.order_by[0].field == "Customers.Country"
    assert q.order_by[0].direction.value == "desc"
    assert q.order_by[1].field == "Customers.Customer ID"
    assert q.order_by[1].direction.value == "asc"


def test_raw_mode_having_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customers"."Country" FROM m HAVING COUNT(*) > 5',
            model,
        )
    msgs = [e.message for e in exc.value.errors]
    assert any("HAVING" in m for m in msgs)


def test_raw_mode_group_by_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customers"."Country" FROM m GROUP BY "Customers"."Country"',
            model,
        )
    msgs = [e.message for e in exc.value.errors]
    assert any("GROUP BY" in m for m in msgs)


def test_raw_mode_with_rollup_rejected(model: SemanticModel) -> None:
    """Regression: trailing ``WITH ROLLUP`` is stripped pre-parse but must
    still be rejected in raw mode. Previously silently dropped.
    """
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customers"."Country" FROM m WITH ROLLUP',
            model,
        )
    codes = [e.code for e in exc.value.errors]
    msgs = [e.message for e in exc.value.errors]
    assert "UNSUPPORTED_SQL_FEATURE" in codes
    assert any("ROLLUP" in m for m in msgs)


def test_raw_mode_with_cube_rejected(model: SemanticModel) -> None:
    """Symmetric to ROLLUP — trailing ``WITH CUBE`` rejected in raw mode."""
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customers"."Country" FROM m WITH CUBE',
            model,
        )
    msgs = [e.message for e in exc.value.errors]
    assert any("CUBE" in m for m in msgs)


def test_mixed_raw_and_aggregate_rejected(model: SemanticModel) -> None:
    """Qualified column + bare dim/measure → MIXED_RAW_AND_AGGREGATE_MODE."""
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customers"."Country", "Total Revenue" FROM m',
            model,
        )
    codes = [e.code for e in exc.value.errors]
    assert "MIXED_RAW_AND_AGGREGATE_MODE" in codes


def test_raw_mode_compiles_through_pipeline(model: SemanticModel) -> None:
    """A raw-mode QueryObject compiles via the pipeline's RawPlanner."""
    from orionbelt.compiler.pipeline import CompilationPipeline

    q = translate_sql_to_query(
        'SELECT "Customers"."Customer ID", "Customers"."Country" FROM m LIMIT 10',
        model,
    )
    result = CompilationPipeline().compile(q, model, "duckdb")
    # Raw mode emits SELECT without GROUP BY
    assert "SELECT" in result.sql.upper()
    assert "GROUP BY" not in result.sql.upper()
    assert "LIMIT" in result.sql.upper()


# --- MEASURE() syntax ---------------------------------------------------------


def test_measure_wrapper_unwraps_to_label(model: SemanticModel) -> None:
    """MEASURE(<label>) is the Snowflake SEMANTIC_VIEW / Databricks marker."""
    q = translate_sql_to_query(
        'SELECT "Customer Country", MEASURE("Total Revenue") FROM sample_model',
        model,
    )
    assert q.select.dimensions == ["Customer Country"]
    assert q.select.measures == ["Total Revenue"]


def test_measure_wrapper_case_insensitive(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", measure("Total Revenue") FROM m',
        model,
    )
    assert q.select.measures == ["Total Revenue"]


def test_measure_wrapper_with_rollup(model: SemanticModel) -> None:
    """MEASURE() composes with WITH ROLLUP."""
    q = translate_sql_to_query(
        'SELECT "Customer Country", MEASURE("Total Revenue") FROM m WITH ROLLUP',
        model,
    )
    assert q.select.measures == ["Total Revenue"]
    assert q.grouping == Grouping.ROLLUP


def test_measure_wrapper_unknown_label(model: SemanticModel) -> None:
    """MEASURE(<unknown>) still surfaces UNKNOWN_SELECT_ITEM."""
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query('SELECT MEASURE("Bogus") FROM m', model)
    assert any(e.code == "UNKNOWN_SELECT_ITEM" for e in exc.value.errors)


# --- aggregate wrap matching --------------------------------------------------


def test_sum_wrap_on_sum_measure_accepted(model: SemanticModel) -> None:
    """SUM(SUM-measure) matches the declared aggregation → stripped."""
    q = translate_sql_to_query(
        'SELECT "Customer Country", SUM("Total Revenue") FROM m',
        model,
    )
    assert q.select.measures == ["Total Revenue"]


def test_count_wrap_on_count_measure_accepted(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", COUNT("Order Count") FROM m',
        model,
    )
    assert q.select.measures == ["Order Count"]


def test_sum_wrap_on_count_measure_rejected(model: SemanticModel) -> None:
    """Wrap mismatch surfaces the declared aggregation in the error."""
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country", SUM("Order Count") FROM m',
            model,
        )
    msgs = [e.message for e in exc.value.errors]
    assert any("declared as `COUNT`" in m and "SUM" in m for m in msgs)


def test_min_wrap_on_sum_measure_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country", MIN("Total Revenue") FROM m',
            model,
        )
    msgs = [e.message for e in exc.value.errors]
    assert any("MIN" in m and "Total Revenue" in m for m in msgs)


def test_count_distinct_routes_to_count_distinct(model: SemanticModel) -> None:
    """COUNT(DISTINCT x) is treated as the count_distinct aggregation kind."""
    # Sample model has no count_distinct measure — verify mismatch error
    # cites the right declared kind.
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT COUNT(DISTINCT "Total Revenue") FROM m',
            model,
        )
    msgs = [e.message for e in exc.value.errors]
    # Total Revenue is SUM-declared; COUNT_DISTINCT wrap → mismatch
    assert any("Total Revenue" in m and ("COUNT" in m or "count" in m) for m in msgs)


def test_wrap_on_metric_rejected_with_clear_message(model: SemanticModel) -> None:
    """Aggregate wrappers on metrics always error — metrics have no
    single declared aggregation."""
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT SUM("Revenue per Order") FROM m',
            model,
        )
    msgs = [e.message for e in exc.value.errors]
    assert any(
        "Metric" in m and "Revenue per Order" in m and ("MEASURE" in m or "bare" in m) for m in msgs
    )


def test_wrap_on_dimension_rejected(model: SemanticModel) -> None:
    """Aggregating a dimension is never valid — dims are not measures."""
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT SUM("Customer Country") FROM m',
            model,
        )
    msgs = [e.message for e in exc.value.errors]
    assert any("dimension" in m.lower() and "Customer Country" in m for m in msgs)


def test_wrap_on_unknown_label_surfaces_unknown_select_item(
    model: SemanticModel,
) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query('SELECT SUM("Bogus") FROM m', model)
    assert any(e.code == "UNKNOWN_SELECT_ITEM" for e in exc.value.errors)


def test_sum_wrap_compiles_through_pipeline(model: SemanticModel) -> None:
    """The wrap is stripped — final SQL is identical to the bare form."""
    from orionbelt.compiler.pipeline import CompilationPipeline

    q_wrapped = translate_sql_to_query(
        'SELECT "Customer Country", SUM("Total Revenue") FROM m',
        model,
    )
    q_bare = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m',
        model,
    )
    sql_w = CompilationPipeline().compile(q_wrapped, model, "duckdb").sql
    sql_b = CompilationPipeline().compile(q_bare, model, "duckdb").sql
    assert sql_w == sql_b


# --- rollup / cube ------------------------------------------------------------


def test_trailing_with_rollup(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WITH ROLLUP',
        model,
    )
    assert q.grouping == Grouping.ROLLUP


def test_trailing_with_cube(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WITH CUBE',
        model,
    )
    assert q.grouping == Grouping.CUBE


def test_group_by_rollup_function_form(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m GROUP BY ROLLUP("Customer Country")',
        model,
    )
    assert q.grouping == Grouping.ROLLUP


def test_group_by_cube_function_form(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m GROUP BY CUBE("Customer Country")',
        model,
    )
    assert q.grouping == Grouping.CUBE


def test_group_by_with_rollup_trailing(model: SemanticModel) -> None:
    """MySQL/ClickHouse-style: GROUP BY dim WITH ROLLUP."""
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m GROUP BY "Customer Country" WITH ROLLUP',
        model,
    )
    assert q.grouping == Grouping.ROLLUP


# --- SQL comments -------------------------------------------------------------


def test_trailing_line_comment_after_with_cube(model: SemanticModel) -> None:
    """``WITH CUBE -- comment`` must classify as semantic, not RAW_SQL_REJECTED.

    The trailing-modifier regex looks for end-of-statement / ORDER BY /
    LIMIT etc. after ``WITH CUBE``; a trailing ``--`` line comment used
    to hide that marker, breaking sqlglot parsing downstream. Stripping
    comments first makes the modifier strip cleanly.
    """
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WITH CUBE -- ORDER BY 1 NULLS FIRST',
        model,
    )
    assert q.grouping == Grouping.CUBE


def test_trailing_line_comment_after_with_rollup(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WITH ROLLUP -- notes here',
        model,
    )
    assert q.grouping == Grouping.ROLLUP


def test_block_comment_between_clauses(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" /* picker hint */ FROM m WITH ROLLUP',
        model,
    )
    assert q.grouping == Grouping.ROLLUP


def test_multiline_block_comment(model: SemanticModel) -> None:
    """Block comments may span multiple lines (DBeaver pastes pretty-printed SQL)."""
    sql = (
        'SELECT "Customer Country", "Total Revenue"\n'
        "/* multi-line\n"
        "   comment block\n"
        "   spans newlines */\n"
        "FROM m WITH CUBE"
    )
    q = translate_sql_to_query(sql, model)
    assert q.grouping == Grouping.CUBE


def test_hash_line_comment(model: SemanticModel) -> None:
    """MySQL / BigQuery support ``#`` as a line comment."""
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WITH CUBE # MySQL-style comment',
        model,
    )
    assert q.grouping == Grouping.CUBE


def test_leading_hash_comment_line(model: SemanticModel) -> None:
    sql = '# header comment\nSELECT "Customer Country", "Total Revenue" FROM m'
    q = translate_sql_to_query(sql, model)
    assert q.grouping is None


def test_comment_inside_string_literal_preserved(model: SemanticModel) -> None:
    """A ``--`` inside a quoted string must NOT be treated as a comment."""
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WHERE "Customer Country" = \'A--B\'',
        model,
    )
    # If the literal had been corrupted by comment stripping, translation
    # would have failed. Reaching this line is the assertion.
    assert q.grouping is None


# --- rejections ---------------------------------------------------------------


def test_unknown_select_item(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query('SELECT "Bogus Column" FROM m', model)
    assert any(e.code == "UNKNOWN_SELECT_ITEM" for e in exc.value.errors)


def test_select_star_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query("SELECT * FROM m", model)
    assert any(
        e.code == "UNSUPPORTED_SQL_FEATURE" and "SELECT *" in e.message for e in exc.value.errors
    )


def test_join_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country" FROM m JOIN other ON 1 = 1',
            model,
        )
    assert any("JOIN" in e.message for e in exc.value.errors)


def test_cte_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'WITH cte AS (SELECT 1) SELECT "Customer Country" FROM cte',
            model,
        )
    assert any("CTE" in e.message or "WITH" in e.message for e in exc.value.errors)


def test_union_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country" FROM m UNION SELECT "Customer Country" FROM m',
            model,
        )
    assert any("UNION" in e.message for e in exc.value.errors)


def test_subquery_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country" FROM m WHERE "Customer Country" IN (SELECT 1)',
            model,
        )
    assert any("Subquer" in e.message for e in exc.value.errors)


def test_mismatched_aggregate_over_measure_rejected(model: SemanticModel) -> None:
    """Wraps must match the measure's declared aggregation (covered in detail
    by the dedicated wrap-matching tests). This guard pins the high-level
    expectation."""
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country", AVG("Total Revenue") FROM m',
            model,
        )
    assert any(e.code == "UNSUPPORTED_SQL_FEATURE" for e in exc.value.errors)


def test_unknown_order_by_field(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country" FROM m ORDER BY "Bogus"',
            model,
        )
    assert any(e.code == "UNKNOWN_ORDER_BY_FIELD" for e in exc.value.errors)


def test_invalid_order_by_position(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country" FROM m ORDER BY 99',
            model,
        )
    assert any(e.code == "INVALID_ORDER_BY_POSITION" for e in exc.value.errors)


def test_or_predicate_rejected(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Customer Country" FROM m '
            "WHERE \"Customer Country\" = 'US' OR \"Customer Country\" = 'CA'",
            model,
        )
    assert any("OR" in e.message for e in exc.value.errors)


def test_compiles_through_pipeline(model: SemanticModel) -> None:
    """Round-trip: translated QueryObject compiles successfully against the model."""
    from orionbelt.compiler.pipeline import CompilationPipeline

    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m '
        "WHERE \"Customer Country\" = 'US' "
        'ORDER BY "Total Revenue" DESC LIMIT 10',
        model,
    )
    result = CompilationPipeline().compile(q, model, "duckdb")
    assert "SELECT" in result.sql.upper()
    assert "ORDER BY" in result.sql.upper()
    assert "LIMIT" in result.sql.upper()


def test_compiles_with_rollup(model: SemanticModel) -> None:
    from orionbelt.compiler.pipeline import CompilationPipeline

    q = translate_sql_to_query(
        'SELECT "Customer Country", "Total Revenue" FROM m WITH ROLLUP',
        model,
    )
    result = CompilationPipeline().compile(q, model, "duckdb")
    assert "GROUP BY ROLLUP" in result.sql
    assert "GROUPING(" in result.sql
