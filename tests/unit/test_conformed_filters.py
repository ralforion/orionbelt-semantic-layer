"""A filter on a conformed fact chooses that fact's rows before it is conformed.

An anchored measure reads an independent fact through a subquery that
aggregates it to the key it shares with the anchor. A filter on that fact - a
query WHERE, a static model filter, or the measure's own ``filters:`` - was
refused (``FILTER_ON_CONFORMED_OBJECT``, ``AnchoredFilterNotSupportedError``):
applied outside, it would compare a per-key total. It is now applied inside the
subquery, joining what it reads beyond the fact, so it chooses the rows that are
aggregated.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.anchored import AnchoredFilterNotSupportedError
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
dataObjects:
  Calendar:
    code: calendar
    schema: main
    columns:
      Date Key: {code: datekey, abstractType: string, primaryKey: true}
      Year: {code: year, abstractType: int}
  Reasons:
    code: reasons
    schema: main
    columns:
      Reason ID: {code: id, abstractType: string, primaryKey: true}
      Category: {code: category, abstractType: string}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Date Key: {code: datekey, abstractType: string}
      Sale Status: {code: status, abstractType: string}
      Sale Qty: {code: qty, abstractType: float}
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
      Return Reason ID: {code: reason_id, abstractType: string}
      Return Status: {code: status, abstractType: string}
      Return Qty: {code: qty, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Reasons
        columnsFrom: [Return Reason ID]
        columnsTo: [Reason ID]
dimensions:
  Year: {dataObject: Calendar, column: Year, resultType: int}
  Day: {dataObject: Calendar, column: Date Key, resultType: string}
  Return Status: {dataObject: Returns, column: Return Status, resultType: string}
  Reason Category: {dataObject: Reasons, column: Category, resultType: string}
  Sale Status: {dataObject: Sales, column: Sale Status, resultType: string}
measures:
  Anchored Cross:
    resultType: float
    aggregation: sum
    anchor: Sales
    expression: '{[Sales].[Sale Qty]} * {[Returns].[Return Qty]}'
  Kept Cross:
    resultType: float
    aggregation: sum
    anchor: Sales
    expression: '{[Sales].[Sale Qty]} * {[Returns].[Return Qty]}'
    filters:
      - column: {dataObject: Returns, column: Return Status}
        operator: equals
        values: [{dataType: string, valueString: keep}]
  Damage Cross:
    resultType: float
    aggregation: sum
    anchor: Sales
    expression: '{[Sales].[Sale Qty]} * {[Returns].[Return Qty]}'
    filters:
      - column: {dataObject: Reasons, column: Category}
        operator: equals
        values: [{dataType: string, valueString: damage}]
  Either Cross:
    resultType: float
    aggregation: sum
    anchor: Sales
    expression: '{[Sales].[Sale Qty]} * {[Returns].[Return Qty]}'
    filters:
      - logic: or
        filters:
          - column: {dataObject: Returns, column: Return Status}
            operator: equals
            values: [{dataType: string, valueString: keep}]
          - column: {dataObject: Sales, column: Sale Status}
            operator: equals
            values: [{dataType: string, valueString: keep}]
  Shared Key Cross:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Sale Qty]} * {[Returns].[Return Qty]}'
  Fixed Cross:
    resultType: float
    aggregation: sum
    anchor: Sales
    expression: '{[Sales].[Sale Qty]} * {[Returns].[Return Qty]}'
    filterContext: {mode: FIXED}
  Sales Qty:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Sale Qty]}'
  Returned Qty:
    resultType: float
    aggregation: sum
    expression: '{[Returns].[Return Qty]}'
metrics:
  Daily Cross:
    type: reaggregate
    measure: Anchored Cross
    per: [Day]
    aggregation: avg
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE main.calendar (datekey VARCHAR, year INT)")
    con.execute("INSERT INTO main.calendar VALUES ('d1', 2024), ('d2', 2024)")
    con.execute("CREATE TABLE main.reasons (id VARCHAR, category VARCHAR)")
    con.execute("INSERT INTO main.reasons VALUES ('R1', 'damage'), ('R2', 'other')")
    con.execute("CREATE TABLE main.sales (id VARCHAR, datekey VARCHAR, status VARCHAR, qty DOUBLE)")
    # d1 sells 2 + 3, d2 sells 6.
    con.execute(
        "INSERT INTO main.sales VALUES ('s1', 'd1', 'keep', 2), ('s2', 'd1', 'drop', 3),"
        " ('s3', 'd2', 'keep', 6)"
    )
    con.execute(
        "CREATE TABLE main.returns"
        " (id VARCHAR, datekey VARCHAR, reason_id VARCHAR, status VARCHAR, qty DOUBLE)"
    )
    # d1 returns 4 (keep, damage) and 5 (drop, other), d2 returns 7 (keep, other).
    con.execute(
        "INSERT INTO main.returns VALUES ('r1', 'd1', 'R1', 'keep', 4),"
        " ('r2', 'd1', 'R2', 'drop', 5), ('r3', 'd2', 'R2', 'keep', 7)"
    )
    return con


def _model(yaml_text: str = MODEL_YAML) -> SemanticModel:
    model, result = ReferenceResolver().resolve(YAML(typ="safe").load(yaml_text))
    assert not result.errors, result.errors
    return model


def _compile(query: dict, yaml_text: str = MODEL_YAML):
    return CompilationPipeline().compile(QueryObject(**query), _model(yaml_text), "duckdb")


def _rows(con: duckdb.DuckDBPyConnection, query: dict, yaml_text: str = MODEL_YAML) -> list:
    sql = _compile(query, yaml_text).sql
    return [
        tuple(None if v is None else float(v) for v in row) for row in con.execute(sql).fetchall()
    ]


def _by_year(measures: list[str], where: list[dict] | None = None) -> dict:
    return {"select": {"dimensions": ["Year"], "measures": measures}, "where": where or []}


KEEP = {"field": "Return Status", "op": "equals", "value": "keep"}


def test_unfiltered(con: duckdb.DuckDBPyConnection) -> None:
    """Per sale, the day's returns: d1 (2 + 3) * 9, d2 6 * 7."""
    assert _rows(con, _by_year(["Anchored Cross"])) == [(2024.0, 87.0)]


def test_a_query_filter_chooses_the_conformed_rows(con: duckdb.DuckDBPyConnection) -> None:
    """Kept returns only: d1 (2 + 3) * 4, d2 6 * 7. It was refused."""
    compiled = _compile(_by_year(["Anchored Cross"], [KEEP]))
    conformed = compiled.sql[compiled.sql.index("__ob_conf_") - 300 :]
    assert '"Returns"."status" = ' in conformed
    assert _rows(con, _by_year(["Anchored Cross"], [KEEP])) == [(2024.0, 62.0)]


def test_a_filter_reached_through_the_conformed_fact(con: duckdb.DuckDBPyConnection) -> None:
    """Reasons is joined inside the subquery: damage is r1 only, on d1; d2's
    sales find no damaged return and read NULL."""
    where = [{"field": "Reason Category", "op": "equals", "value": "damage"}]
    assert _rows(con, _by_year(["Anchored Cross"], where)) == [(2024.0, 20.0)]


def test_a_measure_filter_chooses_the_conformed_rows(con: duckdb.DuckDBPyConnection) -> None:
    """The measure's own filter, applied the same way. It was refused."""
    assert _rows(con, _by_year(["Kept Cross", "Damage Cross", "Anchored Cross"])) == [
        (2024.0, 62.0, 20.0, 87.0)
    ]


def test_a_static_filter_chooses_the_conformed_rows(con: duckdb.DuckDBPyConnection) -> None:
    yaml_text = MODEL_YAML + (
        "filters:\n  - dataObject: Returns\n    column: Return Status\n"
        "    operator: equals\n    value: keep\n"
    )
    assert _rows(con, _by_year(["Anchored Cross"]), yaml_text) == [(2024.0, 62.0)]


def test_the_shared_key_anchor_filters_only_that_fact(con: duckdb.DuckDBPyConnection) -> None:
    """Both facts are conformed per date; the filter chooses returns only:
    d1 5 * 4, d2 6 * 7."""
    assert _rows(con, _by_year(["Shared Key Cross"], [KEEP])) == [(2024.0, 62.0)]


def test_a_union_leg_that_reaches_it_filters_natively(con: duckdb.DuckDBPyConnection) -> None:
    """Returned Qty is a leg of its own, on Returns: the filter is its WHERE
    there (4 + 7), and the anchored measure's in its subquery."""
    assert _rows(con, _by_year(["Anchored Cross", "Returned Qty"], [KEEP])) == [
        (2024.0, 62.0, 11.0)
    ]


def test_a_measure_that_cannot_apply_it_is_refused() -> None:
    """Sales Qty reads the anchor's own rows, which no return filter chooses."""
    with pytest.raises(ResolutionError) as raised:
        _compile(_by_year(["Anchored Cross", "Sales Qty"], [KEEP]))
    assert [e.code for e in raised.value.errors] == ["FILTER_NOT_APPLICABLE_TO_MEASURE"]


def test_a_static_filter_skips_a_measure_that_cannot_apply_it(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """A static filter applies where relevant: the conformed returns are kept
    ones, Sales Qty reads every sale, and the query says so."""
    yaml_text = MODEL_YAML + (
        "filters:\n  - dataObject: Returns\n    column: Return Status\n"
        "    operator: equals\n    value: keep\n"
    )
    compiled = _compile(_by_year(["Anchored Cross", "Sales Qty"]), yaml_text)
    assert "STATIC_FILTER_NOT_APPLIED" in [w.code for w in compiled.warnings]
    assert _rows(con, _by_year(["Anchored Cross", "Sales Qty"]), yaml_text) == [
        (2024.0, 62.0, 11.0)
    ]


def test_a_filter_across_both_sides_is_refused() -> None:
    """``Return Status = keep OR Sale Status = keep`` chooses neither side's
    rows alone, so it has no place to be applied."""
    where = [
        {
            "logic": "or",
            "filters": [KEEP, {"field": "Sale Status", "op": "equals", "value": "keep"}],
        }
    ]
    with pytest.raises(ResolutionError) as raised:
        _compile(_by_year(["Anchored Cross"], where))
    assert [e.code for e in raised.value.errors] == ["FILTER_ON_CONFORMED_OBJECT"]


def test_a_measure_filter_across_both_sides_is_refused() -> None:
    with pytest.raises(AnchoredFilterNotSupportedError, match="Returns"):
        _compile(_by_year(["Either Cross"]))


STATIC_KEEP = (
    "filters:\n  - dataObject: Returns\n    column: Return Status\n"
    "    operator: equals\n    value: keep\n"
)


def _both_reach_reasons(sales: str) -> str:
    """Sales (named *sales*) joins Reasons too, and the measure, anchored on
    the calendar, conforms both facts: a reason filter restricts both."""
    return (
        MODEL_YAML.replace(
            "      Sale Status: {code: status, abstractType: string}\n",
            "      Sale Status: {code: status, abstractType: string}\n"
            "      Sale Reason ID: {code: reason_id, abstractType: string}\n",
        )
        .replace(
            """        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
""",
            """        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Reasons
        columnsFrom: [Sale Reason ID]
        columnsTo: [Reason ID]
""",
        )
        .replace("  Sales:\n", f"  {sales}:\n")
        .replace("dataObject: Sales,", f"dataObject: {sales},")
        .replace("{[Sales].", f"{{[{sales}].")
        .replace("anchor: Sales", f"anchor: {sales}")
        .replace(
            "  Shared Key Cross:\n    resultType: float\n    aggregation: sum\n",
            "  Shared Key Cross:\n    resultType: float\n    aggregation: sum\n"
            "    anchor: Calendar\n",
        )
    )


@pytest.mark.parametrize("sales", ["Sales", "ASales"])
def test_every_conformed_fact_reaching_the_filter_applies_it(
    con: duckdb.DuckDBPyConnection, sales: str
) -> None:
    """Damaged sales s1 (2, d1) and s3 (6, d2), damaged return r1 (4, d1):
    d1 2 * 4, d2 6 * NULL. Only the first fact by name was filtered, so the
    answer changed with the name: 20 for Sales, 60 for ASales."""
    con.execute("ALTER TABLE main.sales ADD COLUMN reason_id VARCHAR")
    con.execute("UPDATE main.sales SET reason_id = CASE id WHEN 's2' THEN 'R2' ELSE 'R1' END")
    where = [{"field": "Reason Category", "op": "equals", "value": "damage"}]
    query = _by_year(["Shared Key Cross"], where)
    assert _rows(con, query, _both_reach_reasons(sales)) == [(2024.0, 8.0)]


def test_a_fixed_context_drops_a_pushed_static_filter(con: duckdb.DuckDBPyConnection) -> None:
    """FIXED drops the non-enforced static filter, in the subquery too: 87,
    beside the filtered 62. The pushed copy bypassed the context: 62."""
    sql = _compile(_by_year(["Fixed Cross", "Anchored Cross"]), MODEL_YAML + STATIC_KEEP).sql
    cursor = con.execute(sql)
    row = dict(zip([d[0] for d in cursor.description], cursor.fetchone(), strict=True))
    assert (float(row["Fixed Cross"]), float(row["Anchored Cross"])) == (87.0, 62.0)


@pytest.mark.parametrize(("op", "total"), [("exists", 20.0), ("nonexists", 67.0)])
def test_an_exists_on_the_conformed_fact(
    con: duckdb.DuckDBPyConnection, op: str, total: float
) -> None:
    """Returns with a damage reason: r1 (d1 5 * 4); without: r2 and r3
    (d1 5 * 5, d2 6 * 7). The correlation to Returns was left outside its
    subquery: "Referenced table Returns not found"."""
    where = [
        {
            "field": "Returns.Return ID",
            "op": op,
            "subquery": {
                "dataObject": "Reasons",
                "filter": [{"field": "Category", "op": "equals", "value": "damage"}],
            },
        }
    ]
    assert _rows(con, _by_year(["Anchored Cross"], where)) == [(2024.0, total)]


def test_the_cache_depends_on_the_subquery_tables() -> None:
    """Returns and Reasons are read only in the conformed subquery; an update
    to either has to invalidate the cached result."""
    where = [{"field": "Reason Category", "op": "equals", "value": "damage"}]
    tables = _compile(_by_year(["Anchored Cross"], where)).physical_tables
    assert {"main.returns", "main.reasons"} <= set(tables)


def test_a_reaggregate_over_an_anchored_measure(con: duckdb.DuckDBPyConnection) -> None:
    """Per day, then averaged: d1 5 * 4, d2 6 * 7 -> 31. The applicability
    check read the metric's placeholder as a measure and refused."""
    assert _rows(con, _by_year(["Daily Cross"], [KEEP])) == [(2024.0, 31.0)]
