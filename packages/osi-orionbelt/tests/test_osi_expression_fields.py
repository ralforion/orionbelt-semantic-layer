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
"""An Ossie field computed by an expression is an OBML computed column.

OBML quotes a column's ``code`` as one name, so ``amount_cents / 100.0`` written
into ``code`` compiled to ``"orders"."amount_cents / 100.0"``, which no table
has. The import now writes ``{amount_cents} / 100.0`` over a column whose code is
``amount_cents``, and the export writes the field's SQL back.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import osi_orionbelt.converter as conv


def _doc(*fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "0.3.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "orders",
                "source": "wh.public.orders",
                "primary_key": ["order_id"],
                "fields": [{"name": "order_id", "expression": {"sql": "order_id"}}, *fields],
            }
        ],
        "metrics": [{"name": "revenue", "expression": {"sql": "SUM(orders.amount)"}}],
    }


def _field(name: str, sql: str) -> dict[str, Any]:
    return {"name": name, "expression": {"sql": sql}}


def _columns(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    converter = conv.OSItoOBML(copy.deepcopy(doc))
    obml = converter.convert()
    return obml["dataObjects"]["orders"]["columns"], converter.warnings


def _export(obml: dict[str, Any]) -> dict[str, str]:
    back = conv.OBMLtoOSI(obml).convert()
    orders = next(ds for ds in back["datasets"] if ds["name"] == "orders")
    return {f["name"]: f["expression"]["dialects"][0]["expression"] for f in orders["fields"]}


def _is_added(column: dict[str, Any]) -> bool:
    return any(
        json.loads(e["data"]).get("obml_source_column") for e in column.get("customExtensions", [])
    )


class TestImport:
    def test_an_expression_reads_an_added_source_column(self) -> None:
        columns, warnings = _columns(_doc(_field("amount", "amount_cents / 100.0")))
        assert columns["amount"]["expression"] == "{amount_cents} / 100.0"
        assert "code" not in columns["amount"]
        assert columns["amount_cents"]["code"] == "amount_cents"
        assert _is_added(columns["amount_cents"])
        assert any("added column 'amount_cents'" in w for w in warnings)

    def test_a_field_computing_the_column_as_is_is_reused(self) -> None:
        columns, _ = _columns(
            _doc(_field("cents", "amount_cents"), _field("amount", "amount_cents / 100.0"))
        )
        assert columns["amount"]["expression"] == "{cents} / 100.0"
        assert "amount_cents" not in columns

    def test_a_quoted_source_column(self) -> None:
        columns, _ = _columns(_doc(_field("amount", '"Order Total" * 2')))
        assert columns["amount"]["expression"] == "{Order Total} * 2"
        assert columns["Order Total"]["code"] == "Order Total"

    def test_an_added_column_does_not_take_a_field_name(self) -> None:
        # The field called amount_cents computes raw_cents, not the column.
        columns, _ = _columns(
            _doc(_field("amount_cents", "raw_cents"), _field("amount", "amount_cents / 100.0"))
        )
        assert columns["amount_cents"]["code"] == "raw_cents"
        assert columns["amount"]["expression"] == "{amount_cents_2} / 100.0"
        assert columns["amount_cents_2"]["code"] == "amount_cents"

    def test_case_and_cast(self) -> None:
        sql = "CASE WHEN status = 'paid' THEN CAST(amount_cents AS DOUBLE) / 100 ELSE 0 END"
        columns, _ = _columns(_doc(_field("amount", sql)))
        assert columns["amount"]["expression"] == (
            "CASE WHEN {status} = 'paid' THEN CAST({amount_cents}, 'double') / 100 ELSE 0 END"
        )

    def test_what_obml_cannot_parse_is_kept_and_reported(self) -> None:
        columns, warnings = _columns(_doc(_field("amount", "first_name || ' ' || last_name")))
        assert columns["amount"]["code"] == "first_name || ' ' || last_name"
        assert any("LOSSY" in w and "'orders.amount'" in w for w in warnings)


class TestExport:
    def test_the_field_sql_comes_back_and_added_columns_stay_out(self) -> None:
        doc = _doc(_field("amount", "amount_cents / 100.0"))
        obml = conv.OSItoOBML(copy.deepcopy(doc)).convert()
        fields = _export(obml)
        assert fields["amount"] == "amount_cents / 100.0"
        assert "amount_cents" not in fields

    def test_an_edited_expression_is_written_from_its_siblings(self) -> None:
        doc = _doc(_field("amount", "CAST(amount_cents AS DOUBLE) / 100"))
        obml = conv.OSItoOBML(copy.deepcopy(doc)).convert()
        columns = obml["dataObjects"]["orders"]["columns"]
        columns["amount"]["expression"] = "cast({amount_cents}, 'double') / 1000"
        assert _export(obml)["amount"] == "CAST(amount_cents AS DOUBLE) / 1000"

    def test_an_added_column_the_model_uses_is_exported(self) -> None:
        doc = _doc(_field("amount", "amount_cents / 100.0"))
        obml = conv.OSItoOBML(copy.deepcopy(doc)).convert()
        obml.setdefault("dimensions", {})["Cents"] = {
            "dataObject": "orders",
            "column": "amount_cents",
            "resultType": "int",
        }
        assert _export(obml)["amount_cents"] == "amount_cents"

    def test_a_round_trip_is_unchanged(self) -> None:
        doc = _doc(_field("amount", "amount_cents / 100.0"))
        once = conv.OBMLtoOSI(conv.OSItoOBML(copy.deepcopy(doc)).convert()).convert()
        twice = conv.OBMLtoOSI(conv.OSItoOBML(copy.deepcopy(once)).convert()).convert()
        assert once["datasets"] == twice["datasets"]


class TestReview:
    def test_scientific_notation_is_written_as_obml_reads_numbers(self) -> None:
        # OBML reads 1e2 as "1" then "e2", which refused the whole model.
        columns, _ = _columns(_doc(_field("amount", "amount_cents / 1e2")))
        assert columns["amount"]["expression"] == "{amount_cents} / 100"

    def test_text_obml_cannot_tokenize_is_refused(self) -> None:
        from osi_orionbelt._field_expressions import obml_unreadable_text

        assert obml_unreadable_text("{a} / 1e2") is not None
        assert obml_unreadable_text("{a} | {b}") is not None
        assert obml_unreadable_text("{a} + LENGTH('x | 1e2')") is None

    def test_an_edited_sibling_code_is_written_from_the_sibling(self) -> None:
        doc = _doc(_field("amount", "amount_cents / 100.0"))
        obml = conv.OSItoOBML(copy.deepcopy(doc)).convert()
        obml["dataObjects"]["orders"]["columns"]["amount_cents"]["code"] = "new_cents"
        assert _export(obml)["amount"] == "new_cents / 100.0"

    def test_braces_in_a_string_literal_are_text(self) -> None:
        obml = conv.OSItoOBML(copy.deepcopy(_doc())).convert()
        columns = obml["dataObjects"]["orders"]["columns"]
        columns["amount_cents"] = {"code": "amount_cents", "abstractType": "int"}
        columns["mixed"] = {
            "expression": "{amount_cents} + LENGTH('{amount_cents}')",
            "abstractType": "int",
        }
        assert _export(obml)["mixed"] == "amount_cents + LENGTH('{amount_cents}')"

    def test_an_added_column_used_as_a_join_key_is_exported(self) -> None:
        doc = _doc(_field("amount", "amount_cents / 100.0"))
        doc["datasets"].append(
            {
                "name": "bands",
                "source": "wh.public.bands",
                "primary_key": ["cents"],
                "fields": [_field("cents", "cents")],
            }
        )
        obml = conv.OSItoOBML(copy.deepcopy(doc)).convert()
        obml["dataObjects"]["orders"]["joins"] = [
            {
                "joinType": "many-to-one",
                "joinTo": "bands",
                "columnsFrom": ["amount_cents"],
                "columnsTo": ["cents"],
            }
        ]
        back = conv.OBMLtoOSI(obml).convert()
        assert "amount_cents" in _export(obml)
        assert conv.validate_osi(back).valid

    def test_a_source_name_no_placeholder_can_hold_gets_one_that_can(self) -> None:
        # {$amount} is not a sibling to OBML, which compiled it as the text '$amount'.
        columns, _ = _columns(_doc(_field("amount", '"$amount" * 2')))
        assert columns["amount"]["expression"] == "{source__amount} * 2"
        assert columns["source__amount"]["code"] == "$amount"
