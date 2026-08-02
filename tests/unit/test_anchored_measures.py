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


def test_an_anchor_may_name_a_data_object_the_facts_share() -> None:
    """``anchor: Calendar`` is the shared-key reading, stated rather than assumed.

    It is not one of the objects the expression reads, but every fact it reads
    joins to it, so conforming them all to its grain is well defined - and it is
    the only way to say which grain when the facts share more than one.
    """
    yaml_text = _with(TWO_FACT_YAML, anchor="Calendar")
    assert not SemanticValidator().validate(_model(yaml_text))
    sql = _sql(yaml_text, ["Cross"])
    assert 'FROM "main"."calendar" AS "Calendar"' in sql
    assert float(_db().execute(sql).fetchall()[0][1]) == pytest.approx(88.0)


def test_an_anchor_that_is_neither_read_nor_shared_is_rejected() -> None:
    """A typo looks exactly like this, and the result would be silently wrong.

    Anchoring on an unrelated object leaves every column conformed in with
    nothing to conform against.
    """
    isolated = TWO_FACT_YAML.replace(
        "dimensions:\n",
        """  Warehouse:
    code: warehouse
    schema: main
    columns:
      Warehouse ID: {code: wid, abstractType: string, primaryKey: true}
dimensions:
""",
        1,
    )
    errors = SemanticValidator().validate(_model(_with(isolated, anchor="Warehouse")))
    assert any(e.code == "INVALID_ANCHOR_DATA_OBJECT" for e in errors), errors


# --- Rule 3 hardening: ambiguity, dimension anchors, the auto-pick warning ---

TWO_SHARED_DIMS_YAML = (
    TWO_FACT_YAML.replace(
        "dimensions:\n",
        """  Store:
    code: store
    schema: main
    columns:
      Store ID: {code: sid, abstractType: string, primaryKey: true}
      Region: {code: region, abstractType: string}
dimensions:
""",
        1,
    )
    .replace(
        """      Qty: {code: qty, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]""",
        """      Qty: {code: qty, abstractType: float}
      Sale Store ID: {code: sid, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Store
        columnsFrom: [Sale Store ID]
        columnsTo: [Store ID]""",
    )
    .replace(
        """      Qty: {code: qty, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]""",
        """      Qty: {code: qty, abstractType: float}
      Return Store ID: {code: sid, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Store
        columnsFrom: [Return Store ID]
        columnsTo: [Store ID]""",
    )
)


def test_two_shared_dimensions_are_refused_rather_than_picked_between() -> None:
    """Conforming at each shared dimension gives a different number.

    Measured on the same rows: at the calendar 42, at the store 30. Picking one
    silently is the failure this whole design exists to avoid, so the measure is
    told to say which it means.
    """
    from orionbelt.compiler.resolution import ResolutionError

    with pytest.raises(ResolutionError) as excinfo:
        _sql(TWO_SHARED_DIMS_YAML, ["Cross"])
    error = next(e for e in excinfo.value.errors if e.code == "ANCHOR_REQUIRED_AMBIGUOUS_KEY")
    # Names both candidates, so the reader knows what the choice is between...
    assert "Calendar" in error.message and "Store" in error.message
    # ...and says how to make it.
    assert "anchor:" in (error.hint or "")


def test_naming_the_shared_dimension_resolves_the_ambiguity() -> None:
    """``anchor:`` is what makes the refusal actionable, and each choice is distinct."""
    for anchor, expected in (("Calendar", 42.0), ("Store", 30.0)):
        yaml_text = _with(TWO_SHARED_DIMS_YAML, anchor=anchor)
        assert not SemanticValidator().validate(_model(yaml_text))
        con = duckdb.connect()
        con.execute("CREATE TABLE main.calendar(datekey VARCHAR, year INT)")
        con.execute("INSERT INTO main.calendar VALUES ('d1',2024)")
        con.execute("CREATE TABLE main.store(sid VARCHAR, region VARCHAR)")
        con.execute("INSERT INTO main.store VALUES ('st1','N'),('st2','S')")
        con.execute("CREATE TABLE main.sales(id VARCHAR, datekey VARCHAR, qty DOUBLE, sid VARCHAR)")
        con.execute("INSERT INTO main.sales VALUES ('s1','d1',10,'st1'),('s2','d1',4,'st2')")
        con.execute(
            "CREATE TABLE main.returns(id VARCHAR, datekey VARCHAR, qty DOUBLE, sid VARCHAR)"
        )
        con.execute("INSERT INTO main.returns VALUES ('r1','d1',3,'st1')")
        query = QueryObject(**{"select": {"dimensions": [], "measures": ["Cross"]}})
        sql = CompilationPipeline().compile(query, _model(yaml_text), "duckdb").sql
        assert float(con.execute(sql).fetchall()[0][0]) == pytest.approx(expected), anchor


def test_an_unreachable_anchor_grain_is_reported_not_left_to_the_database() -> None:
    """Anchoring at a grain the query's dimensions cannot be reached from.

    ``anchor: Store`` with a calendar dimension is unanswerable - Store has no
    path to Calendar - and used to leave the conformed joins referencing a table
    base selection had already moved away from, so the database saw the problem
    first.
    """
    from orionbelt.compiler.resolution import ResolutionError

    with pytest.raises(ResolutionError, match="Calendar"):
        _sql(_with(TWO_SHARED_DIMS_YAML, anchor="Store"), ["Cross"])


def test_choosing_the_only_shared_key_is_reported_as_an_assumption() -> None:
    """One candidate is still a choice made for the model, so it is stated."""
    query = QueryObject(**{"select": {"dimensions": ["Year"], "measures": ["Cross"]}})
    result = CompilationPipeline().compile(query, _model(TWO_FACT_YAML), "duckdb")
    codes = [w.code for w in result.warnings]
    assert "CONFORMED_GRAIN_ASSUMED" in codes, result.warnings
    assert (
        not CompilationPipeline()
        .compile(query, _model(_with(TWO_FACT_YAML, anchor="Returns")), "duckdb")
        .warnings
    )


# --- Reachability through the surfaces the measure is used from ---


def test_a_metric_over_an_anchored_measure_is_conformed_too() -> None:
    """A metric inlines its components, so it reaches the projection separately.

    Walking only ``resolved.measures`` left ``{[Cross]} + 1`` projecting the raw
    ``SUM("Sales"."qty" * "Returns"."qty")`` with no conformed subqueries under
    it, over a FROM containing neither fact, so it failed to bind.
    """
    yaml_text = (
        TWO_FACT_YAML
        + """metrics:
  Cross Plus One: {dataType: double, expression: '{[Cross]} + 1'}
"""
    )
    sql = _sql(yaml_text, ["Cross Plus One"])
    assert "__ob_conf_" in sql
    assert '"Sales"."qty" * "Returns"."qty"' not in sql
    # The measure alone is 88 on this data, so the metric is 89.
    assert float(_db().execute(sql).fetchall()[0][1]) == pytest.approx(89.0)


def test_query_level_allow_fan_out_is_accepted_by_the_published_query_schema() -> None:
    """A field Pydantic accepts but the schema rejects is unusable over REST.

    The guarded API paths validate the payload against ``query-schema.json``
    before Pydantic sees it, so a flag missing there is refused as an
    unexpected additional property however well the model handles it.
    """
    import json
    from pathlib import Path

    import jsonschema

    schema = json.loads(Path("schema/query-schema.json").read_text())
    payload = {"select": {"measures": ["Cross"]}, "allowFanOut": True}
    jsonschema.validate(payload, schema)


def test_the_anchor_is_exported_to_rdf_under_its_own_predicate() -> None:
    """``obsl:anchorGrain``, not ``obsl:anchoredTo``.

    SHACL reserves ``anchoredTo`` for column-less row counts and forbids it on a
    measure that reads columns, which an anchored expression measure does. Two
    concepts, two predicates.
    """
    from orionbelt.obsl.exporter import OBSL, export_obsl

    graph = export_obsl(_model(_with(TWO_FACT_YAML, anchor="Returns")), "m")
    anchored = {str(s) for s, _, _ in graph.triples((None, OBSL.anchorGrain, None))}
    assert any(s.endswith("/measure/cross") for s in anchored), anchored
    # The row-count predicate stays reserved for synthesized counts.
    count_anchored = {str(s) for s, _, _ in graph.triples((None, OBSL.anchoredTo, None))}
    assert not any(s.endswith("/measure/cross") for s in count_anchored), count_anchored


WRAPPED_YAML = (
    TWO_FACT_YAML
    + """metrics:
  Running Cross:
    type: cumulative
    measure: Cross
    timeDimension: Year
  Cross Rank:
    type: window
    windowFunction: dense_rank
    measure: Cross
"""
)


@pytest.mark.parametrize("metric", ["Running Cross", "Cross Rank"])
def test_a_wrapper_reprojecting_an_anchored_measure_reads_the_conformed_columns(
    metric: str,
) -> None:
    """Cumulative and window metrics build their own base CTE from the measure.

    Each re-derives the component's aggregate rather than taking the planner's
    column, so both projected the raw ``SUM("Sales"."qty" * "Returns"."qty")``
    into a CTE whose FROM joins conformed subqueries in place of those tables.
    The expression they re-project now comes from
    ``ResolvedQuery.conformed_expressions``.
    """
    sql = _sql(WRAPPED_YAML, [metric])
    assert '"Sales"."qty" * "Returns"."qty"' not in sql
    assert "__ob_conf_" in sql
    # Binds and runs, which is what the raw expression could not do.
    assert _db().execute(sql).fetchall()


def test_the_published_schema_describes_the_default_that_is_implemented() -> None:
    """The contract is public, so a stale rule in it is a wrong answer to users.

    An earlier design defaulted to the leftmost data object the expression
    named. That was removed for making a commutative rewrite change the result,
    but the description outlived it.
    """
    import json
    from pathlib import Path

    for path in (
        "schema/obml-schema.json",
        "packages/osi-orionbelt/src/osi_orionbelt/schemas/obml-schema.json",
    ):
        schema = json.loads(Path(path).read_text())
        described = schema["definitions"]["measure"]["properties"]["anchor"]["description"]
        assert "leftmost" not in described, path
        assert "CONFORMED_GRAIN_ASSUMED" in described, path
        assert "ANCHOR_REQUIRED_AMBIGUOUS_KEY" in described, path


SECONDARY_PATHS_YAML = TWO_FACT_YAML.replace(
    """      Sale Date Key: {code: datekey, abstractType: string}
      Qty: {code: qty, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]""",
    """      Sale Date Key: {code: datekey, abstractType: string}
      Ship Date Key: {code: ship_datekey, abstractType: string}
      Qty: {code: qty, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Ship Date Key]
        columnsTo: [Date Key]
        secondary: true
        pathName: ship""",
)


def test_conforming_follows_the_join_path_the_query_selected() -> None:
    """``usePathNames`` picks the join, so it has to pick the conform key too.

    A secondary path replaces its pair's primary join, and the conformed
    subquery groups by that join's column. Reading the primary's column instead
    answers at a different date entirely, with nothing to indicate it: the SQL
    is valid, the number is just from the wrong grain.
    """
    query = QueryObject(
        **{
            "select": {"dimensions": ["Year"], "measures": ["Cross"]},
            "usePathNames": [{"source": "Sales", "target": "Calendar", "pathName": "ship"}],
        }
    )
    sql = CompilationPipeline().compile(query, _model(SECONDARY_PATHS_YAML), "duckdb").sql
    assert '"Sales"."ship_datekey" AS "__ob_ak0"' in sql
    assert '"Sales"."datekey" AS "__ob_ak0"' not in sql


def test_conforming_uses_the_primary_join_when_no_path_is_selected() -> None:
    """The override is opt-in: without it the primary join still wins."""
    sql = _sql(SECONDARY_PATHS_YAML, ["Cross"])
    assert '"Sales"."datekey" AS "__ob_ak0"' in sql
    assert "ship_datekey" not in sql
