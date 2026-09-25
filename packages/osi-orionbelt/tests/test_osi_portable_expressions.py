"""Exported metric expressions must compute what OrionBelt computes.

Other Ossie consumers read only the expression, not our extension, so:

* every column reference resolves to ``<dataset>.<field>`` of the document;
* measure filters, totals, synthesized counts and metric-on-metric references
  are spelled out in the SQL;
* what has no faithful expression is left out, warned about, and restored by
  the reverse conversion.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
import sqlglot
from sqlglot import expressions as exp

import osi_orionbelt.converter as conv
from osi_orionbelt._portable import sql_ident

_MODEL: dict[str, Any] = {
    "version": 1.0,
    "dataObjects": {
        "Order Lines": {
            "code": "FCT_ORDER_LINES",
            "database": "DW",
            "schema": "SALES",
            "columns": {
                "Line ID": {"code": "LINE_ID", "abstractType": "string", "primaryKey": True},
                "Amount": {"code": "AMT", "abstractType": "float"},
                "Status": {"code": "STATUS", "abstractType": "string"},
                "Line Date": {"code": "LINE_DT", "abstractType": "date"},
                "Customer": {"code": "CUST_ID", "abstractType": "string"},
            },
            "joins": [
                {
                    "joinType": "many-to-one",
                    "joinTo": "Customers",
                    "columnsFrom": ["Customer"],
                    "columnsTo": ["Customer ID"],
                }
            ],
        },
        "Customers": {
            "code": "DIM_CUSTOMERS",
            "database": "DW",
            "schema": "SALES",
            "columns": {
                "Customer ID": {"code": "ID", "abstractType": "string", "primaryKey": True},
                "Country": {"code": "COUNTRY", "abstractType": "string"},
            },
        },
    },
    "dimensions": {
        "Line Date": {
            "dataObject": "Order Lines",
            "column": "Line Date",
            "resultType": "date",
            "timeGrain": "month",
        },
        "Customer Country": {
            "dataObject": "Customers",
            "column": "Country",
            "resultType": "string",
        },
    },
    "measures": {
        "Sales": {
            "columns": [{"dataObject": "Order Lines", "column": "Amount"}],
            "resultType": "float",
            "aggregation": "sum",
        },
        "US Sales": {
            "columns": [{"dataObject": "Order Lines", "column": "Amount"}],
            "resultType": "float",
            "aggregation": "sum",
            "filters": [
                {
                    "column": {"dataObject": "Customers", "column": "Country"},
                    "operator": "equals",
                    "values": [{"dataType": "string", "valueString": "US"}],
                },
                {
                    "logic": "or",
                    "negated": True,
                    "filters": [
                        {
                            "column": {"dataObject": "Order Lines", "column": "Status"},
                            "operator": "inlist",
                            "values": [
                                {"dataType": "string", "valueString": "void"},
                                {"dataType": "string", "valueString": "it's test"},
                            ],
                        },
                        {
                            "column": {"dataObject": "Order Lines", "column": "Status"},
                            "operator": "contains",
                            "values": [{"dataType": "string", "valueString": "50%"}],
                        },
                    ],
                },
            ],
        },
        "Total Sales": {
            "columns": [{"dataObject": "Order Lines", "column": "Amount"}],
            "resultType": "float",
            "aggregation": "sum",
            "total": True,
        },
        "Average Line": {
            "columns": [{"dataObject": "Order Lines", "column": "Amount"}],
            "resultType": "float",
            "aggregation": "avg",
            "total": True,
            "defaultValue": 0,
        },
        "Customers Reached": {
            "columns": [{"dataObject": "Order Lines", "column": "Customer"}],
            "resultType": "int",
            "aggregation": "count_distinct",
        },
        "Sales At Fixed Grain": {
            "columns": [{"dataObject": "Order Lines", "column": "Amount"}],
            "resultType": "float",
            "aggregation": "sum",
            "grain": {"mode": "FIXED", "include": ["Customer Country"]},
        },
    },
    "metrics": {
        "Sales per Line": {"expression": "{[Sales]} / {[Order Lines Count]}"},
        "US Share": {"expression": "{[US Sales]} / {[Total Sales]}"},
        "US Share Pct": {"expression": "{[US Share]} * 100"},
        "Running Sales": {
            "type": "cumulative",
            "measure": "Sales",
            "timeDimension": "Line Date",
            "partitionBy": ["Customer Country"],
        },
        "Sales Rank": {
            "type": "window",
            "windowFunction": "rank",
            "measure": "Sales",
            "partitionBy": ["Customer Country"],
        },
        "Sales MoM": {
            "type": "period_over_period",
            "expression": "{[Sales]}",
            "periodOverPeriod": {
                "timeDimension": "Line Date",
                "grain": "month",
                "offsetGrain": "month",
                "comparison": "difference",
            },
        },
        "Fixed Grain Share": {"expression": "{[Sales]} / {[Sales At Fixed Grain]}"},
    },
}


def _export(obml: dict[str, Any] = _MODEL) -> tuple[dict[str, Any], list[str]]:
    converter = conv.OBMLtoOSI(copy.deepcopy(obml))
    return converter.convert(), converter.warnings


def _sql(osi: dict[str, Any]) -> dict[str, str]:
    return {
        m["name"]: m["expression"]["dialects"][0]["expression"]
        for m in osi["semantic_model"][0].get("metrics", [])
    }


class TestReferencesResolve:
    def test_every_column_reference_is_a_dataset_field(self) -> None:
        osi, _ = _export()
        model = osi["semantic_model"][0]
        fields = {ds["name"]: {f["name"] for f in ds["fields"]} for ds in model["datasets"]}
        for name, sql in _sql(osi).items():
            assert "{[" not in sql, name
            for column in sqlglot.parse_one(sql).find_all(exp.Column):
                assert column.table in fields, (name, column.sql())
                assert column.name in fields[column.table], (name, column.sql())

    def test_dataset_name_with_space_is_quoted(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["Sales"] == 'SUM("Order Lines".AMT)'

    def test_sql_ident(self) -> None:
        assert sql_ident("Orders") == "Orders"
        assert sql_ident("Order Lines") == '"Order Lines"'
        assert sql_ident('a"b') == '"a""b"'


class TestSemanticsSpelledOut:
    def test_measure_filters_become_case_when(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["US Sales"] == (
            "SUM(CASE WHEN Customers.COUNTRY = 'US' AND NOT ("
            "(\"Order Lines\".STATUS IN ('void', 'it''s test')) OR "
            "(\"Order Lines\".STATUS LIKE '%50\\%%' ESCAPE '\\')"
            ') THEN "Order Lines".AMT END)'
        )

    def test_total_becomes_grand_total_window(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["Total Sales"] == 'SUM(SUM("Order Lines".AMT)) OVER ()'

    def test_avg_total_is_exact_and_default_applies(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["Average Line"] == (
            'COALESCE((SUM(SUM("Order Lines".AMT)) OVER () / '
            'SUM(COUNT("Order Lines".AMT)) OVER ()), 0)'
        )

    def test_count_distinct(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["Customers Reached"] == 'COUNT(DISTINCT "Order Lines".CUST_ID)'

    def test_synthesized_count_is_inlined(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["Sales per Line"] == (
            'SUM("Order Lines".AMT) / COUNT("Order Lines".LINE_ID)'
        )

    def test_metric_on_metric_is_inlined(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["US Share Pct"].startswith("(SUM(CASE WHEN")
        assert _sql(osi)["US Share Pct"].endswith(") * 100")

    def test_cumulative_orders_by_the_time_field(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["Running Sales"] == (
            'SUM(SUM("Order Lines".AMT)) OVER (PARTITION BY Customers.COUNTRY '
            'ORDER BY "Order Lines".LINE_DT ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)'
        )

    def test_window_metric(self) -> None:
        osi, _ = _export()
        assert _sql(osi)["Sales Rank"] == (
            'RANK() OVER (PARTITION BY Customers.COUNTRY ORDER BY SUM("Order Lines".AMT) DESC)'
        )

    def test_databricks_delegation_is_tagged_databricks(self) -> None:
        obml = copy.deepcopy(_MODEL)
        obml["measures"] = {"Delegated": {"aggregation": "measure"}}
        obml["metrics"] = {}
        osi, _ = _export(obml)
        dialect = osi["semantic_model"][0]["metrics"][0]["expression"]["dialects"][0]
        assert dialect == {"dialect": "DATABRICKS", "expression": 'MEASURE("Delegated")'}


class TestNotPortableIsLeftOut:
    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("Sales MoM", "date spine"),
            ("Sales At Fixed Grain", "grain"),
            ("Fixed Grain Share", "grain"),
        ],
    )
    def test_left_out_and_warned(self, name: str, reason: str) -> None:
        osi, warnings = _export()
        assert name not in _sql(osi)
        assert any(name in w and reason in w for w in warnings)

    def test_unknown_measure_reference_is_left_out(self) -> None:
        obml = copy.deepcopy(_MODEL)
        obml["metrics"] = {"Broken": {"expression": "{[Nope]} * 2"}}
        osi, warnings = _export(obml)
        assert "Broken" not in _sql(osi)
        assert any("Broken" in w and "Nope" in w for w in warnings)

    def test_count_without_single_primary_key_is_left_out(self) -> None:
        obml = copy.deepcopy(_MODEL)
        del obml["dataObjects"]["Order Lines"]["columns"]["Line ID"]["primaryKey"]
        osi, warnings = _export(obml)
        assert "Sales per Line" not in _sql(osi)
        assert any("Sales per Line" in w and "primary key" in w for w in warnings)


class TestRoundTrip:
    def test_measures_metrics_and_column_names_survive(self) -> None:
        osi, _ = _export()
        back = conv.OSItoOBML(osi).convert()
        assert back["measures"] == _MODEL["measures"]
        assert back["metrics"] == _MODEL["metrics"]
        for do_name, do_obj in _MODEL["dataObjects"].items():
            assert list(back["dataObjects"][do_name]["columns"]) == list(do_obj["columns"])
        assert back["dataObjects"]["Order Lines"]["joins"][0]["columnsFrom"] == ["Customer"]
        assert back["dataObjects"]["Order Lines"]["joins"][0]["columnsTo"] == ["Customer ID"]
        assert back["dimensions"]["Customer Country"]["column"] == "Country"

    def test_left_out_entities_ride_in_the_model_extension(self) -> None:
        osi, _ = _export()
        ext = next(
            e
            for e in osi["semantic_model"][0]["custom_extensions"]
            if e["vendor_name"] == "ORIONBELT"
        )
        kept = json.loads(ext["data"])["obml_unexported"]
        assert set(kept["metrics"]) == {"Sales MoM", "Fixed Grain Share"}
        assert set(kept["measures"]) == {"Sales At Fixed Grain"}


class TestFlatDocument:
    def test_flat_document_imports(self) -> None:
        """Current Apache Ossie documents put the model at the root."""
        flat = {
            "version": "0.2.0.dev0",
            "name": "shop",
            "datasets": [
                {
                    "name": "orders",
                    "source": "db.s.orders",
                    "primary_key": ["id"],
                    "fields": [
                        {
                            "name": "id",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "id"}]
                            },
                        },
                        {
                            "name": "amount",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "amount"}]
                            },
                        },
                    ],
                }
            ],
            "metrics": [
                {
                    "name": "revenue",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.amount)"}]
                    },
                }
            ],
        }
        obml = conv.OSItoOBML(flat).convert()
        assert obml["measures"]["revenue"]["columns"] == [
            {"dataObject": "orders", "column": "amount"}
        ]

    def test_document_without_a_model_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="No semantic model"):
            conv.OSItoOBML({"version": "0.2.0.dev0", "name": "x"}).convert()
