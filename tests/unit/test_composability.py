"""Tests for Artefacts Composability Resolution (ACR)."""

from __future__ import annotations

import pytest

from orionbelt.compiler.composability import (
    ComposabilityResolver,
    resolve_composables_for_anchors,
    resolve_composables_for_query,
)
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# Two independent facts (Sales, Returns) sharing two dimension tables
# (Customers, Calendar). Combining a Sales measure with a Returns measure
# requires CFL; combining either with the shared dims is a plain star.
MULTI_FACT_YAML = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    columns:
      Customer ID: {code: CUSTOMER_ID, abstractType: string}
      Country: {code: COUNTRY, abstractType: string}
  Calendar:
    code: CALENDAR
    columns:
      Date Key: {code: DATE_KEY, abstractType: string}
      Month: {code: MONTH, abstractType: string}
  Sales:
    code: SALES
    columns:
      Sale ID: {code: SALE_ID, abstractType: string}
      Sale Customer ID: {code: CUSTOMER_ID, abstractType: string}
      Sale Date Key: {code: DATE_KEY, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float, numClass: additive}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Sale Customer ID]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Sale Date Key]
        columnsTo: [Date Key]
  Returns:
    code: RETURNS
    columns:
      Return ID: {code: RETURN_ID, abstractType: string}
      Return Customer ID: {code: CUSTOMER_ID, abstractType: string}
      Return Date Key: {code: DATE_KEY, abstractType: string}
      Refund: {code: REFUND, abstractType: float, numClass: additive}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Return Customer ID]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Calendar
        columnsFrom: [Return Date Key]
        columnsTo: [Date Key]

dimensions:
  Customer Country: {dataObject: Customers, column: Country, resultType: string}
  Sale Month: {dataObject: Calendar, column: Month, resultType: string}

measures:
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Return Amount:
    columns: [{dataObject: Returns, column: Refund}]
    resultType: float
    aggregation: sum
"""


def _load(yaml_content: str) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_content)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, f"model invalid: {result.errors}"
    return model


@pytest.fixture
def multi_fact_model() -> SemanticModel:
    return _load(MULTI_FACT_YAML)


# -- empty anchor ------------------------------------------------------------


def test_empty_query_offers_everything(sales_model: SemanticModel) -> None:
    result = resolve_composables_for_query(sales_model, QueryObject(select={"dimensions": []}))
    assert result.anchor_objects == []
    assert set(result.dimensions) == set(sales_model.dimensions)
    assert set(result.measures) == set(sales_model.effective_measures)
    assert set(result.metrics) == set(sales_model.metrics)
    assert result.cfl_measures == []


# -- single-fact star --------------------------------------------------------


def test_dimension_anchor_offers_fact_measures(sales_model: SemanticModel) -> None:
    # Anchor on a dimension table (Customers); measures live on Orders, which
    # reaches Customers via many-to-one -> all measures composable, no CFL.
    result = resolve_composables_for_anchors(sales_model, ["Customer Country"])
    assert result.anchor_objects == ["Customers"]
    assert "Revenue" in result.measures
    assert "Order Count" in result.measures
    assert result.cfl_measures == []
    # Every dimension is reachable from the Orders root.
    assert set(result.dimensions) == set(sales_model.dimensions)


def test_measure_anchor_offers_dimensions(sales_model: SemanticModel) -> None:
    result = resolve_composables_for_anchors(sales_model, ["Revenue"])
    assert result.anchor_objects == ["Orders"]
    # Orders reaches Customers + Products, so all dims are groupable.
    assert "Customer Country" in result.dimensions
    assert "Product Category" in result.dimensions
    assert "Order Date" in result.dimensions
    assert "Order Count" in result.measures


def test_synthesized_count_is_composable_and_anchor(sales_model: SemanticModel) -> None:
    """Synthesized <object> Count measures take part in ACR like declared ones:
    offered as composable measures, and usable as an anchor that resolves to
    its source object."""
    # Offered as a composable measure when anchoring on a reachable object.
    offered = resolve_composables_for_anchors(sales_model, ["Revenue"])
    assert "Orders Count" in offered.measures
    assert "Customers Count" in offered.measures

    # Usable as the anchor itself: resolves to the Orders fact.
    anchored = resolve_composables_for_anchors(sales_model, ["Orders Count"])
    assert anchored.anchor_objects == ["Orders"]
    assert "Revenue" in anchored.measures


def test_query_as_anchor_star(sales_model: SemanticModel) -> None:
    query = QueryObject(select={"dimensions": ["Customer Country"], "measures": ["Revenue"]})
    result = resolve_composables_for_query(sales_model, query)
    assert set(result.anchor_objects) == {"Customers", "Orders"}
    assert "Order Count" in result.measures
    assert "Product Category" in result.dimensions
    assert result.cfl_measures == []


# -- multi-fact / CFL --------------------------------------------------------


def test_independent_fact_measure_is_cfl(multi_fact_model: SemanticModel) -> None:
    # Anchor: a shared dimension + a Sales measure. Return Amount lives on the
    # independent Returns fact -> combinable only via CFL.
    query = QueryObject(select={"dimensions": ["Customer Country"], "measures": ["Sales Amount"]})
    result = resolve_composables_for_query(multi_fact_model, query)
    assert "Sales Amount" in result.measures
    assert "Return Amount" not in result.measures
    assert "Return Amount" in result.cfl_measures


def test_synthesized_count_cfl_classification(multi_fact_model: SemanticModel) -> None:
    """A count anchored on the query's fact is direct; a count on an independent
    fact is offered via CFL, matching declared-measure classification."""
    query = QueryObject(select={"dimensions": ["Customer Country"], "measures": ["Sales Amount"]})
    result = resolve_composables_for_query(multi_fact_model, query)
    assert "Sales Count" in result.measures
    assert "Returns Count" not in result.measures
    assert "Returns Count" in result.cfl_measures


def test_shared_dimension_anchor_offers_both_facts_directly(
    multi_fact_model: SemanticModel,
) -> None:
    # Anchor on the shared dimension only (no measure yet): each fact can still
    # serve as the base, so both measures are directly composable.
    result = resolve_composables_for_anchors(multi_fact_model, ["Customer Country"])
    assert "Sales Amount" in result.measures
    assert "Return Amount" in result.measures
    assert result.cfl_measures == []


def test_shared_dimension_stays_composable_with_sales_anchor(
    multi_fact_model: SemanticModel,
) -> None:
    query = QueryObject(select={"dimensions": [], "measures": ["Sales Amount"]})
    result = resolve_composables_for_query(multi_fact_model, query)
    # Both shared dimensions are reachable from the Sales fact.
    assert "Customer Country" in result.dimensions
    assert "Sale Month" in result.dimensions


# -- anchor resolution edge cases --------------------------------------------


def test_unknown_anchor_resolves_to_empty(sales_model: SemanticModel) -> None:
    result = resolve_composables_for_anchors(sales_model, ["No Such Thing"])
    # Unknown name contributes no anchor objects -> treated as empty anchor.
    assert result.anchor_objects == []
    assert set(result.measures) == set(sales_model.effective_measures)


def test_metric_anchor_resolves_to_underlying_fact(sales_model: SemanticModel) -> None:
    # "Revenue per Order" derives from Revenue + Order Count (both on Orders).
    result = resolve_composables_for_anchors(sales_model, ["Revenue per Order"])
    assert result.anchor_objects == ["Orders"]
    assert "Customer Country" in result.dimensions


def test_resolver_reuse_across_anchors(multi_fact_model: SemanticModel) -> None:
    resolver = ComposabilityResolver(multi_fact_model)
    dims, measures = resolver.objects_from_anchor_name("Return Amount")
    assert dims == set()
    assert measures == {"Returns"}


# --- join-only requirements (withinGroup) ---------------------------------

DISCONNECTED_WITHIN_GROUP_YAML = """\
version: 1.0

dataObjects:
  Products:
    code: PRODUCTS
    columns:
      Product ID: {code: ID, abstractType: string, primaryKey: true}
      Stock On Hand: {code: STOCK, abstractType: int}
  Sales:
    code: SALES
    columns:
      Sale ID: {code: ID, abstractType: string, primaryKey: true}
      Region: {code: REGION, abstractType: string}

dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}

measures:
  Sale List:
    resultType: string
    aggregation: listagg
    delimiter: ","
    columns: [{dataObject: Sales, column: Sale ID}]
    withinGroup:
      column: {dataObject: Products, column: Stock On Hand}
      order: ASC
"""


def test_acr_excludes_a_measure_whose_within_group_object_is_unreachable() -> None:
    """``withinGroup`` is a join requirement, so an unreachable one is fatal.

    The measure reads no value from ``Products``, but the compiler still has to
    join it for the aggregate's ORDER BY. With no join path the query fails
    with ``UNREACHABLE_REQUIRED_OBJECT``, so ACR must not advertise it - at any
    anchor, including none.
    """
    raw, source_map = TrackedLoader().load_string(DISCONNECTED_WITHIN_GROUP_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    resolver = ComposabilityResolver(model)

    for anchor in (None, "Region"):
        if anchor is None:
            composables = resolver.resolve(set(), set())
        else:
            composables = resolver.resolve(*resolver.objects_from_anchor_name(anchor))
        assert "Sale List" not in set(composables.measures) | set(composables.cfl_measures)


def test_acr_keeps_a_measure_whose_within_group_object_is_reachable() -> None:
    """The same measure stays composable once a join path exists."""
    yaml_text = DISCONNECTED_WITHIN_GROUP_YAML.replace(
        "      Region: {code: REGION, abstractType: string}\n",
        "      Region: {code: REGION, abstractType: string}\n"
        "      Sale Product ID: {code: PRODUCT_ID, abstractType: string}\n"
        "    joins:\n"
        "      - joinType: many-to-one\n"
        "        joinTo: Products\n"
        "        columnsFrom: [Sale Product ID]\n"
        "        columnsTo: [Product ID]\n",
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors

    resolver = ComposabilityResolver(model)
    composables = resolver.resolve(*resolver.objects_from_anchor_name("Region"))
    assert "Sale List" in set(composables.measures) | set(composables.cfl_measures)


def test_acr_follows_join_requirements_through_nested_metrics() -> None:
    """A metric wrapping a metric must inherit its components' requirements.

    ``metric_measure_names`` returns whatever the expression references, which
    may itself be a metric - a derived metric over a window metric, the one
    nesting OBML allows. Resolving one level only made the wrapper look like it
    had no sources and no join requirements at all, so it was advertised while
    compiling raised ``UNREACHABLE_REQUIRED_OBJECT``.
    """
    yaml_text = (
        DISCONNECTED_WITHIN_GROUP_YAML
        + """
metrics:
  Ranked: {type: window, windowFunction: dense_rank, measure: Sale List}
  Wrapped: {expression: "{[Ranked]}"}
"""
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    resolver = ComposabilityResolver(model)

    for anchor in (None, "Region"):
        if anchor is None:
            composables = resolver.resolve(set(), set())
        else:
            composables = resolver.resolve(*resolver.objects_from_anchor_name(anchor))
        advertised = set(composables.metrics) | set(composables.cfl_metrics)
        # Both levels, not just the one directly over the measure.
        assert not advertised & {"Ranked", "Wrapped"}


# --- ACR must not advertise what the grain-dedup pass would refuse ----------

DEDUP_GUARD_YAML = """\
version: 1.0

dataObjects:
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: string, primaryKey: true}
      Stock On Hand: {code: stock_on_hand, abstractType: int}
      Category: {code: category, abstractType: string}
  Sales:
    code: sales
    schema: main
    columns:
      Sale ID: {code: id, abstractType: string, primaryKey: true}
      Sale Product ID: {code: product_id, abstractType: string}
      Region: {code: region, abstractType: string}
      Quantity: {code: quantity, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Sale Product ID]
        columnsTo: [Product ID]

dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
  Category: {dataObject: Products, column: Category, resultType: string}

measures:
  Sold Quantity:
    resultType: int
    aggregation: sum
    expression: '{[Sales].[Quantity]}'
  Total Stock On Hand:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
  Stock Filtered Off Grain:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
    filters:
      - column: {dataObject: Sales, column: Quantity}
        operator: gt
        values: [{dataType: int, valueInt: 1}]
  Stock Filtered In Grain:
    resultType: int
    aggregation: sum
    expression: '{[Products].[Stock On Hand]}'
    filters:
      - column: {dataObject: Products, column: Category}
        operator: equals
        values: [{dataType: string, valueString: tools}]

metrics:
  Stock per Sale:
    expression: '{[Total Stock On Hand]} / {[Sold Quantity]}'
"""


def _dedup_guard_model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(DEDUP_GUARD_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def test_acr_excludes_measures_the_dedup_pass_would_refuse() -> None:
    """ACR promises that whatever it lists compiles.

    A measure sourced from a replicated object whose ``filters:`` reach outside
    that object is refused by ``compiler.grain_dedup`` - its predicate columns
    would land in the rewrite's DISTINCT and split one source row per predicate
    value. ACR classified purely on join reachability and so advertised it.
    """
    result = ComposabilityResolver(_dedup_guard_model()).resolve({"Sales"}, set())
    composable = set(result.measures) | set(result.cfl_measures)

    assert "Stock Filtered Off Grain" not in composable
    # A predicate on the deduplicated object itself is fine - its columns are
    # fixed by the key being deduplicated on.
    assert "Stock Filtered In Grain" in composable
    assert "Total Stock On Hand" in composable
    assert "Sold Quantity" in composable


def test_everything_acr_lists_actually_compiles() -> None:
    """The contract itself, across every anchor rather than one.

    Only the forward direction is asserted - everything ACR lists must compile.
    The converse does not hold today for an unrelated reason: a ``withinGroup``
    naming an object the query never joins compiles to SQL that fails at
    execution (``Referenced table ... not found``), so "did not raise" is not a
    sound oracle for the other direction.
    """
    from orionbelt.compiler.pipeline import CompilationPipeline

    model = _dedup_guard_model()
    resolver = ComposabilityResolver(model)

    anchors: list[tuple[list[str], set[str], set[str]]] = [([], set(), set())]
    for dim in model.dimensions:
        dim_objects, measure_objects = resolver.objects_from_anchor_name(dim)
        anchors.append(([dim], dim_objects, measure_objects))

    for dims, dim_objects, measure_objects in anchors:
        result = resolver.resolve(dim_objects, measure_objects)
        # Metrics too: checking only measures is how a metric over a
        # deduplicated component slipped through this test once already.
        advertised = (
            set(result.measures)
            | set(result.cfl_measures)
            | set(result.metrics)
            | set(result.cfl_metrics)
        )
        for name in advertised:
            query = QueryObject(**{"select": {"dimensions": dims, "measures": [name]}})
            CompilationPipeline().compile(query, model, "duckdb")


def test_guard_holds_for_anchors_that_do_not_reach_the_measure() -> None:
    """Replication is forced by the measure's own clauses, not just the anchor.

    Anchored on ``Category`` - a dimension on the deduplicated object itself -
    nothing in the anchor reaches ``Sales``. The compiler still joins it, to
    evaluate the measure's filter, which replicates ``Products``. Judging
    replication from the anchor alone missed this, and missed the empty anchor
    entirely.
    """
    model = _dedup_guard_model()
    resolver = ComposabilityResolver(model)

    for anchor in ("Category", None):
        if anchor is None:
            result = resolver.resolve(set(), set())
        else:
            result = resolver.resolve(*resolver.objects_from_anchor_name(anchor))
        composable = set(result.measures) | set(result.cfl_measures)
        assert "Stock Filtered Off Grain" not in composable
        assert "Stock Filtered In Grain" in composable


def test_acr_advertises_a_metric_over_a_deduplicated_component() -> None:
    """A deduplicated component is rewritten, not refused - so is its metric.

    The rewrite computes each component separately and rebuilds the metric over
    the results, so a metric is excluded on the same terms as a measure: only
    when the rewrite refuses it outright.
    """
    model = _dedup_guard_model()
    resolver = ComposabilityResolver(model)

    for anchor in (None, "Region", "Category"):
        if anchor is None:
            result = resolver.resolve(set(), set())
        else:
            result = resolver.resolve(*resolver.objects_from_anchor_name(anchor))
        assert "Stock per Sale" in set(result.metrics) | set(result.cfl_metrics)
        assert "Total Stock On Hand" in set(result.measures) | set(result.cfl_measures)


def test_acr_excludes_a_metric_whose_deduplicated_component_is_nested() -> None:
    """A deduplicated measure two metrics away cannot be split out.

    The window wrapper rebuilds its base measure from the fact tables, which a
    dedup CTE cannot serve, so the compiler refuses - and ACR must not
    advertise what it refuses.
    """
    yaml_text = (
        DEDUP_GUARD_YAML
        + """  Stock Rank:
    type: window
    windowFunction: dense_rank
    measure: Total Stock On Hand
  Doubled Stock Rank:
    expression: '{[Stock Rank]} * 2'
"""
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors

    composables = ComposabilityResolver(model).resolve({"Sales"}, set())
    assert "Stock per Sale" in composables.metrics
    assert "Doubled Stock Rank" not in composables.metrics


# ---------------------------------------------------------------------------
# Cross-object computed columns
# ---------------------------------------------------------------------------


def _cross_object_models() -> tuple[SemanticModel, SemanticModel]:
    """(reachable, unreachable) — the same model with one expression changed.

    In the second, ``Store.Zip Matches`` reads ``Warehouse``, which no fact
    joins, so anything reading that column is unplannable.
    """
    import tests.unit.test_cross_object_columns as fixtures

    reachable = fixtures.MODEL_YAML
    unreachable = reachable.replace(
        'expression: "SUBSTRING({Store Zip}, 1, 5) = {[Address].[Zip 5]}"',
        'expression: "{Store Zip} = {[Warehouse].[Warehouse Zip]}"',
    )
    return _load(reachable), _load(unreachable)


def _compiles(model: SemanticModel, dimensions: list[str], measures: list[str]) -> bool:
    from orionbelt.compiler.pipeline import CompilationPipeline
    from orionbelt.models.query import QuerySelect

    try:
        CompilationPipeline().compile(
            QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures)),
            model,
            "postgres",
        )
    except Exception:  # noqa: BLE001 — any refusal counts
        return False
    return True


def test_unreachable_cross_object_reference_is_not_advertised() -> None:
    """ACR weighs what a computed column reads, as the planner does.

    Advertising an artefact the compiler then refuses is worse than not
    advertising it: the whole point of composables is to tell an agent what it
    can ask for.
    """
    _, unreachable = _cross_object_models()
    result = ComposabilityResolver(unreachable).resolve(set(), set())
    assert "Zip Matches" not in result.dimensions
    assert "Zip Differs" not in result.dimensions  # reaches it through a sibling
    assert "Local Sales Amount" not in result.measures
    assert "Store Zip" in result.dimensions  # a plain column is unaffected


def test_reachable_cross_object_reference_is_still_advertised() -> None:
    reachable, _ = _cross_object_models()
    result = ComposabilityResolver(reachable).resolve(set(), set())
    assert "Zip Matches" in result.dimensions
    assert "Local Sales Amount" in result.measures


@pytest.mark.parametrize("reachable_model", [True, False])
def test_what_an_anchor_offers_actually_compiles(reachable_model: bool) -> None:
    """The property that matters, and the guard against this drifting again.

    An empty anchor lists what is available *independently*, so not every pair
    of its dimensions and measures combines — that is what anchoring answers.
    Anchor on each dimension in turn and the measures offered alongside it must
    survive the planner.
    """
    from orionbelt.models.query import QuerySelect

    reachable, unreachable = _cross_object_models()
    model = reachable if reachable_model else unreachable
    resolver = ComposabilityResolver(model)

    checked = 0
    for dimension in resolver.resolve(set(), set()).dimensions:
        anchor = QueryObject(select=QuerySelect(dimensions=[dimension]))
        offered = resolver.resolve(*resolver.objects_from_query(anchor))
        for measure in offered.measures:
            assert _compiles(model, [dimension], [measure]), f"{dimension} + {measure}"
            checked += 1
    assert checked, "nothing offered — the test would prove nothing"


@pytest.mark.parametrize("reachable_model", [True, False])
def test_named_anchor_agrees_with_the_same_anchor_as_a_query(reachable_model: bool) -> None:
    """``GET /composables?anchor=X`` and ``POST`` with a query selecting X are
    two ways to ask the same question, so they have to resolve the same
    objects. They drifted once: the named path returned a dimension's own data
    object and forgot what its expression reads."""
    from orionbelt.models.query import QuerySelect

    reachable, unreachable = _cross_object_models()
    model = reachable if reachable_model else unreachable
    resolver = ComposabilityResolver(model)

    for name in model.dimensions:
        as_query = resolver.objects_from_query(QueryObject(select=QuerySelect(dimensions=[name])))
        assert resolver.objects_from_anchor_name(name) == as_query, name
    for name in model.measures:
        as_query = resolver.objects_from_query(QueryObject(select=QuerySelect(measures=[name])))
        assert resolver.objects_from_anchor_name(name) == as_query, name


@pytest.mark.parametrize("reachable_model", [True, False])
def test_what_a_named_anchor_offers_actually_compiles(reachable_model: bool) -> None:
    """The same property the query anchor is held to, through the GET path."""
    reachable, unreachable = _cross_object_models()
    model = reachable if reachable_model else unreachable
    resolver = ComposabilityResolver(model)

    checked = 0
    for dimension in resolver.resolve(set(), set()).dimensions:
        offered = resolver.resolve(*resolver.objects_from_anchor_name(dimension))
        for measure in offered.measures:
            assert _compiles(model, [dimension], [measure]), f"{dimension} + {measure}"
            checked += 1
    assert checked, "nothing offered — the test would prove nothing"
