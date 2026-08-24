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
