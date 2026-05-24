"""Regression: computed (``expression:``) columns inline at every call site.

A column declared with ``expression:`` instead of ``code:`` used to inline
correctly in only two paths (SELECT projection, WHERE/HAVING comparison
operand) and silently collapse to the empty ``code:`` slot in three
others — JOIN ON predicate, measure-level filter operand, and measure
aggregation argument. Those paths emitted ``"obj"."" `` or, when
collapsed by a CAST, the literal ``1``, producing invalid SQL the
database rejected at execution time.

Each test below runs the compiled SQL against an in-memory DuckDB, so
the safety net catches future regressions structurally — a SQL string
match alone would not detect ``(1 = FALSE)`` collapsing back through a
CAST.

Coverage:

* Arithmetic computed columns — ``{Revenue} - {Discount}``.
* Comparison-typed boolean computed columns — ``{Revenue} >= 100``.
* Function-call computed columns — ``UPPER({Customer ID})``.
* Chained references — an ``expression:`` column that references
  another ``expression:`` column inlines recursively.
"""

from __future__ import annotations

from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb package required for execution tests")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import (  # noqa: E402
    FilterOperator,
    QueryFilter,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
)
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture model — one data object with a mix of base and computed columns.
# Computed columns use arithmetic only (current tokenizer surface).
# ---------------------------------------------------------------------------

_MODEL_YAML = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    schema: main
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country Code:
        code: COUNTRY_CODE
        abstractType: string
      # The Orders side joins on UPPER({Customer ID}); the Customers
      # side uses the same expression so the join key wraps both ends.
      Customer Key:
        expression: 'UPPER({Customer ID})'
        abstractType: string

  Orders:
    code: ORDERS
    schema: main
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Revenue:
        code: REVENUE
        abstractType: float
      Discount:
        code: DISCOUNT
        abstractType: float
      # Computed column — body inlines wherever it's referenced.
      Net Revenue:
        expression: '{Revenue} - {Discount}'
        abstractType: float
      # Computed column used as a measure filter operand. Comparison
      # operators in computed-column expressions land via the
      # tokenizer's comparison-op support.
      Is Large Order:
        expression: '{Revenue} >= 100'
        abstractType: boolean
      # Function-call computed column — exercises the function-call
      # support in the expression tokenizer.
      Customer Key:
        expression: 'UPPER({Customer ID})'
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Customer Key]
        columnsTo: [Customer Key]

dimensions:
  Country Code:
    dataObject: Customers
    column: Country Code
    resultType: string
  # Dimension over a computed column — exercises SELECT/WHERE/ORDER BY.
  Net Revenue:
    dataObject: Orders
    column: Net Revenue
    resultType: float

measures:
  # count_distinct over a computed column — the AGGREGATION ARG path.
  Distinct Net Revenue Buckets:
    columns:
      - dataObject: Orders
        column: Net Revenue
    resultType: int
    aggregation: count_distinct
  # SUM over a computed column.
  Total Net Revenue:
    columns:
      - dataObject: Orders
        column: Net Revenue
    resultType: float
    aggregation: sum
  # Measure-level filter on a boolean computed column — the FILTER
  # OPERAND path. Before the fix the empty ``code:`` slot collapsed
  # under a CAST, emitting ``(1 = FALSE)``. After: the inlined boolean
  # body ``("Orders"."REVENUE" >= 100) = FALSE`` evaluates correctly.
  Small Order Revenue:
    columns:
      - dataObject: Orders
        column: Revenue
    filters:
      - column: {dataObject: Orders, column: Is Large Order}
        operator: equals
        values: [{dataType: boolean, valueBoolean: false}]
    resultType: float
    aggregation: sum
"""

_TABLE_SQL = """\
CREATE TABLE CUSTOMERS (
    CUSTOMER_ID VARCHAR,
    COUNTRY_CODE VARCHAR
);
INSERT INTO CUSTOMERS VALUES
    ('c1', 'US'),
    ('c2', 'UK'),
    ('c3', 'US');

CREATE TABLE ORDERS (
    ORDER_ID VARCHAR,
    CUSTOMER_ID VARCHAR,
    REVENUE DOUBLE,
    DISCOUNT DOUBLE
);
INSERT INTO ORDERS VALUES
    ('o1', 'c1', 150.0, 50.0),  -- Net 100, large flag +50
    ('o2', 'c1',  80.0, 20.0),  -- Net 60,  large flag -20 (small)
    ('o3', 'c2', 200.0, 10.0),  -- Net 190, large flag +100
    ('o4', 'c3',  50.0,  5.0);  -- Net 45,  large flag -50 (small)
"""


@pytest.fixture(scope="module")
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute(_TABLE_SQL)
    yield c
    c.close()


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(_MODEL_YAML)
    m, result = resolver.resolve(raw, source_map)
    assert result.valid, [e.message for e in result.errors]
    return m


@pytest.fixture(scope="module")
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


def _exec(conn: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[Any, ...]]:
    return conn.execute(sql).fetchall()


def _assert_no_empty_identifier(sql: str) -> None:
    """The bug pattern is ``"<alias>"."" `` — a real zero-length column
    identifier emitted because the empty ``code:`` slot leaked through
    a downstream renderer. Any lone ``""`` in the FROM clause is just
    an empty ``database:`` qualifier and is harmless.
    """
    assert '"."" ' not in sql, sql
    assert '"."" )' not in sql, sql
    assert '"."",' not in sql, sql
    assert '".""\n' not in sql, sql


# ---------------------------------------------------------------------------
# Tests — one per call site that previously bypassed make_column_expr.
# ---------------------------------------------------------------------------


class TestComputedColumnInlinerCallSites:
    def test_select_inlines_expression(
        self,
        conn: duckdb.DuckDBPyConnection,
        model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """SELECT projection of a dimension backed by a computed column.

        ``Net Revenue = Revenue - Discount`` — expected per order:
        o1: 100, o2: 60, o3: 190, o4: 45.
        """
        query = QueryObject(
            select=QuerySelect(dimensions=["Net Revenue"]),
            order_by=[QueryOrderBy(field="Net Revenue", direction=SortDirection.ASC)],
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        _assert_no_empty_identifier(sql)
        assert "REVENUE" in sql.upper() and "DISCOUNT" in sql.upper(), sql
        rows = _exec(conn, sql)
        values = [float(r[0]) for r in rows]
        assert values == [45.0, 60.0, 100.0, 190.0]

    def test_where_inlines_expression(
        self,
        conn: duckdb.DuckDBPyConnection,
        model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """WHERE on a dimension over a computed column."""
        query = QueryObject(
            select=QuerySelect(measures=["Total Net Revenue"]),
            where=[QueryFilter(field="Net Revenue", op=FilterOperator.GT, value=100)],
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        _assert_no_empty_identifier(sql)
        rows = _exec(conn, sql)
        # Only o3 (Net=190) qualifies — Total Net Revenue = 190.
        assert float(rows[0][0]) == 190.0

    def test_measure_aggregation_arg_inlines_expression(
        self,
        conn: duckdb.DuckDBPyConnection,
        model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """``count_distinct(<computed column>)`` — the aggregation-arg path.

        Before the fix, ``COUNT(DISTINCT "Orders"."")`` was emitted; the
        DB rejected the zero-length identifier. After: ``COUNT(DISTINCT
        ("Orders"."REVENUE" - "Orders"."DISCOUNT"))`` returns four
        distinct Net Revenue values.
        """
        query = QueryObject(
            select=QuerySelect(measures=["Distinct Net Revenue Buckets"]),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        _assert_no_empty_identifier(sql)
        assert "COUNT(DISTINCT" in sql.upper(), sql
        rows = _exec(conn, sql)
        assert int(rows[0][0]) == 4  # 100, 60, 190, 45 — all distinct

    def test_sum_aggregation_arg_inlines_expression(
        self,
        conn: duckdb.DuckDBPyConnection,
        model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """SUM over a computed column — second instance of the AGG ARG
        path with a different aggregation kind."""
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country Code"],
                measures=["Total Net Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        _assert_no_empty_identifier(sql)
        rows = _exec(conn, sql)
        by_country = {r[0]: float(r[1]) for r in rows}
        # US: o1 (100) + o2 (60) + o4 (45) = 205
        # UK: o3 (190)
        assert by_country["US"] == pytest.approx(205.0)
        assert by_country["UK"] == pytest.approx(190.0)

    def test_measure_filter_inlines_boolean_expression(
        self,
        conn: duckdb.DuckDBPyConnection,
        model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """Measure-level filter operand on a boolean computed column.

        ``Small Order Revenue`` sums ``Revenue`` where ``Is Large Order
        = false``. Before the fix the empty ``code:`` for the computed
        column was collapsed by a CAST into the literal ``1``, so the
        filter compiled to ``(1 = FALSE)`` (operator-type mismatch).
        After: ``(("Orders"."REVENUE" >= 100) = FALSE)`` matches
        o2 (80) + o4 (50) = 130.
        """
        query = QueryObject(
            select=QuerySelect(measures=["Small Order Revenue"]),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        _assert_no_empty_identifier(sql)
        rows = _exec(conn, sql)
        assert float(rows[0][0]) == pytest.approx(130.0)

    def test_join_on_predicate_inlines_expression(
        self,
        conn: duckdb.DuckDBPyConnection,
        model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """JOIN ON with computed join keys on both sides.

        Models with normalisation in the join key (case-folding,
        trimming, CSV-roundtrip cleanup) hit this path — the ON clause
        must emit ``(UPPER(...) = UPPER(...))`` instead of ``("" = "")``.
        Here both Orders.``Customer Key`` and Customers.``Customer Key``
        are ``UPPER({Customer ID})``.
        """
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Country Code"],
                measures=["Total Net Revenue"],
            ),
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        _assert_no_empty_identifier(sql)
        # UPPER appears on both sides of the join ON predicate.
        upper_count = sql.upper().count("UPPER(")
        assert upper_count >= 2, sql
        # Execute and check totals roll up as expected.
        rows = _exec(conn, sql)
        by_country = {r[0]: float(r[1]) for r in rows}
        assert by_country["US"] == pytest.approx(205.0)
        assert by_country["UK"] == pytest.approx(190.0)

    def test_order_by_on_computed_column(
        self,
        conn: duckdb.DuckDBPyConnection,
        model: SemanticModel,
        pipeline: CompilationPipeline,
    ) -> None:
        """ORDER BY on a computed-column dimension — already routed
        through ``make_column_expr`` but exercised here for completeness."""
        query = QueryObject(
            select=QuerySelect(dimensions=["Net Revenue"]),
            order_by=[QueryOrderBy(field="Net Revenue", direction=SortDirection.DESC)],
        )
        result = pipeline.compile(query, model, "duckdb")
        sql = result.sql
        _assert_no_empty_identifier(sql)
        rows = _exec(conn, sql)
        values = [float(r[0]) for r in rows]
        assert values == [190.0, 100.0, 60.0, 45.0]
