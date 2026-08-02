"""A metric over a measure must mean what the measure means.

Every compiler pass that reads ``resolved.measures`` has a sibling path through
``resolved.metric_components``, because a metric is planned by inlining its
components rather than by selecting them. Nothing forces an author to notice the
second path, and five separate defects in one change came from missing it: the
CFL leg projection, metric substitution, the cumulative and window wrappers, the
mixed-grain fan-out warning, and CFL leg assignment. Each time the direct
measure was correct and the metric compiled to SQL naming a table its FROM did
not have, or dropped a warning.

This is the shape of that bug, expressed once. For each representative measure,
the same query is compiled twice - selecting the measure, and selecting a metric
that adds zero to it - and both are *executed*. Execution is what makes the
guard general: an unbound column reference cannot survive it, and a wrongly
grained rewrite shows up as a different number. Adding zero keeps the two
comparable without adding a code path of its own.

Adding a pass, or a rewrite inside one, means adding a case here.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

_BASE = """\
version: 1.0
name: parity
dataObjects:
  Calendar:
    code: calendar
    schema: main
    columns:
      Date Key: {code: datekey, abstractType: string, primaryKey: true}
      Year: {code: year, abstractType: int}
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: pid, abstractType: string, primaryKey: true}
      List Price: {code: price, abstractType: float}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Date Key: {code: datekey, abstractType: string}
      Sale Product ID: {code: pid, abstractType: string}
      Qty: {code: qty, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]
  Returns:
    code: returns
    schema: main
    columns:
      Return ID: {code: id, abstractType: string, primaryKey: true}
      Return Date Key: {code: datekey, abstractType: string}
      Qty: {code: qty, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
  Visits:
    code: visits
    schema: main
    columns:
      Visit ID: {code: id, abstractType: string, primaryKey: true}
      Visit Date Key: {code: datekey, abstractType: string}
      Count: {code: cnt, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Visit Date Key]
        columnsTo: [Date Key]
dimensions:
  Year: {dataObject: Calendar, column: Year, resultType: int}
measures:
  Sales Qty:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Qty]}'
  Visit Total:
    resultType: float
    aggregation: sum
    expression: '{[Visits].[Count]}'
  Extended Price:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Qty]} * {[Products].[List Price]}'
  Anchored Cross:
    resultType: float
    aggregation: sum
    anchor: Sales
    expression: '{[Sales].[Qty]} * {[Returns].[Qty]}'
  Shared Key Cross:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Qty]} * {[Returns].[Qty]}'
  Return Pairs:
    resultType: int
    aggregation: count_distinct
    columns:
      - {dataObject: Returns, column: Return ID}
      - {dataObject: Returns, column: Return Date Key}
  Return List:
    resultType: string
    aggregation: listagg
    delimiter: ","
    columns: [{dataObject: Returns, column: Return ID}]
    withinGroup:
      column: {dataObject: Calendar, column: Date Key}
      order: ASC
"""


def _model(yaml_text: str) -> SemanticModel:
    model, result = ReferenceResolver().resolve(YAML(typ="safe").load(yaml_text))
    assert not result.errors, result.errors
    return model


def _db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE main.calendar(datekey VARCHAR, year INT)")
    con.execute("INSERT INTO main.calendar VALUES ('d1',2024),('d2',2024)")
    con.execute("CREATE TABLE main.products(pid VARCHAR, price DOUBLE)")
    con.execute("INSERT INTO main.products VALUES ('p1',10),('p2',4)")
    con.execute("CREATE TABLE main.sales(id VARCHAR, datekey VARCHAR, pid VARCHAR, qty DOUBLE)")
    con.execute(
        "INSERT INTO main.sales VALUES "
        "('s1','d1','p1',2),('s2','d1','p2',3),('s3','d1','p1',5),('s4','d2','p2',6)"
    )
    con.execute("CREATE TABLE main.returns(id VARCHAR, datekey VARCHAR, qty DOUBLE)")
    con.execute("INSERT INTO main.returns VALUES ('r1','d1',4),('r2','d2',1),('r3','d2',7)")
    con.execute("CREATE TABLE main.visits(id VARCHAR, datekey VARCHAR, cnt DOUBLE)")
    con.execute("INSERT INTO main.visits VALUES ('v1','d1',11),('v2','d2',9)")
    return con


# (case id, the measures a query selects, the one wrapped in a metric).
# Each names the pass whose metric path was, or could be, missed.
_CASES = [
    pytest.param(["Sales Qty"], "Sales Qty", id="plain-star"),
    pytest.param(["Extended Price"], "Extended Price", id="replicated-object-warning"),
    pytest.param(["Anchored Cross"], "Anchored Cross", id="anchored-star"),
    pytest.param(["Shared Key Cross"], "Shared Key Cross", id="shared-key-conform"),
    pytest.param(["Anchored Cross", "Visit Total"], "Anchored Cross", id="anchored-in-cfl"),
    pytest.param(["Sales Qty", "Visit Total"], "Sales Qty", id="cfl-leg"),
    pytest.param(["Return Pairs", "Sales Qty"], "Return Pairs", id="multi-field-in-cfl"),
    pytest.param(["Return List", "Sales Qty"], "Return List", id="ordered-aggregate-in-cfl"),
]


def _compile(yaml_text: str, measures: list[str]):
    query = QueryObject(**{"select": {"dimensions": ["Year"], "measures": measures}})
    return CompilationPipeline().compile(query, _model(yaml_text), "duckdb")


def _echo_metric(measure: str) -> str:
    """A metric that adds nothing, so any difference is the planner's."""
    numeric = f"{{[{measure}]}} + 0"
    return f"metrics:\n  Echo: {{dataType: double, expression: '{numeric}'}}\n"


@pytest.mark.parametrize(("measures", "wrapped"), _CASES)
def test_a_metric_over_a_measure_executes_to_the_same_value(
    measures: list[str], wrapped: str
) -> None:
    """The metric must bind and agree, in whatever plan the query lands in.

    Executed rather than string-compared: an unbound column reference, which is
    how every one of these defects surfaced, cannot survive execution, and a
    rewrite applied at the wrong grain shows up as a different number.
    """
    if wrapped == "Return List":
        pytest.skip("listagg is not numeric, so '+ 0' is not a valid echo")

    con = _db()
    direct = con.execute(_compile(_BASE, measures).sql).fetchall()
    direct_value = float(direct[0][1 + measures.index(wrapped)])

    metric_measures = [m for m in measures if m != wrapped] + ["Echo"]
    echoed = _compile(_BASE + _echo_metric(wrapped), metric_measures)
    row = con.execute(echoed.sql).fetchall()[0]
    assert float(row[-1]) == pytest.approx(direct_value), echoed.sql


@pytest.mark.parametrize(("measures", "wrapped"), _CASES)
def test_a_metric_over_a_measure_compiles_at_all(measures: list[str], wrapped: str) -> None:
    """Covers the shapes ``+ 0`` cannot echo, such as ``listagg``.

    Binding is the weaker claim, but it is the one that failed every time: the
    SQL named a table the plan never joined.
    """
    metric_expr = f"{{[{wrapped}]}}"
    yaml_text = _BASE + f"metrics:\n  Echo: {{dataType: string, expression: '{metric_expr}'}}\n"
    metric_measures = [m for m in measures if m != wrapped] + ["Echo"]
    _db().execute(_compile(yaml_text, metric_measures).sql).fetchall()


@pytest.mark.parametrize(("measures", "wrapped"), _CASES)
def test_a_metric_over_a_measure_carries_the_same_warnings(
    measures: list[str], wrapped: str
) -> None:
    """A warning dropped by the metric path is a silent loss of a correctness note.

    This is exactly how the mixed-grain fan-out warning was missed: selecting
    the measure warned, selecting a metric over it did not, and both compiled
    the same duplicated expression.
    """
    metric_expr = f"{{[{wrapped}]}}"
    yaml_text = _BASE + f"metrics:\n  Echo: {{dataType: string, expression: '{metric_expr}'}}\n"
    metric_measures = [m for m in measures if m != wrapped] + ["Echo"]
    direct_codes = {w.code for w in _compile(_BASE, measures).warnings}
    metric_codes = {w.code for w in _compile(yaml_text, metric_measures).warnings}
    assert direct_codes <= metric_codes, f"metric path lost {sorted(direct_codes - metric_codes)}"


# The wrapper metrics. Each builds its own base CTE and re-derives the
# component's aggregate into it, which is a second copy of the same hazard: the
# re-derived expression names tables the wrapper's FROM does not have. Compared
# by execution only, since a running total or a prior-period value is not equal
# to the measure it is computed from.
_WRAPPER_METRICS = {
    "cumulative": (
        "  Echo:\n    type: cumulative\n    measure: {measure}\n    timeDimension: Year\n"
    ),
    "window": (
        "  Echo:\n    type: window\n    windowFunction: dense_rank\n    measure: {measure}\n"
    ),
    "period_over_period": (
        "  Echo:\n    type: period_over_period\n    expression: '{{[{measure}]}}'\n"
        "    periodOverPeriod:\n      timeDimension: Year\n      grain: year\n"
        "      offset: -1\n      offsetGrain: year\n      comparison: difference\n"
    ),
}


@pytest.mark.parametrize("kind", sorted(_WRAPPER_METRICS))
@pytest.mark.parametrize(
    ("measures", "wrapped"),
    [
        pytest.param(["Anchored Cross"], "Anchored Cross", id="anchored-star"),
        pytest.param(["Anchored Cross", "Visit Total"], "Anchored Cross", id="anchored-in-cfl"),
    ],
)
def test_a_wrapper_metric_over_an_anchored_measure_never_emits_unbound_sql(
    kind: str, measures: list[str], wrapped: str
) -> None:
    """Cumulative, window and period-over-period each re-project the component.

    Whatever they re-project has to be the form the plan beneath them actually
    produced: the conformed expression where the wrapper keeps the planner's
    joins, and the measure's own column once the input is a CTE.

    Refusing at compile time is an acceptable outcome - period-over-period
    rebuilds its FROM from a date spine and genuinely cannot carry the conformed
    subqueries, so it says so. What is never acceptable is SQL the database
    rejects, which is what every one of these produced.
    """
    yaml_text = _BASE + "metrics:\n" + _WRAPPER_METRICS[kind].format(measure=wrapped)
    metric_measures = [m for m in measures if m != wrapped] + ["Echo"]
    try:
        compiled = _compile(yaml_text, metric_measures)
    except (ResolutionError, FanoutError):
        return  # refused with a domain error, which callers surface as a 422
    _db().execute(compiled.sql).fetchall()


def test_a_metric_over_an_anchored_total_measure_executes() -> None:
    """``total: true`` decomposes the metric into its components in a base CTE.

    Same hazard from a fourth direction: the decomposition re-projects the
    component's aggregate, and an anchored component's aggregate reads conformed
    columns rather than the foreign fact's own.
    """
    yaml_text = (
        _BASE.replace(
            """  Anchored Cross:
    resultType: float
    aggregation: sum
    anchor: Sales""",
            """  Anchored Cross:
    resultType: float
    aggregation: sum
    total: true
    anchor: Sales""",
        )
        + "metrics:\n  Echo: {dataType: double, expression: '{[Anchored Cross]} + 1'}\n"
    )
    _db().execute(_compile(yaml_text, ["Echo"]).sql).fetchall()


def test_an_avg_total_over_an_anchored_measure_never_emits_unbound_sql() -> None:
    """``AVG`` + ``total`` decomposes into sum and count helper columns.

    A separate re-projection from the non-AVG path beside it, because an average
    cannot be windowed directly and is computed as ``SUM(s)/SUM(c)``. The helper
    columns are built from the measure's expression, so an anchored measure's
    helpers named the foreign fact.
    """
    yaml_text = _BASE.replace(
        """  Anchored Cross:
    resultType: float
    aggregation: sum
    anchor: Sales""",
        """  Anchored Cross:
    resultType: float
    aggregation: avg
    total: true
    anchor: Sales""",
    )
    _db().execute(_compile(yaml_text, ["Anchored Cross"]).sql).fetchall()

    metric = (
        yaml_text + "metrics:\n  Echo: {dataType: double, expression: '{[Anchored Cross]} + 1'}\n"
    )
    _db().execute(_compile(metric, ["Echo"]).sql).fetchall()


def test_a_derived_metric_over_a_window_metric_over_an_anchored_measure() -> None:
    """One more level of nesting, and a branch of its own.

    A derived metric referencing a window metric lifts the window's *base*
    measure into ``window_base``. That lift is a different code path from the
    direct window projection, so fixing the direct one left this reading the
    resolved expression and naming the foreign fact.
    """
    yaml_text = (
        _BASE
        + """metrics:
  Cross Rank:
    type: window
    windowFunction: dense_rank
    measure: Anchored Cross
  Echo: {dataType: double, expression: '{[Cross Rank]} + 1'}
"""
    )
    try:
        compiled = _compile(yaml_text, ["Echo"])
    except (ResolutionError, FanoutError):
        return  # a documented refusal is acceptable; unbound SQL is not
    _db().execute(compiled.sql).fetchall()
