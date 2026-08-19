"""Planning a query over a nested data object.

``test_nested_data_objects`` covers the OBML surface and ``test_unnest_ast`` the
per-dialect fragment. This is what joins them: the join graph learning an edge
nothing declares, the base object never landing on an object with no table, the
projection reaching a field the way its engine spells it, and the refusals for
the shapes that are not built yet.

What the SQL *means* is asserted where it can be executed -
``tests/integration/test_duckdb_nested_execution.py`` runs the arithmetic, and
``tests/integration/drift/vendor_exec/test_unnest_render_exec.py`` runs each
dialect's fragment against that dialect. String assertions here are for
structure only, because a string assertion passes while the SQL it describes is
invalid: all four review rounds on #344 were exactly that.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.grain_dedup import GrainDedupUnsupportedError
from orionbelt.compiler.graph import JoinGraph
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.dialect.base import UnsupportedNestedAccessError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.models.warnings import WarningCode
from orionbelt.parser import ReferenceResolver, TrackedLoader
from orionbelt.parser.validator import SemanticValidator

#: Every dialect with a FROM-clause unnest. Dremio is absent on purpose - its
#: ``FLATTEN`` is a projection function and it takes the ``code`` fallback.
UNNESTING_DIALECTS = [
    "duckdb",
    "postgres",
    "mysql",
    "clickhouse",
    "bigquery",
    "snowflake",
    "databricks",
]

MODEL_YAML = """
version: "1.0"
name: nested
dataObjects:
  Charges:
    code: charges
    columns:
      Charge Id: {code: id, abstractType: string, primaryKey: true}
      Cost: {code: cost, abstractType: float}
      Account Id: {code: account_id, abstractType: string}
    joins:
      - joinTo: Accounts
        columnsFrom: [Account Id]
        columnsTo: [Account Id]
        joinType: many-to-one
  Accounts:
    code: accounts
    columns:
      Account Id: {code: account_id, abstractType: string, primaryKey: true}
      Account Name: {code: account_name, abstractType: string}
  Charge Labels:
    nestedIn: {dataObject: Charges, column: Labels}
    columns:
      Label Key: {code: Key, abstractType: string}
      Label Value: {code: Value, abstractType: string}
  Charge Credits:
    nestedIn: {dataObject: Charges, column: Credits}
    columns:
      Credit Type: {code: Type, abstractType: string}
      Credit Amount: {code: Amount, abstractType: float}
dimensions:
  Label Key: {dataObject: Charge Labels, column: Label Key}
  Label Value: {dataObject: Charge Labels, column: Label Value}
  Credit Type: {dataObject: Charge Credits, column: Credit Type}
  Account Name: {dataObject: Accounts, column: Account Name}
measures:
  Total Cost:
    columns: [{dataObject: Charges, column: Cost}]
    resultType: float
    aggregation: sum
  Total Credit:
    columns: [{dataObject: Charge Credits, column: Credit Amount}]
    resultType: float
    aggregation: sum
  Credit Ratio:
    expression: "{[Charge Credits].[Credit Amount]}"
    resultType: float
    aggregation: sum
"""


def _load(yaml_text: str = MODEL_YAML) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    assert not SemanticValidator().validate(model)
    return model


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    return _load()


def _compile(
    model: SemanticModel,
    dimensions: list[str],
    measures: list[str],
    dialect: str = "duckdb",
):
    return CompilationPipeline().compile(
        QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures)),
        model,
        dialect,
    )


class TestTheImpliedEdge:
    """A nested object declares no join, so the graph has to supply one."""

    def test_the_parent_reaches_its_nested_child(self, model: SemanticModel) -> None:
        graph = JoinGraph(model)
        assert "Charge Labels" in graph.descendants("Charges")
        assert "Charge Credits" in graph.descendants("Charges")

    def test_the_child_does_not_reach_the_parent(self, model: SemanticModel) -> None:
        """One-way on purpose. The edge exists so a plan can *enter* the array
        from the row that contains it; there is nothing to reach by leaving it
        that the parent does not already reach, and a second direction would
        make the two mutually reachable and the common root a coin toss.
        """
        graph = JoinGraph(model)
        assert "Charges" not in graph.descendants("Charge Labels")

    def test_the_step_is_marked_nested_and_oriented_parent_first(
        self, model: SemanticModel
    ) -> None:
        graph = JoinGraph(model)
        steps = graph.find_join_path({"Charges"}, {"Charges", "Charge Labels"})
        assert len(steps) == 1
        assert steps[0].nested
        assert (steps[0].from_object, steps[0].to_object) == ("Charges", "Charge Labels")
        assert steps[0].from_columns == [] and steps[0].to_columns == []

    def test_the_common_root_of_parent_and_child_is_the_parent(self, model: SemanticModel) -> None:
        graph = JoinGraph(model)
        assert graph.find_common_root({"Charges", "Charge Labels"}) == "Charges"
        # And a nested object on its own still answers with something nameable.
        assert graph.find_common_root({"Charge Labels"}) == "Charges"


class TestTheBaseObject:
    """A nested object has no table, so it can never be what a query selects from."""

    def test_a_nested_only_query_bases_on_the_parent(self, model: SemanticModel) -> None:
        result = _compile(model, ["Credit Type"], ["Total Credit"])
        assert 'FROM "charges" AS "Charges"' in result.sql
        assert result.explain is not None

    def test_the_measure_source_does_not_pin_the_base(self, model: SemanticModel) -> None:
        """``Total Credit`` is sourced from the nested object, which is what
        ``_select_base_object`` would otherwise choose - it prefers a measure's
        source and has no reason of its own to reject one.
        """
        result = _compile(model, [], ["Total Credit"])
        assert 'FROM "charges" AS "Charges"' in result.sql
        assert "UNNEST" in result.sql.upper()


class TestTheFragment:
    """Each dialect's own shape, in the plan rather than in isolation."""

    @pytest.mark.parametrize("dialect", UNNESTING_DIALECTS)
    def test_it_compiles_everywhere_that_can_unnest(
        self, model: SemanticModel, dialect: str
    ) -> None:
        result = _compile(model, ["Label Value"], ["Total Cost"], dialect)
        assert result.sql
        # Asked of the dialect rather than hardcoded: the shapes are a
        # comma-lateral, an ARRAY JOIN, a LATERAL VIEW and a JSON_TABLE, and a
        # test that spells one of them itself is testing its own copy.
        engine = DialectRegistry.get(dialect)
        assert engine.render_unnest(_probe(model, dialect)) in result.sql

    @pytest.mark.parametrize("dialect", UNNESTING_DIALECTS)
    def test_a_nested_column_is_projected_the_way_its_engine_reads_it(
        self, model: SemanticModel, dialect: str
    ) -> None:
        """The trap this exists for: ``ColumnRef`` renders ``"L"."Key"``, which
        Snowflake refuses outright - measured, "SQL compilation error". Six
        engines would look fine, so a DuckDB-only assertion proves nothing.
        """
        engine = DialectRegistry.get(dialect)
        result = _compile(model, ["Label Value"], ["Total Cost"], dialect)
        expected = engine.compile_expr(
            engine.nested_field("Charge Labels", "Value", engine.nested_column_type("string"))
        )
        assert expected in result.sql

    def test_a_measure_expression_reaches_the_field_the_same_way(
        self, model: SemanticModel
    ) -> None:
        """A measure body is parsed from text and resolves straight to a
        physical reference, which is a different road to the same column. Both
        have to spell it identically or one of them is invalid SQL.
        """
        engine = DialectRegistry.get("snowflake")
        result = _compile(model, ["Credit Type"], ["Credit Ratio"], "snowflake")
        expected = engine.compile_expr(
            engine.nested_field("Charge Credits", "Amount", engine.nested_column_type("float"))
        )
        assert expected in result.sql

    def test_the_child_travels_after_its_parent(self, model: SemanticModel) -> None:
        """An unnest names its parent, so the clause that puts the parent in
        scope has to come first. One list of joins is what keeps the order.
        """
        result = _compile(model, ["Label Value", "Account Name"], ["Total Cost"])
        assert result.sql.index('AS "Charges"') < result.sql.index("UNNEST")


class TestTheOtherSurfaces:
    """A nested column is reached the same way from every clause, or one of them
    emits SQL the engine cannot bind.
    """

    def test_a_where_filter_reaches_it(self, model: SemanticModel) -> None:
        engine = DialectRegistry.get("snowflake")
        result = CompilationPipeline().compile(
            QueryObject(
                select=QuerySelect(dimensions=["Label Value"], measures=["Total Cost"]),
                where=[QueryFilter(field="Label Key", op=FilterOperator.EQUALS, value="team")],
            ),
            model,
            "snowflake",
        )
        accessor = engine.compile_expr(
            engine.nested_field("Charge Labels", "Key", engine.nested_column_type("string"))
        )
        assert f"WHERE {accessor} = 'team'" in result.sql

    def test_raw_mode_unnests_too(self, model: SemanticModel) -> None:
        """``select.fields`` bypasses dimensions and measures entirely, and has
        its own planner with its own join loop.
        """
        result = CompilationPipeline().compile(
            QueryObject(select=QuerySelect(fields=["Charges.Cost", "Charge Labels.Label Value"])),
            model,
            "duckdb",
        )
        assert "UNNEST" in result.sql.upper()
        assert 'FROM "charges" AS "Charges"' in result.sql


class TestTheParentMeasure:
    """A parent-side measure is summed once per array element without help."""

    def test_it_is_deduplicated_on_the_parent_key(self, model: SemanticModel) -> None:
        result = _compile(model, ["Label Value"], ["Total Cost"])
        assert "__ob_dedup" in result.sql
        assert "SELECT DISTINCT" in result.sql
        assert '"Charges"."id" AS "__ob_k0"' in result.sql
        assert any(w.code == WarningCode.FAN_TRAP_RISK for w in result.warnings)

    def test_the_nested_measure_is_left_over_the_raw_unnest(self, model: SemanticModel) -> None:
        """Two row sets, meeting at the grain. The nested measure must see every
        element - two identical credits are two credits - while the parent's
        must not see the same charge twice.
        """
        result = _compile(model, ["Credit Type"], ["Total Credit", "Total Cost"])
        main = result.sql.split('"__ob_dedup_0" AS')[0]
        assert "SUM" in main and '"Amount"' in main
        assert '"Charges"."cost" AS "__ob_c0"' in result.sql

    def test_without_a_parent_key_it_is_refused(self) -> None:
        """The deduplication needs to know which rows are one row of the parent.
        A nested object supplies no key by design, so the parent has to.
        """
        model = _load(MODEL_YAML.replace(", primaryKey: true}", "}"))
        with pytest.raises(GrainDedupUnsupportedError, match="declares no primaryKey"):
            _compile(model, ["Label Value"], ["Total Cost"])
        # The nested-side measure never needed one and still does not.
        assert _compile(model, ["Credit Type"], ["Total Credit"]).sql


class TestWhatIsRefused:
    def test_a_measure_on_one_of_two_unnested_objects(self, model: SemanticModel) -> None:
        with pytest.raises(FanoutError, match="unnests 'Charge Credits', 'Charge Labels'"):
            _compile(model, ["Label Value", "Credit Type"], ["Total Credit"])

    def test_but_a_parent_measure_over_both_is_fine(self, model: SemanticModel) -> None:
        """The parent's identity is unaffected by how many arrays multiplied it,
        so the same deduplication still yields one charge per group.
        """
        assert _compile(model, ["Label Value", "Credit Type"], ["Total Cost"]).sql

    def test_a_nested_object_in_a_union_leg(self) -> None:
        """CFL builds one leg per independent fact, each selecting from a table
        of its own. A nested object has none, so the leg would name nothing.
        """
        model = _load(
            MODEL_YAML.replace(
                "  Charge Credits:\n",
                "  Budgets:\n"
                "    code: budgets\n"
                "    columns:\n"
                "      Amount: {code: amount, abstractType: float}\n"
                "  Charge Credits:\n",
                1,
            )
            + """  Budget Amount:
    columns: [{dataObject: Budgets, column: Amount}]
    resultType: float
    aggregation: sum
"""
        )
        with pytest.raises(ResolutionError) as excinfo:
            _compile(model, ["Credit Type"], ["Total Credit", "Budget Amount"])
        assert any(e.code == "NESTED_OBJECT_IN_MULTI_FACT" for e in excinfo.value.errors)


class TestTheCodeFallback:
    """``nestedIn`` and ``code`` together: unnest where possible, table where not."""

    FALLBACK = """
version: "1.0"
name: nested_fallback
dataObjects:
  Charges:
    code: charges
    columns:
      Charge Key: {code: charge_key, abstractType: string, primaryKey: true}
      Cost: {code: cost, abstractType: float}
  Charge Labels:
    nestedIn: {dataObject: Charges, column: Labels}
    code: v_charge_labels
    joins:
      - joinTo: Charges
        columnsFrom: [Charge Key]
        columnsTo: [Charge Key]
        joinType: many-to-one
    columns:
      Charge Key: {code: charge_key, abstractType: string}
      Label Value: {code: Value, abstractType: string}
dimensions:
  Label Value: {dataObject: Charge Labels, column: Label Value}
measures:
  Total Cost:
    columns: [{dataObject: Charges, column: Cost}]
    resultType: float
    aggregation: sum
"""

    def test_the_unnest_wins_where_the_dialect_has_one(self) -> None:
        result = _compile(_load(self.FALLBACK), ["Label Value"], ["Total Cost"], "duckdb")
        assert "UNNEST" in result.sql.upper()
        assert "v_charge_labels" not in result.sql
        assert not [w for w in result.warnings if w.code == WarningCode.NESTED_SOURCE_FALLBACK]

    def test_the_table_is_read_where_it_does_not(self) -> None:
        result = _compile(_load(self.FALLBACK), ["Label Value"], ["Total Cost"], "dremio")
        assert '"v_charge_labels"' in result.sql
        assert "FLATTEN" not in result.sql.upper()
        # The columns come back as ordinary ones: the fallback table is in FROM
        # under the same alias, and reading them as element fields would name
        # something that does not exist.
        assert '"Charge Labels"."Value"' in result.sql

    def test_the_fallback_is_announced(self) -> None:
        """Silently answering differently on one engine is worse than failing:
        a view can filter, rename or aggregate, and nothing checks that it
        matches the array it stands in for.
        """
        result = _compile(_load(self.FALLBACK), ["Label Value"], ["Total Cost"], "dremio")
        fallback = [w for w in result.warnings if w.code == WarningCode.NESTED_SOURCE_FALLBACK]
        assert len(fallback) == 1
        assert "v_charge_labels" in fallback[0].message
        assert fallback[0].context == {
            "dataObject": "Charge Labels",
            "dialect": "dremio",
            "source": "code",
            "code": "v_charge_labels",
        }

    def test_no_fallback_at_all_is_refused(self, model: SemanticModel) -> None:
        with pytest.raises(UnsupportedNestedAccessError, match="Charge Labels"):
            _compile(model, ["Label Value"], ["Total Cost"], "dremio")

    def test_a_fallback_with_no_join_home_is_refused(self) -> None:
        """A flattening view is a separate table, so it needs a key: destroying
        containment is exactly what makes one necessary.
        """
        model = _load(
            self.FALLBACK.replace(
                """    joins:
      - joinTo: Charges
        columnsFrom: [Charge Key]
        columnsTo: [Charge Key]
        joinType: many-to-one
""",
                "",
            )
        )
        with pytest.raises(UnsupportedNestedAccessError, match="declared join back to 'Charges'"):
            _compile(model, ["Label Value"], ["Total Cost"], "dremio")


def _probe(model: SemanticModel, dialect: str):
    """The ``Unnest`` node the planner builds for ``Charge Labels``."""
    from orionbelt.compiler.nested import build_unnest

    graph = JoinGraph(model)
    step = graph.find_join_path({"Charges"}, {"Charges", "Charge Labels"})[0]
    return build_unnest(step, model.data_objects["Charge Labels"], DialectRegistry.get(dialect))
