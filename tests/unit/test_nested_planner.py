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
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
    Subquery,
)
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


def _with_second_fact(yaml_text: str) -> str:
    """The model plus an independent fact, which is what forces a CFL union."""
    return (
        yaml_text.replace(
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


def _with_a_dimension_behind_the_array(yaml_text: str) -> str:
    """A nested object joining onward to a dimension of its own.

    Legal, and the shape a nested *fact* takes: ``Charge Credits`` to a currency
    table. It also puts an ordinary object behind a containment edge, which is
    what several reachability walks had to learn not to route through.
    """
    return (
        yaml_text.replace(
            "      Label Value: {code: Value, abstractType: string}\n",
            "      Label Value: {code: Value, abstractType: string}\n"
            "      Owner Id: {code: OwnerId, abstractType: string}\n"
            "    joins:\n"
            "      - joinTo: Owners\n"
            "        columnsFrom: [Owner Id]\n"
            "        columnsTo: [Owner Id]\n"
            "        joinType: many-to-one\n",
            1,
        )
        .replace(
            "  Charge Credits:\n",
            "  Owners:\n"
            "    code: owners\n"
            "    columns:\n"
            "      Owner Id: {code: owner_id, abstractType: string, primaryKey: true}\n"
            "      Owner Name: {code: owner_name, abstractType: string}\n"
            "      Owner Weight: {code: owner_weight, abstractType: float}\n"
            "  Charge Credits:\n",
            1,
        )
        .replace(
            "  Account Name: {dataObject: Accounts, column: Account Name}\n",
            "  Account Name: {dataObject: Accounts, column: Account Name}\n"
            "  Owner Name: {dataObject: Owners, column: Owner Name}\n",
            1,
        )
    )


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

    def test_a_nested_object_a_filter_alone_names_in_a_union_leg(self) -> None:
        """The refusal has to see objects only a *predicate* names.

        WHERE filters are resolved long after the guard runs, so a check reading
        the query's required objects alone saw nothing here - and the CFL leg
        then dropped the predicate rather than failing to build it, returning
        unfiltered totals with nothing to say so.
        """
        model = _load(_with_second_fact(MODEL_YAML))
        with pytest.raises(ResolutionError) as excinfo:
            CompilationPipeline().compile(
                QueryObject(
                    select=QuerySelect(dimensions=[], measures=["Total Cost", "Budget Amount"]),
                    where=[
                        QueryFilter(
                            field="Charge Labels.Label Key",
                            op=FilterOperator.EQUALS,
                            value="team",
                        )
                    ],
                ),
                model,
                "duckdb",
            )
        assert any(e.code == "NESTED_OBJECT_IN_MULTI_FACT" for e in excinfo.value.errors)

    @pytest.mark.parametrize("dialect", UNNESTING_DIALECTS)
    def test_a_nested_object_inside_another_one(self, dialect: str) -> None:
        """An array inside an array is declarable and not yet compilable.

        The parent is an element rather than a row, and that reference is not a
        column on every engine: Snowflake's ``FLATTEN`` row holds the element
        under ``value``, and MySQL's ``JSON_TABLE`` projects only the scalar
        columns it was told to extract, so the array is not there to read. Both
        compiled to SQL their engine rejects before this refusal.
        """
        model = _load(
            MODEL_YAML.replace(
                "dimensions:\n",
                "  Label Parts:\n"
                "    nestedIn: {dataObject: Charge Labels, column: Parts}\n"
                "    columns:\n"
                "      Part Name: {code: Name, abstractType: string}\n"
                "dimensions:\n",
                1,
            ).replace(
                "  Account Name: {dataObject: Accounts, column: Account Name}\n",
                "  Account Name: {dataObject: Accounts, column: Account Name}\n"
                "  Part Name: {dataObject: Label Parts, column: Part Name}\n",
                1,
            )
        )
        with pytest.raises(ResolutionError) as excinfo:
            _compile(model, ["Part Name"], ["Total Cost"], dialect)
        assert any(e.code == "NESTED_WITHIN_NESTED_UNSUPPORTED" for e in excinfo.value.errors)

    def test_an_exists_subquery_whose_inner_filter_names_one(self) -> None:
        """The third road to the same wall, after the target and the path.

        A subquery's own ``filter`` joins whatever objects it references into
        the body, and that loop asked ``qualify_table`` for a table a nested
        object does not have.
        """
        model = _load(_with_a_dimension_behind_the_array(MODEL_YAML))
        with pytest.raises(ResolutionError) as excinfo:
            CompilationPipeline().compile(
                QueryObject(
                    select=QuerySelect(dimensions=["Account Name"], measures=[]),
                    where=[
                        QueryFilter(
                            field="Accounts.Account Id",
                            op=FilterOperator.EXISTS,
                            subquery=Subquery(
                                data_object="Charges",
                                filter=[
                                    QueryFilter(
                                        field="Charge Labels.Label Key",
                                        op=FilterOperator.EQUALS,
                                        value="team",
                                    )
                                ],
                            ),
                        )
                    ],
                ),
                model,
                "duckdb",
            )
        assert any(e.code == "NESTED_OBJECT_IN_SUBQUERY" for e in excinfo.value.errors)

    def test_an_exists_subquery_targeting_something_behind_one(self) -> None:
        """A subquery body is joins only, so anything behind a containment edge
        is out of its reach too - not only the nested object itself.
        """
        model = _load(_with_a_dimension_behind_the_array(MODEL_YAML))
        with pytest.raises(ResolutionError) as excinfo:
            CompilationPipeline().compile(
                QueryObject(
                    select=QuerySelect(dimensions=[], measures=["Total Cost"]),
                    where=[
                        QueryFilter(
                            field="Charges.Cost",
                            op=FilterOperator.EXISTS,
                            subquery=Subquery(data_object="Owners"),
                        )
                    ],
                ),
                model,
                "duckdb",
            )
        assert any(e.code == "NESTED_OBJECT_IN_SUBQUERY" for e in excinfo.value.errors)

    def test_a_union_leg_cannot_route_through_one(self) -> None:
        """A dimension reachable only *through* a nested object is out of every
        leg's reach, because a leg is a star built out of tables.

        It crashed in ``build_join_condition`` on a step with no columns, and
        excluding the object alone was not enough: under ``UNION ALL BY NAME``
        no leg supplied the column and none NULL-padded it either, so the outer
        SELECT named a column the union does not have.
        """
        model = _load(_with_second_fact(_with_a_dimension_behind_the_array(MODEL_YAML)))
        with pytest.raises(ResolutionError) as excinfo:
            _compile(model, ["Owner Name"], ["Total Cost", "Budget Amount"])
        assert any(e.code == "UNREACHABLE_REQUIRED_OBJECT" for e in excinfo.value.errors)

    def test_a_measure_expression_spanning_a_containment_edge(self) -> None:
        """A measure reading two objects a leg cannot join together.

        ``_single_leg_root`` asked whether one root reached them all, and its
        answer routed the expression to a leg. Measured with the full reachable
        set it said yes - through the unnest - and the leg then projected
        ``"Owners"."owner_weight"`` over a FROM holding only ``charges``.

        Refused rather than handed to the cross-fact path, which is for facts
        that really are independent: it gives each its own leg, and pairing
        these two across legs has no key to pair on.
        """
        model = _load(
            _with_second_fact(_with_a_dimension_behind_the_array(MODEL_YAML)).replace(
                "measures:\n",
                "measures:\n"
                "  Weighted Cost:\n"
                '    expression: "{[Charges].[Cost]} * {[Owners].[Owner Weight]}"\n'
                "    resultType: float\n"
                "    aggregation: sum\n",
                1,
            )
        )
        with pytest.raises(ResolutionError) as excinfo:
            _compile(model, ["Account Name"], ["Weighted Cost", "Budget Amount"])
        assert any(e.code == "UNREACHABLE_REQUIRED_OBJECT" for e in excinfo.value.errors)

    def test_but_a_star_query_reaches_it_normally(self) -> None:
        """The same dimension is perfectly reachable when there is no union: the
        nested object is unnested and its own join walked from there. Refusing
        it everywhere would have cost a nested fact its dimensions.
        """
        model = _load(_with_a_dimension_behind_the_array(MODEL_YAML))
        sql = _compile(model, ["Owner Name"], ["Total Cost"]).sql
        assert "UNNEST" in sql.upper()
        assert '"owners"' in sql

    def test_an_exists_subquery_targeting_one(self, model: SemanticModel) -> None:
        """A correlated subquery has nowhere to select from and nothing to
        correlate on: the rows exist only inside their parent's, which is what
        makes the ordinary join work without either.

        Structured rather than raised. Before the containment edge there was no
        path to walk, so this failed as NO_JOIN_PATH_TO_SUBQUERY by accident;
        with the edge it walked on and hit ``qualify_table``, whose
        ``UnrenderableDataObjectError`` routers hand back as a 500.
        """
        with pytest.raises(ResolutionError) as excinfo:
            CompilationPipeline().compile(
                QueryObject(
                    select=QuerySelect(dimensions=[], measures=["Total Cost"]),
                    where=[
                        QueryFilter(
                            field="Charges.Cost",
                            op=FilterOperator.EXISTS,
                            subquery=Subquery(data_object="Charge Labels"),
                        )
                    ],
                ),
                model,
                "duckdb",
            )
        assert any(e.code == "NESTED_OBJECT_IN_SUBQUERY" for e in excinfo.value.errors)

    def test_a_static_filter_on_one_does_not_refuse_unrelated_queries(self) -> None:
        """A static model filter is a property of the model, not of the query.

        One naming an object a plan cannot reach is documented as skipped rather
        than fatal, so counting them in the multi-fact refusal made a single
        nested static filter break every union query in the model - including
        the ones that never go near it.
        """
        model = _load(
            _with_second_fact(MODEL_YAML)
            + """filters:
  - dataObject: Charge Labels
    column: Label Key
    operator: equals
    value: team
"""
        )
        assert _compile(model, [], ["Total Cost", "Budget Amount"]).sql

    def test_a_nested_object_in_a_union_leg(self) -> None:
        """CFL builds one leg per independent fact, each selecting from a table
        of its own. A nested object has none, so the leg would name nothing.
        """
        model = _load(_with_second_fact(MODEL_YAML))
        with pytest.raises(ResolutionError) as excinfo:
            _compile(model, ["Credit Type"], ["Total Credit", "Budget Amount"])
        assert any(e.code == "NESTED_OBJECT_IN_MULTI_FACT" for e in excinfo.value.errors)


class TestTheApiContract:
    """An unsupported nested access is a *compile* failure, and the surfaces
    that report compile failures without raising have to keep doing so.

    Both of these went the other way first: the handler was added beside the
    ones that raise a 422 and copied their shape, which turned one refused query
    into a dead batch and a ``would_compile: false`` plan into an exception.
    """

    @staticmethod
    def _store() -> tuple[object, str]:
        from orionbelt.service.model_store import ModelStore

        store = ModelStore()
        return store, store.load_model(MODEL_YAML).model_id

    def test_a_batch_reports_it_as_one_errored_item(self) -> None:
        from orionbelt.api.routers.oneshot import _compile
        from orionbelt.api.schemas import OneshotBatchQueryError, OneshotBatchQueryItem

        store, model_id = self._store()
        outcome = _compile(
            store=store,
            model_id=model_id,
            item=OneshotBatchQueryItem(
                id="q",
                query=QueryObject(
                    select=QuerySelect(dimensions=["Label Value"], measures=["Total Cost"])
                ),
            ),
            default_dialect="dremio",
        )
        assert isinstance(outcome, OneshotBatchQueryError)
        assert outcome.code == "UNSUPPORTED_NESTED_ACCESS"
        assert "dremio" in outcome.message

    def test_query_plan_reports_it_as_would_compile_false(self) -> None:
        from orionbelt.api.services.query_compilation import compile_query_for_plan

        store, model_id = self._store()
        result, response = compile_query_for_plan(
            store=store,
            model_id=model_id,
            query=QueryObject(
                select=QuerySelect(dimensions=["Label Value"], measures=["Total Cost"])
            ),
            dialect="dremio",
        )
        assert result is None
        assert response is not None
        assert response.would_compile is False
        assert [w.code for w in response.warnings] == ["UNSUPPORTED_NESTED_ACCESS"]


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
