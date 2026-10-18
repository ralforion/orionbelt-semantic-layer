"""Exported metrics also carry ``OSSIE_SQL_2026`` where it has a form.

Each Ossie metric keeps its ``ANSI_SQL`` expression and gains one in Apache
Ossie's own expression language, rendered in that language's forms and checked
node by node against the subset ``core-spec/expression_language.md`` defines.
A metric with no such form keeps ``ANSI_SQL`` only.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

import osi_orionbelt.converter as conv
from osi_orionbelt._ossie_sql import _FUNCTIONS, subset_violations


def _filter(operator: str, value: Any, data_type: str = "string") -> dict[str, Any]:
    key = {"string": "valueString", "timestamp": "valueDate"}[data_type]
    return {
        "column": {"dataObject": "Orders", "column": "Status"},
        "operator": operator,
        "values": [{"dataType": data_type, key: value}],
    }


def _measure(aggregation: str = "sum", **extra: Any) -> dict[str, Any]:
    return {
        "columns": [{"dataObject": "Orders", "column": "Amount"}],
        "resultType": "float",
        "aggregation": aggregation,
        **extra,
    }


_OBML: dict[str, Any] = {
    "version": 1.0,
    "dataObjects": {
        "Orders": {
            "code": "orders",
            "columns": {
                "Amount": {"code": "amount", "abstractType": "float"},
                "Status": {"code": "status", "abstractType": "string"},
            },
        }
    },
    "measures": {
        "Revenue": _measure(),
        "Discounted": _measure(filters=[_filter("contains", "50%")]),
        "Prefixed": _measure(filters=[_filter("starts_with", "web_")]),
        "Not Void": _measure(filters=[_filter("notcontains", "void")]),
        "Pattern": _measure(filters=[_filter("ilike", "VOID%")]),
        "Escaped": _measure(filters=[_filter("ilike", "VOID\\_%")]),
        "Since": _measure(filters=[_filter("gte", "2024-01-01 00:00:00", "timestamp")]),
        "Typical": _measure("mode"),
        "Hashed": {
            "expression": "MD5({[Orders].[Status]})",
            "resultType": "int",
            "aggregation": "count",
        },
    },
    "metrics": {"Share": {"expression": "{[Discounted]} / {[Revenue]} * 100"}},
}


def _dialects() -> dict[str, dict[str, str]]:
    osi = conv.OBMLtoOSI(copy.deepcopy(_OBML), "sales").convert()
    return {
        m["name"]: {d["dialect"]: d["expression"] for d in m["expression"]["dialects"]}
        for m in osi["metrics"]
    }


class TestBothDialects:
    def test_plain_measure_carries_both(self) -> None:
        revenue = _dialects()["Revenue"]
        assert list(revenue) == ["ANSI_SQL", "OSSIE_SQL_2026"]
        assert revenue["OSSIE_SQL_2026"] == revenue["ANSI_SQL"] == "SUM(Orders.Amount)"

    def test_substring_filters_use_the_language_functions(self) -> None:
        d = _dialects()
        assert "ESCAPE" in d["Discounted"]["ANSI_SQL"]
        assert d["Discounted"]["OSSIE_SQL_2026"] == (
            "SUM(CASE WHEN CONTAINS(Orders.Status, '50%') THEN Orders.Amount END)"
        )
        assert "STARTSWITH(Orders.Status, 'web_')" in d["Prefixed"]["OSSIE_SQL_2026"]
        assert "NOT CONTAINS(Orders.Status, 'void')" in d["Not Void"]["OSSIE_SQL_2026"]

    def test_ilike_is_native(self) -> None:
        d = _dialects()["Pattern"]
        assert "LOWER(" in d["ANSI_SQL"]
        assert "Orders.Status ILIKE 'VOID%'" in d["OSSIE_SQL_2026"]

    def test_timestamp_literal_is_a_cast(self) -> None:
        d = _dialects()["Since"]
        assert "TIMESTAMP '2024-01-01 00:00:00'" in d["ANSI_SQL"]
        assert "CAST('2024-01-01 00:00:00' AS TIMESTAMP)" in d["OSSIE_SQL_2026"]

    def test_metric_over_measures_carries_both(self) -> None:
        share = _dialects()["Share"]
        assert share["OSSIE_SQL_2026"] == (
            "SUM(CASE WHEN CONTAINS(Orders.Status, '50%') THEN Orders.Amount END) "
            "/ SUM(Orders.Amount) * 100"
        )

    def test_every_ossie_expression_is_inside_the_subset(self) -> None:
        for name, dialects in _dialects().items():
            if "OSSIE_SQL_2026" in dialects:
                assert subset_violations(dialects["OSSIE_SQL_2026"]) == [], name


class TestAnsiOnly:
    @pytest.mark.parametrize(
        "name",
        [
            "Escaped",  # needs ESCAPE, which the language does not have
            "Typical",  # MODE is not in the language
            "Hashed",  # an authored expression outside the subset
        ],
    )
    def test_no_ossie_form_keeps_ansi_only(self, name: str) -> None:
        assert list(_dialects()[name]) == ["ANSI_SQL"]


def _hello_world(metric_sql: str) -> dict[str, Any]:
    """The hello-world model of apache/ossie#576, tagged OSSIE_SQL_2026."""

    def expr(sql: str) -> dict[str, Any]:
        return {"dialects": [{"dialect": "OSSIE_SQL_2026", "expression": sql}]}

    return {
        "version": "0.2.0.dev0",
        "name": "hello_world",
        "datasets": [
            {
                "name": "sales",
                "source": "hello.public.sales",
                "fields": [{"name": "amount", "datatype": "Decimal", "expression": expr("amount")}],
            }
        ],
        "metrics": [
            {"name": "total_amount", "datatype": "Decimal", "expression": expr(metric_sql)}
        ],
    }


class TestRoundTrip:
    def test_import_reads_the_ansi_form(self) -> None:
        osi = conv.OBMLtoOSI(copy.deepcopy(_OBML), "sales").convert()
        back = conv.OSItoOBML(osi).convert()
        assert (
            back["measures"]["Discounted"]["filters"] == _OBML["measures"]["Discounted"]["filters"]
        )

    def test_ossie_first_scalar_function_keeps_its_tag(self) -> None:
        # ROUND is REQUIRED by the spec; dropping the tag would retag it again.
        doc = _hello_world("ROUND(SUM(sales.amount), 2)")
        back = conv.OBMLtoOSI(conv.OSItoOBML(doc).convert()).convert()
        dialects = {
            d["dialect"]: d["expression"]
            for d in next(m for m in back["metrics"] if m["name"] == "total_amount")["expression"][
                "dialects"
            ]
        }
        assert subset_violations(dialects["OSSIE_SQL_2026"]) == []
        assert "ROUND(" in dialects["OSSIE_SQL_2026"]

    def test_ossie_first_metric_keeps_its_tag(self) -> None:
        doc = _hello_world("SUM(sales.amount)")
        back = conv.OBMLtoOSI(conv.OSItoOBML(doc).convert()).convert()
        dialects = {
            d["dialect"]: d["expression"] for d in back["metrics"][0]["expression"]["dialects"]
        }
        assert dialects["OSSIE_SQL_2026"] == "SUM(sales.amount)"


class TestSubsetChecker:
    @pytest.mark.parametrize(
        "sql",
        [
            "SUM(t.x)",
            "COUNT(*)",
            "COUNT(DISTINCT t.x)",
            "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.x)",
            "COALESCE(SUM(t.x), 0)",
            "CAST(DATE_TRUNC('month', t.d) AS DATE)",
            "LAG(SUM(t.x), 1, 0) OVER (ORDER BY t.d ASC)",
            "SUM(SUM(t.x)) OVER (PARTITION BY t.r ORDER BY t.d"
            " ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)",
            "SUM(CASE WHEN t.a IN ('x', 'y') AND NOT (t.b IS NULL) THEN t.c END)",
        ],
    )
    def test_allowed(self, sql: str) -> None:
        assert subset_violations(sql) == []

    @pytest.mark.parametrize("name", sorted(_FUNCTIONS))
    def test_every_spec_function_sample_is_allowed(self, name: str) -> None:
        # The samples define both the names and the node types; each must pass.
        assert subset_violations(_FUNCTIONS[name]) == []

    @pytest.mark.parametrize(
        "sql",
        [
            "ROUND(SUM(t.x), 2) + ABS(t.y) + CEIL(t.z) + TRUNCATE(t.x, 1) + LOG10(t.x)",
            "DATEADD(day, 7, t.d) + DATEDIFF(month, t.a, t.b) + EXTRACT(YEAR FROM t.d)",
            "IFF(t.c, 1, 0) + ZEROIFNULL(t.x) + NVL2(t.a, 1, 2) + CHARINDEX('a', t.s)",
            "CAST(t.x AS STRING) + TRY_CAST(t.y AS DECIMAL(10, 2)) + TIMESTAMP_NTZ '2024-01-01'",
            "t.a || t.b",
            "t.a IS NOT DISTINCT FROM t.b",
            "CASE WHEN t.a LIKE 'x%' THEN 'y' ELSE 'z' END",
        ],
    )
    def test_required_scalar_functions_and_syntax(self, sql: str) -> None:
        assert subset_violations(sql) == []

    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            ("MODE(t.x)", "Mode"),
            ("LISTAGG(t.x, ',')", "LISTAGG"),
            ("ANY_VALUE(t.x)", "AnyValue"),
            ("t.a LIKE 'x' ESCAPE '\\'", "Escape"),
            ("AVG(DISTINCT t.x)", "DISTINCT"),
            ("SUM(t.x) + (SELECT 1)", "Subquery"),
            ("CAST(t.x AS INTERVAL)", "INTERVAL"),
            # Written forms sqlglot would normalize into allowed nodes.
            ("SUM(SAFE_CAST(t.x AS INT))", "SAFE_CAST"),
            ("CAST(t.x AS INT64)", "INT64"),
            ("STR_POSITION(t.s, 'a')", "STR_POSITION"),
            # Constructs the spec does not list.
            ("TIMESTAMP '2024-01-01'", "TIMESTAMP"),
            ("INTERVAL '1' DAY", "INTERVAL"),
            ("t.a::int", "::"),
            ("t.a[1]", "["),
            ("CURRENT_USER", "CurrentUser"),
            ("TO_DATE(t.s, 'YYYY')", "EXPERIMENTAL"),
            ("EXTRACT(DECADE FROM t.d)", "EXTRACT"),
            ("DATE_TRUNC('decade', t.d)", "DATE_TRUNC"),
            ("SUM(", "does not parse"),
        ],
    )
    def test_rejected(self, sql: str, reason: str) -> None:
        assert any(reason in v for v in subset_violations(sql))
