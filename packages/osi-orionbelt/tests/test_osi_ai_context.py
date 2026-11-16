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
"""A model's and a metric's ``ai_context`` survive an OBML round trip.

OBML keeps synonyms natively and has no slot for the rest of an Ossie
``ai_context`` (``instructions``, ``examples``, other keys). Datasets and fields
already kept it; the model and its metrics lost all of it but synonyms.
"""

from __future__ import annotations

import copy
from typing import Any

import osi_orionbelt.converter as conv


def _ai(tag: str) -> dict[str, Any]:
    return {
        "instructions": f"{tag} instructions",
        "synonyms": [f"{tag} synonym"],
        "examples": [f"{tag} example"],
    }


def _doc() -> dict[str, Any]:
    return {
        "version": "0.3.0.dev0",
        "name": "shop",
        "ai_context": _ai("model"),
        "datasets": [
            {
                "name": "orders",
                "source": "wh.public.orders",
                "primary_key": ["id"],
                "fields": [
                    {"name": "id", "expression": {"sql": "id"}},
                    {"name": "amount", "expression": {"sql": "amount"}},
                ],
            }
        ],
        "metrics": [
            {
                "name": "revenue",
                "expression": {"sql": "SUM(orders.amount)"},
                "ai_context": _ai("revenue"),
            },
            {
                "name": "per_order",
                "expression": {"sql": "SUM(orders.amount) / COUNT(orders.id)"},
                "ai_context": "per order, as plain text",
            },
            {"name": "top", "expression": {"sql": "MAX(orders.amount)"}},
        ],
    }


def _round_trip(doc: dict[str, Any]) -> dict[str, Any]:
    return conv.OBMLtoOSI(conv.OSItoOBML(copy.deepcopy(doc)).convert()).convert()


def _contexts(osi: dict[str, Any]) -> dict[str, Any]:
    metrics = {m["name"]: m.get("ai_context") for m in osi.get("metrics", [])}
    return {"model": osi.get("ai_context"), **metrics}


class TestRoundTrip:
    def test_the_model_keeps_its_ai_context(self) -> None:
        assert _round_trip(_doc())["ai_context"] == _ai("model")

    def test_a_metric_keeps_its_ai_context(self) -> None:
        contexts = _contexts(_round_trip(_doc()))
        revenue = contexts["revenue"]
        assert revenue["instructions"] == "revenue instructions"
        assert revenue["examples"] == ["revenue example"]
        assert "revenue synonym" in revenue["synonyms"]
        assert contexts["per_order"] == {"instructions": "per order, as plain text"}

    def test_a_round_trip_is_stable(self) -> None:
        once = _round_trip(_doc())
        assert _contexts(_round_trip(once)) == _contexts(once)
        assert conv.validate_osi(once).valid

    def test_a_metric_without_one_gains_none_but_its_synonyms(self) -> None:
        assert _contexts(_round_trip(_doc()))["top"] == {"synonyms": ["top"]}

    def test_the_export_option_overrides_the_model_instructions(self) -> None:
        obml = conv.OSItoOBML(_doc()).convert()
        osi = conv.OBMLtoOSI(obml, ai_instructions="from the export").convert()
        assert osi["ai_context"] == {**_ai("model"), "instructions": "from the export"}


class TestGeneratedInstructions:
    """A cumulative metric's export writes instructions of its own."""

    @staticmethod
    def _obml() -> dict[str, Any]:
        doc = _doc()
        obml = conv.OSItoOBML(doc).convert()
        obml["dataObjects"]["orders"]["columns"]["day"] = {"code": "day", "abstractType": "date"}
        obml["dimensions"]["day"] = {"dataObject": "orders", "column": "day", "resultType": "date"}
        running = {"type": "cumulative", "measure": "revenue", "timeDimension": "day"}
        running["customExtensions"] = [
            {"vendor": "OSI", "data": '{"obml_ai_context": {"instructions": "mine"}}'}
        ]
        obml["metrics"]["running"] = running
        return obml

    def test_they_follow_the_saved_ones(self) -> None:
        osi = conv.OBMLtoOSI(self._obml()).convert()
        text = _contexts(osi)["running"]["instructions"]
        assert text.startswith("mine\n\nRight only in a query grouped by exactly 'day'")

    def test_they_are_not_saved_again(self) -> None:
        once = conv.OBMLtoOSI(self._obml()).convert()
        assert _contexts(_round_trip(once))["running"] == _contexts(once)["running"]
