"""A ``decimal`` measure filter value is exported as the number it names.

A float holding it has already rounded ``1.0000000000000001`` to 1.0, so the
digits travel as text in ``valueString``; the expression is written from them,
and the round-trip metadata keeps them as text.
"""

from __future__ import annotations

import json
from typing import Any

import osi_orionbelt.converter as conv


def _model(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1.0,
        "dataObjects": {
            "Ledger": {
                "code": "LEDGER",
                "columns": {
                    "Entry ID": {"code": "ID", "abstractType": "string", "primaryKey": True},
                    "Amount": {"code": "AMOUNT", "abstractType": "decimal"},
                },
            }
        },
        "measures": {
            "Matching Amount": {
                "columns": [{"dataObject": "Ledger", "column": "Amount"}],
                "resultType": "decimal",
                "aggregation": "sum",
                "filters": [
                    {
                        "column": {"dataObject": "Ledger", "column": "Amount"},
                        "operator": "equals",
                        "values": [{"dataType": "decimal", **value}],
                    }
                ],
            }
        },
    }


def _export(value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    converter = conv.OBMLtoOSI(_model(value))
    return converter.convert(), converter.warnings


def _expression(osi: dict[str, Any]) -> str:
    (metric,) = [m for m in osi["metrics"] if m["name"] == "Matching Amount"]
    return str(metric["expression"]["dialects"][0]["expression"])


def test_text_digits_are_written_into_the_expression() -> None:
    osi, warnings = _export({"valueString": "1.0000000000000001"})
    assert "= 1.0000000000000001" in _expression(osi)
    assert not any("decimal filter value" in w for w in warnings)


def test_the_round_trip_metadata_keeps_the_text() -> None:
    osi, _ = _export({"valueString": "1.0000000000000001"})
    back = conv.OSItoOBML(osi).convert()
    value = back["measures"]["Matching Amount"]["filters"][0]["values"][0]
    assert value == {"dataType": "decimal", "valueString": "1.0000000000000001"}
    assert "1.0000000000000001" in json.dumps(osi)


def test_an_integer_is_exact() -> None:
    osi, warnings = _export({"valueFloat": 9007199254740993})
    assert "= 9007199254740993" in _expression(osi)
    assert not any("decimal filter value" in w for w in warnings)


def test_a_float_is_warned_about() -> None:
    """A double may already be the rounding of what was written."""
    osi, warnings = _export({"valueFloat": 1.0})
    assert "= 1.0" in _expression(osi)
    assert any("decimal filter value 1.0" in w and "valueString" in w for w in warnings)


def test_text_that_is_not_a_number_is_not_spliced_into_sql() -> None:
    osi, warnings = _export({"valueString": "1; DROP TABLE x"})
    assert all(m["name"] != "Matching Amount" for m in osi.get("metrics", []))
    assert any("is not a number" in w for w in warnings)
