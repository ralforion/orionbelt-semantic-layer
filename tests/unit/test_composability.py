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
    assert set(result.measures) == set(sales_model.measures)
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
    assert set(result.measures) == set(sales_model.measures)


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
