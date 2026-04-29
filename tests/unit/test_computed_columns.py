"""Tests for computed columns on ``DataObjectColumn``.

A column with ``expression:`` is inlined wherever it's referenced — in
SELECT lists, GROUP BY, WHERE filters, raw-mode field projections, etc.
``{name}`` placeholders refer to other columns in the same data object.
"""

from __future__ import annotations

import orionbelt.dialect  # noqa: F401 — registers dialects
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_MODEL_YAML = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Year:
        code: REPORTINGDATEYEAR
        abstractType: int
      Month:
        code: REPORTINGDATEMONTH
        abstractType: int
      Reporting Period:
        abstractType: int
        expression: "({Year} * 100 + {Month})"
      Country:
        code: COUNTRY
        abstractType: string

dimensions:
  Order Year:
    dataObject: Orders
    column: Year
    resultType: int
  Reporting Period:
    dataObject: Orders
    column: Reporting Period
    resultType: int
  Country:
    dataObject: Orders
    column: Country
    resultType: string

measures:
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    resultType: int
    aggregation: count
"""


def _model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, sm = loader.load_string(_MODEL_YAML)
    model, result = resolver.resolve(raw, sm)
    assert result.valid, result.errors
    return model


class TestComputedColumnParser:
    def test_computed_column_loads_with_expression(self) -> None:
        m = _model()
        col = m.data_objects["Orders"].columns["Reporting Period"]
        assert col.is_computed
        assert col.expression == "({Year} * 100 + {Month})"

    def test_plain_column_is_not_computed(self) -> None:
        m = _model()
        col = m.data_objects["Orders"].columns["Order ID"]
        assert not col.is_computed
        assert col.expression is None

    def test_code_optional_for_computed(self) -> None:
        m = _model()
        col = m.data_objects["Orders"].columns["Reporting Period"]
        # No `code:` provided in YAML — parser allows that for computed columns.
        assert col.code == ""


class TestComputedColumnInSelect:
    def test_dimension_via_computed_column_inlines_expression(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Reporting Period"], measures=["Order Count"]),
        )
        sql = CompilationPipeline().compile(query, _model(), dialect_name="postgres").sql
        # Expression substituted with table-qualified physical column codes
        # (parser adds parens around sub-expressions for precedence safety).
        assert '"Orders"."REPORTINGDATEYEAR" * 100' in sql
        assert '+ "Orders"."REPORTINGDATEMONTH"' in sql
        # And it should appear in GROUP BY too.
        gb_idx = sql.upper().find("GROUP BY")
        assert gb_idx > 0
        assert "REPORTINGDATEYEAR" in sql[gb_idx:]

    def test_plain_dimension_unchanged(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Country"], measures=["Order Count"]),
        )
        sql = CompilationPipeline().compile(query, _model(), dialect_name="postgres").sql
        assert '"Orders"."COUNTRY"' in sql


class TestComputedColumnInFilter:
    def test_where_filter_on_computed_column_inlines_expression(self) -> None:
        query = QueryObject(
            select=QuerySelect(measures=["Order Count"]),
            where=[
                QueryFilter(field="Reporting Period", op=FilterOperator.GTE, value=202401),
            ],
        )
        sql = CompilationPipeline().compile(query, _model(), dialect_name="postgres").sql
        # The filter's LHS is the inlined expression, not a plain column ref.
        assert '"Orders"."REPORTINGDATEYEAR"' in sql
        assert '"Orders"."REPORTINGDATEMONTH"' in sql
        assert ">= 202401" in sql

    def test_qualified_column_filter_on_computed(self) -> None:
        query = QueryObject(
            select=QuerySelect(measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Orders.Reporting Period",
                    op=FilterOperator.LT,
                    value=202501,
                ),
            ],
        )
        sql = CompilationPipeline().compile(query, _model(), dialect_name="postgres").sql
        assert '"Orders"."REPORTINGDATEYEAR"' in sql
        assert '"Orders"."REPORTINGDATEMONTH"' in sql
        assert "< 202501" in sql


class TestComputedColumnInRawMode:
    def test_raw_mode_field_inlines_expression(self) -> None:
        query = QueryObject(
            select=QuerySelect(fields=["Orders.Reporting Period", "Orders.Order ID"]),
            limit=10,
        )
        sql = CompilationPipeline().compile(query, _model(), dialect_name="postgres").sql
        # Computed column inlined; aliased to the user-facing reference.
        assert '"Orders"."REPORTINGDATEYEAR"' in sql
        assert '"Orders"."REPORTINGDATEMONTH"' in sql
        assert 'AS "Orders.Reporting Period"' in sql
        # Plain raw field unchanged.
        assert '"Orders"."ORDER_ID" AS "Orders.Order ID"' in sql
