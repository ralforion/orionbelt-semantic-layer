"""Tests for filtered measures — CASE WHEN wrapping of measure aggregations."""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import BinaryOp, CaseExpr, ColumnRef, FunctionCall, Literal
from orionbelt.compiler.filters import (
    build_measure_filter_condition,
    collect_measure_filter_objects,
)
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import (
    DataColumnRef,
    DataType,
    FilterLogic,
    FilterValue,
    MeasureFilter,
    MeasureFilterGroup,
    SemanticModel,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# ---------------------------------------------------------------------------
# Inline model YAML for filtered measure tests
# ---------------------------------------------------------------------------

_FILTERED_MODEL_YAML = """\
version: "1.0"
dataObjects:
  Customers:
    code: CUSTOMERS
    database: WH
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string
      Segment:
        code: SEGMENT
        abstractType: string
  Orders:
    code: ORDERS
    database: WH
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Price:
        code: PRICE
        abstractType: float
      Quantity:
        code: QUANTITY
        abstractType: int
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Country:
    dataObject: Customers
    column: Country
    resultType: string
measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'
  US Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'
    filters:
      - column: {dataObject: Customers, column: Country}
        operator: equals
        values: [{dataType: string, valueString: "US"}]
  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count
  US Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count
    filters:
      - column: {dataObject: Customers, column: Country}
        operator: equals
        values: [{dataType: string, valueString: "US"}]
  Segment Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'
    filters:
      - column: {dataObject: Customers, column: Country}
        operator: equals
        values: [{dataType: string, valueString: "US"}]
      - column: {dataObject: Customers, column: Segment}
        operator: equals
        values: [{dataType: string, valueString: "Consumer"}]
  OR Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'
    filters:
      - logic: or
        filters:
          - column: {dataObject: Customers, column: Country}
            operator: equals
            values: [{dataType: string, valueString: "US"}]
          - column: {dataObject: Customers, column: Country}
            operator: equals
            values: [{dataType: string, valueString: "CA"}]
metrics:
  US Revenue Share:
    expression: "{[US Revenue]} / {[Revenue]}"
"""


def _load_model(yaml: str = _FILTERED_MODEL_YAML) -> SemanticModel:
    raw, _ = TrackedLoader().load_string(yaml)
    model, result = ReferenceResolver().resolve(raw)
    assert result.valid, f"Model has errors: {result.errors}"
    return model


# ---------------------------------------------------------------------------
# build_measure_filter_condition tests
# ---------------------------------------------------------------------------


class TestBuildMeasureFilterCondition:
    """Tests for build_measure_filter_condition()."""

    def test_single_equals_filter(self) -> None:
        model = _load_model()
        filters = [
            MeasureFilter(
                column=DataColumnRef(view="Customers", column="Country"),
                operator="equals",
                values=[FilterValue(data_type=DataType.STRING, value_string="US")],
            )
        ]
        errors: list[SemanticError] = []
        result = build_measure_filter_condition(filters, model, errors)
        assert errors == []
        assert isinstance(result, BinaryOp)
        assert result.op == "="
        assert isinstance(result.left, ColumnRef)
        assert result.left.name == "COUNTRY"
        assert result.left.table == "Customers"
        assert isinstance(result.right, Literal)
        assert result.right.value == "US"

    def test_inlist_filter(self) -> None:
        model = _load_model()
        filters = [
            MeasureFilter(
                column=DataColumnRef(view="Customers", column="Country"),
                operator="inlist",
                values=[
                    FilterValue(data_type=DataType.STRING, value_string="US"),
                    FilterValue(data_type=DataType.STRING, value_string="CA"),
                ],
            )
        ]
        errors: list[SemanticError] = []
        result = build_measure_filter_condition(filters, model, errors)
        assert errors == []
        from orionbelt.ast.nodes import InList

        assert isinstance(result, InList)
        assert len(result.values) == 2

    def test_multiple_filters_and(self) -> None:
        model = _load_model()
        filters = [
            MeasureFilter(
                column=DataColumnRef(view="Customers", column="Country"),
                operator="equals",
                values=[FilterValue(data_type=DataType.STRING, value_string="US")],
            ),
            MeasureFilter(
                column=DataColumnRef(view="Customers", column="Segment"),
                operator="equals",
                values=[FilterValue(data_type=DataType.STRING, value_string="Consumer")],
            ),
        ]
        errors: list[SemanticError] = []
        result = build_measure_filter_condition(filters, model, errors)
        assert errors == []
        assert isinstance(result, BinaryOp)
        assert result.op == "AND"

    def test_filter_group_or(self) -> None:
        model = _load_model()
        filters = [
            MeasureFilterGroup(
                logic=FilterLogic.OR,
                filters=[
                    MeasureFilter(
                        column=DataColumnRef(view="Customers", column="Country"),
                        operator="equals",
                        values=[FilterValue(data_type=DataType.STRING, value_string="US")],
                    ),
                    MeasureFilter(
                        column=DataColumnRef(view="Customers", column="Country"),
                        operator="equals",
                        values=[FilterValue(data_type=DataType.STRING, value_string="CA")],
                    ),
                ],
            )
        ]
        errors: list[SemanticError] = []
        result = build_measure_filter_condition(filters, model, errors)
        assert errors == []
        assert isinstance(result, BinaryOp)
        assert result.op == "OR"

    def test_negated_group(self) -> None:
        model = _load_model()
        filters = [
            MeasureFilterGroup(
                logic=FilterLogic.AND,
                negated=True,
                filters=[
                    MeasureFilter(
                        column=DataColumnRef(view="Customers", column="Country"),
                        operator="equals",
                        values=[FilterValue(data_type=DataType.STRING, value_string="US")],
                    ),
                ],
            )
        ]
        errors: list[SemanticError] = []
        result = build_measure_filter_condition(filters, model, errors)
        assert errors == []
        from orionbelt.ast.nodes import UnaryOp

        assert isinstance(result, UnaryOp)
        assert result.op == "NOT"

    def test_unknown_data_object_error(self) -> None:
        model = _load_model()
        filters = [
            MeasureFilter(
                column=DataColumnRef(view="NonExistent", column="Country"),
                operator="equals",
                values=[FilterValue(data_type=DataType.STRING, value_string="US")],
            )
        ]
        errors: list[SemanticError] = []
        result = build_measure_filter_condition(filters, model, errors)
        assert result is None
        assert any(e.code == "UNKNOWN_FILTER_DATA_OBJECT" for e in errors)

    def test_unknown_column_error(self) -> None:
        model = _load_model()
        filters = [
            MeasureFilter(
                column=DataColumnRef(view="Customers", column="NonExistent"),
                operator="equals",
                values=[FilterValue(data_type=DataType.STRING, value_string="US")],
            )
        ]
        errors: list[SemanticError] = []
        result = build_measure_filter_condition(filters, model, errors)
        assert result is None
        assert any(e.code == "UNKNOWN_FILTER_COLUMN" for e in errors)

    def test_empty_filters_returns_none(self) -> None:
        model = _load_model()
        result = build_measure_filter_condition([], model, [])
        assert result is None


# ---------------------------------------------------------------------------
# collect_measure_filter_objects tests
# ---------------------------------------------------------------------------


class TestCollectFilterObjects:
    def test_collects_leaf_object(self) -> None:
        objects: set[str] = set()
        collect_measure_filter_objects(
            MeasureFilter(
                column=DataColumnRef(view="Customers", column="Country"),
                operator="equals",
            ),
            objects,
        )
        assert objects == {"Customers"}

    def test_collects_group_objects(self) -> None:
        objects: set[str] = set()
        collect_measure_filter_objects(
            MeasureFilterGroup(
                filters=[
                    MeasureFilter(
                        column=DataColumnRef(view="Customers", column="Country"),
                        operator="equals",
                    ),
                    MeasureFilter(
                        column=DataColumnRef(view="Orders", column="Order ID"),
                        operator="set",
                    ),
                ]
            ),
            objects,
        )
        assert objects == {"Customers", "Orders"}


# ---------------------------------------------------------------------------
# Resolution tests — verify CASE WHEN wrapping in resolved measures
# ---------------------------------------------------------------------------


class TestMeasureFilterResolution:
    """Verify that resolution wraps measure aggregation with CaseExpr."""

    def test_filtered_measure_has_case_expr(self) -> None:
        model = _load_model()
        from orionbelt.compiler.resolution import QueryResolver

        q = QueryObject.model_validate(
            {"select": {"dimensions": ["Country"], "measures": ["US Revenue"]}}
        )
        resolved = QueryResolver().resolve(q, model)
        assert len(resolved.measures) == 1
        m = resolved.measures[0]
        assert m.name == "US Revenue"
        # Expression should be FunctionCall(SUM, [CaseExpr(...)])
        assert isinstance(m.expression, FunctionCall)
        assert m.expression.name == "SUM"
        assert len(m.expression.args) == 1
        assert isinstance(m.expression.args[0], CaseExpr)

    def test_unfiltered_measure_no_case_expr(self) -> None:
        model = _load_model()
        from orionbelt.compiler.resolution import QueryResolver

        q = QueryObject.model_validate(
            {"select": {"dimensions": ["Country"], "measures": ["Revenue"]}}
        )
        resolved = QueryResolver().resolve(q, model)
        m = resolved.measures[0]
        assert isinstance(m.expression, FunctionCall)
        assert not isinstance(m.expression.args[0], CaseExpr)

    def test_filtered_count_has_case_expr(self) -> None:
        model = _load_model()
        from orionbelt.compiler.resolution import QueryResolver

        q = QueryObject.model_validate(
            {"select": {"dimensions": ["Country"], "measures": ["US Order Count"]}}
        )
        resolved = QueryResolver().resolve(q, model)
        m = resolved.measures[0]
        assert isinstance(m.expression, FunctionCall)
        assert m.expression.name == "COUNT"
        assert isinstance(m.expression.args[0], CaseExpr)

    def test_filter_objects_added_to_required(self) -> None:
        model = _load_model()
        from orionbelt.compiler.resolution import QueryResolver

        q = QueryObject.model_validate(
            {"select": {"dimensions": ["Country"], "measures": ["US Revenue"]}}
        )
        resolved = QueryResolver().resolve(q, model)
        assert "Customers" in resolved.required_objects

    def test_multiple_filters_and_logic(self) -> None:
        """Two top-level filters are combined with AND."""
        model = _load_model()
        from orionbelt.compiler.resolution import QueryResolver

        q = QueryObject.model_validate(
            {"select": {"dimensions": ["Country"], "measures": ["Segment Revenue"]}}
        )
        resolved = QueryResolver().resolve(q, model)
        m = resolved.measures[0]
        assert isinstance(m.expression, FunctionCall)
        case_expr = m.expression.args[0]
        assert isinstance(case_expr, CaseExpr)
        condition = case_expr.when_clauses[0][0]
        assert isinstance(condition, BinaryOp)
        assert condition.op == "AND"


# ---------------------------------------------------------------------------
# SQL compilation tests — verify CASE WHEN appears in generated SQL
# ---------------------------------------------------------------------------


class TestFilteredMeasureSQL:
    """Full pipeline compilation with filtered measures."""

    @pytest.mark.parametrize("dialect", ["duckdb", "postgres", "snowflake", "bigquery"])
    def test_case_when_in_sql(self, dialect: str) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        q = QueryObject.model_validate(
            {"select": {"dimensions": ["Country"], "measures": ["US Revenue"]}}
        )
        result = pipeline.compile(q, model, dialect)
        assert "CASE WHEN" in result.sql
        assert "'US'" in result.sql

    def test_filtered_and_unfiltered_together(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        q = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Country"],
                    "measures": ["Revenue", "US Revenue"],
                }
            }
        )
        result = pipeline.compile(q, model, "duckdb")
        assert "CASE WHEN" in result.sql
        # Revenue should NOT have CASE WHEN — check both appear
        assert '"Revenue"' in result.sql
        assert '"US Revenue"' in result.sql

    def test_ratio_metric_compiles(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        q = QueryObject.model_validate(
            {"select": {"dimensions": ["Country"], "measures": ["US Revenue Share"]}}
        )
        result = pipeline.compile(q, model, "postgres")
        assert "CASE WHEN" in result.sql
        assert "SUM" in result.sql
        assert result.sql_valid

    def test_or_filter_group(self) -> None:
        model = _load_model()
        pipeline = CompilationPipeline()
        q = QueryObject.model_validate(
            {"select": {"dimensions": ["Country"], "measures": ["OR Revenue"]}}
        )
        result = pipeline.compile(q, model, "duckdb")
        assert "CASE WHEN" in result.sql
        assert " OR " in result.sql
        assert "'US'" in result.sql
        assert "'CA'" in result.sql


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------


class TestMeasureFilterValidation:
    """Validation catches bad filter references."""

    def test_invalid_filter_object_detected(self) -> None:
        yaml = """\
version: "1.0"
dataObjects:
  Orders:
    code: ORDERS
    database: WH
    schema: PUBLIC
    columns:
      Price:
        code: PRICE
        abstractType: float
dimensions: {}
measures:
  Bad:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]}'
    filters:
      - column: {dataObject: NonExistent, column: Foo}
        operator: equals
        values: [{dataType: string, valueString: "X"}]
metrics: {}
"""
        raw, src = TrackedLoader().load_string(yaml)
        model, result = ReferenceResolver().resolve(raw, src)
        # Validator should catch bad data object ref
        from orionbelt.parser.validator import SemanticValidator

        errors = SemanticValidator().validate(model)
        assert any(e.code == "UNKNOWN_FILTER_DATA_OBJECT" for e in errors)

    def test_invalid_filter_column_detected(self) -> None:
        yaml = """\
version: "1.0"
dataObjects:
  Orders:
    code: ORDERS
    database: WH
    schema: PUBLIC
    columns:
      Price:
        code: PRICE
        abstractType: float
dimensions: {}
measures:
  Bad:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]}'
    filters:
      - column: {dataObject: Orders, column: NonExistent}
        operator: equals
        values: [{dataType: string, valueString: "X"}]
metrics: {}
"""
        raw, src = TrackedLoader().load_string(yaml)
        model, result = ReferenceResolver().resolve(raw, src)
        from orionbelt.parser.validator import SemanticValidator

        errors = SemanticValidator().validate(model)
        assert any(e.code == "UNKNOWN_FILTER_COLUMN" for e in errors)
