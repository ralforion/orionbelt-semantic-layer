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
"""An Ossie metric with ``FILTER (WHERE ...)`` imports as a measure.

Apache Ossie's expression language requires the aggregate ``FILTER`` clause and
gives its rewrite for an aggregate that ignores NULL inputs:
``AGG(x) FILTER (WHERE p)`` is ``AGG(CASE WHEN p THEN x END)``. Such a metric
used to be preserved as one OBML could not represent.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

import osi_orionbelt.converter as conv


def _doc(sql: str, **dataset: Any) -> dict[str, Any]:
    fields = [{"name": n, "expression": {"sql": n}} for n in ("id", "amount", "status", "cust")]
    return {
        "version": "0.3.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "orders",
                "source": "wh.public.orders",
                "primary_key": ["id"],
                "fields": fields,
                **dataset,
            }
        ],
        "metrics": [{"name": "m", "expression": {"sql": sql}}],
    }


def _convert(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    converter = conv.OSItoOBML(copy.deepcopy(doc))
    return converter.convert(), converter.warnings


_DONE = "CASE WHEN {[orders].[status]} = 'done' THEN "


class TestImport:
    @pytest.mark.parametrize(
        ("sql", "measure"),
        [
            (
                "SUM(orders.amount) FILTER (WHERE orders.status = 'done')",
                {"expression": _DONE + "{[orders].[amount]} END", "aggregation": "sum"},
            ),
            (
                "sum(orders.amount) filter (where orders.status = 'done')",
                {"expression": _DONE + "{[orders].[amount]} END", "aggregation": "sum"},
            ),
            (
                "COUNT(*) FILTER (WHERE orders.status = 'done')",
                {"expression": _DONE + "1 END", "aggregation": "count"},
            ),
            (
                "COUNT(DISTINCT orders.cust) FILTER (WHERE orders.status = 'done')",
                {
                    "expression": _DONE + "{[orders].[cust]} END",
                    "aggregation": "count",
                    "distinct": True,
                },
            ),
            (
                "AVG(orders.amount) FILTER (WHERE orders.status = 'done')",
                {"expression": _DONE + "{[orders].[amount]} END", "aggregation": "avg"},
            ),
        ],
    )
    def test_a_filtered_aggregate_is_a_measure(self, sql: str, measure: dict) -> None:
        obml, warnings = _convert(_doc(sql))
        assert obml["measures"]["m"] == measure
        assert not any("LOSSY" in w for w in warnings)

    def test_a_filtered_percentile_filters_its_ordering(self) -> None:
        sql = (
            "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY orders.amount)"
            " FILTER (WHERE orders.status = 'done')"
        )
        measure = _convert(_doc(sql))[0]["measures"]["m"]
        assert measure["expression"] == _DONE + "{[orders].[amount]} END"
        assert measure["aggregation"] == "percentile_cont"
        assert measure["percentile"] == 0.5

    def test_a_filtered_aggregate_in_a_formula(self) -> None:
        sql = "SUM(orders.amount) FILTER (WHERE orders.status = 'done') / SUM(orders.amount)"
        obml, _ = _convert(_doc(sql))
        filtered = [
            m for m in obml["measures"].values() if m.get("expression", "").startswith(_DONE)
        ]
        assert filtered == [{"expression": _DONE + "{[orders].[amount]} END", "aggregation": "sum"}]
        assert "m" in obml["metrics"]

    def test_a_dataset_metric_with_a_filter(self) -> None:
        dataset_metric = {
            "name": "done_amount",
            "expression": {"sql": "SUM(amount) FILTER (WHERE status = 'done')"},
        }
        obml, _ = _convert(_doc("AGG(orders.done_amount)", metrics=[dataset_metric]))
        assert obml["measures"]["done_amount"]["expression"] == (_DONE + "{[orders].[amount]} END")

    @pytest.mark.parametrize(
        "sql",
        [
            # A windowed aggregate has no measure form, filtered or not.
            "SUM(orders.amount) FILTER (WHERE orders.status = 'done') OVER ()",
            # Two arguments: no single value for the predicate to gate.
            "LISTAGG(orders.status, ',') FILTER (WHERE orders.amount > 1)",
            # Aggregates not relied on to skip NULLs: Databricks' ANY_VALUE
            # keeps them, so a rejected row's NULL could be the answer.
            "ANY_VALUE(orders.amount) FILTER (WHERE orders.status = 'done')",
            "MODE(orders.amount) FILTER (WHERE orders.status = 'done')",
            # Predicates OBML's expression parser does not read: the measure
            # would stop the whole model loading. OBML's ILIKE takes a string
            # pattern, and its LIKE has no ESCAPE clause.
            "SUM(orders.amount) FILTER (WHERE orders.status ILIKE orders.cust)",
            # A backslash escapes in OBML's ILIKE pattern; Snowflake and ANSI
            # SQL have no default escape character.
            "SUM(orders.amount) FILTER (WHERE orders.status ILIKE 'a\\_b')",
            "SUM(orders.amount) FILTER (WHERE orders.status LIKE 'a!_%' ESCAPE '!')",
            "COUNT(*) FILTER (WHERE orders.status ILIKE orders.cust) / COUNT(orders.id)",
        ],
    )
    def test_a_filter_it_cannot_rewrite_is_preserved(self, sql: str) -> None:
        obml, warnings = _convert(_doc(sql))
        assert "m" not in obml.get("measures", {})
        assert "m" not in obml.get("metrics", {})
        assert any("LOSSY" in w and "'m'" in w for w in warnings)


class TestMeasureExpressions:
    """Any aggregated expression, FILTER or not, is checked against OBML's grammar."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SUM(CASE WHEN orders.status ILIKE orders.cust THEN orders.amount END)",
            "SUM(orders.amount | 7)",
            # SQL syntax inside a call, which OBML reads as NAME(arg, ...) only.
            "SUM(orders.amount) FILTER (WHERE EXTRACT(DAYOFWEEK FROM orders.cust) = 1)",
            # A catalog function with an argument count or unit OBML refuses:
            # sqlglot writes TRIM(BOTH ' ' FROM s) as TRIM(s, ' ').
            "SUM(orders.amount) FILTER (WHERE TRIM(BOTH ' ' FROM orders.status) = 'x')",
            "SUM(CASE WHEN ROUND(orders.amount, 1, 2) > 0 THEN 1 END)",
            "SUM(CASE WHEN DATE_TRUNC('decade', orders.cust) > 0 THEN 1 END)",
            # A clock read with a precision: OBML's current_timestamp() takes none.
            "SUM(orders.amount) FILTER (WHERE orders.cust > CURRENT_TIMESTAMP(3))",
        ],
    )
    def test_one_obml_cannot_parse_is_preserved(self, sql: str) -> None:
        obml, warnings = _convert(_doc(sql))
        assert "m" not in obml.get("measures", {})
        assert any("LOSSY" in w and "'m'" in w for w in warnings)

    @pytest.mark.parametrize(
        "sql",
        [
            "SUM(YEAR(orders.status))",
            "SUM(CASE WHEN REGEXP_LIKE(orders.status, 'x') THEN orders.amount END)",
            "SUM(CASE WHEN orders.status IS NOT NULL AND orders.id NOT IN (1, 2)"
            " THEN orders.amount END)",
            "SUM(orders.amount) FILTER (WHERE orders.status NOT LIKE 'x%'"
            " AND orders.id BETWEEN 1 AND 5)",
        ],
    )
    def test_one_obml_parses_imports(self, sql: str) -> None:
        assert "m" in _convert(_doc(sql))[0]["measures"]

    @pytest.mark.parametrize("dialect", ["SNOWFLAKE", "DATABRICKS", "OSSIE_SQL_2026"])
    def test_an_ilike_pattern_with_a_backslash_is_preserved(self, dialect: str) -> None:
        # Snowflake: ILIKE 'a\\_b' matches a\_b literally; OBML's ILIKE would
        # read the backslash as an escape and match a_b.
        doc = _doc("SUM(orders.amount)")
        sql = "SUM(orders.amount) FILTER (WHERE orders.status ILIKE 'a\\\\_b')"
        doc["metrics"][0]["expression"] = {"dialects": [{"dialect": dialect, "expression": sql}]}
        obml, warnings = _convert(doc)
        assert "m" not in obml.get("measures", {})
        assert any("LOSSY" in w and "'m'" in w for w in warnings)

    @pytest.mark.parametrize(
        ("sql", "expression"),
        [
            (
                "SUM(orders.amount) FILTER (WHERE orders.status NOT ILIKE 'do%')",
                "CASE WHEN {[orders].[status]} NOT ILIKE 'do%' THEN {[orders].[amount]} END",
            ),
            (
                "SUM(orders.amount) FILTER (WHERE orders.status IS DISTINCT FROM orders.cust)",
                "CASE WHEN {[orders].[status]} IS DISTINCT FROM {[orders].[cust]}"
                " THEN {[orders].[amount]} END",
            ),
            (
                "COUNT(*) FILTER (WHERE orders.status IS NOT DISTINCT FROM orders.cust)",
                "CASE WHEN {[orders].[status]} IS NOT DISTINCT FROM {[orders].[cust]} THEN 1 END",
            ),
            ("SUM(orders.amount % 7)", "{[orders].[amount]} % 7"),
            ("SUM(MOD(orders.amount, 7))", "{[orders].[amount]} % 7"),
            (
                "COUNT(DISTINCT orders.status || orders.cust)",
                "{[orders].[status]} || {[orders].[cust]}",
            ),
        ],
    )
    def test_the_operators_obml_parses_since_import(self, sql: str, expression: str) -> None:
        # %, ||, ILIKE with a string pattern and IS [NOT] DISTINCT FROM are in
        # OBML's expression grammar.
        obml, warnings = _convert(_doc(sql))
        assert obml["measures"]["m"]["expression"] == expression
        assert not any("LOSSY" in w for w in warnings)

    @pytest.mark.parametrize(
        ("predicate", "obml"),
        [
            ("EXTRACT(YEAR FROM orders.cust) = 2026", "EXTRACT('year', {[orders].[cust]}) = 2026"),
            ("EXTRACT(QUARTER FROM orders.cust) = 1", "EXTRACT('quarter', {[orders].[cust]}) = 1"),
            ("orders.cust <= CURRENT_DATE", "{[orders].[cust]} <= CURRENT_DATE()"),
            ("orders.cust <= CURRENT_TIMESTAMP", "{[orders].[cust]} <= CURRENT_TIMESTAMP()"),
            ("orders.cust < CURRENT_TIME()", "{[orders].[cust]} < CURRENT_TIME()"),
        ],
    )
    def test_sql_forms_become_obml_functions(self, predicate: str, obml: str) -> None:
        # OBML's extract(unit, x) and its clock functions; a bare CURRENT_DATE
        # is a string to OBML.
        sql = f"SUM(orders.amount) FILTER (WHERE {predicate})"
        measure = _convert(_doc(sql))[0]["measures"]["m"]
        assert measure["expression"] == f"CASE WHEN {obml} THEN {{[orders].[amount]}} END"
