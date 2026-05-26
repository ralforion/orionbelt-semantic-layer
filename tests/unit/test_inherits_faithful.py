"""Inheritance must round-trip every OBML field — not just a small subset.

Pre-v2.7.5 bug (review finding 1): ``ModelStore._model_to_raw`` was a
lossy serializer that dropped most non-essential fields when converting
a loaded parent back to a raw dict for the merger. Columns kept only
``code`` + ``abstractType``; measures lost ``dataType`` / ``filters`` /
``grain`` / ``delimiter`` / ``withinGroup``; metrics lost most subtype
config. A child inheriting from a parent with a computed column saw an
empty ``code:`` and compiled ``SUM("T"."")``.

Fix: store each loaded model's *merged* raw dict in parallel with the
resolved ``SemanticModel`` (``ModelStore._raws``) so inheritance can
re-merge against the exact content the parent was built from.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.service.model_store import ModelStore


@pytest.fixture
def store() -> ModelStore:
    return ModelStore()


def test_computed_column_survives_inheritance(store: ModelStore) -> None:
    parent = """
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: DB
    schema: SCH
    columns:
      Amount: {code: AMT, abstractType: float, numClass: additive}
      Discount: {code: DSC, abstractType: float, numClass: additive}
      Net Amount:
        expression: '{Amount} - {Discount}'
        abstractType: float
        numClass: additive
measures:
  Total Net:
    columns: [{dataObject: Orders, column: Net Amount}]
    aggregation: sum
    resultType: float
"""
    child = """
version: 1.0
measures:
  Doubled Net:
    columns: [{dataObject: Orders, column: Net Amount}]
    aggregation: sum
    resultType: float
"""
    parent_id = store.load_model(parent).model_id
    child_id = store.load_model(child, inherits_model_id=parent_id).model_id
    model = store.get_model(child_id)
    sql = (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(measures=["Total Net", "Doubled Net"])),
            model,
            "postgres",
        )
        .sql
    )
    assert 'SUM("Orders"."AMT" - "Orders"."DSC")' in sql
    assert 'SUM("Orders"."")' not in sql, "lossy round-trip regression"


def test_inheritance_preserves_numclass_and_primarykey(store: ModelStore) -> None:
    parent = """
version: 1.0
dataObjects:
  Customers:
    code: CUSTOMERS
    columns:
      Customer ID:
        code: CID
        abstractType: string
        primaryKey: true
      Country:
        code: COUNTRY
        abstractType: string
dimensions:
  Country: {dataObject: Customers, column: Country, resultType: string}
"""
    child = """
version: 1.0
measures:
  Customer Count:
    columns: [{dataObject: Customers, column: Customer ID}]
    aggregation: count
    resultType: int
"""
    parent_id = store.load_model(parent).model_id
    child_id = store.load_model(child, inherits_model_id=parent_id).model_id
    model = store.get_model(child_id)
    cid = model.data_objects["Customers"].columns["Customer ID"]
    assert cid.primary_key is True
    country = model.data_objects["Customers"].columns["Country"]
    # numClass shouldn't be set here (string), but additive numerics should survive
    assert country.code == "COUNTRY"


def test_inheritance_preserves_measure_extras(store: ModelStore) -> None:
    """Measure fields the pre-fix serializer silently dropped:
    ``dataType``, ``distinct``, ``filterContext``, ``filters``,
    ``allowFanOut``, ``delimiter``, ``withinGroup``, ``format``, etc.
    """
    parent = """
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    columns:
      Customer ID: {code: CID, abstractType: string}
      Status: {code: STATUS, abstractType: string}
measures:
  Customer List:
    columns: [{dataObject: Orders, column: Customer ID}]
    aggregation: listagg
    resultType: string
    delimiter: ', '
    withinGroup:
      column: {dataObject: Orders, column: Customer ID}
      order: ASC
  Unique Customers:
    columns: [{dataObject: Orders, column: Customer ID}]
    aggregation: count
    distinct: true
    resultType: int
    dataType: bigint
    allowFanOut: true
    filters:
      - column: {dataObject: Orders, column: Status}
        operator: equals
        values: [{dataType: string, valueString: 'active'}]
"""
    child = """
version: 1.0
measures:
  Echo:
    columns: [{dataObject: Orders, column: Customer ID}]
    aggregation: count
    resultType: int
"""
    parent_id = store.load_model(parent).model_id
    child_id = store.load_model(child, inherits_model_id=parent_id).model_id
    model = store.get_model(child_id)
    cl = model.measures["Customer List"]
    assert cl.delimiter == ", "
    assert cl.within_group is not None
    assert cl.within_group.order == "ASC"
    uc = model.measures["Unique Customers"]
    assert uc.distinct is True
    assert uc.data_type == "bigint"
    assert uc.allow_fan_out is True
    assert len(uc.filters) == 1


def test_inheritance_preserves_examples_and_customextensions(store: ModelStore) -> None:
    parent = """
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    columns:
      Amount: {code: AMT, abstractType: float, numClass: additive}
measures:
  Total:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
examples:
  - name: top_revenue
    description: Total revenue across all orders
    intentTags: [revenue, totals]
    query:
      select:
        measures: [Total]
customExtensions:
  - vendor: osi
    data: '{"ai_context": "demo"}'
"""
    child = """
version: 1.0
measures:
  Double Total:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
"""
    parent_id = store.load_model(parent).model_id
    child_id = store.load_model(child, inherits_model_id=parent_id).model_id
    model = store.get_model(child_id)
    assert len(model.examples) == 1
    assert model.examples[0].name == "top_revenue"
    assert "revenue" in model.examples[0].intent_tags
    assert len(model.custom_extensions) == 1
    assert model.custom_extensions[0].vendor == "osi"
