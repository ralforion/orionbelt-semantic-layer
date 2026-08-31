"""A boolean measure source becomes a number only where one is read.

ClickHouse carries a type through MIN/MAX/any and its decimal conversion reads
the value as text, so ``MAX(flag)`` declared decimal arrived as 'true' and
raised. The cast that fixes it has to stop at the measures whose output is a
number: applied to every boolean source it changed what a *pass-through*
measure returns on every dialect, which is a public semantic, not a ClickHouse
detail.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """version: 1.0
dataObjects:
  ev:
    code: ev
    columns:
      Flag: {code: flag, abstractType: boolean}
      Amt:  {code: amt, abstractType: float, numClass: additive}
      Guarded:
        expression: "CASE WHEN {Flag} THEN {Amt} ELSE 0 END"
        abstractType: float
        numClass: additive
      Positive:
        expression: "{Amt} > 0"
        abstractType: boolean
measures:
  MaxPass:    {columns: [{dataObject: ev, column: Flag}], resultType: string, aggregation: max}
  AnyPass:
    columns: [{dataObject: ev, column: Flag}]
    resultType: string
    aggregation: any_value
  ListPass:   {columns: [{dataObject: ev, column: Flag}], resultType: string, aggregation: listagg}
  MinPass:    {columns: [{dataObject: ev, column: Flag}], resultType: string, aggregation: min}
  MaxDecimal:
    columns: [{dataObject: ev, column: Flag}]
    resultType: float
    dataType: "decimal(18, 2)"
    aggregation: max
  SumFlag:    {columns: [{dataObject: ev, column: Flag}], resultType: float, aggregation: sum}
  ExprPass:
    expression: "{[ev].[Flag]}"
    resultType: string
    aggregation: max
  ExprDecimal:
    expression: "{[ev].[Flag]}"
    resultType: float
    dataType: "decimal(18, 2)"
    aggregation: max
  CaseSum:
    expression: "CASE WHEN {[ev].[Flag]} THEN {[ev].[Amt]} ELSE 0 END"
    resultType: float
    aggregation: sum
  ComputedSum:
    columns: [{dataObject: ev, column: Guarded}]
    resultType: float
    aggregation: sum
  PositiveSum:
    columns: [{dataObject: ev, column: Positive}]
    resultType: float
    aggregation: sum
  PositiveExpr:
    expression: "{[ev].[Positive]}"
    resultType: float
    aggregation: sum
"""

DIALECTS = ("duckdb", "clickhouse", "postgres", "bigquery", "mysql", "snowflake")


@pytest.fixture(scope="module")
def model():
    path = Path(tempfile.mkdtemp()) / "m.yaml"
    path.write_text(MODEL_YAML)
    raw, source_map = TrackedLoader().load(path)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def _projection(model, measure: str, dialect: str) -> str:
    sql = (
        CompilationPipeline()
        .compile(QueryObject(select=QuerySelect(measures=[measure])), model, dialect)
        .sql
    )
    return sql.split("FROM")[0]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("measure", ["MaxPass", "AnyPass", "ListPass", "MinPass", "ExprPass"])
def test_a_pass_through_measure_keeps_its_boolean(model, measure: str, dialect: str) -> None:
    """No cast is emitted for these, so there is nothing for a number to serve.

    Casting them anyway changed the answer rather than its type: measured on
    DuckDB, ``MAX(flag)`` came back 1 rather than true and ``LISTAGG(flag)``
    read '1,0' rather than 'true,false'.
    """
    assert "CAST(" not in _projection(model, measure, dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("measure", ["MaxDecimal", "SumFlag", "ExprDecimal"])
def test_a_numeric_measure_reads_the_flag_as_a_number(model, measure: str, dialect: str) -> None:
    """These do emit a cast, and a boolean cannot arrive at a numeric one.

    ``ExprDecimal`` is the same measure as ``MaxDecimal`` written the other
    way. They are two spellings of one thing and the rule has to reach both:
    keyed on the ``columns:`` branch alone, the expression form went on
    failing exactly as before.
    """
    # Two casts, not one. Checking merely that a cast is present passes on the
    # broken rendering too, because the declared decimal emits one of its own
    # around the aggregate -- the boolean arriving inside it uncast is exactly
    # the bug. The inner one is what this is about.
    assert _projection(model, measure, dialect).count("CAST(") >= 2


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("measure", ["CaseSum", "ComputedSum"])
def test_a_boolean_used_as_a_predicate_is_left_alone(model, measure: str, dialect: str) -> None:
    """A predicate is not read as a number, and casting it breaks the query.

    Both of these have a numeric output, so the measure *is* read as a number
    -- but the boolean inside it is a condition, not the value. Rewriting every
    reference produced ``CASE WHEN CAST(flag AS INTEGER)``, which PostgreSQL
    refuses ("argument of CASE/WHEN must be type boolean") and BigQuery with
    it, while DuckDB accepted it and answered the same number. ``ComputedSum``
    reaches the same reference through a computed column's body, so a measure
    could break without naming a boolean at all.
    """
    projection = _projection(model, measure, dialect)
    assert "WHEN CAST(" not in projection, projection
    assert "CASE WHEN" in projection, projection


@pytest.mark.parametrize("measure", ["PositiveSum", "PositiveExpr"])
def test_a_computed_boolean_column_is_a_known_gap(model, measure: str) -> None:
    """Recorded rather than fixed, and older than this rule.

    ``{expression: "{Amt} > 0", abstractType: boolean}`` inlines to the
    comparison, which carries no declared type, so the source cannot be
    recognised as boolean and the measure emits ``SUM("ev"."amt" > 0)``.
    PostgreSQL rejects that -- its ``sum`` has no boolean overload -- and
    BigQuery with it.

    ``main`` emits the same SQL for the same model, in both measure spellings,
    so this rule leaves the gap exactly where it found it while fixing the
    bare-column case beside it. Closing it means carrying a declared type onto
    an inlined body. Pinned here so it cannot change unnoticed, and so the
    limitation is findable from the rule that does not cover it.
    """
    projection = _projection(model, measure, "postgres")
    assert "CAST(" in projection  # the declared numeric output, not the source
    assert '"ev"."amt" > 0' in projection, projection
    assert 'CAST("ev"."amt" > 0' not in projection, projection
