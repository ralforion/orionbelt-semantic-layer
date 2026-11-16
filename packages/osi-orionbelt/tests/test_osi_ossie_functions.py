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
"""Functions of Apache Ossie's expression language, as OBML's catalog has them.

OBML's catalog has each of these under another name or form. Imported as
written, ``DATEADD`` and ``DATEDIFF`` were kept aside (sqlglot read the bare
date part as a column), and the others were passed through as written, to run
only on an engine that happens to have them.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from sqlglot import expressions as exp

import osi_orionbelt.converter as conv
from osi_orionbelt._ossie_parse import parse_sql
from osi_orionbelt._ossie_sql import subset_violations

_FIELDS = ("id", "amount", "status", "opened", "closed")


def _doc(sql: str, dialect: str | None = None, **dataset: Any) -> dict[str, Any]:
    expression = (
        {"sql": sql} if dialect is None else {"dialects": [{"dialect": dialect, "expression": sql}]}
    )
    return {
        "version": "0.3.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "orders",
                "source": "wh.public.orders",
                "primary_key": ["id"],
                "fields": [{"name": n, "expression": {"sql": n}} for n in _FIELDS],
                **dataset,
            }
        ],
        "metrics": [{"name": "m", "expression": expression}],
    }


def _convert(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    converter = conv.OSItoOBML(copy.deepcopy(doc))
    return converter.convert(), converter.warnings


_O, _C, _S, _A = (
    "{[orders].[opened]}",
    "{[orders].[closed]}",
    "{[orders].[status]}",
    "{[orders].[amount]}",
)
_DAY_OF_YEAR = f"(DATE_DIFF('day', DATE_TRUNC('YEAR', {_O}), {_O}) + 1)"

CASES = [
    ("SUM(DATEDIFF(day, orders.opened, orders.closed))", f"DATE_DIFF('day', {_O}, {_C})"),
    ("SUM(DATEDIFF('month', orders.opened, orders.closed))", f"DATE_DIFF('month', {_O}, {_C})"),
    ("MAX(DATEADD(month, -1, orders.opened))", f"DATE_ADD('month', -1, {_O})"),
    ("SUM(YEAR(orders.opened))", f"EXTRACT('year', {_O})"),
    ("SUM(DAY(orders.opened))", f"EXTRACT('day', {_O})"),
    ("SUM(DATE_PART('quarter', orders.opened))", f"EXTRACT('quarter', {_O})"),
    ("SUM(EXTRACT(WEEK FROM orders.opened))", f"EXTRACT('week', {_O})"),
    ("SUM(EXTRACT(DAYOFWEEKISO FROM orders.opened))", f"EXTRACT('day_of_week', {_O})"),
    ("SUM(DAYOFYEAR(orders.opened))", _DAY_OF_YEAR),
    ("SUM(EXTRACT(DAYOFYEAR FROM orders.opened))", _DAY_OF_YEAR),
    ("SUM(LENGTH(TRIM(BOTH ' ' FROM orders.status)))", f"LENGTH(TRIM({_S}))"),
    ("SUM(LENGTH(TRIM(LEADING FROM orders.status)))", f"LENGTH(LTRIM({_S}))"),
    ("SUM(IFF(orders.amount > 0, 1, 0))", f"CASE WHEN {_A} > 0 THEN 1 ELSE 0 END"),
    ("SUM(NVL2(orders.amount, 1, 0))", f"CASE WHEN NOT {_A} IS NULL THEN 1 ELSE 0 END"),
    ("SUM(ZEROIFNULL(orders.amount))", f"COALESCE({_A}, 0)"),
    ("SUM(NULLIFZERO(orders.amount))", f"NULLIF({_A}, 0)"),
    ("SUM(POSITION('a' IN orders.status))", f"POSITION('a', {_S})"),
    ("SUM(CHARINDEX('a', orders.status))", f"POSITION('a', {_S})"),
    (
        "COUNT(CASE WHEN CONTAINS(orders.status, 'a') THEN 1 END)",
        f"CASE WHEN (POSITION('a', {_S}) > 0) THEN 1 END",
    ),
    ("MAX(LEFT(orders.status, 2))", f"SUBSTRING({_S}, 1, 2)"),
    (
        "MAX(RIGHT(orders.status, orders.id + 1))",
        f"SUBSTRING({_S}, GREATEST(LENGTH({_S}) - ({{[orders].[id]}} + 1) + 1, 1))",
    ),
    ("SUM(PI() * orders.amount)", f"3.141592653589793 * {_A}"),
    ("SUM(DEGREES(orders.amount))", f"({_A} * 180 / 3.141592653589793)"),
    ("SUM(RADIANS(orders.amount + 1))", f"(({_A} + 1) * 3.141592653589793 / 180)"),
]


class TestImport:
    @pytest.mark.parametrize(("sql", "expression"), CASES)
    def test_a_function_imports_as_obml_s(self, sql: str, expression: str) -> None:
        obml, warnings = _convert(_doc(sql))
        assert obml["measures"]["m"]["expression"] == expression
        assert not any("LOSSY" in w for w in warnings)

    @pytest.mark.parametrize("dialect", ["SNOWFLAKE", "DATABRICKS"])
    def test_a_vendor_dateadd(self, dialect: str) -> None:
        obml, _ = _convert(_doc("MAX(DATEADD(day, 7, orders.opened))", dialect))
        assert obml["measures"]["m"]["expression"] == f"DATE_ADD('day', 7, {_O})"

    def test_snowflake_datediff_counts_boundaries_as_obml_s_does(self) -> None:
        sql = "SUM(DATEDIFF(day, orders.opened, orders.closed))"
        obml, warnings = _convert(_doc(sql, "SNOWFLAKE"))
        assert obml["measures"]["m"]["expression"] == f"DATE_DIFF('day', {_O}, {_C})"
        assert not any("RECOUNTED" in w for w in warnings)

    def test_databricks_two_argument_datediff_is_the_days_between(self) -> None:
        sql = "SUM(DATEDIFF(orders.closed, orders.opened))"
        obml, warnings = _convert(_doc(sql, "DATABRICKS"))
        assert obml["measures"]["m"]["expression"] == f"DATE_DIFF('day', {_O}, {_C})"
        assert not any("RECOUNTED" in w for w in warnings)

    @pytest.mark.parametrize(
        ("sql", "unit"),
        [
            ("SUM(DATEDIFF(DAY, orders.opened, orders.closed))", "day"),
            ("SUM(DATEDIFF(MONTH, orders.opened, orders.closed))", "month"),
            ("SUM(TIMESTAMPDIFF(HOUR, orders.opened, orders.closed))", "hour"),
        ],
    )
    def test_a_databricks_whole_unit_count_is_obml_s_and_warned(self, sql: str, unit: str) -> None:
        # Databricks counts the units elapsed; OBML's date_diff, one meaning on
        # every engine, the boundaries crossed. The import says the numbers change.
        obml, warnings = _convert(_doc(sql, "DATABRICKS"))
        assert obml["measures"]["m"]["expression"] == f"DATE_DIFF('{unit}', {_O}, {_C})"
        assert any(
            w.startswith("RECOUNTED: OSI metric 'm'") and f"whole {unit}s" in w for w in warnings
        )

    def test_a_field_with_one_is_warned_too(self) -> None:
        doc = _doc("SUM(orders.amount)")
        doc["datasets"][0]["fields"].append(
            {
                "name": "age",
                "expression": {
                    "dialects": [
                        {"dialect": "DATABRICKS", "expression": "DATEDIFF(DAY, opened, closed)"}
                    ]
                },
            }
        )
        obml, warnings = _convert(doc)
        column = obml["dataObjects"]["orders"]["columns"]["age"]
        assert column["expression"] == "DATE_DIFF('day', {opened}, {closed})"
        assert any(w.startswith("RECOUNTED: field 'orders.age'") for w in warnings)

    @pytest.mark.parametrize(
        "sql",
        [
            # Numbered from Sunday on some engines, from Monday on others.
            "SUM(EXTRACT(DAYOFWEEK FROM orders.opened))",
            "SUM(DATEADD(millisecond, 1, orders.opened))",
            "SUM(LENGTH(TRIM(BOTH 'x' FROM orders.status)))",
        ],
    )
    def test_a_form_obml_has_no_call_for_is_kept_aside(self, sql: str) -> None:
        obml, warnings = _convert(_doc(sql))
        assert "m" not in obml.get("measures", {})
        assert any("LOSSY" in w and "'m'" in w for w in warnings)

    def test_a_dataset_metric_does_not_read_the_date_part_as_a_field(self) -> None:
        # Qualified with its dataset, the bare ``day`` was ``orders.day``.
        late = {"name": "late", "expression": {"sql": "SUM(DATEDIFF(day, opened, closed))"}}
        obml, warnings = _convert(_doc("AGG(orders.late)", metrics=[late]))
        assert obml["measures"]["late"]["expression"] == f"DATE_DIFF('day', {_O}, {_C})"
        assert not any("LOSSY" in w for w in warnings)

    @pytest.mark.parametrize(
        ("sql", "expression"),
        [
            ("YEAR(opened)", "EXTRACT('year', {opened})"),
            ("DATEDIFF(day, opened, closed)", "DATE_DIFF('day', {opened}, {closed})"),
            ("CURRENT_DATE", "CURRENT_DATE()"),
        ],
    )
    def test_a_field_computed_by_one(self, sql: str, expression: str) -> None:
        doc = _doc("SUM(orders.amount)")
        doc["datasets"][0]["fields"].append({"name": "f", "expression": {"sql": sql}})
        obml, _ = _convert(doc)
        assert obml["dataObjects"]["orders"]["columns"]["f"]["expression"] == expression


class TestParse:
    def test_the_date_part_is_a_keyword_and_written_back_as_read(self) -> None:
        tree = parse_sql("DATEDIFF(day, t.a, t.b) + DATEADD('month', 1, t.c)", None)
        assert not any(c.name.lower() in ("day", "month") for c in tree.find_all(exp.Column))
        assert tree.sql() == "DATEDIFF(DAY, t.a, t.b) + DATEADD(MONTH, 1, t.c)"
        assert parse_sql(tree.sql(), None) == tree

    def test_a_string_is_not_a_call(self) -> None:
        assert parse_sql("'DATEDIFF(day, a, b)'", None).sql() == "'DATEDIFF(day, a, b)'"


class TestExport:
    @pytest.mark.parametrize(("sql", "_"), CASES)
    def test_the_ossie_form_reads_back_as_the_same_measure(self, sql: str, _: str) -> None:
        imported, _warnings = _convert(_doc(sql))
        osi = conv.OBMLtoOSI(copy.deepcopy(imported)).convert()
        metric = next(m for m in osi["metrics"] if m["name"] == "m")
        ossie = {d["dialect"]: d["expression"] for d in metric["expression"]["dialects"]}[
            "OSSIE_SQL_2026"
        ]
        assert subset_violations(ossie) == []
        # Another tool reads the Ossie form alone, without OBML's extensions.
        again, _warnings = _convert(_doc(ossie))
        assert again["measures"]["m"] == imported["measures"]["m"]

    def test_the_ossie_form_uses_the_language_s_names(self) -> None:
        sql = (
            "SUM(DATEDIFF(day, orders.opened, DATEADD(day, 7, orders.closed))"
            " + YEAR(orders.opened) + CHARINDEX('a', orders.status))"
        )
        imported, warnings = _convert(_doc(sql))
        assert not any("LOSSY" in w for w in warnings)
        osi = conv.OBMLtoOSI(copy.deepcopy(imported)).convert()
        metric = next(m for m in osi["metrics"] if m["name"] == "m")
        ossie = {d["dialect"]: d["expression"] for d in metric["expression"]["dialects"]}[
            "OSSIE_SQL_2026"
        ]
        assert ossie == (
            "SUM(DATEDIFF('day', orders.opened, DATEADD('day', 7, orders.closed))"
            " + DATE_PART('year', orders.opened) + CHARINDEX('a', orders.status))"
        )
