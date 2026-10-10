"""``first`` / ``last`` measures survive an Ossie roundtrip through the extension.

ANSI SQL has no aggregate for the value at the greatest key (each engine spells
it its own way), so the measure is not exported as an Ossie metric; it comes
back intact on the reverse conversion, and the warning says why.
"""

from __future__ import annotations

from typing import Any

import osi_orionbelt.converter as conv

_CLOSE: dict[str, Any] = {
    "columns": [{"dataObject": "Trades", "column": "Price"}],
    "aggregation": "last",
    "withinGroup": {"column": {"dataObject": "Trades", "column": "Traded At"}},
}

_OBML: dict[str, Any] = {
    "version": 1.0,
    "dataObjects": {
        "Trades": {
            "code": "TRADES",
            "database": "WAREHOUSE",
            "schema": "PUBLIC",
            "columns": {
                "Price": {"code": "PRICE", "abstractType": "float"},
                "Traded At": {"code": "TRADED_AT", "abstractType": "timestamp"},
            },
        },
    },
    "measures": {"Close": _CLOSE},
}


def test_not_exported_as_an_ossie_metric() -> None:
    converter = conv.OBMLtoOSI(_OBML)
    osi = converter.convert()
    assert "Close" not in {m["name"] for m in osi["semantic_model"][0].get("metrics", [])}
    assert any("Close" in w and "greatest key" in w for w in converter.warnings)


def test_roundtrip_restores_the_definition() -> None:
    obml = conv.OSItoOBML(conv.OBMLtoOSI(_OBML).convert()).convert()
    assert obml["measures"]["Close"] == _CLOSE
