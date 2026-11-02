"""The shapes a federated query took on since 2.36.0, with every leg on each engine.

An ``exists`` / ``nonexists`` subquery in the leg of its subject's source, a
chain of three sources, a role-playing dimension, a filter after the join on
the day of a month the query selects, and cumulative, period-over-period and
window metrics, whose legs return each date as stored for the combine step to
truncate, raw-mode rows joined in the combine step, measures with a grain or a
filter context of their own, and measures of independent facts, a query of
their own each, unioned in the combine step. Every source is the engine itself, as in
``test_federated_equality_exec``: the combined answer must be the engine's own
over one source.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.federated_executor import combine, normalize_leg
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

MODEL = """\
version: 1.0
dataObjects:
  Orders:
    code: o
{shop}    columns:
      Order ID: {{code: id, abstractType: int}}
      Order Customer: {{code: c, abstractType: int}}
      Order Rep: {{code: r, abstractType: int}}
      Order Date: {{code: d, abstractType: date}}
      Amount: {{code: a, abstractType: decimal, numClass: additive}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Employees
        pathName: sales
        columnsFrom: [Order Rep]
        columnsTo: [Employee ID]
  Customers:
    code: c
{crm}    columns:
      Customer ID: {{code: id, abstractType: int}}
      Customer Region: {{code: rc, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region]
        columnsTo: [Region ID]
  Regions:
    code: g
{geo}    columns:
      Region ID: {{code: id, abstractType: int}}
      Region Name: {{code: n, abstractType: string}}
  Tickets:
    code: k
{crm}    columns:
      Ticket Customer: {{code: c, abstractType: int}}
      Severity: {{code: s, abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Ticket Customer]
        columnsTo: [Customer ID]
  Employees:
    code: e
{crm}    columns:
      Employee ID: {{code: id, abstractType: int}}
      Name: {{code: n, abstractType: string}}
dimensions:
  Region: {{dataObject: Regions, column: Region Name, resultType: string}}
  Order Date: {{dataObject: Orders, column: Order Date, resultType: date}}
  Sales Rep:
    {{dataObject: Employees, column: Name, resultType: string, via: Orders, pathName: sales}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
  Region Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    grain: {{mode: FIXED, include: [Region]}}
  Revenue Any Region:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext: {{exclude: [Region]}}
  Revenue Since February:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext:
      mode: FIXED
      include: [{{field: Order Date, op: ">=", value: "2024-02-01"}}]
metrics:
  Running Revenue: {{type: cumulative, measure: Revenue, timeDimension: Order Date}}
  Rolling Revenue: {{type: cumulative, measure: Revenue, timeDimension: Order Date, window: 2}}
  Revenue MoM:
    type: period_over_period
    expression: '{{[Revenue]}}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
  Revenue Rank:
    {{type: window, windowFunction: dense_rank, measure: Revenue, partitionBy: [Region]}}
"""

# Order 6's customer 99 is missing; customer 12 has no region and customer 13's
# region 3 is missing. Ticket 4's customer 98 is not a customer.
ORDERS = [
    (1, 10, 1, "2024-01-05", "100.50"),
    (2, 10, 2, "2024-01-20", "20.25"),
    (3, 11, 1, "2024-02-03", "7.00"),
    (4, 12, 3, "2024-02-14", "55.10"),
    (5, 13, 2, "2024-03-01", "3.15"),
    (6, 99, 1, "2024-03-09", "40.00"),
    (7, 11, 2, "2024-03-30", "12.00"),
]
CUSTOMERS = [(10, 1), (11, 2), (12, None), (13, 3)]
REGIONS = [(1, "EU"), (2, "US")]
TICKETS = [(10, "high"), (10, "low"), (12, "low"), (98, "high")]
EMPLOYEES = [(1, "Ann"), (2, "Bob"), (3, "Cid")]

# int, decimal(18, 2), string, date
TYPES = {
    "postgres": ("BIGINT", "NUMERIC(18,2)", "VARCHAR(20)", "DATE"),
    "mysql": ("SIGNED", "DECIMAL(18,2)", "CHAR", "DATE"),
    "snowflake": ("BIGINT", "NUMBER(18,2)", "VARCHAR", "DATE"),
    "bigquery": ("INT64", "NUMERIC", "STRING", "DATE"),
    "clickhouse": (
        "Nullable(Int64)",
        "Nullable(Decimal(18,2))",
        "Nullable(String)",
        "Nullable(Date32)",
    ),
    "dremio": ("BIGINT", "DECIMAL(18,2)", "VARCHAR", "DATE"),
    "databricks": ("BIGINT", "DECIMAL(18,2)", "STRING", "DATE"),
    "duckdb": ("BIGINT", "DECIMAL(18,2)", "VARCHAR", "DATE"),
}


def _tables(dialect: str) -> str:
    """``WITH o AS (...), ...``: the rows, as tables of this engine."""
    whole, decimal, text, day = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST(NULL AS {kind})" if v is None else f"CAST('{v}' AS {kind})"

    def table(name: str, columns: list[tuple[str, str]], rows: list[tuple[Any, ...]]) -> str:
        body = " UNION ALL ".join(
            "SELECT "
            + ", ".join(
                f"{value(v, kind)} AS {q(column)}"
                for v, (column, kind) in zip(row, columns, strict=True)
            )
            for row in rows
        )
        return f"{q(name)} AS ({body})"

    return "WITH " + ", ".join(
        [
            table(
                "o",
                [("id", whole), ("c", whole), ("r", whole), ("d", day), ("a", decimal)],
                ORDERS,
            ),
            table("c", [("id", whole), ("rc", whole)], CUSTOMERS),
            table("g", [("id", whole), ("n", text)], REGIONS),
            table("k", [("c", whole), ("s", text)], TICKETS),
            table("e", [("id", whole), ("n", text)], EMPLOYEES),
        ]
    )


def _on_tables(dialect: str, sql: str) -> str:
    """*sql* reading the rows as tables of this engine, beside its own ``WITH``."""
    if sql.startswith("WITH "):
        return f"{_tables(dialect)},\n{sql.removeprefix('WITH ')}"
    return f"{_tables(dialect)}\n{sql}"


def _model(federated: bool) -> str:
    def source(name: str) -> str:
        return f"    dataSource: {name}\n" if federated else ""

    return MODEL.format(shop=source("shop"), crm=source("crm"), geo=source("geo"))


def _tickets(op: str) -> dict[str, Any]:
    subquery = {
        "dataObject": "Tickets",
        "filter": [{"field": "Severity", "op": "=", "value": "high"}],
    }
    return {"field": "Customers.Customer ID", "op": op, "subquery": subquery}


BY_REGION = {"dimensions": ["Region"], "measures": ["Revenue"]}
BY_MONTH_REGION = ["Order Date:month", "Region"]
QUERIES = {
    "exists": {"select": BY_REGION, "where": [_tickets("exists")]},
    "nonexists": {"select": BY_REGION, "where": [_tickets("nonexists")]},
    "a_chain_of_three_sources": {"select": BY_REGION},
    "a_role": {"select": {"dimensions": ["Sales Rep"], "measures": ["Revenue"]}},
    "the_day_of_a_selected_month": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Order Date", "op": "<", "value": "2024-01-15"},
                    {"field": "Region", "op": "=", "value": "US"},
                ],
            }
        ],
    },
    "a_running_total": {"select": {"dimensions": BY_MONTH_REGION, "measures": ["Running Revenue"]}},
    "a_rolling_window": {
        "select": {"dimensions": BY_MONTH_REGION, "measures": ["Rolling Revenue"]}
    },
    "a_rank": {"select": {"dimensions": BY_MONTH_REGION, "measures": ["Revenue Rank", "Revenue"]}},
    "shown_from_february": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Running Revenue", "Revenue MoM"],
        },
        "where": [
            {"field": "Order Date", "op": ">=", "value": "2024-02-01"},
            {"field": "Region", "op": "in", "value": ["EU", "US"]},
        ],
    },
    "a_grain": {
        "select": {"dimensions": BY_MONTH_REGION, "measures": ["Revenue", "Region Revenue"]}
    },
    "a_filter_context": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Revenue", "Revenue Any Region", "Revenue Since February"],
        },
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
    },
    "independent_facts": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue", "Tickets Count"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    "raw_rows": {
        "select": {
            "fields": [
                "Orders.Order ID",
                "Orders.Order Date",
                "Orders.Amount",
                "Regions.Region Name",
            ]
        },
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Region", "op": "=", "value": "US"},
                    {"field": "Orders.Amount", "op": ">", "value": 50},
                ],
            }
        ],
    },
    "raw_distinct_rows": {
        "select": {"fields": ["Orders.Order Customer", "Regions.Region Name"], "distinct": True},
        "orderBy": [{"field": "1"}],
        "limit": 4,
    },
}


def _canonical(row: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """*row* as compared: names without case, decimals without their scale."""

    def value(v: Any) -> Any:
        if isinstance(v, Decimal):
            return v.normalize()
        if isinstance(v, int) and not isinstance(v, bool):
            return Decimal(v)
        if isinstance(v, datetime):
            return v.date()
        return v

    return tuple(sorted((k.lower(), value(v)) for k, v in row.items()))


def _rows(rows: list[dict[str, Any]]) -> list[tuple[tuple[str, Any], ...]]:
    return sorted((_canonical(r) for r in rows), key=repr)


def _assert_shapes(target: VendorTarget) -> None:
    store = ModelStore()
    single_id = store.load_model(_model(False)).model_id
    federated_id = store.load_model(_model(True)).model_id
    wrong = []
    for name, raw in QUERIES.items():
        if raw["select"].get("fields") and target.dialect == "bigquery":
            # BigQuery refuses the dot of a raw field's name, Orders.Order ID,
            # as a column name: raw mode fails there on one source too.
            continue
        query = QueryObject.model_validate(raw)
        single = store.compile_query(single_id, query, target.dialect)
        plan = store.compile_query(federated_id, query, target.dialect).federation
        assert plan is not None, f"{target.name} {name}: not federated"
        tables = {
            leg.name: normalize_leg(
                leg, pa.Table.from_pylist(target.execute(_on_tables(target.dialect, leg.sql)))
            )
            for leg in plan.legs
        }
        combined = combine(plan, tables, None).arrow_table.to_pylist()
        expected = target.execute(_on_tables(target.dialect, single.sql))
        if _rows(combined) != _rows(expected) or not expected:
            wrong.append(f"{name}: {combined} != {expected}")
    assert not wrong, f"{target.name}:\n" + "\n".join(wrong)


def test_duckdb_federated_shapes(vendor_duckdb: VendorTarget) -> None:
    _assert_shapes(vendor_duckdb)


def test_postgres_federated_shapes(vendor_postgres: VendorTarget) -> None:
    _assert_shapes(vendor_postgres)


def test_mysql_federated_shapes(vendor_mysql: VendorTarget) -> None:
    _assert_shapes(vendor_mysql)


def test_clickhouse_federated_shapes(vendor_clickhouse: VendorTarget) -> None:
    _assert_shapes(vendor_clickhouse)


def test_snowflake_federated_shapes(vendor_snowflake: VendorTarget) -> None:
    _assert_shapes(vendor_snowflake)


def test_bigquery_federated_shapes(vendor_bigquery: VendorTarget) -> None:
    _assert_shapes(vendor_bigquery)


def test_databricks_federated_shapes(vendor_databricks: VendorTarget) -> None:
    _assert_shapes(vendor_databricks)


def test_dremio_federated_shapes(vendor_dremio: VendorTarget) -> None:
    _assert_shapes(vendor_dremio)
