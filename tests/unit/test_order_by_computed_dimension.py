"""ORDER BY a computed dimension, in a query a wrapper puts inside a CTE (#358).

A computed column's source is an expression, so the ordering key the resolver
builds is that expression rather than a column reference. Four passes wrap the
planner's SELECT in a CTE and rebuild ORDER BY over it - totals, cumulative,
window and period-over-period - and each rebuilt only the column form, leaving
the expression to be inlined a second time into a query where the table it
names is out of scope.

The failure was silent up to the database: the model validated clean,
``sql_valid`` came back true, no warning was raised, and PostgreSQL answered
``missing FROM-clause entry for table "Customer"`` - naming a *logical* model
object, so it read as a join problem rather than a projection-scope one.

The last section covers what executing the fixed query then found: the
period-over-period wrapper reading a dimension's physical column name where the
other planners go through ``make_column_expr``, which is the same mistake one
clause over.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser import ReferenceResolver, TrackedLoader

MODEL_YAML = """
version: 1.0
name: order_by_computed

dataObjects:
  Customer:
    code: customer
    columns:
      Customer ID: {code: id, abstractType: string}
      Country:     {code: country, abstractType: string}
      Order Date:  {code: order_date, abstractType: date}
      Amount:      {code: amount, abstractType: float, numClass: additive}
      Region:
        expression: "CASE WHEN {Country} IN ('DE', 'FR') THEN 'EU' ELSE 'Other' END"
        abstractType: string

dimensions:
  Country:     {dataObject: Customer, column: Country, resultType: string}
  Region:      {dataObject: Customer, column: Region, resultType: string}
  Order Month: {dataObject: Customer, column: Order Date, resultType: date, timeGrain: month}

measures:
  Total Amount:
    columns: [{dataObject: Customer, column: Amount}]
    resultType: float
    aggregation: sum
  Portfolio Amount:
    columns: [{dataObject: Customer, column: Amount}]
    resultType: float
    aggregation: sum
    grain: {mode: FIXED}
  Grand Total Amount:
    columns: [{dataObject: Customer, column: Amount}]
    resultType: float
    aggregation: sum
    total: true

metrics:
  Share Of Total:
    expression: '{[Total Amount]} / NULLIF({[Portfolio Amount]}, 0)'
  Running Amount:
    type: cumulative
    measure: Total Amount
    timeDimension: Order Month
  Amount Rank:
    type: window
    windowFunction: dense_rank
    measure: Total Amount
    orderDirection: desc
  Amount MoM:
    type: period_over_period
    expression: '{[Total Amount]}'
    periodOverPeriod:
      timeDimension: Order Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""

#: One query per pass that wraps the planner's SELECT in a CTE, each ordered by
#: the computed dimension. ``Share Of Total`` reaches the totals wrapper through
#: a ``grain: FIXED`` measure and ``Grand Total Amount`` through ``total: true``.
WRAPPED_QUERIES: dict[str, dict] = {
    "totals (grain FIXED)": {
        "select": {"dimensions": ["Region"], "measures": ["Total Amount", "Share Of Total"]},
        "orderBy": [{"field": "Region", "direction": "asc"}],
    },
    "totals (total: true)": {
        "select": {"dimensions": ["Region"], "measures": ["Total Amount", "Grand Total Amount"]},
        "orderBy": [{"field": "Region", "direction": "asc"}],
    },
    "cumulative": {
        "select": {
            "dimensions": ["Region", "Order Month"],
            "measures": ["Total Amount", "Running Amount"],
        },
        "orderBy": [{"field": "Region", "direction": "asc"}],
    },
    "window": {
        "select": {"dimensions": ["Region"], "measures": ["Total Amount", "Amount Rank"]},
        "orderBy": [{"field": "Region", "direction": "asc"}],
    },
    "period over period": {
        "select": {
            "dimensions": ["Region", "Order Month"],
            "measures": ["Total Amount", "Amount MoM"],
        },
        "orderBy": [{"field": "Region", "direction": "asc"}],
    },
}


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def _sql(model: SemanticModel, query: dict, dialect: str = "duckdb") -> str:
    return CompilationPipeline().compile(QueryObject(**query), model, dialect).sql


def _outer_order_by(sql: str) -> str:
    """The ORDER BY of the outermost query, which is the last one emitted."""
    head, _, tail = sql.rpartition("ORDER BY")
    assert head, f"no ORDER BY in:\n{sql}"
    return tail.strip()


# ── the defect ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", sorted(WRAPPED_QUERIES))
def test_outer_order_by_names_the_projected_alias(model: SemanticModel, case: str) -> None:
    """The CTE evaluated the expression and named it; the outer query sorts on that name."""
    order_by = _outer_order_by(_sql(model, WRAPPED_QUERIES[case]))
    assert order_by == '"Region" ASC'


@pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
@pytest.mark.parametrize("case", sorted(WRAPPED_QUERIES))
def test_no_dialect_re_inlines_the_expression(
    model: SemanticModel, case: str, dialect: str
) -> None:
    """It is the shared projection path, not a dialect renderer, so all eight.

    The base table is only in scope inside the CTE. Naming it in the outer
    ORDER BY is the defect, whatever the dialect spells the CASE like.
    """
    order_by = _outer_order_by(_sql(model, WRAPPED_QUERIES[case], dialect))
    assert "Customer" not in order_by, order_by
    assert "CASE" not in order_by.upper(), order_by


@pytest.mark.parametrize("case", sorted(WRAPPED_QUERIES))
def test_the_wrapped_query_executes_and_sorts(model: SemanticModel, case: str) -> None:
    """Executed, because compiling is what looked fine before.

    'EU' before 'Other' is the whole of what was asked for, and the rows have
    to come back at all to say it.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE customer AS SELECT * FROM (VALUES"
        " ('c1', 'DE', DATE '2024-01-15', 100.0),"
        " ('c2', 'FR', DATE '2024-02-15', 200.0),"
        " ('c3', 'US', DATE '2024-03-15', 300.0),"
        " ('c4', 'US', DATE '2024-01-20', 50.0)"
        ") t(id, country, order_date, amount)"
    )
    rows = con.execute(_sql(model, WRAPPED_QUERIES[case])).fetchall()
    regions = [row[0] for row in rows]
    assert regions == sorted(regions)
    assert set(regions) == {"EU", "Other"}


# ── the forms that already worked, pinned ───────────────────────────────────


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("Country", '"Country" ASC'),
        ("Order Month", '"Order Month" ASC'),
        ("Total Amount", '"Total Amount" ASC'),
        ("1", "1 ASC"),
    ],
)
def test_the_other_ordering_keys_are_unchanged(
    model: SemanticModel, field: str, expected: str
) -> None:
    """A plain column, a time-grained dimension, a measure and an ordinal.

    All four resolved to the projection's alias before this change. The ordinal
    is the workaround the issue documents, and it has to keep working.
    """
    query = {
        "select": {
            "dimensions": ["Country", "Order Month"],
            "measures": ["Total Amount", "Grand Total Amount"],
        },
        "orderBy": [{"field": field, "direction": "asc"}],
    }
    assert _outer_order_by(_sql(model, query)) == expected


def test_an_unwrapped_query_still_inlines_the_expression(model: SemanticModel) -> None:
    """Nothing to read an alias from without a wrap, so the CASE belongs there.

    The star planner's ORDER BY sits in the same query as the GROUP BY it
    repeats, which is what makes the inlined form correct in that shape and
    wrong in the wrapped one.
    """
    query = {
        "select": {"dimensions": ["Region"], "measures": ["Total Amount"]},
        "orderBy": [{"field": "Region", "direction": "asc"}],
    }
    assert "CASE WHEN" in _outer_order_by(_sql(model, query))


# ── the projection defect the same query shape hit next ─────────────────────


def test_period_over_period_projects_the_computed_dimension(model: SemanticModel) -> None:
    """``pop_base`` read ``source_column``, which a computed dimension has none of.

    It quoted the empty string, so the CTE selected ``"Customer"."" AS "Region"``
    and no engine parsed the statement. Found by executing the fixed ORDER BY.

    The expression is evaluated once, in the derived table the spine is joined
    to, and read back by the alias that table projects it under.
    """
    sql = _sql(model, WRAPPED_QUERIES["period over period"])
    assert '""' not in sql, sql
    # The source carries the column out; the expression is evaluated over what
    # it projects, so the dimension is spelled once and reads in scope.
    assert '"Customer"."country" AS "Customer__country"' in sql
    assert 'CASE WHEN "__ob_pop_src"."Customer__country"' in sql


TZ_MODEL_YAML = """
version: 1.0
name: pop_timezone
settings:
  queryTimezone: Europe/Berlin
  defaultTimezone: UTC

dataObjects:
  Event:
    code: event
    columns:
      Created:  {code: created, abstractType: timestamp}
      Occurred: {code: occurred, abstractType: timestamp}
      Amount:   {code: amount, abstractType: float, numClass: additive}

dimensions:
  Created At:     {dataObject: Event, column: Created, resultType: timestamp}
  Occurred Month: {dataObject: Event, column: Occurred, resultType: timestamp, timeGrain: month}

measures:
  Total:
    columns: [{dataObject: Event, column: Amount}]
    resultType: float
    aggregation: sum

metrics:
  Total MoM:
    type: period_over_period
    expression: '{[Total]}'
    periodOverPeriod:
      timeDimension: Occurred Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""


def test_period_over_period_reads_a_dimension_in_the_query_timezone() -> None:
    """The same funnel carries the query zone, which this wrapper alone skipped.

    ``make_column_expr`` converts a timestamp column to ``queryTimezone`` at
    the leaf, so every planner reads the same instant in the same frame. Reading
    ``source_column`` bypassed that, and a PoP query grouped a timestamp
    dimension by the database's zone while an otherwise identical query without
    the metric grouped it by the model's.
    """
    raw, source_map = TrackedLoader().load_string(TZ_MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = {
        "select": {
            "dimensions": ["Created At", "Occurred Month"],
            "measures": ["Total", "Total MoM"],
        }
    }
    assert "AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Berlin'" in _sql(model, query)


POP_DIMENSIONS_YAML = """
version: 1.0
name: pop_dimensions

dataObjects:
  Event:
    code: event
    columns:
      Created:  {code: created, abstractType: date}
      Occurred: {code: occurred, abstractType: date}
      Amount:   {code: amount, abstractType: float, numClass: additive}
      Effective:
        expression: "CASE WHEN {Amount} > 0 THEN {Occurred} ELSE {Created} END"
        abstractType: date

dimensions:
  Created Month:   {dataObject: Event, column: Created, resultType: date, timeGrain: month}
  Occurred Month:  {dataObject: Event, column: Occurred, resultType: date, timeGrain: month}
  Effective Month: {dataObject: Event, column: Effective, resultType: date, timeGrain: month}

measures:
  Total:
    columns: [{dataObject: Event, column: Amount}]
    resultType: float
    aggregation: sum

metrics:
  Total MoM:
    type: period_over_period
    expression: '{[Total]}'
    periodOverPeriod:
      timeDimension: Occurred Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
  Effective MoM:
    type: period_over_period
    expression: '{[Total]}'
    periodOverPeriod:
      timeDimension: Effective Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""


@pytest.fixture(scope="module")
def pop_model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(POP_DIMENSIONS_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def _pop_connection() -> duckdb.DuckDBPyConnection:
    """Two January rows and one February row, so a month grouping is visible."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE event AS SELECT * FROM (VALUES"
        " (DATE '2024-01-05', DATE '2024-01-05', 10.0),"
        " (DATE '2024-01-20', DATE '2024-01-20', 20.0),"
        " (DATE '2024-02-10', DATE '2024-02-10', 30.0)"
        ") t(created, occurred, amount)"
    )
    return con


def test_a_computed_column_is_a_valid_pop_time_dimension(pop_model: SemanticModel) -> None:
    """The date-range scan and the spine join read it the same way the projection does.

    Both built the column from its physical name, which a computed dimension
    does not have, so they emitted ``MIN("Event"."")`` and joined on the same -
    from a model that validates clean and warns about nothing.
    """
    query = {"select": {"dimensions": ["Effective Month"], "measures": ["Total", "Effective MoM"]}}
    sql = _sql(pop_model, query)
    assert '""' not in sql, sql
    assert 'CASE WHEN "Event"."amount" > 0' in sql
    assert _pop_connection().execute(sql).fetchall()


def test_a_second_time_grained_dimension_keeps_its_grain(pop_model: SemanticModel) -> None:
    """``pop_base`` grouped by the raw value under a column labelled by the month.

    Two rows of the same month stayed two rows, each carrying a day-level date
    in a column called ``Created Month``. Executed, because the wrong answer
    here is a plausible-looking one.
    """
    query = {
        "select": {
            "dimensions": ["Occurred Month", "Created Month"],
            "measures": ["Total", "Total MoM"],
        }
    }
    rows = _pop_connection().execute(_sql(pop_model, query)).fetchall()
    assert len(rows) == 2, rows
    by_month = {row[1].strftime("%Y-%m-%d"): row[2] for row in rows}
    assert by_month == {"2024-01-01": 30, "2024-02-01": 30}


CROSS_OBJECT_YAML = """
version: 1.0
name: pop_cross_object

dataObjects:
  Calendar:
    code: calendar
    columns:
      Day:        {code: day, abstractType: date}
      Is Holiday: {code: is_holiday, abstractType: boolean}
  Event:
    code: event
    joins:
      - joinTo: Calendar
        columnsFrom: [Created]
        columnsTo: [Day]
        joinType: many-to-one
    columns:
      Created: {code: created, abstractType: date}
      Amount:  {code: amount, abstractType: float, numClass: additive}
      Effective:
        expression: "CASE WHEN {[Calendar].[Is Holiday]} THEN {Created} ELSE {Created} END"
        abstractType: date

dimensions:
  Effective Month: {dataObject: Event, column: Effective, resultType: date, timeGrain: month}

measures:
  Total:
    columns: [{dataObject: Event, column: Amount}]
    resultType: float
    aggregation: sum

metrics:
  Effective MoM:
    type: period_over_period
    expression: '{[Total]}'
    periodOverPeriod:
      timeDimension: Effective Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
"""


def test_a_pop_time_dimension_may_read_another_data_object() -> None:
    """The expression is computed inside the source, where its joins are in scope.

    It was refused while ``pop_base`` hung the fact tables off the spine: the
    spine join came first, and a join's ON cannot name a table joined after it.
    With the join tree in a derived table beneath the spine, the bucket is a
    plain column by the time the spine reads it, and the shape is ordinary.
    """
    raw, source_map = TrackedLoader().load_string(CROSS_OBJECT_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = {"select": {"dimensions": ["Effective Month"], "measures": ["Total", "Effective MoM"]}}
    sql = _sql(model, query)
    assert '"__ob_pop_src"."__ob_bucket" = "date_spine".spine_date' in sql

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE calendar AS SELECT * FROM (VALUES"
        " (DATE '2024-01-05', true), (DATE '2024-02-05', false)) t(day, is_holiday)"
    )
    con.execute(
        "CREATE TABLE event AS SELECT * FROM (VALUES"
        " (DATE '2024-01-05', 10.0), (DATE '2024-02-05', 20.0)) t(created, amount)"
    )
    rows = sorted(con.execute(sql).fetchall(), key=lambda row: row[0])
    assert rows == [
        (datetime.date(2024, 1, 1), Decimal("10.00"), None),
        (datetime.date(2024, 2, 1), Decimal("20.00"), Decimal("10.00")),
    ]
