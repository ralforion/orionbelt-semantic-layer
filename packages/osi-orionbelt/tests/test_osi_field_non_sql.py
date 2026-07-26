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
"""A field's column `code` must never be a non-SQL expression.

An OSI field can carry expressions in non-SQL dialects (MDX, TABLEAU, MAQL). The
converter used to fall back to the first dialect's expression, so an MDX-only
field wrote its MDX text into the OBML column `code` - a physical SQL column
reference - producing broken SQL silently. The code is now taken only from a SQL
dialect (ANSI_SQL preferred, then any other SQL dialect); a field with only
non-SQL expressions falls back to the field name with a warning.
"""

from __future__ import annotations

from typing import Any

import osi_orionbelt.converter as conv


def _dialect(dialect: str, expression: str) -> dict[str, Any]:
    return {"dialect": dialect, "expression": expression}


def _convert_field(name: str, dialects: list[dict[str, Any]]) -> tuple[str, list[str]]:
    osi = {
        "version": "0.2.0.dev0",
        "semantic_model": [
            {
                "name": "s",
                "datasets": [
                    {
                        "name": "Orders",
                        "source": "A.P.ORDERS",
                        "fields": [{"name": name, "expression": {"dialects": dialects}}],
                    }
                ],
            }
        ],
    }
    c = conv.OSItoOBML(osi)
    obml = c.convert()
    (column,) = obml["dataObjects"]["Orders"]["columns"].values()
    return column["code"], c.warnings


class TestSqlDialectCode:
    def test_ansi_sql_expression_is_used(self) -> None:
        code, warnings = _convert_field("amt", [_dialect("ANSI_SQL", "amount")])
        assert code == "amount"
        assert not any("non-SQL" in w for w in warnings)

    def test_ansi_preferred_over_non_sql(self) -> None:
        code, warnings = _convert_field(
            "amt", [_dialect("MDX", "[Measures].[X]"), _dialect("ANSI_SQL", "amount")]
        )
        assert code == "amount"
        assert not any("non-SQL" in w for w in warnings)

    def test_other_sql_dialects_are_used(self) -> None:
        for dialect in ("SNOWFLAKE", "DATABRICKS", "BIGQUERY"):
            code, warnings = _convert_field("amt", [_dialect(dialect, "amount")])
            assert code == "amount", dialect
            assert not any("non-SQL" in w for w in warnings), dialect


class TestNonSqlDialectFallback:
    def test_mdx_only_falls_back_to_name_with_warning(self) -> None:
        code, warnings = _convert_field("sales", [_dialect("MDX", "[Measures].[Sales]")])
        # The MDX text must NOT become the physical column code.
        assert code == "sales"
        assert "[Measures]" not in code
        assert any("non-SQL" in w and "sales" in w for w in warnings)

    def test_tableau_and_maql_only_fall_back(self) -> None:
        for dialect in ("TABLEAU", "MAQL"):
            code, warnings = _convert_field("calc", [_dialect(dialect, "SUM([Sales])")])
            assert code == "calc", dialect
            assert any("non-SQL" in w for w in warnings), dialect

    def test_first_sql_dialect_wins_over_non_sql(self) -> None:
        # A field with a leading non-SQL dialect and a trailing SQL one still
        # uses the SQL expression, not the field name.
        code, warnings = _convert_field(
            "amt", [_dialect("TABLEAU", "SUM([Amt])"), _dialect("SNOWFLAKE", "amount")]
        )
        assert code == "amount"
        assert not any("non-SQL" in w for w in warnings)
