"""Resolver must round-trip top-level + dataObject metadata (v2.7.5, #83).

Pre-fix bug: ``ReferenceResolver`` parsed ``name`` / ``description`` at
the top level and ``description`` on each dataObject, but never passed
them into the ``SemanticModel`` / ``DataObject`` constructors. Result:
authored documentation silently vanished from every downstream surface
(``GET /v1/models`` discovery, RDF graph exporter, ``/v1/schema``, the
new ontology drift guard).
"""

from __future__ import annotations

import pytest

from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_YAML = """
version: 1.0
name: sales_analytics
description: Cross-region sales analytics for the EMEA business unit
dataObjects:
  Orders:
    code: ORDERS
    description: Order facts from the OLTP system, refreshed hourly
    columns:
      ID:
        code: ID
        abstractType: string
  Customers:
    code: CUSTOMERS
    description: Customer master from the CRM
    columns:
      ID:
        code: CID
        abstractType: string
"""


@pytest.fixture
def loaded():
    raw, sm = TrackedLoader().load_string(_YAML)
    model, vr = ReferenceResolver().resolve(raw, sm)
    assert vr.valid, vr.errors
    return model


def test_model_name_round_trips(loaded) -> None:
    assert loaded.name == "sales_analytics"


def test_model_description_round_trips(loaded) -> None:
    assert loaded.description == "Cross-region sales analytics for the EMEA business unit"


def test_dataobject_description_round_trips(loaded) -> None:
    assert (
        loaded.data_objects["Orders"].description
        == "Order facts from the OLTP system, refreshed hourly"
    )
    assert loaded.data_objects["Customers"].description == "Customer master from the CRM"


def test_model_metadata_absent_when_unset() -> None:
    """Missing keys must produce ``None`` (not empty string, not an error)."""
    raw, sm = TrackedLoader().load_string(
        "version: 1.0\ndataObjects: {}\ndimensions: {}\nmeasures: {}\n"
    )
    model, vr = ReferenceResolver().resolve(raw, sm)
    assert vr.valid, vr.errors
    assert model.name is None
    assert model.description is None
