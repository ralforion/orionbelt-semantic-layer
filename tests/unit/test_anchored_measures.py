"""A measure expression reading two independent facts.

``SUM({[Sales].[Qty]} * {[Returns].[Qty]})`` has no value until something says
which rows it runs over: the two facts are independent, so no row carries both
columns. Each fact is aggregated to the key it shares with the other and joined
many-to-one, which is what makes the expression evaluable without a cartesian.

Which grain it is evaluated at is the whole question, and the tests here pin the
two answers apart. By default it is the *shared key*, because that reading is
symmetric: swapping the operands of a product cannot move the number. ``anchor:``
overrides it to a fact's own row grain, which is a different and equally valid
question, and one only the model can choose.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import anchored_conformed_objects, shared_key_anchor
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

TWO_FACT_YAML = """\
version: 1.0
name: anchored
dataObjects:
  Calendar:
    code: calendar
    schema: main
    columns:
      Date Key: {code: datekey, abstractType: string, primaryKey: true}
      Year: {code: year, abstractType: int}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Date Key: {code: datekey, abstractType: string}
      Qty: {code: qty, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
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
dimensions:
  Year: {dataObject: Calendar, column: Year, resultType: int}
measures:
  Cross:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Qty]} * {[Returns].[Qty]}'
"""


def _model(yaml_text: str) -> SemanticModel:
    model, result = ReferenceResolver().resolve(YAML(typ="safe").load(yaml_text))
    assert not result.errors, result.errors
    return model


def _sql(yaml_text: str, measures: list[str], dialect: str = "duckdb") -> str:
    query = QueryObject(**{"select": {"dimensions": ["Year"], "measures": measures}})
    return CompilationPipeline().compile(query, _model(yaml_text), dialect).sql


def _db() -> duckdb.DuckDBPyConnection:
    """Deliberately asymmetric row counts per key, so anchors disagree.

    Sales has three rows on ``d1`` against Returns' one, and one on ``d2``
    against Returns' two. A fixture with matching counts makes every anchor
    agree by coincidence and would let a wrong default pass.
    """
    con = duckdb.connect()
    con.execute("CREATE TABLE main.calendar(datekey VARCHAR, year INT)")
    con.execute("INSERT INTO main.calendar VALUES ('d1',2024),('d2',2024)")
    con.execute("CREATE TABLE main.sales(id VARCHAR, datekey VARCHAR, qty DOUBLE)")
    con.execute(
        "INSERT INTO main.sales VALUES ('s1','d1',2),('s2','d1',3),('s3','d1',5),('s4','d2',6)"
    )
    con.execute("CREATE TABLE main.returns(id VARCHAR, datekey VARCHAR, qty DOUBLE)")
    con.execute("INSERT INTO main.returns VALUES ('r1','d1',4),('r2','d2',1),('r3','d2',7)")
    return con


def _with(yaml_text: str, *, aggregation: str = "sum", anchor: str | None = None) -> str:
    out = yaml_text.replace("    aggregation: sum", f"    aggregation: {aggregation}")
    if anchor:
        out = out.replace(
            "    resultType: float\n", f"    resultType: float\n    anchor: {anchor}\n"
        )
    return out


SWAPPED = TWO_FACT_YAML.replace(
    "'{[Sales].[Qty]} * {[Returns].[Qty]}'", "'{[Returns].[Qty]} * {[Sales].[Qty]}'"
)


@pytest.mark.parametrize(
    ("aggregation", "expected"),
    [("sum", 88.0), ("avg", 44.0), ("min", 40.0), ("max", 48.0)],
)
def test_the_default_grain_is_the_shared_key_and_survives_swapping_the_operands(
    aggregation: str, expected: float
) -> None:
    """The property that rules out every order-based default.

    ``a * b`` and ``b * a`` are the same product. Anchoring on whichever fact is
    written first makes them differ - ``AVG`` 22 against 29.33 on this data,
    because Sales has four rows to Returns' three - which no modeller expects
    from reordering a multiplication. Conforming *both* facts to the key they
    share is symmetric, so the two spellings have to agree.
    """
    con = _db()
    straight = con.execute(
        _sql(_with(TWO_FACT_YAML, aggregation=aggregation), ["Cross"])
    ).fetchall()[0][1]
    swapped = con.execute(_sql(_with(SWAPPED, aggregation=aggregation), ["Cross"])).fetchall()[0][1]
    assert float(straight) == pytest.approx(expected)
    assert float(swapped) == pytest.approx(expected)


def test_the_default_conforms_both_facts_and_roots_the_query_at_the_shared_key() -> None:
    """Neither fact drives: the key object does, with both facts aggregated onto it."""
    model = _model(TWO_FACT_YAML)
    measure = model.measures["Cross"]
    assert shared_key_anchor(model, measure) == "Calendar"
    assert anchored_conformed_objects(model, measure) == {"Sales", "Returns"}

    sql = _sql(TWO_FACT_YAML, ["Cross"])
    assert 'FROM "main"."calendar" AS "Calendar"' in sql
    # One GROUP BY subquery per fact, each joined on the calendar's own key.
    assert sql.count("__ob_conf_") >= 4
    assert '"Calendar"."datekey" = "__ob_conf_0"."__ob_ak0"' in sql
    assert '"Calendar"."datekey" = "__ob_conf_1"."__ob_ak0"' in sql


@pytest.mark.parametrize(
    ("anchor", "aggregation", "expected"),
    [
        # Per Sales row: qty * SUM(returns.qty for its date). 4 rows.
        ("Sales", "avg", 22.0),
        ("Sales", "min", 8.0),
        ("Sales", "max", 48.0),
        # Per Returns row: SUM(sales.qty for its date) * qty. 3 rows.
        ("Returns", "avg", 29.33),
        ("Returns", "min", 6.0),
        ("Returns", "max", 42.0),
    ],
)
def test_an_explicit_anchor_evaluates_at_that_facts_own_row_grain(
    anchor: str, aggregation: str, expected: float
) -> None:
    """``anchor:`` asks a different question, and the two answers differ.

    This is why the anchor cannot be guessed: both readings are defensible and
    they disagree on every aggregate whose value depends on the row population.
    """
    sql = _sql(_with(TWO_FACT_YAML, aggregation=aggregation, anchor=anchor), ["Cross"])
    assert float(_db().execute(sql).fetchall()[0][1]) == pytest.approx(expected, abs=0.01)


def test_sum_cannot_distinguish_the_anchors_which_is_why_sum_alone_proves_nothing() -> None:
    """``SUM`` is invariant across all three readings: each totals to the same thing.

    Recorded so a future change cannot be validated on ``SUM`` alone and called
    correct - the aggregates above are what actually discriminate.
    """
    con = _db()
    values = {
        label: float(con.execute(_sql(yaml_text, ["Cross"])).fetchall()[0][1])
        for label, yaml_text in (
            ("default", TWO_FACT_YAML),
            ("anchor-sales", _with(TWO_FACT_YAML, anchor="Sales")),
            ("anchor-returns", _with(TWO_FACT_YAML, anchor="Returns")),
        )
    }
    assert set(values.values()) == {88.0}


def test_the_conformed_fact_joins_many_to_one_so_the_anchor_keeps_its_grain() -> None:
    """The point of conforming: the joined side is one row per key, so no fanout.

    Joining the raw facts would pair every sale with every return of the same
    date - 5 rows from 4 and 3 here - and evaluate the expression over pairs
    that exist in neither fact.
    """
    con = _db()
    raw_pairs = con.execute(
        "SELECT COUNT(*) FROM main.sales s JOIN main.returns r USING(datekey)"
    ).fetchall()[0][0]
    assert raw_pairs == 5

    sql = _sql(_with(TWO_FACT_YAML, aggregation="count", anchor="Sales"), ["Cross"])
    assert con.execute(sql).fetchall()[0][1] == 4  # the Sales rows, not the 5 pairs


def test_a_declared_fact_to_fact_join_is_used_instead_of_conforming() -> None:
    """Conforming is the fallback, never an override of the model's own joins.

    Where a join path already reaches both facts the expression is evaluated
    over it, at the many side's grain, exactly as it was before anchors existed.
    """
    joined = TWO_FACT_YAML.replace(
        """      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
""",
        """      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Sales
        columnsFrom: [Return Date Key]
        columnsTo: [Sale ID]
""",
    )
    model = _model(joined)
    assert shared_key_anchor(model, model.measures["Cross"]) is None
    assert anchored_conformed_objects(model, model.measures["Cross"]) == set()

    sql = _sql(joined, ["Cross"])
    assert "__ob_conf_" not in sql
    assert '"Returns"."datekey" = "Sales"."id"' in sql


def test_a_measure_reading_one_fact_is_untouched() -> None:
    """No conforming where there is nothing to conform."""
    single = TWO_FACT_YAML.replace(
        "'{[Sales].[Qty]} * {[Returns].[Qty]}'", "'{[Returns].[Qty]} * 2'"
    )
    model = _model(single)
    assert shared_key_anchor(model, model.measures["Cross"]) is None
    assert "__ob_conf_" not in _sql(single, ["Cross"])


@pytest.mark.parametrize(
    "dialect", ["duckdb", "postgres", "bigquery", "snowflake", "clickhouse", "mysql", "dremio"]
)
def test_the_conformed_subquery_compiles_on_every_dialect(dialect: str) -> None:
    """The subquery is a plain grouped SELECT, so every dialect renders its own."""
    sql = _sql(TWO_FACT_YAML, ["Cross"], dialect)
    assert "__ob_ak0" in sql
    assert sql.upper().count("GROUP BY") >= 2


def test_an_anchor_naming_an_unknown_data_object_is_rejected() -> None:
    errors = SemanticValidator().validate(_model(_with(TWO_FACT_YAML, anchor="Nope")))
    assert any(e.code == "INVALID_ANCHOR_DATA_OBJECT" for e in errors), errors


def test_an_anchor_the_expression_never_reads_is_rejected() -> None:
    """A typo looks exactly like this, and the result would be silently wrong.

    Anchoring on an object the expression does not read leaves every column
    conformed in and the anchor acting as a bare row multiplier.
    """
    errors = SemanticValidator().validate(_model(_with(TWO_FACT_YAML, anchor="Calendar")))
    assert any(e.code == "INVALID_ANCHOR_DATA_OBJECT" for e in errors), errors
