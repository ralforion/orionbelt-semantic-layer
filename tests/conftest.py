"""Shared test fixtures for OrionBelt Semantic Layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.service.session_manager import SessionManager


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip docker-marked tests unless ``-m docker`` is explicitly requested."""
    marker_expr = config.getoption("-m", default="")
    if "docker" in str(marker_expr):
        return  # user explicitly asked for docker tests
    skip_docker = pytest.mark.skip(reason="Docker tests not selected — run with: pytest -m docker")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip_docker)


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SALES_MODEL_DIR = FIXTURES_DIR / "sales_model"
QUERIES_DIR = FIXTURES_DIR / "queries"


@pytest.fixture
def loader() -> TrackedLoader:
    return TrackedLoader()


@pytest.fixture
def resolver() -> ReferenceResolver:
    return ReferenceResolver()


@pytest.fixture
def sales_model_raw(loader: TrackedLoader) -> tuple[dict, object]:
    """Load the sales model fixture as raw dict."""
    return loader.load(SALES_MODEL_DIR / "model.yaml")


@pytest.fixture
def sales_model(sales_model_raw: tuple[dict, object], resolver: ReferenceResolver) -> SemanticModel:
    """Load and resolve the sales model fixture."""
    raw, source_map = sales_model_raw
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Sales model has validation errors: {result.errors}"
    return model


@pytest.fixture
def session_manager() -> SessionManager:
    """SessionManager with long TTL and no cleanup thread (for tests)."""
    return SessionManager(ttl_seconds=3600, cleanup_interval=9999)


SAMPLE_MODEL_YAML = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
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
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Order Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Order Customer ID
        columnsTo:
          - Customer ID

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string

measures:
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum

  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count

  Grand Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
    total: true

metrics:
  Revenue per Order:
    expression: '{[Total Revenue]} / {[Order Count]}'

  Revenue Share:
    expression: '{[Total Revenue]} / {[Grand Total Revenue]}'
"""
