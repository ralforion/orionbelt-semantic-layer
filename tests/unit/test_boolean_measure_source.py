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
