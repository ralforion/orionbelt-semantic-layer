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
"""Apache Ossie 0.3.0.dev0 documents, read and written.

Ossie's compliance models (``fixtures/ossie_compliance``) are written in the
0.3.0.dev0 shape: ``expression: {sql: ...}``, metrics declared on their dataset,
and ``nonportable_extensions``. The import read none of it, so they converted to
a model with no measures at all.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml

import osi_orionbelt.converter as conv

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ossie_compliance"


def _field(name: str, sql: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"name": name, "expression": {"sql": sql or name}, **extra}


def _shop(**extra: Any) -> dict[str, Any]:
    """Orders and customers, every expression in the ``sql`` form."""
    doc: dict[str, Any] = {
        "version": "0.3.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "orders",
                "source": "wh.public.orders",
                "primary_key": ["order_id"],
                "fields": [
                    _field("order_id"),
                    _field("customer_id"),
                    _field("amount", "amount_cents / 100.0"),
                ],
                "metrics": [
                    {"name": "total_revenue", "expression": {"sql": "SUM(amount)"}},
                    {"name": "order_count", "expression": {"sql": "COUNT(*)"}},
                ],
            },
            {
                "name": "customers",
                "source": "wh.public.customers",
                "primary_key": ["customer_id"],
                "fields": [_field("customer_id"), _field("region", dimension={})],
            },
        ],
        "relationships": [
            {
                "name": "orders_to_customers",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            }
        ],
    }
    doc.update(extra)
    return doc


def _convert(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    converter = conv.OSItoOBML(copy.deepcopy(doc))
    return converter.convert(), converter.warnings


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((_FIXTURES / name).read_text())
    return loaded


def _stash(obml: dict[str, Any], key: str) -> list[Any]:
    for ext in obml.get("customExtensions", []):
        data = json.loads(ext["data"]) if ext.get("vendor") == "OSI" else {}
        if key in data:
            return list(data[key])
    return []


def _unconverted(obml: dict[str, Any]) -> list[str]:
    return [m["name"] for m in _stash(obml, "obml_unconverted_metrics")]


class TestSqlForm:
    def test_field_sql_is_the_column_code(self) -> None:
        obml, _ = _convert(_shop())
        # Read as the field name before, silently: `amount` for `amount_cents / 100.0`.
        assert obml["dataObjects"]["orders"]["columns"]["amount"]["code"] == "amount_cents / 100.0"

    def test_metric_sql_becomes_a_measure(self) -> None:
        doc = _shop(metrics=[{"name": "revenue", "expression": {"sql": "SUM(orders.amount)"}}])
        obml, _ = _convert(doc)
        assert obml["measures"]["revenue"]["columns"] == [
            {"dataObject": "orders", "column": "amount"}
        ]

    def test_sql_wins_over_an_ossie_dialect_entry(self) -> None:
        doc = _shop()
        doc["datasets"][0]["fields"][2]["expression"] = {
            "sql": "amount_cents / 100.0",
            "dialects": [
                {"dialect": "OSSIE_SQL_2026", "expression": "stale"},
                {"dialect": "SNOWFLAKE", "expression": "amount_cents / 100"},
            ],
        }
        obml, _ = _convert(doc)
        assert obml["dataObjects"]["orders"]["columns"]["amount"]["code"] == "amount_cents / 100.0"


class TestDatasetScopedMetrics:
    def test_a_dataset_metric_reads_its_own_fields(self) -> None:
        obml, _ = _convert(_shop())
        assert obml["measures"]["total_revenue"] == {
            "columns": [{"dataObject": "orders", "column": "amount"}],
            "aggregation": "sum",
        }

    def test_count_star_counts_the_primary_key(self) -> None:
        obml, _ = _convert(_shop())
        assert obml["measures"]["order_count"]["columns"] == [
            {"dataObject": "orders", "column": "order_id"}
        ]
        assert obml["measures"]["order_count"]["aggregation"] == "count"

    def test_count_star_without_a_primary_key_is_kept_out_with_a_warning(self) -> None:
        doc = _shop()
        del doc["datasets"][0]["primary_key"]
        obml, warnings = _convert(doc)
        assert "order_count" not in obml.get("measures", {})
        kept = _stash(obml, "obml_unconverted_dataset_metrics")
        assert [(k["dataset"], k["metric"]["name"]) for k in kept] == [("orders", "order_count")]
        assert any("'orders Count'" in w for w in warnings)

    def test_a_kept_back_dataset_metric_is_written_back_on_its_dataset(self) -> None:
        doc = _shop()
        del doc["datasets"][0]["primary_key"]
        back = conv.OBMLtoOSI(_convert(doc)[0]).convert()
        orders = next(ds for ds in back["datasets"] if ds["name"] == "orders")
        written = {m["name"]: m["expression"] for m in orders.get("metrics", [])}
        assert written["order_count"]["dialects"][0]["expression"] == "COUNT(*)"
        assert "order_count" not in {m["name"] for m in back.get("metrics", [])}
        assert conv.validate_osi(back).valid

    def test_a_source_column_is_the_physical_column_not_the_field_of_that_name(self) -> None:
        # Field ``amount`` computes discounted_amount; source.amount is the column.
        doc = _shop()
        doc["datasets"][0]["fields"][2] = _field("amount", "discounted_amount")
        doc["datasets"][0]["metrics"] = [
            {"name": "gross", "expression": {"sql": "SUM(source.amount)"}}
        ]
        obml, warnings = _convert(doc)
        column = obml["measures"]["gross"]["columns"][0]["column"]
        assert column != "amount"
        assert obml["dataObjects"]["orders"]["columns"][column]["code"] == "amount"
        assert any("source column 'amount'" in w for w in warnings)

    def test_a_quoted_source_column_is_stored_without_its_quotes(self) -> None:
        doc = _shop()
        doc["datasets"][0]["metrics"] = [
            {"name": "gross", "expression": {"sql": 'SUM(source."Order Total")'}}
        ]
        obml, _ = _convert(doc)
        column = obml["measures"]["gross"]["columns"][0]["column"]
        # OBML quotes the code itself: '"Order Total"' compiled to """Order Total""".
        assert obml["dataObjects"]["orders"]["columns"][column]["code"] == "Order Total"
        back = conv.OBMLtoOSI(obml).convert()
        orders = next(ds for ds in back["datasets"] if ds["name"] == "orders")
        field = next(f for f in orders["fields"] if f["name"] == column)
        assert field["expression"]["dialects"][0]["expression"] == '"Order Total"'

    def test_a_synthesized_name_does_not_collide_by_case(self) -> None:
        # SOURCE_AMOUNT and source_amount are one bare identifier.
        doc = _shop()
        doc["datasets"][0]["fields"].append(_field("SOURCE_AMOUNT", "something_else"))
        doc["datasets"][0]["metrics"] = [
            {"name": "gross", "expression": {"sql": "SUM(source.amount)"}}
        ]
        obml, _ = _convert(doc)
        column = obml["measures"]["gross"]["columns"][0]["column"]
        assert column.upper() != "SOURCE_AMOUNT"
        assert obml["dataObjects"]["orders"]["columns"][column]["code"] == "amount"

    def test_a_generated_name_does_not_take_an_existing_one(self) -> None:
        doc = _shop(
            metrics=[
                {"name": "total_revenue", "expression": {"sql": "MAX(orders.amount)"}},
                {"name": "orders total_revenue", "expression": {"sql": "MIN(orders.amount)"}},
            ]
        )
        obml, _ = _convert(doc)
        aggregations = {n: m["aggregation"] for n, m in obml["measures"].items()}
        assert aggregations["total_revenue"] == "max"
        assert aggregations["orders total_revenue"] == "min"
        assert aggregations["orders total_revenue 2"] == "sum"

    def test_a_source_column_resolves_to_the_field_computing_it(self) -> None:
        doc = _shop()
        doc["datasets"][0]["fields"].append(_field("cents", "amount_cents"))
        doc["datasets"][0]["metrics"] = [
            {"name": "cents_total", "expression": {"sql": "SUM(source.amount_cents)"}}
        ]
        obml, _ = _convert(doc)
        assert obml["measures"]["cents_total"]["columns"] == [
            {"dataObject": "orders", "column": "cents"}
        ]

    def test_a_name_taken_by_a_model_metric_is_prefixed(self) -> None:
        doc = _shop(
            metrics=[{"name": "total_revenue", "expression": {"sql": "MAX(orders.amount)"}}]
        )
        obml, warnings = _convert(doc)
        assert obml["measures"]["total_revenue"]["aggregation"] == "max"
        assert obml["measures"]["orders total_revenue"]["aggregation"] == "sum"
        assert any("'orders total_revenue'" in w for w in warnings)

    def test_agg_of_a_dataset_metric_reads_its_measure(self) -> None:
        doc = _shop(
            metrics=[
                {
                    "name": "revenue_per_customer",
                    "expression": {
                        "sql": "AGG(orders.total_revenue) / COUNT(DISTINCT customers.customer_id)"
                    },
                }
            ]
        )
        obml, _ = _convert(doc)
        formula = obml["metrics"]["revenue_per_customer"]["expression"]
        assert formula.startswith("{[total_revenue]} /")

    def test_agg_as_the_whole_expression(self) -> None:
        doc = _shop(
            metrics=[{"name": "revenue_again", "expression": {"sql": "AGG(orders.total_revenue)"}}]
        )
        obml, _ = _convert(doc)
        assert obml["measures"]["revenue_again"]["columns"] == [
            {"dataObject": "orders", "column": "amount"}
        ]


class TestNonportableExtensions:
    def test_an_orionbelt_stash_is_restored(self) -> None:
        doc = _shop()
        doc["datasets"][0]["nonportable_extensions"] = [
            {"vendor_name": "ORIONBELT", "data": {"obml_owner": "sales-team"}}
        ]
        obml, _ = _convert(doc)
        assert obml["dataObjects"]["orders"]["owner"] == "sales-team"

    def test_a_foreign_extension_survives_the_round_trip_as_an_object(self) -> None:
        doc = _shop()
        payload = {"model": "mart_orders", "tags": ["finance"]}
        doc["datasets"][0]["nonportable_extensions"] = [{"vendor_name": "DBT", "data": payload}]
        back = conv.OBMLtoOSI(_convert(doc)[0]).convert()
        orders = next(ds for ds in back["datasets"] if ds["name"] == "orders")
        assert {"vendor_name": "DBT", "data": payload} in orders["nonportable_extensions"]

    def test_text_that_is_not_an_object_is_wrapped_with_a_warning(self) -> None:
        obml, _ = _convert(_shop())
        obml["customExtensions"] = [{"vendor": "LEGACY", "data": "plain text"}]
        converter = conv.OBMLtoOSI(obml)
        osi = converter.convert()
        assert {"vendor_name": "LEGACY", "data": {"value": "plain text"}} in osi[
            "nonportable_extensions"
        ]
        assert any("LEGACY" in w for w in converter.warnings)


class TestExport:
    def test_the_export_is_a_valid_current_document(self) -> None:
        osi = conv.OBMLtoOSI(_convert(_load("retail_model.ossie.yaml"))[0]).convert()
        assert osi["version"] == "0.3.0.dev0"
        result = conv.validate_osi(osi)
        assert result.schema_validation_performed
        assert result.valid, result.schema_errors + result.semantic_errors

    def test_no_custom_extensions_remain(self) -> None:
        osi = conv.OBMLtoOSI(_convert(_shop())[0]).convert()
        assert "custom_extensions" not in json.dumps(osi)
        assert all(isinstance(e["data"], dict) for e in osi["nonportable_extensions"])

    def test_a_field_keeps_its_ossie_tag(self) -> None:
        osi = conv.OBMLtoOSI(_convert(_shop())[0]).convert()
        orders = next(ds for ds in osi["datasets"] if ds["name"] == "orders")
        amount = next(f for f in orders["fields"] if f["name"] == "amount")
        tags = {d["dialect"]: d["expression"] for d in amount["expression"]["dialects"]}
        assert tags == {
            "ANSI_SQL": "amount_cents / 100.0",
            "OSSIE_SQL_2026": "amount_cents / 100.0",
        }


class TestQuotedFieldIdentifiers:
    def test_the_code_is_the_name_and_the_export_writes_the_sql_back(self) -> None:
        doc = _shop()
        doc["datasets"][0]["fields"].append(_field("total", '"Order Total"'))
        obml, _ = _convert(doc)
        assert obml["dataObjects"]["orders"]["columns"]["total"]["code"] == "Order Total"
        back = conv.OBMLtoOSI(obml).convert()
        orders = next(ds for ds in back["datasets"] if ds["name"] == "orders")
        total = next(f for f in orders["fields"] if f["name"] == "total")
        assert total["expression"]["dialects"][0]["expression"] == '"Order Total"'

    def test_an_edited_code_wins_over_the_saved_sql(self) -> None:
        doc = _shop()
        doc["datasets"][0]["fields"].append(_field("total", '"Order Total"'))
        obml, _ = _convert(doc)
        obml["dataObjects"]["orders"]["columns"]["total"]["code"] = "new_total"
        back = conv.OBMLtoOSI(obml).convert()
        orders = next(ds for ds in back["datasets"] if ds["name"] == "orders")
        total = next(f for f in orders["fields"] if f["name"] == "total")
        assert total["expression"]["dialects"][0]["expression"] == "new_total"
        again, _ = _convert(back)
        assert again["dataObjects"]["orders"]["columns"]["total"]["code"] == "new_total"

    def test_an_edited_name_is_quoted_where_it_has_to_be(self) -> None:
        doc = _shop()
        doc["datasets"][0]["fields"].append(_field("total", '"Order Total"'))
        for edited, written in (("New Total", '"New Total"'), ('My "x"', '"My ""x"""')):
            obml, _ = _convert(doc)
            obml["dataObjects"]["orders"]["columns"]["total"]["code"] = edited
            back = conv.OBMLtoOSI(obml).convert()
            orders = next(ds for ds in back["datasets"] if ds["name"] == "orders")
            total = next(f for f in orders["fields"] if f["name"] == "total")
            assert total["expression"]["dialects"][0]["expression"] == written
            again, _ = _convert(back)
            assert again["dataObjects"]["orders"]["columns"]["total"]["code"] == edited

    def test_import_stashes_stay_out_of_ai_context(self) -> None:
        doc = _shop()
        doc["datasets"][0]["fields"].append(_field("total", '"Order Total"', label="Total"))
        back = conv.OBMLtoOSI(_convert(doc)[0]).convert()
        orders = next(ds for ds in back["datasets"] if ds["name"] == "orders")
        total = next(f for f in orders["fields"] if f["name"] == "total")
        assert total["label"] == "Total"
        assert not any(key.startswith("obml_") for key in total.get("ai_context", {}))


class TestValidationByVersion:
    def test_a_previous_version_document_is_checked_against_its_schema(self) -> None:
        doc = {
            "version": "0.2.0.dev0",
            "name": "m",
            "datasets": [
                {
                    "name": "t",
                    "source": "a.b.c",
                    "fields": [
                        {
                            "name": "x",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "x"}]
                            },
                        }
                    ],
                }
            ],
        }
        assert conv.validate_osi(doc).valid

    def test_both_compliance_models_are_valid(self) -> None:
        for name in ("retail_model.ossie.yaml", "scott_model.ossie.yaml"):
            result = conv.validate_osi(_load(name))
            assert result.valid, (name, result.schema_errors, result.semantic_errors)

    def test_duplicate_dataset_metric_names_are_reported(self) -> None:
        doc = _shop()
        doc["datasets"][0]["metrics"].append(
            {"name": "total_revenue", "expression": {"sql": "SUM(amount)"}}
        )
        assert any("DUPLICATE_METRIC" in e for e in conv.validate_osi(doc).semantic_errors)


class TestConstructsOBMLCannotTake:
    """Kept out with a warning, never written as OBML that stops the model loading."""

    def test_model_scoped_count_star_is_kept_out(self) -> None:
        doc = _shop(metrics=[{"name": "row_count", "expression": {"sql": "COUNT(*)"}}])
        obml, _ = _convert(doc)
        assert "row_count" not in obml.get("measures", {})
        assert "row_count" in _unconverted(obml)
        assert all(m.get("expression") != "*" for m in obml["measures"].values())

    def test_a_double_cast_is_written_as_obml_cast(self) -> None:
        doc = _shop(
            metrics=[
                {
                    "name": "per_customer",
                    "expression": {
                        "sql": "CAST(SUM(orders.amount) AS DOUBLE)"
                        " / COUNT(DISTINCT customers.customer_id)"
                    },
                },
                {"name": "avg_amount", "expression": {"sql": "AVG(CAST(orders.amount AS DOUBLE))"}},
                {
                    "name": "rounded",
                    "expression": {"sql": "SUM(CAST(orders.amount AS DECIMAL(18, 2)))"},
                },
            ]
        )
        obml, _ = _convert(doc)
        assert obml["metrics"]["per_customer"]["expression"].startswith("CAST({[")
        assert "'double')" in obml["metrics"]["per_customer"]["expression"]
        assert obml["measures"]["avg_amount"]["expression"] == "CAST({[orders].[amount]}, 'double')"
        assert (
            obml["measures"]["rounded"]["expression"]
            == "CAST({[orders].[amount]}, 'decimal(18, 2)')"
        )

    def test_a_cast_obml_has_no_type_for_is_kept_out(self) -> None:
        doc = _shop(
            metrics=[{"name": "n", "expression": {"sql": "COUNT(CAST(orders.amount AS VARCHAR))"}}]
        )
        obml, warnings = _convert(doc)
        assert "n" not in obml.get("measures", {})
        assert any("Metric 'n'" in w and "cast takes" in w for w in warnings)

    def test_a_field_outside_every_aggregate_is_kept_out(self) -> None:
        doc = _shop(
            metrics=[{"name": "odd", "expression": {"sql": "SUM(orders.amount) - orders.amount"}}]
        )
        obml, warnings = _convert(doc)
        assert "odd" not in obml.get("metrics", {})
        assert "odd" in _unconverted(obml)
        assert any("outside every aggregate" in w for w in warnings)

    def test_a_self_relationship_is_kept_out_and_exported_again(self) -> None:
        doc = _load("scott_model.ossie.yaml")
        obml, warnings = _convert(doc)
        assert all(j["joinTo"] != "emp" for j in obml["dataObjects"]["emp"].get("joins", []))
        assert any("emp_to_manager" in w and "LOSSY" in w for w in warnings)
        back = conv.OBMLtoOSI(obml).convert()
        assert "emp_to_manager" in {r["name"] for r in back["relationships"]}

    def test_a_join_key_marked_as_a_dimension_is_one(self) -> None:
        doc = _shop()
        doc["datasets"][0]["fields"][1]["dimension"] = {}
        obml, _ = _convert(doc)
        assert {"dataObject": "orders", "column": "customer_id"} in [
            {k: d[k] for k in ("dataObject", "column")} for d in obml["dimensions"].values()
        ]


class TestComplianceModels:
    def test_retail_measures(self) -> None:
        obml, _ = _convert(_load("retail_model.ossie.yaml"))
        measures = obml["measures"]
        for name, column in {
            "total_revenue": ("orders", "amount"),
            "total_returns": ("returns", "amount"),
            "customer_count": ("customers", "customer_id"),
            "return_count": ("returns", "return_id"),
            "visit_count": ("visits", "visit_id"),
            "ticket_count": ("tickets", "ticket_id"),
        }.items():
            assert measures[name]["columns"] == [{"dataObject": column[0], "column": column[1]}]
        assert "CAST(" in obml["metrics"]["revenue_per_customer"]["expression"]
        # Rejected by the spec when a query names them, so not loaded as queryable.
        assert {"amount_less_order", "running_revenue", "row_count"} <= set(_unconverted(obml))

    def test_scott_measures(self) -> None:
        obml, _ = _convert(_load("scott_model.ossie.yaml"))
        assert obml["measures"]["sum_sal"]["columns"] == [{"dataObject": "emp", "column": "sal"}]
        assert obml["measures"]["emp_count"]["columns"] == [
            {"dataObject": "emp", "column": "empno"}
        ]
        assert obml["measures"]["avg_sal"]["expression"] == "CAST({[emp].[sal]}, 'double')"
        assert obml["measures"]["dept_count"]["columns"] == [
            {"dataObject": "dept", "column": "deptno"}
        ]
