"""Tests for compiler.health — structural model health metrics."""

from __future__ import annotations

import pytest

from orionbelt.compiler.health import compute_health
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_TWO_FACTS_SHARED_DIM = """\
version: 1.0
dataObjects:
  Customers:
    code: CUSTOMERS
    database: WH
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string
  Orders:
    code: ORDERS
    database: WH
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Customer ID]
        columnsTo: [Customer ID]
  Returns:
    code: RETURNS
    database: WH
    schema: PUBLIC
    columns:
      Return ID:
        code: RETURN_ID
        abstractType: string
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Refund:
        code: REFUND
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Country:
    dataObject: Customers
    column: Country
    resultType: string
measures:
  Total Amount:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  Total Refund:
    columns: [{dataObject: Returns, column: Refund}]
    aggregation: sum
    resultType: float
"""

_ORPHAN_MODEL = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WH
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Customer ID]
        columnsTo: [Customer ID]
  Customers:
    code: CUSTOMERS
    database: WH
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string
  Marketing:
    code: MARKETING
    database: WH
    schema: PUBLIC
    columns:
      Campaign ID:
        code: CAMPAIGN_ID
        abstractType: string
      Spend:
        code: SPEND
        abstractType: float
dimensions:
  Country:
    dataObject: Customers
    column: Country
    resultType: string
measures:
  Order Amount:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
"""


@pytest.fixture
def loader() -> TrackedLoader:
    return TrackedLoader()


@pytest.fixture
def resolver() -> ReferenceResolver:
    return ReferenceResolver()


def _resolve(yaml_str: str, loader: TrackedLoader, resolver: ReferenceResolver):
    raw, source_map = loader.load_string(yaml_str)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, result.errors
    return model


class TestComputeHealth:
    def test_clean_model_returns_ok(
        self, loader: TrackedLoader, resolver: ReferenceResolver
    ) -> None:
        from tests.conftest import SAMPLE_MODEL_YAML

        model = _resolve(SAMPLE_MODEL_YAML, loader, resolver)
        h = compute_health(model)
        assert h.status == "ok"
        assert h.data_objects == 2
        assert h.joins == 1
        assert h.orphan_data_objects == []
        assert h.fan_trap_risks == []
        assert h.unreachable_dimensions == []
        assert h.warnings_count == 0

    def test_orphan_data_object_detected(
        self, loader: TrackedLoader, resolver: ReferenceResolver
    ) -> None:
        model = _resolve(_ORPHAN_MODEL, loader, resolver)
        h = compute_health(model)
        assert h.status == "warnings"
        assert "Marketing" in h.orphan_data_objects
        assert h.warnings_count >= 1

    def test_fan_trap_risk_detected(
        self, loader: TrackedLoader, resolver: ReferenceResolver
    ) -> None:
        model = _resolve(_TWO_FACTS_SHARED_DIM, loader, resolver)
        h = compute_health(model)
        assert len(h.fan_trap_risks) == 1
        risk = h.fan_trap_risks[0]
        assert "WH.PUBLIC.ORDERS" in risk.tables
        assert "WH.PUBLIC.RETURNS" in risk.tables
        assert risk.suggested_pattern == "composite_fact_layer"
        assert h.status == "warnings"

    def test_single_data_object_not_orphan(
        self, loader: TrackedLoader, resolver: ReferenceResolver
    ) -> None:
        single = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WH
    schema: PUBLIC
    columns:
      Amount:
        code: AMOUNT
        abstractType: float
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
"""
        model = _resolve(single, loader, resolver)
        h = compute_health(model)
        assert h.orphan_data_objects == []
        assert h.status == "ok"
