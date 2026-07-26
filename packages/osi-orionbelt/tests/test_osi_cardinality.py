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
"""Relationship cardinality inference from primary_key / unique_keys.

The OSI logical schema has no cardinality field, so the converter used to emit
`many-to-one` for every relationship. It now infers the OBML join cardinality by
comparing `to_columns` against the `to` dataset's keys and `from_columns` against
the `from` dataset's - falling back to `many-to-one` (OSI's declared from=many /
to=one direction) only when the target declares no keys.
"""

from __future__ import annotations

from typing import Any

import osi_orionbelt.converter as conv


def _field(name: str) -> dict[str, Any]:
    return {"name": name, "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]}}


def _model(
    *,
    from_columns: list[str],
    to_columns: list[str],
    to_primary_key: list[str] | None = None,
    to_unique_keys: list[list[str]] | None = None,
    from_primary_key: list[str] | None = None,
) -> dict[str, Any]:
    orders: dict[str, Any] = {
        "name": "Orders",
        "source": "A.P.ORDERS",
        "fields": [_field(c) for c in ("customer_id", "id")],
    }
    customers: dict[str, Any] = {
        "name": "Customers",
        "source": "A.P.CUSTOMERS",
        "fields": [_field(c) for c in ("id", "region_id")],
    }
    if to_primary_key:
        customers["primary_key"] = to_primary_key
    if to_unique_keys:
        customers["unique_keys"] = to_unique_keys
    if from_primary_key:
        orders["primary_key"] = from_primary_key
    return {
        "version": "0.2.0.dev0",
        "semantic_model": [
            {
                "name": "sales",
                "datasets": [orders, customers],
                "relationships": [
                    {
                        "name": "orders_customers",
                        "from": "Orders",
                        "to": "Customers",
                        "from_columns": from_columns,
                        "to_columns": to_columns,
                    }
                ],
            }
        ],
    }


def _join_type(osi: dict[str, Any]) -> tuple[str | None, list[str]]:
    c = conv.OSItoOBML(osi)
    obml = c.convert()
    for do in obml["dataObjects"].values():
        for join in do.get("joins", []):
            return join["joinType"], c.warnings
    return None, c.warnings


class TestCardinalityInference:
    def test_fk_to_primary_key_is_many_to_one(self) -> None:
        jt, warnings = _join_type(
            _model(from_columns=["customer_id"], to_columns=["id"], to_primary_key=["id"])
        )
        assert jt == "many-to-one"
        assert not any("defaulting" in w for w in warnings)

    def test_fk_to_unique_key_is_many_to_one(self) -> None:
        jt, _ = _join_type(
            _model(from_columns=["customer_id"], to_columns=["id"], to_unique_keys=[["id"]])
        )
        assert jt == "many-to-one"

    def test_both_sides_unique_is_one_to_one(self) -> None:
        jt, _ = _join_type(
            _model(
                from_columns=["customer_id"],
                to_columns=["id"],
                to_primary_key=["id"],
                from_primary_key=["customer_id"],
            )
        )
        assert jt == "one-to-one"

    def test_target_has_keys_but_fk_is_not_one_is_many_to_many(self) -> None:
        # Customers has a PK on `id`, but the FK points at `region_id` (not a
        # unique key) -> a source row can match many targets -> fan-out.
        jt, _ = _join_type(
            _model(from_columns=["customer_id"], to_columns=["region_id"], to_primary_key=["id"])
        )
        assert jt == "many-to-many"

    def test_keyless_defaults_to_many_to_one_with_warning(self) -> None:
        jt, warnings = _join_type(_model(from_columns=["customer_id"], to_columns=["id"]))
        assert jt == "many-to-one"
        assert any("defaulting to many-to-one" in w for w in warnings)

    def test_composite_key_superset_is_unique(self) -> None:
        # to_columns is a superset of the composite PK -> still unique.
        jt, _ = _join_type(
            _model(
                from_columns=["customer_id"],
                to_columns=["id", "region_id"],
                to_primary_key=["id", "region_id"],
            )
        )
        assert jt == "many-to-one"

    def test_case_insensitive_key_match(self) -> None:
        jt, _ = _join_type(
            _model(from_columns=["customer_id"], to_columns=["ID"], to_primary_key=["id"])
        )
        assert jt == "many-to-one"
