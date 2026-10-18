"""A grain finer than the one a column's values hold is refused.

A column of month starts, truncated to a day, is its month starts: ``Order
Month:day`` answered one row per month under a label that says day, and
``:week`` the weeks of the month starts, without an error. A column holds a
grain - read from a computed column that is one ``date_trunc`` call, or
declared with ``grain`` - and a grain its values cannot answer is refused with
``GRAIN_FINER_THAN_DECLARED``.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel, TimeGrain, grain_derives, truncation_grain
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

MODEL_YAML = """
version: 1.0
dataObjects:
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Date: {code: order_date, abstractType: date}
      Order Month:
        expression: "date_trunc('month', {Order Date})"
        abstractType: date
      Month Key: {code: month_start, abstractType: date, grain: month}
      Amount: {code: amount, abstractType: float}
dimensions:
  Order Date: {dataObject: Orders, column: Order Date, resultType: date}
  Monthly Date: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}
  Order Month: {dataObject: Orders, column: Order Month, resultType: date}
  Month Key: {dataObject: Orders, column: Month Key, resultType: date}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders (id INTEGER, order_date DATE, month_start DATE, amount DOUBLE)"
    )
    con.execute(
        "INSERT INTO orders VALUES"
        " (1, DATE '2026-01-05', DATE '2026-01-01', 10),"
        " (2, DATE '2026-01-20', DATE '2026-01-01', 20),"
        " (3, DATE '2026-02-07', DATE '2026-02-01', 30),"
        " (4, DATE '2026-04-11', DATE '2026-04-01', 40)"
    )
    return con


def _model(yaml_text: str = MODEL_YAML) -> tuple[SemanticModel, list[tuple[str, str | None]]]:
    """The model and the load errors (code, path): the parser's and the validator's."""
    model, result = ReferenceResolver().resolve(YAML(typ="safe").load(yaml_text))
    errors = [*result.errors, *SemanticValidator().validate(model)]
    return model, [(e.code, e.path) for e in errors]


def _sql(dimension: str) -> str:
    model, errors = _model()
    assert errors == []
    query = QueryObject(**{"select": {"dimensions": [dimension], "measures": ["Revenue"]}})
    return CompilationPipeline().compile(query, model, "duckdb").sql


def _rows(con: duckdb.DuckDBPyConnection, dimension: str) -> list[tuple[str, float]]:
    return sorted((str(d)[:10], float(v)) for d, v in con.execute(_sql(dimension)).fetchall())


@pytest.mark.parametrize(
    "dimension",
    ["Order Month:day", "Order Month:week", "Month Key:day", "Month Key:week"],
)
def test_a_finer_grain_is_refused(dimension: str) -> None:
    with pytest.raises(ResolutionError) as raised:
        _sql(dimension)
    assert [e.code for e in raised.value.errors] == ["GRAIN_FINER_THAN_DECLARED"]
    assert "holds 'month' values" in raised.value.errors[0].message


def test_the_same_and_coarser_grains_are_answered(con: duckdb.DuckDBPyConnection) -> None:
    months = [("2026-01-01", 30.0), ("2026-02-01", 30.0), ("2026-04-01", 40.0)]
    assert _rows(con, "Order Month") == months
    assert _rows(con, "Order Month:month") == months
    assert _rows(con, "Month Key:month") == months
    assert _rows(con, "Order Month:quarter") == [("2026-01-01", 60.0), ("2026-04-01", 40.0)]
    assert _rows(con, "Month Key:year") == [("2026-01-01", 100.0)]


def test_a_time_grain_over_days_stays_a_default(con: duckdb.DuckDBPyConnection) -> None:
    """``timeGrain: month`` over a column of days is the dimension's default
    bucket: its column holds days, and ``:day`` of it returns them."""
    assert _rows(con, "Monthly Date:day") == [
        ("2026-01-05", 10.0),
        ("2026-01-20", 20.0),
        ("2026-02-07", 30.0),
        ("2026-04-11", 40.0),
    ]


@pytest.mark.parametrize(
    ("addition", "path"),
    [
        (
            "  Daily Month: {dataObject: Orders, column: Order Month, resultType: date,"
            " timeGrain: day}\n",
            "dimensions.Daily Month",
        ),
        (
            "metrics:\n"
            "  Weekly:\n"
            "    type: reaggregate\n"
            "    measure: Revenue\n"
            "    per: ['Month Key:week']\n"
            "    aggregation: avg\n",
            "metrics.Weekly.per",
        ),
        (
            "metrics:\n"
            "  Prior Day:\n"
            "    type: period_over_period\n"
            "    expression: '{[Revenue]}'\n"
            "    periodOverPeriod:\n"
            "      timeDimension: Order Month\n"
            "      grain: day\n"
            "      offset: -1\n"
            "      offsetGrain: day\n"
            "      comparison: previousValue\n",
            "metrics.Prior Day.periodOverPeriod",
        ),
        (
            "metrics:\n"
            "  Week To Date:\n"
            "    type: cumulative\n"
            "    measure: Revenue\n"
            "    timeDimension: Order Month\n"
            "    grainToDate: week\n",
            "metrics.Week To Date.grainToDate",
        ),
    ],
    ids=["dimension-time-grain", "reaggregate-per", "period-over-period", "cumulative-to-date"],
)
def test_a_model_naming_a_finer_grain_is_refused_at_load(addition: str, path: str) -> None:
    if addition.startswith("metrics:"):
        yaml_text = MODEL_YAML + addition
    else:
        yaml_text = MODEL_YAML.replace("measures:\n", addition + "measures:\n", 1)
    assert ("GRAIN_FINER_THAN_DECLARED", path) in _model(yaml_text)[1]


def test_a_grain_needs_a_date_bearing_column() -> None:
    yaml_text = MODEL_YAML.replace(
        "Amount: {code: amount, abstractType: float}",
        "Amount: {code: amount, abstractType: float, grain: month}",
    )
    assert ("TIME_GRAIN_ON_NON_TEMPORAL", "dataObjects.Orders.columns.Amount") in _model(yaml_text)[
        1
    ]


@pytest.mark.parametrize(
    ("expression", "grain"),
    [
        ("date_trunc('month', {d})", TimeGrain.MONTH),
        (" DATE_TRUNC( 'Week' , {d}) ", TimeGrain.WEEK),
        ("date_trunc('month', coalesce({d}, ')'))", TimeGrain.MONTH),
        ("(date_trunc('month', {d}))", TimeGrain.MONTH),
        (" (( date_trunc('month', {d}) )) ", TimeGrain.MONTH),
        ("(date_trunc('month', {d})) + INTERVAL 1 DAY", None),
        ("(date_trunc('month', {d})) - (date_trunc('day', {d}))", None),
        ("date_trunc('month', {d}) + INTERVAL 1 DAY", None),
        ("cast(date_trunc('month', {d}) as date)", None),
        ("date_trunc('fortnight', {d})", None),
        ("{d}", None),
    ],
)
def test_the_grain_read_from_an_expression(expression: str, grain: TimeGrain | None) -> None:
    assert truncation_grain(expression) == grain


@pytest.mark.parametrize(
    ("requested", "held", "derives"),
    [
        (TimeGrain.MONTH, TimeGrain.MONTH, True),
        (TimeGrain.QUARTER, TimeGrain.MONTH, True),
        (TimeGrain.YEAR, TimeGrain.MONTH, True),
        (TimeGrain.DAY, TimeGrain.MONTH, False),
        (TimeGrain.WEEK, TimeGrain.MONTH, False),
        (TimeGrain.WEEK, TimeGrain.DAY, True),
        (TimeGrain.WEEK, TimeGrain.WEEK, True),
        (TimeGrain.MONTH, TimeGrain.WEEK, False),
        (TimeGrain.DAY, TimeGrain.HOUR, True),
        (TimeGrain.HOUR, TimeGrain.DAY, False),
    ],
)
def test_which_grains_derive(requested: TimeGrain, held: TimeGrain, derives: bool) -> None:
    assert grain_derives(requested, held) is derives


def test_a_year_to_date_over_months_loads() -> None:
    """``grainToDate: year`` over a month column restarts per year: whole months."""
    yaml_text = MODEL_YAML + (
        "metrics:\n"
        "  Month To Date:\n"
        "    type: cumulative\n"
        "    measure: Revenue\n"
        "    timeDimension: Order Month\n"
        "    grainToDate: year\n"
    )
    assert _model(yaml_text)[1] == []


def test_a_parenthesised_truncation_is_refused_finer() -> None:
    """``(date_trunc('month', ...))`` holds months as well: its day was
    answered with the month starts."""
    yaml_text = MODEL_YAML.replace(
        "expression: \"date_trunc('month', {Order Date})\"",
        "expression: \"(date_trunc('month', {Order Date}))\"",
    )
    model, errors = _model(yaml_text)
    assert errors == []
    query = QueryObject(**{"select": {"dimensions": ["Order Month:day"], "measures": ["Revenue"]}})
    with pytest.raises(ResolutionError) as raised:
        CompilationPipeline().compile(query, model, "duckdb")
    assert [e.code for e in raised.value.errors] == ["GRAIN_FINER_THAN_DECLARED"]
