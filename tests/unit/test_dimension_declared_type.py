"""A time-grained dimension keeps the type its model declares (#369).

A dimension declaring ``resultType: date`` kept that type until it was given a
``timeGrain``. Then it became whatever the engine's truncation function returns,
which is not the same thing on every engine, measured on the same model and the
same column:

===========  ==================  =========================
engine       plain, ungrained    plain, ``timeGrain: month``
===========  ==================  =========================
DuckDB       ``DATE``            ``TIMESTAMP``
PostgreSQL   ``date``            ``timestamptz``
ClickHouse   ``Date``            ``Date``
===========  ==================  =========================

The PostgreSQL cell is the one that makes it more than a mismatch:
``date_trunc('month', DATE ...)`` resolves to the ``timestamptz`` overload, so
the value carries the *session's* zone and a client converting to UTC can read
the month before. A ``date`` has no instant, which is the point of declaring
one.

Measures have always been cast to their declared type at projection; dimensions
were cast nowhere.
"""

from __future__ import annotations

import datetime

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.models.types import parse_data_type
from orionbelt.parser import ReferenceResolver, TrackedLoader

MODEL_YAML = """
version: 1.0
name: dimension_types

dataObjects:
  Event:
    code: event
    columns:
      Occurred: {code: occurred, abstractType: date}
      Stamp:    {code: stamp, abstractType: timestamp}
      Amount:   {code: amount, abstractType: float, numClass: additive}

dimensions:
  Occurred Day:   {dataObject: Event, column: Occurred, resultType: date}
  Occurred Month: {dataObject: Event, column: Occurred, resultType: date, timeGrain: month}
  Stamp Hour:     {dataObject: Event, column: Stamp, resultType: timestamp, timeGrain: hour}
  Month Label:    {dataObject: Event, column: Occurred, resultType: string, timeGrain: month}
  Zoned Clock:    {dataObject: Event, column: Stamp, resultType: time_tz}

measures:
  Total:
    columns: [{dataObject: Event, column: Amount}]
    resultType: float
    aggregation: sum
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def _sql(model: SemanticModel, dimension: str, dialect: str = "duckdb") -> str:
    query = QueryObject.model_validate(
        {"select": {"dimensions": [dimension], "measures": ["Total"]}}
    )
    return CompilationPipeline().compile(query, model, dialect).sql


# ── the defect ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
def test_a_grained_date_dimension_is_cast_to_date(model: SemanticModel, dialect: str) -> None:
    """Every dialect, because the engines disagree about what truncation returns.

    Asserted on the *dimension's* own projection, and on the dialect's own
    spelling of a date, since a measure carries a cast of its own and eight
    engines do not name a type the same way.
    """
    d = DialectRegistry.get(dialect)
    sql = _sql(model, "Occurred Month", dialect)
    alias = d.quote_identifier("Occurred Month")
    projection = sql[sql.index("SELECT") + len("SELECT ") : sql.index(f" AS {alias}")]
    date_type = d.render_obml_type(parse_data_type("date"))
    # The whole grain expression is wrapped, rather than the type name merely
    # appearing somewhere: MySQL's month grain is ``DATE_FORMAT``, which carries
    # the word DATE in its own name.
    assert projection.startswith("CAST("), projection
    assert projection.endswith(f" AS {date_type})") or projection.endswith(
        f" AS Nullable({date_type}))"
    ), projection


def test_the_select_and_the_group_by_stay_identical(model: SemanticModel) -> None:
    """Both go through one funnel, so the cast cannot land on only one of them.

    On PostgreSQL, which spells its GROUP BY out; DuckDB emits ``GROUP BY ALL``
    and has nothing to compare.
    """
    sql = _sql(model, "Occurred Month", "postgres")
    select = sql[sql.index("SELECT") : sql.index("FROM")]
    group_by = sql[sql.index("GROUP BY") :]
    grained = 'CAST(DATE_TRUNC(\'month\', "Event"."occurred") AS DATE)'
    assert grained in select
    assert grained in group_by


def test_duckdb_returns_a_date_for_a_date_dimension(model: SemanticModel) -> None:
    """Executed: the type a client binds, not just the SQL text.

    Grained and ungrained agreed on the value and disagreed on the type, and
    only the ungrained one matched the declaration.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE event AS SELECT * FROM (VALUES"
        " (DATE '2024-01-10', TIMESTAMP '2024-01-10 08:30', 10.0)) t(occurred, stamp, amount)"
    )
    types = {}
    for dimension in ("Occurred Day", "Occurred Month"):
        cursor = con.execute(_sql(model, dimension))
        types[dimension] = str(cursor.description[0][1])
        cursor.fetchall()
    assert types == {"Occurred Day": "DATE", "Occurred Month": "DATE"}


# ── what the cast deliberately leaves alone ─────────────────────────────────


def test_a_timestamp_dimension_keeps_its_time(model: SemanticModel) -> None:
    """The cast follows the declaration, not the grain.

    An hour grain over a column declared ``timestamp`` has a time part to keep,
    and casting it to the grain's own idea of a type would throw that away.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE event AS SELECT * FROM (VALUES"
        " (DATE '2024-01-10', TIMESTAMP '2024-01-10 08:30', 10.0)) t(occurred, stamp, amount)"
    )
    rows = con.execute(_sql(model, "Stamp Hour")).fetchall()
    assert str(rows[0][0]) == "2024-01-10 08:00:00"


def test_a_string_dimension_over_a_grain_is_not_cast(model: SemanticModel) -> None:
    """A grain declared ``string`` is asking for a label, and a label it stays.

    MySQL renders a month grain as ``DATE_FORMAT``, a string by construction, so
    a model that wants the label declares ``resultType: string``. Casting that
    to ``CHAR`` would add a cast that says nothing.
    """
    sql = _sql(model, "Month Label", "mysql")
    assert "DATE_FORMAT(`Event`.`occurred`, '%Y-%m-01') AS `Month Label`" in sql
    assert "CAST(DATE_FORMAT" not in sql


def test_an_ungrained_dimension_is_unchanged(model: SemanticModel) -> None:
    """Nothing to correct: the column already has the type the model declares."""
    projection = _sql(model, "Occurred Day").split("FROM")[0]
    assert projection.count("CAST(") == 1  # the measure's own cast
    assert '"Event"."occurred" AS "Occurred Day"' in projection


EXCLUDE_MODEL_YAML = """
version: 1.0
name: exclude_grain

dataObjects:
  Calendar:
    code: calendar
    columns:
      Day: {code: day, abstractType: date}
  Product:
    code: product
    columns:
      SKU:      {code: sku, abstractType: string}
      Category: {code: category, abstractType: string}
  Sales:
    code: sales
    joins:
      - {joinTo: Calendar, columnsFrom: [Sold On], columnsTo: [Day], joinType: many-to-one}
      - {joinTo: Product, columnsFrom: [SKU], columnsTo: [SKU], joinType: many-to-one}
    columns:
      Sold On: {code: sold_on, abstractType: date}
      SKU:     {code: sku, abstractType: string}
      Amount:  {code: amount, abstractType: float, numClass: additive}

dimensions:
  Sale Month: {dataObject: Calendar, column: Day, resultType: date, timeGrain: month}
  Category:   {dataObject: Product, column: Category, resultType: string}

measures:
  Total:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
"""


def _exclude_sql() -> str:
    raw, source_map = TrackedLoader().load_string(EXCLUDE_MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = QueryObject.model_validate(
        {"select": {"dimensions": ["Sale Month", "Category"]}, "dimensionsExclude": True}
    )
    return CompilationPipeline().compile(query, model, "duckdb").sql


def test_dimensions_exclude_asks_at_the_declared_grain() -> None:
    """Both sides of the EXCEPT read the dimension the same way.

    This path built its own projections and never rendered the grain at all, so
    the candidate combinations were *day* pairs under a column labelled by the
    month, and the anti-join answered a question nobody asked. Both sides go
    through the one funnel now, so they cannot describe different things.
    """
    sql = _exclude_sql()
    grained = 'CAST(DATE_TRUNC(\'month\', "Calendar"."day") AS DATE) AS "Sale Month"'
    assert sql.count(grained) == 2, sql
    assert '"Calendar"."day" AS "Sale Month"' not in sql


def test_dimensions_exclude_returns_the_missing_month_combinations() -> None:
    """Executed, because the wrong answer here is a plausible-looking one.

    January sold Toys, February sold Books. The combinations that never
    happened are January/Books and February/Toys. Ungrained, the query compared
    day pairs and answered with three of the four *days*, including a January
    day on which Toys were not sold.
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE calendar AS SELECT * FROM (VALUES"
        " (DATE '2024-01-05'), (DATE '2024-01-20'), (DATE '2024-02-10')) t(day)"
    )
    con.execute(
        "CREATE TABLE product AS SELECT * FROM (VALUES"
        " ('s1', 'Toys'), ('s2', 'Books')) t(sku, category)"
    )
    con.execute(
        "CREATE TABLE sales AS SELECT * FROM (VALUES"
        " (DATE '2024-01-05', 's1', 10.0), (DATE '2024-02-10', 's2', 20.0)) t(sold_on, sku, amount)"
    )
    rows = sorted(con.execute(_exclude_sql()).fetchall(), key=str)
    assert rows == [
        (datetime.date(2024, 1, 1), "Books"),
        (datetime.date(2024, 2, 1), "Toys"),
    ]


# ── a grain the query names, which the model never saw ──────────────────────


class TestAQueryGrainIsHeldToTheSameRule:
    """``dimension:grain`` overrides the model, so it can write the lossy pair.

    The validator refuses a declared ``resultType`` that cannot hold its own
    ``timeGrain``, but a query names a grain of its own: ``Occurred Day:hour``
    asks for hours of a dimension that declares ``date`` and no grain at all.
    The model is valid and cannot be checked for it - the combination does not
    exist until the query is written - and the cast merged the buckets just the
    same. Measured on DuckDB over three rows at 09:00, 10:00 and 11:00: two
    rows, the two hours of one day summed into 30.00, with no error.
    """

    @pytest.mark.parametrize(
        "dimension",
        [
            "Occurred Day:hour",  # date drops the time of day, and merges the buckets
            # ``time_tz`` is not a cast target, so this one merges nothing: the
            # value is left alone and only the label is wrong. Refused all the
            # same - a grain always carries a date.
            "Zoned Clock:hour",
            "Zoned Clock:month",
        ],
    )
    def test_a_grain_the_declared_type_cannot_hold_is_refused(
        self, model: SemanticModel, dimension: str
    ) -> None:
        from orionbelt.compiler.resolution import ResolutionError

        with pytest.raises(ResolutionError) as caught:
            _sql(model, dimension)
        assert [e.code for e in caught.value.errors] == ["RESULT_TYPE_LOSES_GRAIN"]
        assert dimension.split(":")[1] in caught.value.errors[0].message

    @pytest.mark.parametrize(
        "dimension",
        [
            "Stamp Hour:day",  # timestamp holds a coarser bucket
            "Occurred Day:month",  # date holds a month
            "Month Label:hour",  # a string is not cast, so nothing is dropped
        ],
    )
    def test_a_grain_the_declared_type_holds_is_allowed(
        self, model: SemanticModel, dimension: str
    ) -> None:
        assert "SELECT" in _sql(model, dimension)

    def test_an_hourly_override_still_answers_hours_where_it_is_declarable(
        self, model: SemanticModel
    ) -> None:
        """Executed, so the refusal above cannot be a refusal of the feature.

        ``Stamp Hour`` declares ``timestamp``, which holds an hour, so asking
        for one over it is exactly the query the other test refuses on a
        ``date`` dimension - and it must still come back with a row per hour.
        """
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE event AS SELECT * FROM (VALUES"
            " (DATE '2024-03-15', TIMESTAMP '2024-03-15 09:00', 10.0),"
            " (DATE '2024-03-15', TIMESTAMP '2024-03-15 10:00', 20.0),"
            " (DATE '2024-04-20', TIMESTAMP '2024-04-20 11:00', 5.0)) t(occurred, stamp, amount)"
        )
        rows = sorted(con.execute(_sql(model, "Stamp Hour:hour")).fetchall())
        assert [(r[0], float(r[1])) for r in rows] == [
            (datetime.datetime(2024, 3, 15, 9, 0), 10.0),
            (datetime.datetime(2024, 3, 15, 10, 0), 20.0),
            (datetime.datetime(2024, 4, 20, 11, 0), 5.0),
        ]
