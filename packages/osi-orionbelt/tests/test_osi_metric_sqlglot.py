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
"""sqlglot-based metric decomposition.

The regex decomposer could only handle a bare ``AGG(col)``, ``AGG(expr)``, or a
flat ``AGG(...) op AGG(...)``; anything with an aggregate *nested inside* a
larger expression (``ROUND(SUM(...))``, ``CASE WHEN SUM(...)``) fell through to
preserve-verbatim + LOSSY. The sqlglot decomposer parses the real AST, so those
now decompose into auto-measures + a metric formula.
"""

from __future__ import annotations

from typing import Any

import osi_orionbelt.converter as conv


def _model(expression: str, dialect: str = "ANSI_SQL") -> dict[str, Any]:
    return {
        "version": "0.2.0.dev0",
        "semantic_model": [
            {
                "name": "sales",
                "datasets": [
                    {
                        "name": "Orders",
                        "source": "A.P.ORDERS",
                        "fields": [
                            {
                                "name": c,
                                "expression": {
                                    "dialects": [{"dialect": "ANSI_SQL", "expression": c}]
                                },
                            }
                            for c in ("amount", "tax", "id", "region")
                        ],
                    }
                ],
                "metrics": [
                    {
                        "name": "M",
                        "expression": {
                            "dialects": [{"dialect": dialect, "expression": expression}]
                        },
                    }
                ],
            }
        ],
    }


def _convert(expression: str, dialect: str = "ANSI_SQL") -> tuple[dict[str, Any], list[str]]:
    c = conv.OSItoOBML(_model(expression, dialect))
    obml = c.convert()
    return obml, [w for w in c.warnings if w.startswith("LOSSY")]


class TestNewlyDecomposable:
    """Aggregates nested in a larger expression now decompose (were LOSSY)."""

    def test_round_of_sum(self) -> None:
        obml, lossy = _convert("ROUND(SUM(Orders.amount), 2)")
        assert not lossy
        (measure,) = obml["measures"].values()
        assert measure["aggregation"] == "sum"
        assert obml["metrics"]["M"]["expression"] == "ROUND({[_Orders_amount_sum]}, 2)"

    def test_case_when_over_aggregates(self) -> None:
        obml, lossy = _convert("CASE WHEN SUM(Orders.amount) > 0 THEN SUM(Orders.tax) ELSE 0 END")
        assert not lossy
        assert set(obml["measures"]) == {"_Orders_amount_sum", "_Orders_tax_sum"}
        expr = obml["metrics"]["M"]["expression"]
        assert "{[_Orders_amount_sum]}" in expr and "{[_Orders_tax_sum]}" in expr

    def test_string_literal_with_dot_is_not_a_ref(self) -> None:
        # 'north.1' is a string literal, not an Orders.<col> reference.
        obml, lossy = _convert(
            "SUM(CASE WHEN Orders.region = 'north.1' THEN Orders.amount ELSE 0 END)"
        )
        assert not lossy
        (measure,) = obml["measures"].values()
        assert "{[Orders].[region]}" in measure["expression"]
        assert "{[north]" not in measure["expression"]


class TestDecomposedCountIsInt:
    """Regression: the decompose path hard-coded every leaf to float."""

    def test_count_leaf_result_type_is_int(self) -> None:
        obml, _ = _convert("SUM(Orders.amount) / COUNT(DISTINCT Orders.id)")
        assert obml["measures"]["_Orders_amount_sum"]["resultType"] == "float"
        count = obml["measures"]["_Orders_id_count_distinct"]
        assert count["resultType"] == "int"
        assert count["aggregation"] == "count"
        assert count["distinct"] is True


class TestStillPreserved:
    """No aggregate, or unparseable -> preserved verbatim + LOSSY (unchanged)."""

    def test_no_aggregate_is_preserved(self) -> None:
        _, lossy = _convert("Orders.amount + Orders.tax")
        assert lossy

    def test_nested_aggregate_is_preserved(self) -> None:
        # SUM(COUNT(...)) has no measure+metric representation.
        _, lossy = _convert("SUM(COUNT(Orders.id))")
        assert lossy
