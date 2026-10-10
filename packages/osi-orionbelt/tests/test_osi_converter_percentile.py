"""Percentile measures and reaggregate metrics survive an Ossie roundtrip.

A percentile measure exports as the ordered-set aggregate every engine that
has one spells the same way, with its fraction; the fraction comes back on the
reverse conversion. A percentile reaggregate metric is kept in the extension,
as every reaggregate metric is.
"""

from __future__ import annotations

from typing import Any

import osi_orionbelt.converter as conv

_OBML: dict[str, Any] = {
    "version": 1.0,
    "dataObjects": {
        "Orders": {
            "code": "ORDERS",
            "database": "WAREHOUSE",
            "schema": "PUBLIC",
            "columns": {
                "Customer": {"code": "CUSTOMER_ID", "abstractType": "string"},
                "Amount": {"code": "AMOUNT", "abstractType": "float"},
            },
        },
    },
    "dimensions": {
        "Customer": {"dataObject": "Orders", "column": "Customer", "resultType": "string"},
    },
    "measures": {
        "Revenue": {
            "columns": [{"dataObject": "Orders", "column": "Amount"}],
            "aggregation": "sum",
        },
        "P90 Amount": {
            "columns": [{"dataObject": "Orders", "column": "Amount"}],
            "aggregation": "percentile_cont",
            "percentile": 0.9,
        },
        "Tiny Amount": {
            "columns": [{"dataObject": "Orders", "column": "Amount"}],
            "aggregation": "percentile_disc",
            "percentile": 0.00001,
        },
    },
    "metrics": {
        "P90 Customer Revenue": {
            "type": "reaggregate",
            "measure": "Revenue",
            "per": ["Customer"],
            "aggregation": "percentile_disc",
            "percentile": 0.9,
        },
    },
}


def _expression(osi: dict[str, Any], name: str) -> str:
    metric = next(m for m in osi["semantic_model"][0]["metrics"] if m["name"] == name)
    return str(metric["expression"]["dialects"][0]["expression"])


def test_exported_as_the_ordered_set_aggregate() -> None:
    osi = conv.OBMLtoOSI(_OBML).convert()
    assert _expression(osi, "P90 Amount") == (
        'PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY "Orders"."AMOUNT")'
    )
    # Plain notation: 1e-05 is not a SQL literal everywhere.
    assert _expression(osi, "Tiny Amount").startswith("PERCENTILE_DISC(0.00001) WITHIN GROUP")


def test_roundtrip_restores_the_fraction() -> None:
    obml = conv.OSItoOBML(conv.OBMLtoOSI(_OBML).convert()).convert()
    assert obml["measures"]["P90 Amount"] == _OBML["measures"]["P90 Amount"]
    assert obml["measures"]["Tiny Amount"] == _OBML["measures"]["Tiny Amount"]
    assert obml["metrics"]["P90 Customer Revenue"] == _OBML["metrics"]["P90 Customer Revenue"]
