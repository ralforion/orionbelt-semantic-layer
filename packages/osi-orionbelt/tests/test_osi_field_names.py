"""An Ossie field is named by the OBML column name, never by the column code.

The code is the field's expression, and an Ossie import writes a calculated
field's SQL into it (apache/ossie#601). Naming the field by its code turned
``amount_with_tax`` into a field called ``amount * 1.1`` on the way back.
"""

from __future__ import annotations

import json
from typing import Any

import osi_orionbelt.converter as conv


def _expr(sql: str) -> dict[str, Any]:
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": sql}]}


def _ossie() -> dict[str, Any]:
    return {
        "version": "0.2.0",
        "semantic_model": [
            {
                "name": "sales",
                "datasets": [
                    {
                        "name": "orders",
                        "source": "db.orders",
                        "primary_key": ["id"],
                        "fields": [
                            {"name": "id", "expression": _expr("order_id")},
                            {"name": "amount", "expression": _expr("amount")},
                            {"name": "amount_with_tax", "expression": _expr("amount * 1.1")},
                        ],
                    }
                ],
                "metrics": [
                    {
                        "name": "taxed_revenue",
                        "expression": _expr("SUM(orders.amount_with_tax)"),
                    }
                ],
            }
        ],
    }


def _dataset(osi: dict[str, Any]) -> dict[str, Any]:
    return osi["semantic_model"][0]["datasets"][0]


def _fields(osi: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in _dataset(osi)["fields"]}


class TestOssieFirstRoundTrip:
    def test_calculated_field_keeps_its_name(self) -> None:
        back = conv.OBMLtoOSI(conv.OSItoOBML(_ossie()).convert()).convert()
        fields = _fields(back)
        assert set(fields) == {"id", "amount", "amount_with_tax"}
        calc = fields["amount_with_tax"]
        assert calc["expression"]["dialects"][0]["expression"] == "amount * 1.1"
        assert "description" not in calc
        assert "ai_context" not in calc

    def test_renamed_field_keeps_its_name_and_primary_key(self) -> None:
        back = conv.OBMLtoOSI(conv.OSItoOBML(_ossie()).convert()).convert()
        assert _fields(back)["id"]["expression"]["dialects"][0]["expression"] == "order_id"
        assert _dataset(back)["primary_key"] == ["id"]

    def test_primary_key_matches_the_field_name(self) -> None:
        obml = conv.OSItoOBML(_ossie()).convert()
        assert obml["dataObjects"]["orders"]["columns"]["id"].get("primaryKey") is True

    def test_metric_references_the_calculated_field(self) -> None:
        back = conv.OBMLtoOSI(conv.OSItoOBML(_ossie()).convert()).convert()
        metric = next(
            m for m in back["semantic_model"][0]["metrics"] if m["name"] == "taxed_revenue"
        )
        assert metric["expression"]["dialects"][0]["expression"] == (
            'SUM("orders"."amount_with_tax")'
        )

    def test_no_column_name_stash(self) -> None:
        back = conv.OBMLtoOSI(conv.OSItoOBML(_ossie()).convert()).convert()
        for field in _dataset(back)["fields"]:
            for ext in field.get("custom_extensions", []):
                assert "obml_column_name" not in json.loads(ext["data"])


class TestLegacyCodeNamedExport:
    """Exports before 0.5.0 named fields by code and stashed the column name."""

    def test_column_name_and_primary_key_restored(self) -> None:
        legacy = {
            "version": "0.2.0",
            "semantic_model": [
                {
                    "name": "sales",
                    "datasets": [
                        {
                            "name": "Orders",
                            "source": "WH.PUBLIC.orders",
                            "primary_key": ["order_id"],
                            "fields": [
                                {
                                    "name": "order_id",
                                    "expression": _expr("order_id"),
                                    "custom_extensions": [
                                        {
                                            "vendor_name": "ORIONBELT",
                                            "data": json.dumps({"obml_column_name": "Order ID"}),
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        columns = conv.OSItoOBML(legacy).convert()["dataObjects"]["Orders"]["columns"]
        assert set(columns) == {"Order ID"}
        assert columns["Order ID"]["code"] == "order_id"
        assert columns["Order ID"].get("primaryKey") is True


def _legacy_field(code: str, column: str) -> dict[str, Any]:
    """A field as exports before 0.5.0 wrote it: named by code, column stashed."""
    return {
        "name": code,
        "expression": _expr(code),
        "custom_extensions": [
            {"vendor_name": "ORIONBELT", "data": json.dumps({"obml_column_name": column})}
        ],
    }


def _legacy_export() -> dict[str, Any]:
    return {
        "version": "0.2.0",
        "semantic_model": [
            {
                "name": "sales",
                "datasets": [
                    {
                        "name": "Orders",
                        "source": "WH.PUBLIC.orders",
                        "primary_key": ["order_id"],
                        "fields": [
                            _legacy_field("order_id", "Order ID"),
                            _legacy_field("customer_id", "Customer"),
                            _legacy_field("amount", "Net Amount"),
                        ],
                    },
                    {
                        "name": "Customers",
                        "source": "WH.PUBLIC.customers",
                        "unique_keys": [["customer_id"]],
                        "fields": [_legacy_field("customer_id", "Customer ID")],
                    },
                ],
                "relationships": [
                    {
                        "name": "orders_to_customers",
                        "from": "Orders",
                        "to": "Customers",
                        "from_columns": ["customer_id"],
                        "to_columns": ["customer_id"],
                    }
                ],
                "metrics": [
                    {
                        "name": "grand_total",
                        "expression": _expr('SUM("Orders"."amount") OVER ()'),
                    }
                ],
            }
        ],
    }


class TestLegacyStashesFollowTheRename:
    """What an older export left verbatim in the OBML refers to codes."""

    def test_unique_keys_name_the_fields(self) -> None:
        obml = conv.OSItoOBML(_legacy_export()).convert()
        assert obml["dataObjects"]["Orders"]["joins"][0]["joinType"] == "many-to-one"
        osi = conv.OBMLtoOSI(obml).convert()
        customers = osi["semantic_model"][0]["datasets"][1]
        assert customers["unique_keys"] == [["Customer ID"]]
        back = conv.OSItoOBML(osi).convert()
        assert back["dataObjects"]["Orders"]["joins"][0]["joinType"] == "many-to-one"

    def test_preserved_metric_references_the_fields(self) -> None:
        obml = conv.OSItoOBML(_legacy_export()).convert()
        osi = conv.OBMLtoOSI(obml).convert()
        metric = next(m for m in osi["semantic_model"][0]["metrics"] if m["name"] == "grand_total")
        assert metric["expression"]["dialects"][0]["expression"] == (
            'SUM("Orders"."Net Amount") OVER ()'
        )

    def test_unparseable_preserved_metric_is_kept_and_warned(self) -> None:
        legacy = _legacy_export()
        legacy["semantic_model"][0]["metrics"] = [
            {"name": "broken", "expression": _expr('SUM("Orders"."amount" OVER (')}
        ]
        exporter = conv.OBMLtoOSI(conv.OSItoOBML(legacy).convert())
        osi = exporter.convert()
        metric = next(m for m in osi["semantic_model"][0]["metrics"] if m["name"] == "broken")
        assert metric["expression"]["dialects"][0]["expression"] == 'SUM("Orders"."amount" OVER ('
        assert any("broken" in w and "physical" in w for w in exporter.warnings)
