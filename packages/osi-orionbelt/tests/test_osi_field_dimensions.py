# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Every Ossie field is an OBML dimension.

Ossie lets a query group by any field; the ``dimension`` marker only adds
``is_time``. The import made a dimension of marked fields only, so Ossie's own
compliance models, which mark none, had nothing to group by.
"""

from __future__ import annotations

import copy
from typing import Any

import osi_orionbelt.converter as conv


def _doc(*datasets: dict[str, Any], metrics: list[dict[str, Any]] | None = None) -> dict:
    return {
        "version": "0.3.0.dev0",
        "name": "shop",
        "datasets": list(datasets),
        "metrics": metrics or [{"name": "revenue", "expression": {"sql": "SUM(orders.amount)"}}],
    }


def _dataset(name: str, *fields: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "source": f"wh.public.{name}", "fields": list(fields)}


def _field(name: str, sql: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"name": name, "expression": {"sql": sql or name}, **extra}


_ORDERS = _dataset(
    "orders",
    _field("order_id"),
    _field("amount"),
    _field("ordered_on", datatype="Date"),
    _field("status", dimension={"is_time": False}),
)


def _convert(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    converter = conv.OSItoOBML(copy.deepcopy(doc))
    return converter.convert(), converter.warnings


def _dimensions(doc: dict[str, Any]) -> dict[str, Any]:
    return _convert(doc)[0].get("dimensions", {})


def _markers(osi: dict[str, Any]) -> dict[str, Any]:
    return {
        (ds["name"], f["name"]): f.get("dimension")
        for ds in osi["datasets"]
        for f in ds.get("fields", [])
    }


class TestImport:
    def test_every_field_is_a_dimension(self) -> None:
        dims = _dimensions(_doc(_ORDERS))
        assert set(dims) == {"order_id", "amount", "ordered_on", "status"}
        assert dims["amount"] == {
            "dataObject": "orders",
            "column": "amount",
            "resultType": "float",
        }
        assert dims["ordered_on"]["resultType"] == "date"

    def test_a_marked_field_keeps_its_name(self) -> None:
        # The unmarked field comes first in the document; the marked one wins.
        doc = _doc(
            _dataset("stores", _field("region")),
            _dataset("customers", _field("region", dimension={})),
        )
        obml, warnings = _convert(doc)
        assert obml["dimensions"]["region"]["dataObject"] == "customers"
        assert obml["dimensions"]["stores region"]["dataObject"] == "stores"
        assert any("stores region" in w for w in warnings)

    def test_a_field_obml_cannot_read_is_a_dimension_only_when_marked(self) -> None:
        doc = _doc(
            _dataset(
                "orders",
                _field("label", "first_name || last_name"),
                _field("marked", "first_name || last_name", dimension={}),
            )
        )
        dims = _dimensions(doc)
        assert "label" not in dims
        assert "marked" in dims

    def test_a_field_named_like_a_metric_is_renamed(self) -> None:
        # OBML refuses the whole model when a dimension and a measure share a name.
        doc = _doc(_dataset("orders", _field("amount"), _field("revenue")))
        obml, warnings = _convert(doc)
        assert "revenue" in obml["measures"]
        assert obml["dimensions"]["orders revenue"]["column"] == "revenue"
        assert "revenue" not in obml["dimensions"]
        assert any("orders revenue" in w for w in warnings)

    def test_a_marked_field_named_like_a_metric_is_renamed_too(self) -> None:
        doc = _doc(_dataset("orders", _field("amount"), _field("revenue", dimension={})))
        dims = _dimensions(doc)
        assert "revenue" not in dims
        assert dims["orders revenue"]["column"] == "revenue"

    def test_a_field_named_like_a_count_is_renamed(self) -> None:
        doc = _doc(_dataset("orders", _field("amount"), _field("orders Count", "cnt")))
        dims = _dimensions(doc)
        assert "orders Count" not in dims
        assert dims["orders orders Count"]["column"] == "orders Count"

    def test_a_column_the_import_adds_is_not_a_dimension(self) -> None:
        doc = _doc(_dataset("orders", _field("amount", "amount_cents / 100.0")))
        obml, _ = _convert(doc)
        assert "amount_cents" in obml["dataObjects"]["orders"]["columns"]
        assert set(obml["dimensions"]) == {"amount"}

    def test_a_field_named_like_a_qualified_dimension_keeps_it(self) -> None:
        # returns.amount is qualified to "returns amount"; the field literally
        # named so must not overwrite that dimension.
        doc = _doc(
            _dataset("orders", _field("amount", dimension={})),
            _dataset(
                "returns",
                _field("amount", dimension={}),
                _field("returns amount", "refund_total"),
            ),
        )
        obml, warnings = _convert(doc)
        dims = obml["dimensions"]
        assert dims["amount"]["dataObject"] == "orders"
        assert dims["returns amount"] == {
            "dataObject": "returns",
            "column": "amount",
            "resultType": "float",
        }
        assert dims["returns returns amount"]["column"] == "returns amount"
        assert any("returns returns amount" in w for w in warnings)

    def test_renamed_dimensions_get_distinct_names(self) -> None:
        doc = _doc(
            _dataset(
                "orders",
                _field("amount"),
                _field("revenue"),
                _field("revenue 2", "revenue_2"),
                _field("orders revenue", "orders_revenue"),
            ),
            metrics=[
                {"name": "revenue", "expression": {"sql": "SUM(orders.amount)"}},
                {"name": "revenue 2", "expression": {"sql": "MAX(orders.amount)"}},
            ],
        )
        dims = _dimensions(doc)
        assert {key: dim["column"] for key, dim in dims.items()} == {
            "amount": "amount",
            "orders revenue": "orders revenue",
            "orders revenue 2": "revenue",
            "orders revenue 2 2": "revenue 2",
        }

    def test_converting_twice_gives_the_same_model(self) -> None:
        converter = conv.OSItoOBML(copy.deepcopy(_doc(_ORDERS)))
        first = copy.deepcopy(converter.convert())
        assert converter.convert() == first


class TestRoundTrip:
    def test_an_unmarked_field_comes_back_unmarked(self) -> None:
        doc = _doc(_ORDERS)
        obml, _ = _convert(doc)
        back = conv.OBMLtoOSI(obml).convert()
        assert _markers(back) == {
            ("orders", "order_id"): None,
            ("orders", "amount"): None,
            ("orders", "ordered_on"): None,
            ("orders", "status"): {"is_time": False},
        }
        assert _dimensions(back) == obml["dimensions"]

    def test_a_round_trip_is_stable(self) -> None:
        doc = _doc(_ORDERS, _dataset("stores", _field("amount")))
        obml, _ = _convert(doc)
        once = conv.OBMLtoOSI(obml).convert()
        twice = conv.OBMLtoOSI(_convert(once)[0]).convert()
        assert _markers(twice) == _markers(once)
        assert _dimensions(twice) == obml["dimensions"]

    def test_an_obml_column_without_a_dimension_stays_without_one(self) -> None:
        obml = {
            "version": 1.0,
            "dataObjects": {
                "orders": {
                    "code": "orders",
                    "database": "wh",
                    "schema": "public",
                    "columns": {
                        "region": {"code": "region", "abstractType": "string"},
                        "amount": {"code": "amount", "abstractType": "float"},
                    },
                }
            },
            "dimensions": {"Region": {"dataObject": "orders", "column": "region"}},
            "measures": {
                "Revenue": {
                    "columns": [{"dataObject": "orders", "column": "amount"}],
                    "aggregation": "sum",
                    "resultType": "float",
                }
            },
        }
        back = conv.OBMLtoOSI(copy.deepcopy(obml)).convert()
        assert set(_dimensions(back)) == {"Region"}

    def test_a_dimension_removed_in_obml_stays_removed(self) -> None:
        obml, _ = _convert(_doc(_ORDERS))
        del obml["dimensions"]["amount"]
        back = conv.OBMLtoOSI(obml).convert()
        assert set(_dimensions(back)) == {"order_id", "ordered_on", "status"}
