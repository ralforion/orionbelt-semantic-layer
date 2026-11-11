"""The shapes a federated query took on since 2.36.0, with every leg on each engine.

An ``exists`` / ``nonexists`` subquery in the leg of its subject's source, a
chain of three sources, a role-playing dimension, a filter after the join on
the day of a month the query selects, and cumulative, period-over-period and
window metrics, whose legs return each date as stored for the combine step to
truncate, raw-mode rows joined in the combine step, measures with a grain or a
filter context of their own, measures of independent facts, a query of
their own each, unioned in the combine step (rolled up beside a total and
filter contexts, and at two grains), and conditions after the join on
strings, static filters and blank tests too, each run in its leg. Every source
is the engine itself, as in ``test_federated_equality_exec``: the combined
answer must be the engine's own over one source.
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
      Status: {{code: s, abstractType: string}}
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
  Customer: {{dataObject: Customers, column: Customer ID, resultType: int}}
  Status: {{dataObject: Orders, column: Status, resultType: string}}
  Sales Rep:
    {{dataObject: Employees, column: Name, resultType: string, via: Orders, pathName: sales}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
  Median Order: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: median}}
  # Named by numbers, which a sort reads as the names before positions.
  "2024": {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: max}}
  "1": {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: min}}
  Paid Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filters:
      - column: {{dataObject: Orders, column: Status}}
        operator: equals
        values: [{{dataType: string, valueString: paid}}]
  Revenue Total:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    total: true
  Region Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    grain: {{mode: FIXED, include: [Region]}}
  Revenue Any Region:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext: {{exclude: [Region]}}
  Revenue All:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext: {{mode: FIXED}}
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
  Ticket Rank:
    {{type: window, windowFunction: dense_rank, measure: Tickets Count, partitionBy: [Region]}}
  Revenue Elsewhere: {{expression: '{{[Revenue Total]}} - {{[Revenue]}}'}}
  Revenue per Customer:
    {{type: reaggregate, measure: Revenue, per: [Customer], aggregation: avg}}
  Revenue per Day:
    {{type: reaggregate, measure: Revenue, per: [Order Date], aggregation: max}}
  Customers with Revenue:
    {{type: reaggregate, measure: Revenue, per: [Customer], aggregation: count}}
  Carried Revenue:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: sum
    population: dimension
    carryForward: true
"""

# Order 6's customer 99 is missing; customer 12 has no region and customer 13's
# region 3 is missing. Ticket 4's customer 98 is not a customer.
ORDERS = [
    (1, 10, 1, "2024-01-05", "100.50", "paid"),
    (2, 10, 2, "2024-01-20", "20.25", "Paid"),
    (3, 11, 1, "2024-02-03", "7.00", "open"),
    (4, 12, 3, "2024-02-14", "55.10", "PAID"),
    (5, 13, 2, "2024-03-01", "3.15", "paid"),
    (6, 99, 1, "2024-03-09", "40.00", "open"),
    (7, 11, 2, "2024-03-30", "12.00", "paid"),
]
CUSTOMERS = [(10, 1), (11, 2), (12, None), (13, 3)]
REGIONS = [(1, "EU"), (2, "US")]
TICKETS = [(10, "high"), (10, "low"), (12, "low"), (98, "high")]
# "bob" sorts between "Ann" and "Cid" by a collation, after both by bytes.
EMPLOYEES = [(1, "Ann"), (2, "bob"), (3, "Cid")]

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
                [
                    ("id", whole),
                    ("c", whole),
                    ("r", whole),
                    ("d", day),
                    ("a", decimal),
                    ("s", text),
                ],
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


def _model(federated: bool, static: str = "") -> str:
    def source(name: str) -> str:
        return f"    dataSource: {name}\n" if federated else ""

    return MODEL.format(shop=source("shop"), crm=source("crm"), geo=source("geo")) + static


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
    "an_exists_or_a_condition_on_the_orders": {
        "select": BY_REGION,
        "where": [
            {
                "logic": "or",
                "filters": [
                    _tickets("exists"),
                    {"field": "Order Date", "op": ">=", "value": "2024-03-01"},
                ],
            }
        ],
    },
    "a_nonexists_or_a_condition_on_the_orders": {
        "select": BY_REGION,
        "where": [
            {
                "logic": "or",
                "filters": [
                    _tickets("nonexists"),
                    {"field": "Order Date", "op": ">=", "value": "2024-03-01"},
                ],
            }
        ],
    },
    "a_reaggregate_beside_the_tickets": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue per Customer", "Tickets Count"]},
    },
    "a_count_reaggregate_beside_the_tickets": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Customers with Revenue", "Tickets Count"],
        },
    },
    "filters_on_table_columns": {
        "select": BY_REGION,
        "where": [
            {"field": "Regions.Region Name", "op": "in", "value": ["EU", "US"]},
            {"field": "Orders.Amount", "op": ">", "value": 10},
        ],
    },
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
    "a_reaggregate": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Revenue per Customer", "Revenue"],
        },
    },
    "a_reaggregate_per_a_date": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue per Day"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    "a_reaggregate_carried_forward": {
        "select": {"dimensions": ["Order Date:month", "Region"], "measures": ["Carried Revenue"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    # Ordered by a median, the measures are computed from the fact leg's
    # rows: MySQL's leg evaluates the filter, 'Paid' and 'PAID' are paid.
    "a_sum_from_rows_filtered_on_a_string": {
        "select": {"dimensions": ["Region"], "measures": ["Paid Revenue", "Revenue"]},
        "orderBy": [{"field": "Median Order", "direction": "desc"}],
    },
    "a_measure_of_the_customers": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue", "Customers Count"]}
    },
    "a_measure_of_the_customers_without_tickets": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue", "Customers Count"]},
        "where": [_tickets("nonexists")],
    },
    "a_measure_of_the_customers_the_orders_reach": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Customers Count"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    "independent_facts": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue", "Tickets Count"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    "independent_facts_by_month": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Tickets Count"]},
        "where": [
            {
                "logic": "and",
                "filters": [
                    {"field": "Order Date", "op": ">=", "value": "2024-02-01"},
                    {"field": "Region", "op": "=", "value": "EU"},
                ],
            }
        ],
    },
    "independent_facts_rolled_up": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue", "Tickets Count"],
        },
        "grouping": "rollup",
    },
    "independent_facts_rolled_up_in_one_region": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue", "Tickets Count"],
        },
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
        "grouping": "rollup",
    },
    "independent_facts_with_a_grain": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Region Revenue", "Tickets Count"],
        }
    },
    "independent_facts_with_a_filter_context": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue Any Region", "Tickets Count"]},
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
    },
    "independent_facts_rolled_up_beside_a_total": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue Total", "Tickets Count"],
        },
        "grouping": "rollup",
    },
    "independent_facts_rolled_up_beside_filter_contexts": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue Any Region", "Revenue Since February", "Tickets Count"],
        },
        "where": [{"field": "Region", "op": "in", "value": ["EU", "US"]}],
        "grouping": "rollup",
    },
    "independent_facts_rolled_up_beside_filter_contexts_in_one_region": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue Any Region", "Revenue Since February", "Tickets Count"],
        },
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
        "grouping": "rollup",
    },
    # The revenue of the other regions, from the total, which the totals pass
    # rebuilds from its components over the filter context's query, not over
    # the fact tables. A difference: a quotient is a double in the combine step
    # and a decimal of the engine's own scale on one database.
    "a_metric_over_a_total_beside_a_filter_context": {
        "select": {
            "dimensions": ["Region"],
            "measures": ["Revenue Elsewhere", "Revenue Any Region"],
        },
    },
    "independent_facts_with_a_metric_over_a_total_beside_a_filter_context": {
        "select": {
            "dimensions": ["Region"],
            "measures": ["Revenue Elsewhere", "Revenue Any Region", "Tickets Count"],
        },
    },
    "independent_facts_beside_a_fixed_context_after_a_date_filter": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue All", "Tickets Count"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-03-05"}],
    },
    "independent_facts_rolled_up_beside_a_fixed_context_after_a_date_filter": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue All", "Revenue Since February", "Tickets Count"],
        },
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-03-05"}],
        "grouping": "rollup",
    },
    "independent_facts_rolled_up_at_two_grains": {
        "select": {
            "dimensions": ["Region", "Order Date:quarter", "Order Date:month"],
            "measures": ["Revenue", "Tickets Count"],
        },
        "grouping": "rollup",
    },
    "independent_facts_running_total": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Running Revenue", "Tickets Count"],
        }
    },
    "independent_facts_rolling_window": {
        "select": {
            "dimensions": ["Order Date:month", "Region"],
            "measures": ["Rolling Revenue", "Tickets Count"],
        }
    },
    # Ranked by the tickets' count, 0 for a month of orders alone.
    "independent_facts_ranked": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Ticket Rank", "Revenue"],
        }
    },
    # Ranked by the revenue, NULL for the month of the tickets alone: last, on
    # one database and federated, where PostgreSQL ranked it first.
    "independent_facts_ranked_by_a_null": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue Rank", "Tickets Count"],
        }
    },
    "independent_facts_filled": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Tickets Count"]},
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
        "fill": True,
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
    # Conditions after the join on strings, each run in its leg: MySQL's
    # collation calls 'eu' 'EU' and 'PAID' 'paid', where DuckDB compares bytes.
    "a_string_filter_after_the_join": {
        "select": {"dimensions": ["Sales Rep"], "measures": ["Revenue"]},
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Region", "op": "=", "value": "eu"},
                    {"field": "Status", "op": "=", "value": "PAID"},
                ],
            }
        ],
    },
    "a_string_filter_a_filter_context_drops": {
        "select": {"measures": ["Revenue", "Revenue Any Region"]},
        "where": [{"field": "Region", "op": "in", "value": ["eu", "us"]}],
    },
    "raw_rows_filtered_on_a_string": {
        "select": {"fields": ["Orders.Order ID", "Regions.Region Name"]},
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Region", "op": "like", "value": "e%"},
                    {"field": "Sales Rep", "op": "=", "value": "Cid"},
                ],
            }
        ],
    },
    # A blank test holds for the region of orders 4, 5 and 6, none or missing.
    "a_blank_test_after_the_join": {
        "select": {"dimensions": ["Sales Rep"], "measures": ["Revenue"]},
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Region", "op": "blank"},
                    {"field": "Status", "op": "=", "value": "OPEN"},
                ],
            }
        ],
    },
    "raw_rows_of_a_negated_filled_test": {
        "select": {"fields": ["Orders.Order ID", "Regions.Region Name"]},
        "where": [{"negated": True, "filters": [{"field": "Region", "op": "notblank"}]}],
    },
}
# A static filter on the regions, which runs after the join, in their leg.
STATIC = (
    "filters:\n  - {dataObject: Regions, column: Region Name, operator: notequals, value: us}\n"
)
BLANK = "filters:\n  - {dataObject: Regions, column: Region Name, operator: blank}\n"
STATIC_QUERIES: dict[str, Any] = {
    "a_static_string_filter": {"select": {"dimensions": ["Sales Rep"], "measures": ["Revenue"]}},
    "raw_rows_of_a_static_string_filter": {
        "select": {"fields": ["Orders.Order ID", "Employees.Name"]}
    },
}
# A static filter on the order date, which a fixed filter context drops: the
# tickets keep the region whose orders it leaves out.
LATE = (
    "filters:\n  - {dataObject: Orders, column: Order Date, operator: gte, value: '2024-03-05'}\n"
)
LATE_QUERIES: dict[str, Any] = {
    "independent_facts_beside_a_fixed_context_after_a_static_date_filter": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue All", "Tickets Count"]},
    },
    "independent_facts_rolled_up_beside_a_fixed_context_after_a_static_date_filter": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue All", "Revenue Since February", "Tickets Count"],
        },
        "grouping": "rollup",
    },
}
# A static filter on the tickets, which a filter context dropping the region
# keeps: the orders' own scan does not read the tickets, and one database
# applied it there too.
SEVERE = "filters:\n  - {dataObject: Tickets, column: Severity, operator: equals, value: high}\n"
SEVERE_QUERIES: dict[str, Any] = {
    "independent_facts_beside_a_context_after_a_static_filter_on_the_other_fact": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue Any Region", "Tickets Count"]},
    },
    "independent_facts_in_one_region_beside_a_context_after_a_static_filter_on_the_other_fact": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue Any Region", "Tickets Count"]},
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
    },
}
# Sorted where a group, a row or the period of the tickets is NULL, compared in
# their order: the engine places NULL first or last, and so does the combined
# answer. Limited without a sort, a query is sorted by its dimensions or fields.
ORDERED_QUERIES: dict[str, Any] = {
    "sorted_by_a_dimension_with_a_null_group": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
        "orderBy": [{"field": "Region"}],
    },
    "sorted_down_by_a_dimension_with_a_null_group": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
        "orderBy": [{"field": "Region", "direction": "desc"}],
    },
    "limited_without_a_sort": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
        "limit": 2,
    },
    # Sorted by each grain: the month, then the year, then the region.
    "limited_at_two_grains_without_a_sort": {
        "select": {
            "dimensions": ["Order Date:month", "Order Date:year", "Region"],
            "measures": ["Revenue"],
        },
        "limit": 3,
    },
    # Strings of another source, sorted as their engine sorts them.
    "sorted_by_a_string_of_another_source": {
        "select": {"dimensions": ["Sales Rep"], "measures": ["Revenue"]},
        "orderBy": [{"field": "Sales Rep"}],
    },
    "sorted_down_by_a_string_of_another_source": {
        "select": {"dimensions": ["Sales Rep"], "measures": ["Revenue"]},
        "orderBy": [{"field": "Sales Rep", "direction": "desc"}],
    },
    "limited_by_a_string_without_a_sort": {
        "select": {"dimensions": ["Sales Rep"], "measures": ["Revenue"]},
        "limit": 2,
    },
    "sorted_by_a_measure_named_2024_and_a_string": {
        "select": {"dimensions": ["Sales Rep"], "measures": ["Revenue", "2024"]},
        "orderBy": [{"field": "2024", "direction": "desc"}, {"field": "Sales Rep"}],
    },
    "sorted_by_a_measure_named_1_and_a_string": {
        "select": {"dimensions": ["Sales Rep"], "measures": ["Revenue", "1"]},
        "orderBy": [{"field": "1", "direction": "desc"}, {"field": "Sales Rep"}],
    },
    "rolled_up_by_a_string": {
        "select": {"dimensions": ["Sales Rep"], "measures": ["Revenue"]},
        "grouping": "rollup",
    },
    "raw_rows_sorted_by_a_string_field": {
        "select": {"fields": ["Orders.Order ID", "Employees.Name"]},
        "orderBy": [{"field": "Employees.Name"}, {"field": "Orders.Order ID"}],
    },
    "raw_rows_sorted_down_by_a_null_field": {
        "select": {"fields": ["Orders.Order ID", "Regions.Region Name"]},
        "orderBy": [
            {"field": "Regions.Region Name", "direction": "desc"},
            {"field": "Orders.Order ID"},
        ],
    },
    "independent_facts_sorted_down_by_month": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Tickets Count"]},
        "orderBy": [{"field": "Order Date", "direction": "desc"}],
    },
    "independent_facts_limited_without_a_sort": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Tickets Count"]},
        "limit": 2,
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
    wrong = []
    for static, queries in (
        ("", QUERIES),
        (STATIC, STATIC_QUERIES),
        (BLANK, STATIC_QUERIES),
        (LATE, LATE_QUERIES),
        (SEVERE, SEVERE_QUERIES),
    ):
        wrong += _wrong_shapes(target, store, static, queries)
    wrong += _wrong_shapes(target, store, "", ORDERED_QUERIES, ordered=True)
    assert not wrong, f"{target.name}:\n" + "\n".join(wrong)


def _wrong_shapes(
    target: VendorTarget,
    store: ModelStore,
    static: str,
    queries: dict[str, Any],
    *,
    ordered: bool = False,
) -> list[str]:
    """The shapes of *queries* whose combined answer is not the engine's own,
    *ordered* in the same order too."""
    single_id = store.load_model(_model(False, static)).model_id
    federated_id = store.load_model(_model(True, static)).model_id
    wrong = []
    for name, raw in queries.items():
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
        compared = (lambda rows: [_canonical(r) for r in rows]) if ordered else _rows
        if compared(combined) != compared(expected) or not expected:
            wrong.append(f"{name}: {combined} != {expected}")
    return wrong


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
