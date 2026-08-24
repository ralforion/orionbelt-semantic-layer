"""Tests for computed columns on ``DataObjectColumn``.

A column with ``expression:`` is inlined wherever it's referenced — in
SELECT lists, GROUP BY, WHERE filters, raw-mode field projections, etc.
``{name}`` placeholders refer to other columns in the same data object.
"""

from __future__ import annotations

import pytest

import orionbelt.dialect  # noqa: F401 — registers dialects
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

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


class TestComputedColumnInOrderBy:
    """Regression: ORDER BY on a computed column must inline its expression.

    Computed columns have an empty ``code`` — the previous resolver emitted
    ``ColumnRef(name="", table=...)``, which renders as ``ORDER BY "Orders".""``
    (invalid SQL).
    """

    def test_aggregate_order_by_computed_dimension_inlines_expression(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Reporting Period"], measures=["Order Count"]),
            order_by=[QueryOrderBy(field="Reporting Period", direction=SortDirection.DESC)],
        )
        sql = CompilationPipeline().compile(query, _model(), dialect_name="postgres").sql
        ob_idx = sql.upper().rfind("ORDER BY")
        assert ob_idx > 0
        ob_clause = sql[ob_idx:]
        # No dangling empty-name reference.
        assert '""' not in ob_clause
        # Expression inlined — both physical columns appear in the ORDER BY clause.
        assert "REPORTINGDATEYEAR" in ob_clause
        assert "REPORTINGDATEMONTH" in ob_clause
        assert "DESC" in ob_clause

    def test_raw_order_by_computed_field_inlines_expression(self) -> None:
        query = QueryObject(
            select=QuerySelect(fields=["Orders.Reporting Period", "Orders.Order ID"]),
            order_by=[
                QueryOrderBy(field="Orders.Reporting Period", direction=SortDirection.ASC),
            ],
        )
        sql = CompilationPipeline().compile(query, _model(), dialect_name="postgres").sql
        ob_idx = sql.upper().rfind("ORDER BY")
        assert ob_idx > 0
        ob_clause = sql[ob_idx:]
        assert '""' not in ob_clause
        assert "REPORTINGDATEYEAR" in ob_clause
        assert "REPORTINGDATEMONTH" in ob_clause


_STRING_LITERAL_MODEL_YAML = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Zip:
        code: ZIP
        abstractType: string
      Zip 5:
        abstractType: string
        expression: "regexp_extract({Zip}, '[0-9]{5}')"
      Quoted:
        abstractType: string
        expression: "'{Zip}'"
      Nested:
        abstractType: string
        expression: "concat({Quoted}, '{Zip}')"

dimensions:
  Zip 5:
    dataObject: Orders
    column: Zip 5
    resultType: string
  Quoted:
    dataObject: Orders
    column: Quoted
    resultType: string
  Nested:
    dataObject: Orders
    column: Nested
    resultType: string

measures:
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    resultType: int
    aggregation: count
"""


def _string_literal_model() -> SemanticModel:
    raw, sm = TrackedLoader().load_string(_STRING_LITERAL_MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    return model


def _compile_dimension(dimension: str) -> str:
    query = QueryObject(
        select=QuerySelect(dimensions=[dimension], measures=["Order Count"]),
    )
    return CompilationPipeline().compile(query, _string_literal_model(), "duckdb").sql


class TestComputedColumnStringLiterals:
    """Braces inside a string literal are data, not column placeholders.

    Substitution used to run over the whole expression, so a placeholder
    naming a real column was rewritten inside quotes and emitted as the
    literal text ``'{[Orders].[Zip]}'``.
    """

    def test_regex_quantifier_survives(self) -> None:
        sql = _compile_dimension("Zip 5")
        assert "'[0-9]{5}'" in sql
        assert '"Orders"."ZIP"' in sql

    def test_placeholder_inside_literal_is_not_substituted(self) -> None:
        sql = _compile_dimension("Quoted")
        assert "'{Zip}'" in sql
        assert "[Orders]" not in sql

    def test_nested_computed_column_keeps_its_literals(self) -> None:
        """The inlining path in expr_parser applies the same rule."""
        sql = _compile_dimension("Nested")
        # The {Quoted} reference resolves and inlines; both literals survive.
        assert sql.count("'{Zip}'") == 2
        assert "[Orders]" not in sql


class TestUnparseableComputedColumn:
    """A body the parser cannot read is an error, not a fallback (#359).

    A computed column *is* its expression: there is no ``code`` to fall back to,
    so a body that does not parse leaves nothing to select. The compiler used to
    invent something anyway, a reference to the column's own display name as
    though it were a physical column, and the model loaded, the query compiled,
    ``sql_valid`` came back true, and the database rejected a statement naming
    an object that only exists in the model.

    A metric whose formula does not parse has always been refused with
    ``INVALID_METRIC_EXPRESSION``. This is the same answer for the other
    declaration form, at both ends: at load, where the model is written, and at
    compile, where the column would have been built.
    """

    #: Ordinary SQL the format invites - `DataObjectColumn` tells authors an
    #: expression is dialect-leaky and to pin ``defaultDialect`` - and which the
    #: parser does not take. ``CAST`` is #355 and the simple ``CASE`` is #360.
    UNPARSEABLE = {
        "concat operator": "{Code} || '-eu'",
        "interval literal": "{Day} + INTERVAL 1 DAY",
        "cast": "CAST({Amount} AS INT)",
        "extract": "EXTRACT(YEAR FROM {Day})",
        "simple case": "CASE {Code} WHEN 'DE' THEN 'EU' ELSE 'Other' END",
    }

    MODEL_YAML = """\
version: 1.0
name: unparseable
dataObjects:
  Event:
    code: event
    columns:
      Code:   {code: code, abstractType: string}
      Day:    {code: day, abstractType: date}
      Amount: {code: amount, abstractType: float, numClass: additive}
      Tagged:
        expression: "%s"
        abstractType: string
dimensions:
  Tagged: {dataObject: Event, column: Tagged, resultType: string}
measures:
  Total:
    columns: [{dataObject: Event, column: Amount}]
    resultType: float
    aggregation: sum
"""

    def _model(self, expression: str) -> SemanticModel:
        raw, source_map = TrackedLoader().load_string(self.MODEL_YAML % expression)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return model

    def test_the_model_does_not_validate(self) -> None:
        """Reported where it can still be fixed cheaply: at the model."""
        for label, expression in self.UNPARSEABLE.items():
            errors = [
                e
                for e in SemanticValidator().validate(self._model(expression))
                if e.code == "INVALID_COLUMN_EXPRESSION"
            ]
            assert len(errors) == 1, f"{label}: {errors}"
            assert errors[0].path == "dataObjects.Event.columns.Tagged.expression"
            assert "Tagged" in errors[0].message

    def test_compiling_it_raises_rather_than_inventing_a_column(self) -> None:
        """The other end, for a model that reached the compiler anyway.

        It used to emit ``SELECT "Event"."Tagged"``, a column no table has, and
        DuckDB answered ``Table "Event" does not have a column named "Tagged"``.
        """
        query = QueryObject(select=QuerySelect(dimensions=["Tagged"], measures=["Total"]))
        with pytest.raises(ResolutionError) as excinfo:
            CompilationPipeline().compile(query, self._model("{Code} || '-eu'"), "duckdb")
        (error,) = excinfo.value.errors
        assert error.code == "INVALID_COLUMN_EXPRESSION"
        assert error.path == "dataObjects.Event.columns.Tagged.expression"

    def test_a_body_that_parses_is_untouched(self) -> None:
        """The control: the same shape with a body the parser reads."""
        model = self._model("CASE WHEN {Code} = 'DE' THEN 'EU' ELSE 'Other' END")
        assert not [
            e for e in SemanticValidator().validate(model) if e.code == "INVALID_COLUMN_EXPRESSION"
        ]
        query = QueryObject(select=QuerySelect(dimensions=["Tagged"], measures=["Total"]))
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert 'CASE WHEN "Event"."code" = \'DE\'' in sql

    def test_a_cycle_is_left_to_the_check_that_names_both_ends(self) -> None:
        """A cyclic pair recurses rather than failing to parse.

        ``_check_no_cyclic_computed_columns`` reports it with both columns in
        hand, which is more use than whichever one the recursion stopped on.
        """
        yaml = """\
version: 1.0
dataObjects:
  Event:
    code: event
    columns:
      A: {expression: "{B} + 1", abstractType: int}
      B: {expression: "{A} + 1", abstractType: int}
"""
        raw, source_map = TrackedLoader().load_string(yaml)
        model, _ = ReferenceResolver().resolve(raw, source_map)
        codes = [e.code for e in SemanticValidator().validate(model)]
        assert "INVALID_COLUMN_EXPRESSION" not in codes
        assert any("CYCLIC" in code for code in codes), codes

    def test_the_bundled_models_still_validate(self) -> None:
        """The rule is new, so it has to be silent on every model in the repo."""
        import pathlib

        noisy: list[str] = []
        for path in sorted(pathlib.Path("examples").rglob("*.obml.yml")):
            raw, source_map = TrackedLoader().load(path)
            model, result = ReferenceResolver().resolve(raw, source_map)
            if result.errors:
                continue
            noisy.extend(
                f"{path}: {e.message}"
                for e in SemanticValidator().validate(model)
                if e.code == "INVALID_COLUMN_EXPRESSION"
            )
        assert not noisy, "\n".join(noisy)
