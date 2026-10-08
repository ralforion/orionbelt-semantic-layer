"""Reaggregate metrics survive an Ossie roundtrip through the model-level extension.

A reaggregate metric needs a second query layer, so it has no single portable
expression and is not exported as an Ossie metric. It must come back intact on
the reverse conversion, and the warning must say why it was left out.
"""

from __future__ import annotations

from typing import Any

import osi_orionbelt.converter as conv

_REAGGREGATE: dict[str, Any] = {
    "type": "reaggregate",
    "measure": "Revenue",
    "per": ["Customer", "Order Date:day"],
    "aggregation": "avg",
    "description": "Average revenue per customer and day",
}

_OBML: dict[str, Any] = {
    "version": 1.0,
    "dataObjects": {
        "Orders": {
            "code": "ORDERS",
            "database": "WAREHOUSE",
            "schema": "PUBLIC",
            "columns": {
                "Customer": {"code": "CUSTOMER_ID", "abstractType": "string"},
                "Order Date": {"code": "ORDER_DATE", "abstractType": "date"},
                "Amount": {"code": "AMOUNT", "abstractType": "float"},
            },
        },
    },
    "dimensions": {
        "Customer": {"dataObject": "Orders", "column": "Customer", "resultType": "string"},
        "Order Date": {"dataObject": "Orders", "column": "Order Date", "resultType": "date"},
    },
    "measures": {
        "Revenue": {
            "columns": [{"dataObject": "Orders", "column": "Amount"}],
            "aggregation": "sum",
        },
    },
    "metrics": {"Avg Revenue per Customer": _REAGGREGATE},
}


def test_not_exported_as_an_ossie_metric() -> None:
    converter = conv.OBMLtoOSI(_OBML)
    osi = converter.convert()
    names = {m["name"] for m in osi["semantic_model"][0].get("metrics", [])}
    assert "Avg Revenue per Customer" not in names
    assert any(
        "Avg Revenue per Customer" in w and "second query layer" in w for w in converter.warnings
    )


def test_roundtrip_restores_the_definition() -> None:
    osi = conv.OBMLtoOSI(_OBML).convert()
    obml = conv.OSItoOBML(osi).convert()
    assert obml["metrics"]["Avg Revenue per Customer"] == _REAGGREGATE
