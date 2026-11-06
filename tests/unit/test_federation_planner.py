"""Federated planning: a query over two data sources returns what one would.

The differential test of ``design/PLAN_federation.md`` §10. The same tables
live once in one DuckDB file and once split over two, ``shop`` (orders) and
``crm`` (customers). A query compiled against the split model is run the way
federation runs it - each leg on its own file, the combine query over the
legs' results in an in-memory DuckDB - and must return the rows the same
query returns against the single file. The data holds the cases that tell a
right plan from a plausible one: an order whose customer is missing, a
customer without a region, and several orders per customer.
"""

from __future__ import annotations

import functools
import gc
import weakref
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.ast.nodes import FunctionCall  # noqa: E402
from orionbelt.compiler.federation import KeyMap  # noqa: E402
from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.dialect.duckdb import DuckDBDialect  # noqa: E402
from orionbelt.dialect.registry import DialectRegistry  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.service.compilation_cache import CompilationCache  # noqa: E402
from orionbelt.service.federated_executor import injected_fact_leg  # noqa: E402
from orionbelt.service.model_store import ModelStore  # noqa: E402

ORDERS = """
CREATE TABLE orders AS SELECT * FROM (VALUES
    (1, 10, DATE '2024-01-05', 100.50),
    (2, 10, DATE '2024-01-20', 20.25),
    (3, 11, DATE '2024-02-03', 7.00),
    (4, 12, DATE '2024-02-14', 55.10),
    (5, 13, DATE '2024-03-01', 3.15),
    (6, 99, DATE '2024-03-09', 40.00),
    (7, 11, DATE '2024-03-30', 12.00)
) t(order_id, customer_id, order_date, amount)
"""
CUSTOMERS = """
CREATE TABLE customers AS SELECT * FROM (VALUES
    (10, 'EU', 'acme'),
    (11, 'US', 'acme'),
    (12, NULL, 'acme'),
    (13, 'EU', 'other')
) t(customer_id, region, tenant)
"""


AMOUNT = "{dataObject: Orders, column: Amount}"


def model_yaml(
    *,
    federated: bool,
    tenant_filter: bool = False,
    extra: str = "",
    static: str = "",
    metric: str = "",
    settings: str = "",
    dimension: str = "",
    order_columns: str = "",
    customer_columns: str = "",
) -> str:
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    tenant = "  - {dataObject: Customers, column: Tenant, operator: equals, value: acme}\n"
    lines = (tenant if tenant_filter else "") + static
    filters = f"filters:\n{lines}" if lines else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
{settings}dataObjects:
  Orders:
    code: orders
{shop}    columns:
      Order ID: {{code: order_id, abstractType: int}}
      Order Customer: {{code: customer_id, abstractType: int}}
      Order Date: {{code: order_date, abstractType: date}}
      Amount: {{code: amount, abstractType: decimal, numClass: additive}}
{order_columns}    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Customers:
    code: customers
{crm}    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
      Region: {{code: region, abstractType: string}}
      Tenant: {{code: tenant, abstractType: string}}
{customer_columns}dimensions:
  Region: {{dataObject: Customers, column: Region, resultType: string}}
  Order Date: {{dataObject: Orders, column: Order Date, resultType: date}}
{dimension}measures:
  Revenue: {{columns: [{AMOUNT}], aggregation: sum, resultType: decimal}}
  Top Order: {{columns: [{AMOUNT}], aggregation: max, resultType: decimal}}
  Smallest Order: {{columns: [{AMOUNT}], aggregation: min, resultType: decimal}}
{extra}metrics:
  Revenue per Order: {{expression: "{{[Revenue]}} / {{[Orders Count]}}"}}
{metric}{filters}"""


@pytest.fixture(scope="module")
def files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("fed")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [ORDERS, CUSTOMERS]),
        ("shop", [ORDERS]),
        ("crm", [CUSTOMERS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


def _compile(yaml: str, query: dict[str, Any]) -> Any:
    store = ModelStore()
    model_id = store.load_model(yaml).model_id
    return store.compile_query(model_id, QueryObject.model_validate(query), "duckdb")


def _rows(con: Any, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in con.execute(sql).fetchall()]


def run_single(files: dict[str, str], query: dict[str, Any], **model: Any) -> list[Any]:
    compiled = _compile(model_yaml(federated=False, **model), query)
    assert compiled.federation is None
    con = duckdb.connect(files["all"], read_only=True)
    try:
        return _rows(con, compiled.sql)
    finally:
        con.close()


def _leg_table(paths: dict[str, str], leg: Any) -> Any:
    con = duckdb.connect(paths[leg.data_source], read_only=True)
    try:
        return con.sql(leg.sql).to_arrow_table()
    finally:
        con.close()


def _combined_rows(tables: dict[str, Any], sql: str) -> list[Any]:
    combine = duckdb.connect()
    for name, table in tables.items():
        combine.register(name, table)
    return sorted(_rows(combine, sql), key=repr)


def legs_registered(paths: dict[str, str], plan: Any) -> Any:
    """A DuckDB connection holding each leg of *plan*, run on its data source.

    Where the fact leg may read only the keys its dimension legs return, or
    join maps of them, the plan run as the executor runs it
    (``injected_fact_leg``) must combine to the same rows: checked here for
    each federated query these tests run.
    """
    tables = {leg.name: _leg_table(paths, leg) for leg in plan.legs}
    if plan.injections or plan.key_maps:
        fact, maps = injected_fact_leg(plan, tables, 10_000)
        read = {**tables, **maps, fact.name: _leg_table(paths, fact)}
        expected = _combined_rows(tables, plan.combine_sql)
        assert _combined_rows(read, plan.combine_sql) == expected, fact.sql
    combine = duckdb.connect()
    for name, table in tables.items():
        combine.register(name, table)
    return combine


def run_federated(files: dict[str, str], query: dict[str, Any], **model: Any) -> list[Any]:
    compiled = _compile(model_yaml(federated=True, **model), query)
    plan = compiled.federation
    assert plan is not None
    combine = legs_registered(files, plan)
    return _rows(combine, compiled.sql)


def assert_same(files: dict[str, str], query: dict[str, Any], **model: Any) -> None:
    single = run_single(files, query, **model)
    federated = run_federated(files, query, **model)
    ordered = bool(query.get("orderBy")) or query.get("limit") is not None
    if ordered:
        assert federated == single
    else:
        assert sorted(federated, key=repr) == sorted(single, key=repr)
    assert single, "the comparison needs rows"


MEASURES = ["Revenue", "Orders Count", "Top Order", "Smallest Order", "Revenue per Order"]
EU = {"field": "Region", "op": "=", "value": "EU"}

DIFFERENTIAL = {
    "by_dimension_island": {"select": {"dimensions": ["Region"], "measures": MEASURES}},
    "by_both_islands": {
        "select": {"dimensions": ["Order Date:month", "Region"], "measures": MEASURES}
    },
    "no_dimensions_with_a_filter_on_the_dimension_side": {
        "select": {"measures": MEASURES},
        "where": [{"field": "Region", "op": "in", "value": ["EU", "US"]}],
    },
    "filter_on_fact_island": {
        "select": {"dimensions": ["Region"], "measures": MEASURES},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    "filter_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": MEASURES},
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
    },
    "negated_filter_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": MEASURES},
        "where": [{"field": "Region", "op": "!=", "value": "EU"}],
    },
    "null_filter_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": MEASURES},
        "where": [{"field": "Region", "op": "is_null"}],
    },
    "blank_filter_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": MEASURES},
        "where": [{"field": "Region", "op": "blank"}],
    },
    "set_filter_on_dimension_island_without_its_dimensions": {
        "select": {"measures": MEASURES},
        "where": [{"field": "Region", "op": "is_not_null"}],
    },
    "negated_null_or_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": MEASURES},
        "where": [
            {
                "logic": "or",
                "negated": True,
                "filters": [
                    {"field": "Region", "op": "=", "value": "EU"},
                    {"field": "Region", "op": "is_null"},
                ],
            }
        ],
    },
    "null_or_on_dimension_island": {
        "select": {"dimensions": ["Region"], "measures": MEASURES},
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Region", "op": "=", "value": "US"},
                    {"field": "Region", "op": "is_null"},
                ],
            }
        ],
    },
    "filters_in_the_leg_and_across_islands": {
        "select": {"dimensions": ["Region"], "measures": MEASURES},
        "where": [
            {"field": "Region", "op": "in", "value": ["EU", "US"]},
            {
                "logic": "or",
                "filters": [
                    {"field": "Region", "op": "=", "value": "US"},
                    {"field": "Order Date", "op": "<", "value": "2024-02-01"},
                ],
            },
        ],
    },
    "or_across_islands": {
        "select": {"dimensions": ["Region"], "measures": MEASURES},
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Region", "op": "=", "value": "US"},
                    {"field": "Order Date", "op": "<", "value": "2024-02-01"},
                ],
            }
        ],
    },
    "having_ordered_limited": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue", "Orders Count"]},
        "having": [{"field": "Revenue", "op": ">", "value": 10}],
        "orderBy": [{"field": "Revenue", "direction": "desc"}],
        "limit": 2,
    },
}


class TestDifferential:
    @pytest.mark.parametrize("query", DIFFERENTIAL.values(), ids=DIFFERENTIAL.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(files, query)

    def test_a_static_filter_on_the_dimension_side_drops_the_rows_it_excludes(
        self, files: dict[str, str]
    ) -> None:
        """Customer 13 is another tenant, and order 6 has no customer: in one
        database the filter on the left-joined customers drops both orders.
        The combine step joins the customers' leg as an inner join to do the
        same, so a tenant filter cannot fail open across sources."""
        query = {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}}

        assert_same(files, query, tenant_filter=True)
        totals = sum(r[1] for r in run_federated(files, query, tenant_filter=True))
        assert float(totals) == pytest.approx(100.50 + 20.25 + 7.00 + 55.10 + 12.00)

    def test_a_total_reads_the_rows_a_leg_filter_keeps(self, files: dict[str, str]) -> None:
        totals = (
            f"  Revenue Total: {{columns: [{AMOUNT}], aggregation: sum, total: true}}\n"
            f"  Top Total: {{columns: [{AMOUNT}], aggregation: max, total: true}}\n"
        )
        query = {
            "select": {
                "dimensions": ["Order Date:month"],
                "measures": ["Revenue", "Revenue Total", "Top Total"],
            },
            "where": [EU],
        }

        assert_same(files, query, extra=totals)
        assert {float(r[2]) for r in run_federated(files, query, extra=totals)} == {123.90}

    def test_the_orphan_order_keeps_its_value_under_no_region(self, files: dict[str, str]) -> None:
        rows = dict(
            run_federated(files, {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}})
        )

        # Customer 12 has no region and order 6 no customer: both are NULL.
        assert float(rows[None]) == pytest.approx(55.10 + 40.00)


AVG = "  Average Order: {columns: [{dataObject: Orders, column: Amount}], aggregation: avg}\n"
AVERAGES = (
    AVG + "  Average Id: {columns: [{dataObject: Orders, column: Order ID}], aggregation: avg, "
    "resultType: int}\n"
    + "  Average Float: {columns: [{dataObject: Orders, column: Amount}], aggregation: avg, "
    "resultType: float}\n"
)
AVERAGE_MEASURES = ["Average Order", "Average Id", "Average Float", "Revenue"]
AVERAGE_QUERIES = {
    "by_dimension_island": {"select": {"dimensions": ["Region"], "measures": AVERAGE_MEASURES}},
    "by_both_islands": {
        "select": {"dimensions": ["Order Date:month", "Region"], "measures": AVERAGE_MEASURES}
    },
    "filter_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": AVERAGE_MEASURES},
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
    },
    "no_dimensions_with_a_filter_on_the_dimension_side": {
        "select": {"measures": AVERAGE_MEASURES},
        "where": [{"field": "Region", "op": "in", "value": ["EU", "US"]}],
    },
    "having_ordered": {
        "select": {"dimensions": ["Region"], "measures": ["Average Order"]},
        "having": [{"field": "Average Order", "op": ">", "value": 10}],
        "orderBy": [{"field": "Average Order", "direction": "desc"}],
    },
}


# One database returns the pairs its joined rows hold: no month crossed with
# a region its orders do not reach, a month with the region NULL for the order
# whose customer is missing.
WITHOUT_MEASURES = {
    "dimensions_of_both_sources": {"select": {"dimensions": ["Order Date:month", "Region"]}},
    "filtered_on_the_other_source": {
        "select": {"dimensions": ["Order Date:month"]},
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
    },
    "a_filter_true_for_null_on_the_other_source": {
        "select": {"dimensions": ["Order Date:month"]},
        "where": [{"field": "Region", "op": "is_null"}],
    },
    "filtered_on_the_facts_source": {
        "select": {"dimensions": ["Order Date", "Region"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    "ordered_and_limited": {
        "select": {"dimensions": ["Order Date:month", "Region"]},
        "orderBy": [{"field": "Region"}, {"field": "Order Date"}],
        "limit": 3,
    },
}


class TestWithoutMeasures:
    """Dimensions alone: the distinct values the fact rows reach, as on one source."""

    @pytest.mark.parametrize("query", WITHOUT_MEASURES.values(), ids=WITHOUT_MEASURES.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(files, query)

    def test_a_static_filter_on_the_other_source(self, files: dict[str, str]) -> None:
        assert_same(
            files, {"select": {"dimensions": ["Order Date:month", "Region"]}}, tenant_filter=True
        )

    def test_the_fact_leg_returns_its_dimensions_and_keys_once_each(self) -> None:
        plan = _compile(
            model_yaml(federated=True), WITHOUT_MEASURES["filtered_on_the_other_source"]
        ).federation

        orders, customers = plan.legs
        assert [c for c, _ in orders.columns] == ["Order Date", "__fk0_0"]
        assert orders.rows is None
        assert "GROUP BY ALL" in orders.sql
        assert '"Customers"."region" = \'EU\'' in customers.sql


class TestAverage:
    """An average is the total of the legs' partial sums over the total of their counts."""

    @pytest.mark.parametrize("query", AVERAGE_QUERIES.values(), ids=AVERAGE_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(files, query, extra=AVERAGES)

    def test_a_derived_metric_over_an_average(self, files: dict[str, str]) -> None:
        query = {"select": {"dimensions": ["Region"], "measures": ["Average Share"]}}
        metric = '  Average Share: {expression: "{[Average Order]} / {[Revenue]}"}\n'

        assert_same(files, query, extra=AVERAGES, metric=metric)

    def test_the_leg_returns_sums_and_counts_not_an_average(self) -> None:
        plan = _compile(
            model_yaml(federated=True, extra=AVG),
            {"select": {"dimensions": ["Region"], "measures": ["Average Order"]}},
        ).federation

        fact = plan.legs[0]
        assert "AVG(" not in fact.sql.upper()
        assert {"__avg0_sum", "__avg0_count"} <= dict(fact.columns).keys()
        assert fact.exact_sums == (("__avg0_sum", 9),)
        # Divided in integers, never through DuckDB's floating-point ``/``.
        assert "HUGEINT" in plan.combine_sql
        assert "//" in plan.combine_sql

    def test_an_integer_average_divides_at_scale_zero_and_a_float_one_not_exactly(self) -> None:
        plan = _compile(
            model_yaml(federated=True, extra=AVERAGES),
            {"select": {"dimensions": ["Region"], "measures": ["Average Id", "Average Float"]}},
        ).federation

        # Sorted by measure name: Average Float, then Average Id.
        assert plan.legs[0].exact_sums == (("__avg1_sum", 0),)


DISTINCTS = (
    "  Customers Ordering: {columns: [{dataObject: Orders, column: Order Customer}], "
    "aggregation: count_distinct, resultType: int}\n"
    "  Order Days: {columns: [{dataObject: Orders, column: Order Date}], "
    "aggregation: count, distinct: true, resultType: int}\n" + AVG
)
DISTINCT_MEASURES = ["Customers Ordering", "Order Days", "Revenue", "Average Order", "Top Order"]
DISTINCT_QUERIES = {
    "by_dimension_island": {"select": {"dimensions": ["Region"], "measures": DISTINCT_MEASURES}},
    "by_both_islands": {
        "select": {"dimensions": ["Order Date:month", "Region"], "measures": DISTINCT_MEASURES}
    },
    "filter_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": DISTINCT_MEASURES},
        "where": [{"field": "Region", "op": "!=", "value": "EU"}],
    },
    "no_dimensions_with_a_filter_on_the_dimension_side": {
        "select": {"measures": DISTINCT_MEASURES},
        "where": [{"field": "Region", "op": "in", "value": ["EU", "US"]}],
    },
    "having_ordered": {
        "select": {"dimensions": ["Region"], "measures": ["Customers Ordering", "Revenue"]},
        "having": [{"field": "Customers Ordering", "op": ">", "value": 0}],
        "orderBy": [{"field": "Customers Ordering", "direction": "desc"}, {"field": "Region"}],
    },
}


EXPRESSIONS = (
    '  Doubled Revenue: {expression: "{[Orders].[Amount]} * 2", aggregation: sum, '
    "resultType: decimal}\n"
    '  Average Doubled: {expression: "{[Orders].[Amount]} * 2", aggregation: avg, '
    "resultType: decimal}\n"
)


class TestExpressionMeasures:
    def test_a_measure_written_as_an_expression_is_computed_in_the_leg(
        self, files: dict[str, str]
    ) -> None:
        """It was taken for a metric and left out of the legs, so the combine
        step reported it unknown."""
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Doubled Revenue", "Average Doubled"]}
        }

        assert_same(files, query, extra=EXPRESSIONS)


class TestCountDistinct:
    """The fact leg groups by the counted column too; the combine step counts it."""

    @pytest.mark.parametrize("query", DISTINCT_QUERIES.values(), ids=DISTINCT_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(files, query, extra=DISTINCTS)

    def test_a_value_reaching_two_groups_is_counted_once(self, files: dict[str, str]) -> None:
        """Customer 10 ordered twice, on two dates: per date it is two leg
        rows, yet one customer for the region."""
        rows = dict(
            run_federated(
                files,
                {"select": {"dimensions": ["Region"], "measures": ["Customers Ordering"]}},
                extra=DISTINCTS,
            )
        )

        # EU: customers 10 and 13; US: 11; no region: 12 and the orphan 99.
        assert rows == {"EU": 2, "US": 1, None: 2}

    def test_the_leg_groups_by_the_counted_column(self) -> None:
        plan = _compile(
            model_yaml(federated=True, extra=DISTINCTS),
            {"select": {"dimensions": ["Region"], "measures": ["Customers Ordering"]}},
        ).federation

        fact = plan.legs[0]
        assert "__distinct0" in dict(fact.columns)
        # The combine step counts the leg's values, not the leg's own counts.
        assert 'COUNT(DISTINCT "leg_0"."__distinct0")' in plan.combine_sql


# Each order's status, under a collation that ignores case: one database
# orders 'b' before 'C', where the combine step, comparing bytes, puts it after.
COLLATED_ORDERS = """
CREATE TABLE orders AS SELECT order_id, customer_id, order_date, amount,
    status COLLATE NOCASE AS status
FROM (VALUES
    (1, 10, DATE '2024-01-05', 100.50, 'b'),
    (2, 10, DATE '2024-01-20', 20.25, 'C'),
    (3, 11, DATE '2024-02-03', 7.00, 'a'),
    (4, 12, DATE '2024-02-14', 55.10, 'B'),
    (5, 13, DATE '2024-03-01', 3.15, 'D'),
    (6, 99, DATE '2024-03-09', 40.00, 'e'),
    (7, 11, DATE '2024-03-30', 12.00, 'Z')
) t(order_id, customer_id, order_date, amount, status)
"""
STATUS = "      Status: {code: status, abstractType: string}\n"
STRING_BOUNDS = (
    "  Least Status: {columns: [{dataObject: Orders, column: Status}], aggregation: min}\n"
    "  Greatest Status: {columns: [{dataObject: Orders, column: Status}], aggregation: max}\n"
    "  Least Overall:\n"
    "    columns: [{dataObject: Orders, column: Status}]\n"
    "    aggregation: min\n"
    "    grain: {mode: FIXED}\n"
    "  Greatest Total:\n"
    "    {columns: [{dataObject: Orders, column: Status}], aggregation: max, total: true}\n"
)
BOUNDS = ["Least Status", "Greatest Status", "Revenue"]
# Per customer, the least status is 'b', 'a', 'B', 'D' and 'e', the greatest
# 'C', 'Z', 'B', 'D' and 'e': each region's or all of them, one database's
# differ from their bytewise least and greatest.
BOUND_QUERIES = {
    "by_region": {"select": {"dimensions": ["Region"], "measures": BOUNDS}},
    "rollup": {"select": {"dimensions": ["Region"], "measures": BOUNDS}, "grouping": "rollup"},
    "grain_and_total": {
        "select": {"dimensions": ["Region"], "measures": ["Least Overall", "Greatest Total"]}
    },
}


@pytest.fixture(scope="module")
def collated(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("collated")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [COLLATED_ORDERS, CUSTOMERS]),
        ("shop", [COLLATED_ORDERS]),
        ("crm", [CUSTOMERS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestStringsInTheirSourcesOrder:
    """A least or greatest string is its source's: the fact leg ranks the values."""

    @pytest.mark.parametrize("query", BOUND_QUERIES.values(), ids=BOUND_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, collated: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(collated, query, order_columns=STATUS, extra=STRING_BOUNDS)

    def test_the_fact_leg_ranks_its_strings(self) -> None:
        plan = _compile(
            model_yaml(federated=True, order_columns=STATUS, extra=STRING_BOUNDS),
            {"select": {"dimensions": ["Region"], "measures": [*BOUNDS, "Top Order"]}},
        ).federation

        fact = plan.legs[0]
        ranks = [c for c, kind in fact.columns if c.startswith("__rank")]
        # The strings alone: a decimal orders alike in every engine.
        assert len(ranks) == 2
        for measure in ("Least Status", "Greatest Status"):
            assert f'DENSE_RANK() OVER (ORDER BY "__leg"."{measure}" ASC)' in fact.sql
        assert 'arg_min("leg_0"."Least Status"' in plan.combine_sql
        assert 'arg_max("leg_0"."Greatest Status"' in plan.combine_sql
        assert 'MAX("leg_0"."Top Order")' in plan.combine_sql


# When each order was placed, written in UTC, and each customer signed up,
# written in New York: ``crm`` holds its customers in New York's wall-clock
# time, and ``all`` both tables in UTC.
PLACED = """
CREATE TABLE orders AS SELECT order_id, customer_id, order_date, amount,
    CAST(placed AS TIMESTAMP) AS placed_at
FROM (VALUES
    (1, 10, DATE '2024-01-05', 100.50, '2024-01-05 10:00:00'),
    (2, 10, DATE '2024-01-20', 20.25, '2024-01-31 23:30:00'),
    (3, 11, DATE '2024-02-03', 7.00, '2024-02-03 08:15:00'),
    (4, 12, DATE '2024-02-14', 55.10, '2024-03-30 23:10:00'),
    (5, 13, DATE '2024-03-01', 3.15, '2024-03-31 00:30:00'),
    (6, 99, DATE '2024-03-09', 40.00, '2024-03-09 12:00:00'),
    (7, 11, DATE '2024-03-30', 12.00, '2024-10-27 00:30:00')
) t(order_id, customer_id, order_date, amount, placed)
"""


def signed_up(rows: list[str]) -> str:
    return f"""
CREATE TABLE customers AS SELECT customer_id, region, tenant,
    CAST(signed AS TIMESTAMP) AS signed_up
FROM (VALUES
    (10, 'EU', 'acme', '{rows[0]}'),
    (11, 'US', 'acme', '{rows[1]}'),
    (12, NULL, 'acme', '{rows[2]}'),
    (13, 'EU', 'other', '{rows[3]}')
) t(customer_id, region, tenant, signed)
"""


SIGNED_UP_UTC = signed_up(
    ["2024-01-01 00:30:00", "2024-03-01 01:00:00", "2024-06-01 16:00:00", "2024-11-03 02:00:00"]
)
SIGNED_UP_NEW_YORK = signed_up(
    ["2023-12-31 19:30:00", "2024-02-29 20:00:00", "2024-06-01 12:00:00", "2024-11-02 22:00:00"]
)
ZONED = {
    "order_columns": "      Placed At: {code: placed_at, abstractType: timestamp}\n",
    "customer_columns": "      Signed Up: {code: signed_up, abstractType: timestamp}\n",
    "dimension": (
        "  Placed At: {dataObject: Orders, column: Placed At, resultType: timestamp}\n"
        "  Signed Up: {dataObject: Customers, column: Signed Up, resultType: timestamp}\n"
    ),
    "extra": (
        "  First Placed: {columns: [{dataObject: Orders, column: Placed At}], "
        "aggregation: min, resultType: timestamp}\n"
        "  Last Placed: {columns: [{dataObject: Orders, column: Placed At}], "
        "aggregation: max, resultType: timestamp}\n"
    ),
}
ZONED_QUERIES = {
    "by_day_and_region": {
        "select": {"dimensions": ["Placed At:day", "Region"], "measures": MEASURES}
    },
    "by_signup_month": {
        "select": {"dimensions": ["Signed Up:month"], "measures": ["Revenue", "First Placed"]}
    },
    "bounds_by_region": {
        "select": {"dimensions": ["Region"], "measures": ["First Placed", "Last Placed"]}
    },
    "as_stored": {"select": {"dimensions": ["Placed At", "Signed Up"], "measures": ["Revenue"]}},
    "filters_in_both_legs": {
        "select": {"dimensions": ["Placed At:month", "Region"], "measures": ["Revenue"]},
        "where": [
            {"field": "Placed At", "op": ">=", "value": "2024-02-01"},
            {"field": "Signed Up", "op": "<", "value": "2024-03-01 01:30:00"},
        ],
    },
}


@pytest.fixture(scope="module")
def placed(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("placed")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [PLACED, SIGNED_UP_UTC]),
        ("shop", [PLACED]),
        ("crm", [SIGNED_UP_NEW_YORK]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


@pytest.fixture
def zones(placed: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """``shop`` and ``crm`` configured, their sessions declared in UTC and New York."""
    monkeypatch.setenv("OBSL_DATABASES", "shop,crm")
    for name, zone in (("shop", "UTC"), ("crm", "America/New_York")):
        monkeypatch.setenv(f"DB_{name.upper()}_TYPE", "duckdb")
        monkeypatch.setenv(f"DB_{name.upper()}_DUCKDB_DATABASE", placed[name])
        monkeypatch.setenv(f"DB_{name.upper()}_TIMEZONE", zone)
    return placed


BERLIN = "  defaultTimezone: Europe/Berlin\n"
UTC_READ_IN_BERLIN = "  defaultTimezone: UTC\n  queryTimezone: Europe/Berlin\n"


class TestTimestampsInOneZone:
    """Each leg reads a timestamp without a zone in the query's, from its source's."""

    @pytest.mark.parametrize("query", ZONED_QUERIES.values(), ids=ZONED_QUERIES.keys())
    @pytest.mark.parametrize(
        "settings", [BERLIN, "  queryTimezone: Europe/Berlin\n"], ids=["default", "query"]
    )
    def test_federated_returns_what_one_source_in_the_query_zone_returns(
        self, zones: dict[str, str], query: dict[str, Any], settings: str
    ) -> None:
        # One database holding both tables in UTC, read in Berlin's time.
        single = run_single(zones, query, settings=UTC_READ_IN_BERLIN, **ZONED)
        federated = run_federated(zones, query, settings=settings, **ZONED)
        assert sorted(federated, key=repr) == sorted(single, key=repr)
        assert single

    def test_each_leg_converts_from_its_source_zone(self, zones: dict[str, str]) -> None:
        compiled = _compile(
            model_yaml(federated=True, settings=BERLIN, **ZONED),
            ZONED_QUERIES["as_stored"],
        )
        plan = compiled.federation
        shop, crm = (leg.sql for leg in plan.legs)
        assert """"placed_at" AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Berlin'""" in shop
        assert """"signed_up" AT TIME ZONE 'America/New_York' AT TIME ZONE 'Europe/Berlin'""" in crm
        assert plan.timezone == "Europe/Berlin"
        assert not compiled.warnings

    def test_a_leg_reading_keys_given_converts_too(self, zones: dict[str, str]) -> None:
        plan = _compile(
            model_yaml(federated=True, settings=BERLIN, **ZONED),
            {"select": {"dimensions": ["Placed At"], "measures": ["Revenue"]}, "where": [EU]},
        ).federation
        assert plan.injections
        injected = plan.inject({plan.injections[0].column: [10, 13]}).sql
        assert "AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Berlin'" in injected

    def test_a_source_in_the_query_zone_converts_nothing(
        self, zones: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another name of UTC is UTC.
        monkeypatch.setenv("DB_SHOP_TIMEZONE", "Etc/UTC")
        plan = _compile(
            model_yaml(federated=True, settings="  defaultTimezone: UTC\n", **ZONED),
            ZONED_QUERIES["as_stored"],
        ).federation
        shop, crm = (leg.sql for leg in plan.legs)
        assert "AT TIME ZONE" not in shop
        assert "AT TIME ZONE 'America/New_York' AT TIME ZONE 'UTC'" in crm
        assert plan.timezone == "UTC"

    @pytest.mark.parametrize(
        ("settings", "converted", "label"),
        [
            (
                UTC_READ_IN_BERLIN,
                "AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Berlin'",
                "Europe/Berlin",
            ),
            ("  defaultTimezone: UTC\n  overrideDatabaseTimezone: true\n", None, None),
        ],
        ids=["query_zone", "override"],
    )
    def test_a_model_stating_the_zone_of_its_timestamps_leads(
        self, zones: dict[str, str], settings: str, converted: str | None, label: str | None
    ) -> None:
        # Every leg reads them as the model says, whatever its source's sessions run in.
        plan = _compile(
            model_yaml(federated=True, settings=settings, **ZONED), ZONED_QUERIES["as_stored"]
        ).federation
        crm = plan.legs[1].sql
        assert "America/New_York" not in crm
        assert (converted in crm) if converted else "AT TIME ZONE" not in crm
        assert plan.timezone == label

    def test_a_query_zone_leads_the_default_one(self, zones: dict[str, str]) -> None:
        # Without a zone they were written in, from each source's.
        plan = _compile(
            model_yaml(federated=True, settings="  queryTimezone: Asia/Tokyo\n", **ZONED),
            ZONED_QUERIES["as_stored"],
        ).federation
        shop, crm = (leg.sql for leg in plan.legs)
        assert "AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Tokyo'" in shop
        assert "AT TIME ZONE 'America/New_York' AT TIME ZONE 'Asia/Tokyo'" in crm
        assert plan.timezone == "Asia/Tokyo"

    def test_a_source_whose_zone_is_not_known_is_named(self) -> None:
        # Neither source is configured here.
        compiled = _compile(
            model_yaml(federated=True, settings=BERLIN, **ZONED), ZONED_QUERIES["as_stored"]
        )
        assert all("AT TIME ZONE" not in leg.sql for leg in compiled.federation.legs)
        unknown = [w for w in compiled.warnings if w.code == "UNDECLARED_TIMESTAMP_ZONE"]
        assert [w.context["dataSource"] for w in unknown] == ["shop", "crm"]
        assert "not converted to Europe/Berlin" in unknown[0].message

    def test_a_leg_without_timestamps_looks_no_zone_up(
        self, zones: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orionbelt.service import datasource_selection

        asked: list[str | None] = []
        real = datasource_selection.source_timezone

        def counting(name: str | None, dialect: str) -> str | None:
            asked.append(name)
            return real(name, dialect)

        monkeypatch.setattr(datasource_selection, "source_timezone", counting)
        _compile(
            model_yaml(federated=True, settings=BERLIN, **ZONED),
            {"select": {"dimensions": ["Placed At", "Region"], "measures": ["Revenue"]}},
        )
        assert asked == ["shop"]


A = "{dataObject: Orders, column: Amount}"
ON_DATE = "withinGroup: {column: {dataObject: Orders, column: Order Date}}"
FROM_ROWS = (
    f"  Median Order: {{columns: [{A}], aggregation: median}}\n"
    '  Median Share: {expression: "{[Orders].[Amount]} / {[Orders].[Order ID]}", '
    "aggregation: median, resultType: float}\n"
    f"  P90 Order: {{columns: [{A}], aggregation: percentile_cont, percentile: 0.9}}\n"
    f"  P25 Order: {{columns: [{A}], aggregation: percentile_disc, percentile: 0.25}}\n"
    "  Commonest Customer: {columns: [{dataObject: Orders, column: Order Customer}], "
    "aggregation: mode, resultType: int}\n"
    f"  First Amount: {{columns: [{A}], aggregation: first, {ON_DATE}}}\n"
    f"  Last Amount: {{columns: [{A}], aggregation: last, {ON_DATE}}}\n"
    "  Order IDs: {columns: [{dataObject: Orders, column: Order ID}], aggregation: listagg, "
    "delimiter: ', ', withinGroup: {column: {dataObject: Orders, column: Order ID}}, "
    "resultType: string}\n"
    f"  Amount Spread: {{columns: [{A}], aggregation: variance, resultType: float}}\n"
    "  Id Amount Corr: {columns: [{dataObject: Orders, column: Order ID}, "
    f"{A}], aggregation: corr, resultType: float}}\n"
    f"  Distinct Amounts: {{columns: [{A}], aggregation: sum, distinct: true}}\n"
    f"  Total Average: {{columns: [{A}], aggregation: avg, total: true}}\n"
    f"  Total Median: {{columns: [{A}], aggregation: median, total: true}}\n"
    "  Big Customers: {columns: [{dataObject: Orders, column: Order Customer}], "
    f"aggregation: count_distinct, resultType: int, filters: [{{column: {A}, operator: gt, "
    "values: [{dataType: int, valueInt: 10}]}]}\n"
    "  Customers With Total: {columns: [{dataObject: Orders, column: Order Customer}], "
    "aggregation: count_distinct, resultType: int, total: true}\n"
    '  Shifted Customers: {expression: "{[Orders].[Order Customer]} + 1", '
    "aggregation: count_distinct, resultType: int}\n"
    '  Shifted Customer Count: {expression: "{[Orders].[Order Customer]} + 1", '
    "aggregation: count, distinct: true, resultType: int}\n"
)
ROW_MEASURES = [
    "Median Order",
    "Median Share",
    "P90 Order",
    "P25 Order",
    "Commonest Customer",
    "First Amount",
    "Last Amount",
    "Order IDs",
    "Amount Spread",
    "Id Amount Corr",
    "Distinct Amounts",
    "Total Average",
    "Total Median",
    "Big Customers",
    "Customers With Total",
    "Shifted Customers",
    "Shifted Customer Count",
]
ROW_QUERIES = {
    "by_dimension_island": {
        "select": {
            "dimensions": ["Region"],
            "measures": [*ROW_MEASURES, *MEASURES, "Customers Ordering"],
        }
    },
    "by_both_islands": {
        "select": {"dimensions": ["Order Date:month", "Region"], "measures": ROW_MEASURES}
    },
    "filter_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": [*ROW_MEASURES, *MEASURES]},
        "where": [EU],
    },
    "null_filter_on_dimension_island": {
        "select": {"dimensions": ["Order Date:month"], "measures": ROW_MEASURES},
        "where": [{"field": "Region", "op": "is_null"}],
    },
    "filter_across_islands": {
        "select": {"dimensions": ["Region"], "measures": ROW_MEASURES},
        "where": [
            {
                "logic": "or",
                "filters": [EU, {"field": "Order Date", "op": ">=", "value": "2024-03-01"}],
            }
        ],
    },
    "no_dimensions_with_a_filter_on_the_dimension_side": {
        "select": {"measures": [*ROW_MEASURES, *MEASURES]},
        "where": [{"field": "Region", "op": "in", "value": ["EU", "US"]}],
    },
    "having_ordered_limited": {
        "select": {"dimensions": ["Region"], "measures": ["Median Order", "Revenue"]},
        "having": [{"field": "Median Order", "op": ">", "value": 10}],
        "orderBy": [{"field": "Median Order", "direction": "desc"}],
        "limit": 2,
    },
    "rollup": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Median Order", "Revenue", "Orders Count"],
        },
        "grouping": "rollup",
    },
}


class TestFromRows:
    """What does not combine from partial results is computed from the fact leg's rows."""

    @pytest.mark.parametrize("query", ROW_QUERIES.values(), ids=ROW_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(files, query, extra=FROM_ROWS + DISTINCTS)

    def test_a_derived_metric_over_a_median(self, files: dict[str, str]) -> None:
        query = {"select": {"dimensions": ["Region"], "measures": ["Median Of Revenue"]}}
        metric = '  Median Of Revenue: {expression: "{[Median Order]} / {[Revenue]}"}\n'

        assert_same(files, query, extra=FROM_ROWS, metric=metric)

    def test_the_models_numeric_type_applies_to_the_combined_values(
        self, files: dict[str, str]
    ) -> None:
        """A sum of 0.1239 came back 0.12 beside a median, and a metric over
        partial results as well: the combine model had no
        ``defaultNumericDataType``."""
        extra = FROM_ROWS + (
            '  Milli Revenue: {expression: "{[Orders].[Amount]} * 0.001", aggregation: sum}\n'
        )
        metric = '  Milli Doubled: {expression: "{[Milli Revenue]} * 2"}\n'
        settings = "  defaultNumericDataType: decimal(18, 4)\n"
        both = ["Milli Revenue", "Median Order"]
        for measures in (both, ["Milli Doubled"]):
            query = {"select": {"dimensions": ["Region"], "measures": measures}}
            assert_same(files, query, extra=extra, metric=metric, settings=settings)

        query = {"select": {"dimensions": ["Region"], "measures": both}}
        rows = {r[0]: r[1] for r in run_federated(files, query, extra=extra, settings=settings)}
        assert rows["EU"] == Decimal("0.1239")

    def test_the_leg_counts_the_rows_of_each_group_of_values(self) -> None:
        compiled = _compile(
            model_yaml(federated=True, extra=FROM_ROWS + DISTINCTS),
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Median Order", "First Amount", "Customers Ordering"],
                }
            },
        )
        fact = compiled.federation.legs[0]

        assert fact.rows == "__rows"
        assert "MEDIAN" not in fact.sql.upper()
        # Each column read once: the amount for both measures, the date the
        # first one is ordered by, and the counted customer apart.
        assert set(dict(fact.columns)) == {
            "__fk0_0",
            "__distinct0",
            "__value0",
            "__value1",
            "__rows",
        }
        assert 'range(CAST(COALESCE("__leg"."__rows", 0) AS BIGINT))' in compiled.sql
        assert 'FROM "leg_0_rows" AS "leg_0"' in compiled.sql
        assert compiled.warnings == []

    def test_a_query_combining_from_partial_results_returns_none(self) -> None:
        plan = _compile(
            model_yaml(federated=True, extra=FROM_ROWS),
            {"select": {"dimensions": ["Region"], "measures": MEASURES}},
        ).federation

        assert plan.legs[0].rows is None
        assert "leg_0_rows" not in plan.combine_sql

    def test_injected_keys_keep_the_leg_returning_rows(self) -> None:
        plan = _compile(
            model_yaml(federated=True, extra=FROM_ROWS),
            {"select": {"dimensions": ["Order Date"], "measures": ["Median Order"]}, "where": [EU]},
        ).federation

        injected = plan.inject({"__fk0_0": [10, 13]})

        assert injected.rows == plan.legs[0].rows == "__rows"
        assert "IN (10, 13)" in injected.sql


IN_BERLIN = """\
version: 1.0
settings:
  defaultDialect: duckdb
  queryTimezone: Europe/Berlin
dataObjects:
  Readings:
    code: readings
{facts}    columns:
      Key: {{code: k, abstractType: int}}
      Taken: {{code: taken, abstractType: timestamp_tz}}
      Value: {{code: v, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Sites, columnsFrom: [Key], columnsTo: [Site Key]}}
  Sites:
    code: sites
{labels}    columns:
      Site Key: {{code: k, abstractType: int}}
      Site: {{code: s, abstractType: string}}
dimensions:
  Site: {{dataObject: Sites, column: Site, resultType: string}}
measures:
  First Value:
    columns: [{{dataObject: Readings, column: Value}}]
    aggregation: first
    withinGroup: {{column: {{dataObject: Readings, column: Taken}}}}
    resultType: int
  Last Value:
    columns: [{{dataObject: Readings, column: Value}}]
    aggregation: last
    withinGroup: {{column: {{dataObject: Readings, column: Taken}}}}
    resultType: int
  Earliest:
    columns: [{{dataObject: Readings, column: Taken}}]
    aggregation: min
    resultType: timestamp
"""
# 00:30 and 01:30 UTC are both 02:30 in Berlin the night it falls back.
READINGS = (
    "CREATE TABLE readings AS SELECT * FROM (VALUES"
    " (1, TIMESTAMPTZ '2024-10-27 00:30:00+00', 100),"
    " (1, TIMESTAMPTZ '2024-10-27 01:30:00+00', 1)) t(k, taken, v)"
)
SITES = "CREATE TABLE sites AS SELECT * FROM (VALUES (1, 'north')) t(k, s)"


@pytest.fixture(scope="module")
def zoned(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("zoned")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "facts", "labels")}
    for name, statements in (
        ("all", [READINGS, SITES]),
        ("facts", [READINGS]),
        ("labels", [SITES]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestFromRowsInTheQueryZone:
    """A first or last is ordered by its key in the column's own time, as on one source."""

    def _run(self, zoned: dict[str, str], federated: bool, query: dict[str, Any]) -> list[Any]:
        sources = "    dataSource: facts\n", "    dataSource: labels\n"
        facts, labels = sources if federated else ("", "")
        compiled = _compile(IN_BERLIN.format(facts=facts, labels=labels), query)
        if compiled.federation is None:
            con = duckdb.connect(zoned["all"], read_only=True)
            try:
                return _rows(con, compiled.sql)
            finally:
                con.close()
        combine = legs_registered(zoned, compiled.federation)
        return _rows(combine, compiled.sql)

    def test_two_instants_of_one_wall_clock_time_keep_their_order(
        self, zoned: dict[str, str]
    ) -> None:
        """Read in Berlin time, both readings were 02:30, and the tie went to
        the smaller value: the first came back 1, not 100."""
        query = {
            "select": {
                "dimensions": ["Site"],
                "measures": ["First Value", "Last Value", "Earliest"],
            }
        }

        single = self._run(zoned, False, query)

        assert self._run(zoned, True, query) == single
        assert single[0][1:3] == (100, 1)

    def test_injected_keys_read_the_key_in_its_own_time_too(self) -> None:
        yaml = IN_BERLIN.format(facts="    dataSource: facts\n", labels="    dataSource: labels\n")
        query = {
            "select": {"dimensions": ["Site"], "measures": ["First Value", "Earliest"]},
            "where": [{"field": "Site", "op": "=", "value": "north"}],
        }
        plan = _compile(yaml, query).federation

        for leg in (plan.legs[0], plan.inject({"__fk0_0": [1]})):
            # The minimum reads the reading in Berlin time, the ordering as stored.
            assert '"Readings"."taken" AT TIME ZONE \'Europe/Berlin\'' in leg.sql
            assert '"Readings"."taken" AS' in leg.sql


class TestPlan:
    def test_a_query_on_one_source_is_not_federated(self) -> None:
        compiled = _compile(
            model_yaml(federated=True),
            {"select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]}},
        )

        assert compiled.federation is None

    def test_a_query_without_dimensions_reads_one_source(self) -> None:
        compiled = _compile(model_yaml(federated=True), {"select": {"measures": MEASURES}})

        assert compiled.federation is None

    def test_legs_name_their_source_and_objects(self) -> None:
        plan = _compile(
            model_yaml(federated=True),
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}},
        ).federation

        assert [(leg.name, leg.data_source, leg.data_objects) for leg in plan.legs] == [
            ("leg_0", "shop", ("Orders",)),
            ("leg_1", "crm", ("Customers",)),
        ]
        assert plan.combine_dialect == "duckdb"

    def test_legs_name_the_joins_whose_keys_they_return(self) -> None:
        # What makes a leg as large as it is: a row per customer, whatever the regions.
        plan = _compile(
            model_yaml(federated=True),
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}, "where": [EU]},
        ).federation
        join = "Orders.Order Customer -> Customers.Customer ID (between 'shop' and 'crm')"

        assert [leg.joins for leg in plan.legs] == [(join,), (join,)]
        assert plan.inject({plan.injections[0].column: [10]}).joins == (join,)

    def test_a_leg_of_a_chain_names_both_its_joins(self) -> None:
        plan = _compile(
            chain_yaml("shop", "crm", "geo"),
            {"select": {"dimensions": ["Region Name"], "measures": ["Revenue"]}},
        ).federation
        orders = "Orders.Order Customer -> Customers.Customer ID (between 'shop' and 'crm')"
        regions = "Customers.Customer Region -> Regions.Region ID (between 'crm' and 'geo')"

        assert {leg.data_source: leg.joins for leg in plan.legs} == {
            "shop": (orders,),
            "crm": (orders, regions),
            "geo": (regions,),
        }

    def test_raw_legs_and_parts_name_their_joins_too(self) -> None:
        join = "Orders.Order Customer -> Customers.Customer ID (between 'shop' and 'crm')"
        raw = _compile(
            model_yaml(federated=True),
            {"select": {"fields": ["Orders.Amount", "Customers.Region"]}},
        ).federation
        parts = _compile(
            independent_model(True, tickets="shop", **INDEPENDENT_MODEL), {"select": BY_REGION_BOTH}
        ).federation

        assert [leg.joins for leg in raw.legs] == [(join,), (join,)]
        assert {leg.name: leg.joins for leg in parts.legs}["__part0_leg_0"] == (join,)
        # A leg of a query of fields returns rows, which no join placement narrows.
        assert all(leg.raw for leg in raw.legs)
        assert not any(leg.raw for leg in parts.legs)

    def test_a_filter_on_the_fact_side_runs_in_its_leg(self) -> None:
        plan = _compile(
            model_yaml(federated=True),
            {
                "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
                "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
            },
        ).federation

        assert "2024-02-01" in plan.legs[0].sql
        assert "2024-02-01" not in plan.combine_sql

    def test_a_filter_true_for_null_on_the_dimension_side_runs_only_in_the_combine_step(
        self,
    ) -> None:
        plan = _compile(
            model_yaml(federated=True),
            {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
                "where": [{"field": "Region", "op": "is_null"}],
            },
        ).federation

        assert "IS NULL" not in plan.legs[1].sql
        assert "IS NULL" in plan.combine_sql

    def test_a_filter_false_for_null_on_the_dimension_side_runs_in_its_leg(self) -> None:
        """The combine step keeps the fact rows the filtered leg matched, and
        joins it although the query reads nothing else there."""
        plan = _compile(
            model_yaml(federated=True),
            {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
                "where": [{"field": "Region", "op": "=", "value": "EU"}],
            },
        ).federation

        assert "'EU'" in plan.legs[1].sql
        assert "'EU'" not in plan.combine_sql
        assert '"leg_1"."__fk0_0" IS NOT NULL' in plan.combine_sql

    @pytest.mark.parametrize(
        ("item", "in_leg"),
        [
            ({"field": "Region", "op": "!=", "value": "EU"}, True),
            ({"field": "Region", "op": "notblank"}, True),
            ({"field": "Region", "op": "blank"}, False),
            (
                {"logic": "and", "filters": [{"field": "Region", "op": "is_null"}, EU]},
                True,
            ),
            ({"logic": "or", "filters": [{"field": "Region", "op": "is_null"}, EU]}, False),
            ({"negated": True, "filters": [{"field": "Region", "op": "is_null"}]}, True),
            ({"negated": True, "filters": [{"field": "Region", "op": "is_not_null"}]}, False),
            (
                {"negated": True, "filters": [{"logic": "or", "filters": []}]},
                False,
            ),
        ],
        ids=[
            "not_equal",
            "not_blank",
            "blank",
            "and_with_null",
            "or_with_null",
            "not_null",
            "not_set",
            "negated_no_condition",
        ],
    )
    def test_a_dimension_side_filter_runs_in_its_leg_when_false_or_unknown_for_null(
        self, item: dict[str, Any], in_leg: bool
    ) -> None:
        plan = _compile(
            model_yaml(federated=True),
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}, "where": [item]},
        ).federation

        assert ("WHERE" in plan.legs[1].sql) is in_leg
        assert ('"leg_1"."__fk0_0" IS NOT NULL' in plan.combine_sql) is in_leg

    @pytest.mark.parametrize(
        ("order_key", "customer_key", "injected"),
        [
            ("int", "int", ["int"]),
            ("string", "string", ["string"]),
            ("string", "int", []),
            ("date", "date", []),
        ],
        ids=["integers", "strings", "two_types", "dates"],
    )
    def test_a_filtered_dimension_legs_keys_of_one_type_may_filter_the_fact_leg(
        self, order_key: str, customer_key: str, injected: list[str]
    ) -> None:
        yaml = (
            model_yaml(federated=True)
            .replace(
                "Order Customer: {code: customer_id, abstractType: int}",
                f"Order Customer: {{code: customer_id, abstractType: {order_key}}}",
            )
            .replace(
                "Customer ID: {code: customer_id, abstractType: int}",
                f"Customer ID: {{code: customer_id, abstractType: {customer_key}}}",
            )
        )

        plan = _compile(
            yaml,
            {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
                "where": [EU],
            },
        ).federation

        assert [(i.leg, i.abstract_type) for i in plan.injections] == [
            ("leg_1", kind) for kind in injected
        ]
        assert (plan.inject is not None) is bool(injected)

    @pytest.mark.parametrize(
        "where",
        [[], [{"field": "Region", "op": "is_null"}]],
        ids=["no_filter", "a_filter_true_for_null"],
    )
    def test_an_unfiltered_dimension_leg_filters_no_fact_leg(
        self, where: list[dict[str, Any]]
    ) -> None:
        """Its fact rows without a key there count too."""
        plan = _compile(
            model_yaml(federated=True),
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}, "where": where},
        ).federation

        assert plan.injections == ()
        assert plan.inject is None

    def test_a_key_of_two_columns_filters_no_fact_leg(self) -> None:
        yaml = (
            model_yaml(federated=True)
            .replace(
                "      Order Date: {code: order_date, abstractType: date}\n",
                "      Order Date: {code: order_date, abstractType: date}\n"
                "      Order Tenant: {code: tenant, abstractType: string}\n",
            )
            .replace(
                "columnsFrom: [Order Customer]\n        columnsTo: [Customer ID]",
                "columnsFrom: [Order Customer, Order Tenant]\n"
                "        columnsTo: [Customer ID, Tenant]",
            )
        )

        plan = _compile(
            yaml,
            {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
                "where": [EU],
            },
        ).federation

        assert len(plan.legs) == 2
        assert plan.injections == ()

    def test_injected_keys_are_written_as_the_fact_legs_literals(self) -> None:
        yaml = (
            model_yaml(federated=True)
            .replace(
                "Order Customer: {code: customer_id, abstractType: int}",
                "Order Customer: {code: customer_id, abstractType: string}",
            )
            .replace(
                "Customer ID: {code: customer_id, abstractType: int}",
                "Customer ID: {code: customer_id, abstractType: string}",
            )
        )
        plan = _compile(
            yaml,
            {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
                "where": [EU],
            },
        ).federation
        assert plan.inject is not None

        sql = plan.inject({"__fk0_0": ["a", "it's"]}).sql

        assert "\"Orders\".\"customer_id\" IN ('a', 'it''s')" in sql
        assert sql.endswith("LIMIT 5000001")

    def test_a_query_changed_after_planning_leaves_the_injected_fact_leg_as_planned(
        self,
    ) -> None:
        store = ModelStore()
        model_id = store.load_model(model_yaml(federated=True)).model_id
        query = QueryObject.model_validate(
            {
                "select": {"measures": ["Revenue"]},
                "where": [EU, {"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
            }
        )
        plan = store.compile_query(model_id, query, "duckdb").federation
        assert plan.inject is not None

        query.where[1].value = "2024-01-01"

        assert "'2024-02-01'" in plan.inject({"__fk0_0": [13]}).sql

    def test_a_cached_plan_that_injects_keys_lets_its_model_go(self) -> None:
        """The compilation cache drops a model's entries once nothing else holds it."""
        store = ModelStore()
        model = store.get_model(store.load_model(model_yaml(federated=True), dedup=False).model_id)
        del store
        cache = CompilationCache(max_entries=8)
        query = QueryObject.model_validate({"select": {"measures": ["Revenue"]}, "where": [EU]})
        plan = cache.compile(CompilationPipeline(), query, model, "duckdb").federation
        assert plan.inject is not None
        assert cache.stats().entries == 1
        collected = weakref.ref(model)

        del model
        gc.collect()

        assert collected() is None
        assert cache.stats().entries == 0

    def test_a_relative_date_filter_on_the_dimension_side_runs_in_its_leg(self) -> None:
        """Refused while it ran in the combine step; its leg runs it as one
        database would."""
        item = {
            "field": "Customer Since",
            "op": "relative",
            "value": {"unit": "day", "count": 30, "direction": "past"},
        }
        yaml = (
            model_yaml(federated=True)
            .replace(
                "      Tenant: {code: tenant, abstractType: string}\n",
                "      Tenant: {code: tenant, abstractType: string}\n"
                "      Since: {code: since, abstractType: date}\n",
            )
            .replace(
                "dimensions:\n",
                "dimensions:\n"
                "  Customer Since: {dataObject: Customers, column: Since, resultType: date}\n",
            )
        )

        plan = _compile(
            yaml, {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}, "where": [item]}
        ).federation

        assert '"since"' in plan.legs[1].sql
        assert "since" not in plan.combine_sql.lower()


ON_CUSTOMERS = (
    "  Customer Rows: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: count, resultType: int}\n"
)

# One database counts the customers in a query of their own, without the
# query's filters.
UNFILTERED_CUSTOMERS = (
    "  All Customer Rows: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: count, resultType: int, filterContext: {mode: FIXED}}\n"
)

REFUSALS = {
    "customers_without_orders_a_filter_on_the_orders_picks": (
        ON_CUSTOMERS,
        {
            "select": {"dimensions": ["Region"], "measures": ["Revenue", "Customer Rows"]},
            "where": [{"field": "Order Date", "op": "is_null"}],
        },
        "FEDERATION_MULTI_FACT",
    ),
    "a_customer_measure_with_a_filter_context": (
        UNFILTERED_CUSTOMERS,
        {"select": {"dimensions": ["Region"], "measures": ["Revenue", "All Customer Rows"]}},
        "FEDERATION_MULTI_FACT",
    ),
}


def _either(*filters: dict[str, Any]) -> dict[str, Any]:
    return {"logic": "or", "filters": list(filters)}


US = {"field": "Region", "op": "=", "value": "US"}
# Every order is of 2024: the past fifty years hold them all, the next thirty
# days none, whichever day the tests run.
PAST_YEARS = {"field": "Order Date", "op": "relative", "value": {"unit": "year", "count": 50}}
NEXT_DAYS = {
    "field": "Order Date",
    "op": "relative",
    "value": {"unit": "day", "count": 30, "direction": "future"},
}
LATE = {"field": "Order Date", "op": ">=", "value": "2024-02-14"}
ORDER_MONTH = (
    "  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}\n"
)
AFTER_THE_JOIN = {
    "relative_or_another_source": {
        "select": {"dimensions": ["Region"], "measures": MEASURES},
        "where": [_either(NEXT_DAYS, US)],
    },
    "relative_holding_for_all": {
        "select": {"dimensions": ["Order Date:month"], "measures": MEASURES},
        "where": [_either(PAST_YEARS, EU)],
    },
    "relative_beside_a_condition_true_for_null": {
        "select": {"dimensions": ["Order Date:month", "Region"], "measures": MEASURES},
        "where": [_either({"field": "Region", "op": "is_null"}, NEXT_DAYS)],
    },
    "on_a_dimension_selected_at_a_grain": {
        "select": {"dimensions": ["Order Date:month"], "measures": MEASURES},
        "where": [_either(LATE, EU)],
    },
    "on_a_dimension_selected_at_a_grain_without_measures": {
        "select": {"dimensions": ["Order Date:month", "Region"]},
        "where": [_either(LATE, EU)],
    },
    "on_a_dimension_selected_at_a_grain_from_rows": {
        "select": {"dimensions": ["Order Date:quarter"], "measures": ["Median Order"]},
        "where": [_either(LATE, EU)],
    },
}


BY_MONTH_AND_REGION = {
    "select": {"dimensions": ["Order Date:month", "Region"], "measures": ["Revenue"]}
}
STRING_KEYS = {
    "Order Customer: {code: customer_id, abstractType: int}": (
        "Order Customer: {code: customer_id, abstractType: string}"
    ),
    "Customer ID: {code: customer_id, abstractType: int}": (
        "Customer ID: {code: customer_id, abstractType: string}"
    ),
}


class TestKeyMaps:
    """The fact leg joining a dimension leg as a map of its keys (``KeyMap``)."""

    def test_the_fact_leg_groups_by_each_keys_ordinal_in_the_keys_column(self) -> None:
        plan = _compile(model_yaml(federated=True), BY_MONTH_AND_REGION).federation

        assert plan.key_maps == (KeyMap("leg_1", "__fk0_0"),)
        assert plan.injections == ()
        assert plan.inject is not None
        sql = plan.inject({}, {"__fk0_0": [(10, 1), (11, 2), (13, 1)]}).sql
        assert '"__map0"."n" AS "__fk0_0"' in sql
        assert (
            'LEFT JOIN (SELECT * FROM (VALUES (10, 1), (11, 2), (13, 1)) AS "m"("k", "n")) '
            'AS "__map0" ON "Orders"."customer_id" = "__map0"."k"'
        ) in sql
        assert plan.inject({}).sql == plan.legs[0].sql

    def test_a_filtered_dimension_legs_map_is_the_only_keys_the_fact_leg_reads(self) -> None:
        """The combine step keeps only the fact rows the leg matched: an inner
        join in the fact leg keeps the same ones."""
        query = {
            "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
            "where": [EU],
        }
        plan = _compile(model_yaml(federated=True), query).federation

        assert [m.leg for m in plan.key_maps] == [i.leg for i in plan.injections] == ["leg_1"]
        sql = plan.inject({}, {"__fk0_0": [(10, 1), (13, 1)]}).sql
        assert 'INNER JOIN (SELECT * FROM (VALUES (10, 1), (13, 1)) AS "m"("k", "n"))' in sql

    @pytest.mark.parametrize(
        ("yaml", "query"),
        [
            (
                model_yaml(federated=True),
                {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}},
            ),
            (
                functools.reduce(
                    lambda text, pair: text.replace(*pair),
                    STRING_KEYS.items(),
                    model_yaml(federated=True),
                ),
                BY_MONTH_AND_REGION,
            ),
            (
                model_yaml(federated=True, extra=FROM_ROWS),
                {
                    "select": {
                        "dimensions": ["Order Date:month", "Region"],
                        "measures": ["Median Order"],
                    }
                },
            ),
            (
                model_yaml(federated=True, extra=DISTINCTS),
                {
                    "select": {
                        "dimensions": ["Order Date:month", "Region"],
                        "measures": ["Customers Ordering"],
                    }
                },
            ),
            (
                model_yaml(
                    federated=True,
                    dimension=(
                        "  Customer Number: "
                        "{dataObject: Orders, column: Order Customer, resultType: int}\n"
                    ),
                ),
                {"select": {"dimensions": ["Customer Number", "Region"], "measures": ["Revenue"]}},
            ),
        ],
        ids=[
            "grouped_by_the_key_alone",
            "a_string_key",
            "a_leg_of_rows",
            "a_count_of_distinct_keys",
            "a_dimension_on_the_key",
        ],
    )
    def test_no_map_where_it_would_not_group_the_leg_by_less_or_the_key_is_read_otherwise(
        self, yaml: str, query: dict[str, Any]
    ) -> None:
        plan = _compile(yaml, query).federation

        assert plan is not None
        assert plan.key_maps == ()

    @pytest.mark.parametrize("grouping", [None, "rollup", "cube"], ids=["groups", "rollup", "cube"])
    def test_a_mapped_fact_leg_combines_as_one_source_does(
        self, files: dict[str, str], grouping: str | None
    ) -> None:
        query = {
            "select": {"dimensions": ["Region", "Order Date:month"], "measures": MEASURES},
            **({"grouping": grouping} if grouping else {}),
        }
        assert _compile(model_yaml(federated=True), query).federation.key_maps

        assert_same(files, query)

    def test_strings_a_mapped_fact_leg_ranks_keep_their_sources_order(
        self, collated: dict[str, str]
    ) -> None:
        query = {"select": {"dimensions": ["Region", "Order Date:month"], "measures": BOUNDS}}
        model = {"order_columns": STATUS, "extra": STRING_BOUNDS}
        assert _compile(model_yaml(federated=True, **model), query).federation.key_maps

        assert_same(collated, query, **model)

    def test_no_map_of_keys_a_nonexists_leg_joins_on(self) -> None:
        query = {**BY_MONTH_AND_REGION, "where": [_tickets("nonexists")]}

        plan = _compile(subquery_model(True), query).federation

        assert plan.key_maps == ()

    @pytest.mark.parametrize(
        "dialect",
        [
            "postgres",
            "mysql",
            "clickhouse",
            "duckdb",
            "snowflake",
            "bigquery",
            "databricks",
            "dremio",
        ],
    )
    def test_each_engine_measured_joins_a_map(
        self, dialect: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """5 million groups by key against a map to 10 to 1,000 groups
        (``design/PLAN_federation.md`` §14.3): faster on every engine but
        ClickHouse and DuckDB, where the leg still shrinks to a row per group.
        An engine not measured joins none."""
        query = QueryObject.model_validate(BY_MONTH_AND_REGION)

        def plan_for() -> Any:
            store = ModelStore()
            model_id = store.load_model(model_yaml(federated=True)).model_id
            return store.compile_query(model_id, query, dialect).federation

        plan = plan_for()
        monkeypatch.setattr(type(DialectRegistry.get(dialect)), "pushes_key_maps", False)
        unmeasured = plan_for()

        assert plan.legs[0].dialect == dialect
        assert plan.key_maps == (KeyMap("leg_1", "__fk0_0"),)
        assert unmeasured.key_maps == ()

    @pytest.mark.parametrize(
        ("sql_type", "mapped"),
        [(None, True), ("UInt64", True), ("Nullable(UInt128)", False), ("Int256", False)],
        ids=["undeclared", "64_bit", "128_bit", "256_bit"],
    )
    def test_a_clickhouse_key_too_wide_for_the_maps_keys_joins_no_map(
        self, sql_type: str | None, mapped: bool
    ) -> None:
        """A ClickHouse join needs a common type: the map's ``Decimal(38, 0)``
        keys have one with every integer up to 64 bits and every decimal."""
        declared = f", sqlType: '{sql_type}'" if sql_type else ""
        yaml = model_yaml(federated=True).replace(
            "Order Customer: {code: customer_id, abstractType: int}",
            f"Order Customer: {{code: customer_id, abstractType: int{declared}}}",
        )
        store = ModelStore()
        model_id = store.load_model(yaml).model_id
        query = QueryObject.model_validate(BY_MONTH_AND_REGION)

        plan = store.compile_query(model_id, query, "clickhouse").federation

        assert bool(plan.key_maps) is mapped
        if mapped:
            sql = plan.inject({}, {"__fk0_0": [(10, 1)]}).sql
            assert "values('k Decimal(38, 0), n UInt32', (10, 1))" in sql

    def test_the_map_is_named_apart_from_the_legs_data_objects(self) -> None:
        yaml = functools.reduce(
            lambda text, pair: text.replace(*pair),
            [("  Orders:\n", "  __map0:\n"), ("Orders", "__map0")],
            model_yaml(federated=True),
        )
        plan = _compile(yaml, BY_MONTH_AND_REGION).federation
        assert plan.key_maps

        sql = plan.inject({}, {"__fk0_0": [(10, 1)]}).sql

        assert 'AS "___map0" ON "__map0"."customer_id" = "___map0"."k"' in sql


class TestFiltersAfterTheJoin:
    """A filter reading several sources runs in the combine step, as one database runs it."""

    @pytest.mark.parametrize("query", AFTER_THE_JOIN.values(), ids=AFTER_THE_JOIN.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(files, query, extra=FROM_ROWS)

    def test_a_dimension_selected_at_a_grain_is_compared_at_its_own(
        self, files: dict[str, str]
    ) -> None:
        """The month of each order, beside its quarter: the leg returns both."""
        query = {
            "select": {"dimensions": ["Order Month:quarter"], "measures": MEASURES},
            "where": [_either({"field": "Order Month", "op": "=", "value": "2024-02-01"}, US)],
        }

        assert_same(files, query, dimension=ORDER_MONTH)
        # Orders 3 and 4 of February, and 7 of a US customer.
        assert [r[2] for r in run_single(files, query, dimension=ORDER_MONTH)] == [3]

    def test_a_dimension_of_another_source_selected_at_a_grain(self) -> None:
        """The customers' signup month, on another source than their sales:
        the customers' leg returns the day too. Sale 1's customer signed up
        on May 2 and sale 5's is missing; sales 2 and 3 are of May, 4 of June."""

        def customers_apart(yaml: str) -> str:
            return _dated(yaml).replace(
                "  Customers:\n    code: customers\n    dataSource: facts\n",
                "  Customers:\n    code: customers\n    dataSource: labels\n",
            )

        query = {
            "select": {"dimensions": ["Signup Date:month"], "measures": ["Sales Count"]},
            "where": [
                _either(
                    {"field": "Signup Date", "op": ">=", "value": "2023-05-10"},
                    {"field": "Product ID", "op": ">", "value": 10},
                )
            ],
        }

        expected, got = _repeated("sum", query, customers_apart, DATED_TABLES)

        assert got == expected == [(date(2023, 5, 1), 2), (date(2023, 6, 1), 1)]

    def test_a_relative_filter_after_the_join_reads_the_combine_steps_date(self) -> None:
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
            "where": [_either(NEXT_DAYS, US)],
        }

        plan = _compile(model_yaml(federated=True), query).federation

        assert "CURRENT_DATE" not in plan.legs[0].sql
        assert "CURRENT_DATE" in plan.combine_sql


PERIOD_METRICS = (
    "  Running Revenue: {type: cumulative, measure: Revenue, timeDimension: Order Date}\n"
    "  Rolling Revenue:\n"
    "    {type: cumulative, measure: Revenue, timeDimension: Order Date, window: 2}\n"
    "  QTD Revenue:\n"
    "    {type: cumulative, measure: Revenue, timeDimension: Order Date, grainToDate: quarter}\n"
    "  Span Revenue:\n"
    "    type: cumulative\n    measure: Revenue\n    timeDimension: Order Date\n"
    "    windowSpan: {count: 30, unit: day}\n"
    "  Revenue MoM:\n    type: period_over_period\n    expression: '{[Revenue]}'\n"
    "    periodOverPeriod:\n      timeDimension: Order Date\n      grain: month\n"
    "      offset: -1\n      offsetGrain: month\n      comparison: difference\n"
    "  Rank in Region:\n"
    "    {type: window, windowFunction: dense_rank, measure: Revenue, partitionBy: [Region]}\n"
    "  Revenue Before:\n"
    "    type: window\n    windowFunction: lag\n    measure: Revenue\n    offset: 1\n"
    "    timeDimension: Order Date\n"
    "  Revenue per Day:\n"
    "    {type: reaggregate, measure: Revenue, per: [Order Date], aggregation: avg}\n"
    "  Running Orders: {type: cumulative, measure: Orders Count, timeDimension: Order Date}\n"
)
FEB_ON = {"field": "Order Date", "op": ">=", "value": "2024-02-01"}
KNOWN_REGION = {"field": "Region", "op": "in", "value": ["EU", "US"]}
BY_MONTH_REGION = ["Order Date:month", "Region"]
PERIOD_QUERIES = {
    "running_total": {"select": {"dimensions": BY_MONTH_REGION, "measures": ["Running Revenue"]}},
    "rolling_window": {"select": {"dimensions": BY_MONTH_REGION, "measures": ["Rolling Revenue"]}},
    "grain_to_date": {"select": {"dimensions": BY_MONTH_REGION, "measures": ["QTD Revenue"]}},
    "time_span": {"select": {"dimensions": BY_MONTH_REGION, "measures": ["Span Revenue"]}},
    "time_span_filtered_on_the_other_source": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Span Revenue"]},
        "where": [EU],
    },
    "period_over_period": {
        "select": {"dimensions": BY_MONTH_REGION, "measures": ["Revenue MoM", "Revenue"]}
    },
    "rank_in_a_partition": {
        "select": {"dimensions": BY_MONTH_REGION, "measures": ["Rank in Region", "Revenue"]}
    },
    "lag": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue Before"]},
        "where": [KNOWN_REGION],
    },
    "beside_a_derived_metric": {
        "select": {
            "dimensions": BY_MONTH_REGION,
            "measures": ["Running Revenue", "Revenue per Order"],
        }
    },
    # A filter on the time dimension says which periods to show, not which to
    # read: March's running total and February's change read January.
    "running_total_shown_from_february": {
        "select": {"dimensions": BY_MONTH_REGION, "measures": ["Running Revenue", "Revenue"]},
        "where": [FEB_ON],
    },
    "change_shown_from_february": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue MoM"]},
        "where": [FEB_ON, KNOWN_REGION],
    },
    "rolling_quarters_shown_from_february": {
        "select": {
            "dimensions": ["Order Date:quarter", "Region"],
            "measures": ["Rolling Revenue", "Running Revenue"],
        },
        "where": [FEB_ON],
    },
    "a_running_count_filled": {
        "select": {"dimensions": BY_MONTH_REGION, "measures": ["Running Orders", "Revenue"]},
        "fill": True,
    },
}


SPREAD = (
    f"  Average Order: {{columns: [{AMOUNT}], aggregation: avg, resultType: decimal}}\n"
    f"  Order Variance: {{columns: [{AMOUNT}], aggregation: variance, resultType: float}}\n"
    f"  Middle Order: {{columns: [{AMOUNT}], aggregation: median}}\n"
    "  Ordering Customers:\n"
    "    columns: [{dataObject: Orders, column: Order Customer}]\n"
    "    aggregation: count_distinct\n    resultType: int\n"
)


def _metrics_over(measure: str) -> str:
    """A running total, a rolling window, a time span and a change of *measure*."""
    on = f"measure: {measure}, timeDimension: Order Date"
    return (
        f"  Running Of: {{type: cumulative, {on}}}\n"
        f"  Rolling Of: {{type: cumulative, {on}, window: 2}}\n"
        f"  Span Of: {{type: cumulative, {on}, windowSpan: {{count: 30, unit: day}}}}\n"
        "  Change Of:\n    type: period_over_period\n"
        f"    expression: '{{[{measure}]}}'\n"
        "    periodOverPeriod:\n      timeDimension: Order Date\n      grain: month\n"
        "      offset: -1\n      offsetGrain: month\n      comparison: difference\n"
    )


CALENDAR = (
    "CREATE TABLE calendar AS SELECT CAST(d AS DATE) AS day"
    " FROM range(DATE '2024-01-01', DATE '2024-04-01', INTERVAL 1 DAY) t(d)"
)


def calendar_yaml(
    federated: bool, calendar_source: str = "shop", metric: str = "", dimension: str = ""
) -> str:
    """The orders joined to a calendar by their date, with a running total
    over the calendar's day, and *metric* and *dimension* too."""
    yaml = model_yaml(
        federated=federated,
        metric=PERIOD_METRICS + "  Calendar Running:\n"
        "    {type: cumulative, measure: Revenue, timeDimension: Calendar Date}\n" + metric,
        dimension="  Calendar Date: {dataObject: Calendar, column: Day, resultType: date}\n"
        + dimension,
    ).replace(
        "        columnsTo: [Customer ID]\n  Customers:",
        "        columnsTo: [Customer ID]\n"
        "      - {joinType: many-to-one, joinTo: Calendar, columnsFrom: [Order Date], "
        "columnsTo: [Day]}\n"
        "  Customers:",
    )
    source = f"    dataSource: {calendar_source}\n" if federated else ""
    calendar = (
        f"  Calendar:\n    code: calendar\n{source}    columns:\n"
        "      Day: {code: day, abstractType: date}\n"
    )
    return yaml.replace("dimensions:\n", calendar + "dimensions:\n", 1)


@pytest.fixture(scope="module")
def calendar_files(tmp_path_factory: Any) -> dict[str, str]:
    """The calendar beside the orders, and on a source of its own, ``geo``."""
    root = tmp_path_factory.mktemp("calendar")
    tables = {
        "all": [ORDERS, CUSTOMERS, CALENDAR],
        "shop": [ORDERS, CALENDAR],
        "crm": [CUSTOMERS],
        "geo": [CALENDAR],
    }
    paths = {name: str(root / f"{name}.duckdb") for name in tables}
    for name, statements in tables.items():
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestPeriodMetrics:
    """Cumulative, period-over-period and window metrics run in the combine step."""

    @pytest.mark.parametrize("query", PERIOD_QUERIES.values(), ids=PERIOD_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(files, query, metric=PERIOD_METRICS)

    @pytest.mark.parametrize("dimension", ["Order Month", "Order Month:quarter"])
    def test_over_a_dimension_declaring_its_grain(
        self, files: dict[str, str], dimension: str
    ) -> None:
        """A rolling window over Order Month, a month by declaration, counts
        months: March reads February too (US 19.00, not 12.00)."""
        metric = PERIOD_METRICS + (
            "  Rolling Months:\n"
            "    {type: cumulative, measure: Revenue, timeDimension: Order Month, window: 2}\n"
        )
        query = {"select": {"dimensions": [dimension, "Region"], "measures": ["Rolling Months"]}}

        assert_same(files, query, metric=metric, dimension=ORDER_MONTH)

    def test_a_time_filter_runs_after_the_join_so_the_history_is_read(
        self, files: dict[str, str]
    ) -> None:
        """February's change is against January's 120.75, which the filter does
        not show: the fact leg reads it, each order at its own date."""
        query = PERIOD_QUERIES["change_shown_from_february"]

        rows = run_federated(files, query, metric=PERIOD_METRICS)
        plan = _compile(model_yaml(federated=True, metric=PERIOD_METRICS), query).federation

        assert dict(rows)[date(2024, 2, 1)] == Decimal("7.00") - Decimal("120.75")
        assert "2024-02-01" not in plan.legs[0].sql
        assert "date_trunc" not in plan.legs[0].sql.lower()

    @pytest.mark.parametrize("selected", ["Order Month:day", "Order Month:week"])
    def test_a_finer_grain_than_a_dimension_declares(
        self, files: dict[str, str], selected: str
    ) -> None:
        """Order Month, a month by declaration, selected by day: its leg reads
        each order's date, not the month it declares."""
        metric = PERIOD_METRICS + (
            "  Running Months: {type: cumulative, measure: Revenue, timeDimension: Order Month}\n"
        )
        query = {
            "select": {
                "dimensions": [selected, "Region"],
                "measures": ["Running Months", "Revenue"],
            }
        }

        assert_same(files, query, metric=metric, dimension=ORDER_MONTH)

    def test_a_time_span_over_a_dimension_declaring_its_grain(self, files: dict[str, str]) -> None:
        """Thirty days before each month's end, over the orders' dates: January
        reads its own orders, not the first of the month alone."""
        metric = PERIOD_METRICS + (
            "  Span Months:\n"
            "    type: cumulative\n    measure: Revenue\n    timeDimension: Order Month\n"
            "    windowSpan: {count: 30, unit: day}\n"
        )
        query = {"select": {"dimensions": ["Order Month", "Region"], "measures": ["Span Months"]}}

        assert_same(files, query, metric=metric, dimension=ORDER_MONTH)
        assert (date(2024, 1, 1), "EU", Decimal("120.75")) in run_federated(
            files, query, metric=metric, dimension=ORDER_MONTH
        )

    @pytest.mark.parametrize("metric", ["Running Revenue", "Revenue MoM"])
    def test_a_time_filter_on_another_dimension_of_the_date(
        self, files: dict[str, str], metric: str
    ) -> None:
        """Filtered on Order Month, shown by Order Date: one date column, so
        the filter shows periods and the history is read, as on one database."""
        query = {
            "select": {"dimensions": ["Order Date:month"], "measures": [metric, "Revenue"]},
            "where": [{"field": "Order Month", "op": ">=", "value": "2024-02-01"}, KNOWN_REGION],
        }

        assert_same(files, query, metric=PERIOD_METRICS, dimension=ORDER_MONTH)

    @pytest.mark.parametrize(
        "query",
        [
            {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Running Revenue"]},
                "where": [
                    {"field": "Order Month", "op": ">=", "value": "2024-02-01"},
                    KNOWN_REGION,
                ],
            },
            {
                "select": {
                    "dimensions": ["Order Month:day", "Region"],
                    "measures": ["Rank in Region"],
                }
            },
        ],
        ids=["filtered_from_february", "selected_by_day"],
    )
    def test_a_dimension_over_a_date_declared_as_text(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        """Order Month reads Order Date's column, a date, though it declares no
        result type (a string): its leg returns the date, and its filter and
        grain read it, as one database's do."""
        month = "  Order Month: {dataObject: Orders, column: Order Date, timeGrain: month}\n"

        assert_same(files, query, metric=PERIOD_METRICS, dimension=month)

    @pytest.mark.parametrize(
        "measures", [["Running Revenue", "Revenue"], ["QTD Revenue", "Rolling Revenue"]]
    )
    def test_without_the_time_dimension_selected(
        self, files: dict[str, str], measures: list[str]
    ) -> None:
        """A running total by region alone sums up to the last date: the fact
        leg returns the dates for it, which the query does not select."""
        query = {"select": {"dimensions": ["Region"], "measures": measures}}

        assert_same(files, query, metric=PERIOD_METRICS)

    @pytest.mark.parametrize("calendar_source", ["shop", "geo"])
    def test_over_a_joined_calendar(
        self, calendar_files: dict[str, str], calendar_source: str
    ) -> None:
        """The running total's dates are the calendar's, beside the orders or on
        a source of its own: selected by month, the query joins it."""
        query = {
            "select": {
                "dimensions": ["Calendar Date:month", "Region"],
                "measures": ["Calendar Running", "Revenue"],
            }
        }
        single = _compile(calendar_yaml(False), query)
        con = duckdb.connect(calendar_files["all"], read_only=True)
        try:
            expected = sorted(_rows(con, single.sql), key=repr)
        finally:
            con.close()
        compiled = _compile(calendar_yaml(True, calendar_source), query)
        combine = legs_registered(calendar_files, compiled.federation)

        assert sorted(_rows(combine, compiled.sql), key=repr) == expected
        assert expected

    @pytest.mark.parametrize("calendar_source", ["shop", "geo"])
    @pytest.mark.parametrize("dimensions", [["Region"], []], ids=["by_region", "alone"])
    def test_over_a_calendar_the_query_does_not_join_is_refused(
        self, dimensions: list[str], calendar_source: str
    ) -> None:
        """One database joins the calendar in the running total's own query,
        by the orders' date, and it limits no other measure: joined in a leg,
        it would limit them all. Alone, the shop's orders and a calendar of
        another source are federated, not compiled for the shop."""
        query = {"select": {"dimensions": dimensions, "measures": ["Calendar Running", "Revenue"]}}
        if calendar_source == "shop" and not dimensions:
            # The shop's orders and calendar alone: one source, as before.
            assert _compile(calendar_yaml(True, calendar_source), query).federation is None
            return

        with pytest.raises(ResolutionError) as caught:
            _compile(calendar_yaml(True, calendar_source), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_METRIC"
        assert "'Calendar Running' reads the dates of 'Calendar', which the query does not" in (
            error.message
        )

    def test_over_a_calendar_reached_through_another_source_is_refused(self) -> None:
        """The calendar is the shop's, reached through the CRM's customers'
        signup: the query reads the CRM too, so it is federated, and refused,
        not compiled for the shop with the CRM's customers in its SQL."""
        yaml = (
            calendar_yaml(True)
            .replace(
                "      - {joinType: many-to-one, joinTo: Calendar, columnsFrom: [Order Date], "
                "columnsTo: [Day]}\n",
                "",
            )
            .replace(
                "      Tenant: {code: tenant, abstractType: string}\n",
                "      Tenant: {code: tenant, abstractType: string}\n"
                "      Signup: {code: signup, abstractType: date}\n"
                "    joins:\n"
                "      - {joinType: many-to-one, joinTo: Calendar, columnsFrom: [Signup], "
                "columnsTo: [Day]}\n",
            )
        )
        query = {"select": {"measures": ["Calendar Running", "Revenue"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_METRIC"

    @pytest.mark.parametrize("metric", ["Running Of", "Rolling Of", "Span Of", "Change Of"])
    @pytest.mark.parametrize(
        "measure", ["Average Order", "Order Variance", "Middle Order", "Ordering Customers"]
    )
    def test_over_a_measure_that_does_not_add_up(
        self, files: dict[str, str], measure: str, metric: str
    ) -> None:
        """An average over a time span reads the orders in the span: the fact
        leg returns its rows. Grouped by date it summed partial sums beside the
        dates, which DuckDB refused."""
        query = {"select": {"dimensions": BY_MONTH_REGION, "measures": [metric, measure]}}
        model = {"extra": SPREAD, "metric": _metrics_over(measure)}

        _assert_same_rows(
            sorted(run_federated(files, query, **model), key=repr),
            sorted(run_single(files, query, **model), key=repr),
        )

    @pytest.mark.parametrize(
        ("dimension", "column"),
        [
            ("Order Stamp:month", "date"),
            ("Order Date:month", "timestamp_tz"),
            ("Order Date", "timestamp_tz"),
        ],
        ids=["zoned_at_a_grain", "a_date_over_a_zoned_column", "cast_to_a_date"],
    )
    def test_a_zoned_timestamp_the_combine_step_would_reshape_is_refused(
        self, dimension: str, column: str
    ) -> None:
        """Returned as stored, its truncation, or its cast to a date, would be
        in the combine step's time zone: an order of Los Angeles' New Year's
        Eve would move into January."""
        query = {"select": {"dimensions": [dimension, "Region"], "measures": ["Revenue Rank"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(self._zoned_yaml(column), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_METRIC"
        assert "reads a timestamp with a time zone, which the combine step would" in (error.message)

    def test_a_zoned_timestamp_left_as_it_is_is_ranked(self) -> None:
        query = {"select": {"dimensions": ["Order Stamp", "Region"], "measures": ["Revenue Rank"]}}

        assert _compile(self._zoned_yaml("timestamp_tz"), query).federation is not None

    @staticmethod
    def _zoned_yaml(column: str) -> str:
        stamp = (
            "  Order Stamp:\n"
            "    {dataObject: Orders, column: Order Date, resultType: timestamp_tz}\n"
        )
        metric = PERIOD_METRICS + (
            "  Revenue Rank: {type: window, windowFunction: dense_rank, measure: Revenue}\n"
        )
        return model_yaml(federated=True, metric=metric, dimension=stamp).replace(
            "code: order_date, abstractType: date", f"code: order_date, abstractType: {column}"
        )


TWO_GRAINS = ["Order Date:month", "Order Date:quarter", "Region"]
OPTION_QUERIES = {
    "two_grains": ({"select": {"dimensions": TWO_GRAINS, "measures": ["Revenue"]}}, ""),
    "two_grains_beside_a_running_total": (
        {"select": {"dimensions": TWO_GRAINS, "measures": ["Running Revenue", "Revenue"]}},
        PERIOD_METRICS,
    ),
    "as_of": (
        {
            "select": {"dimensions": ["Region"], "measures": ["Running Revenue"]},
            "asOf": "2024-01-31",
            "where": [KNOWN_REGION],
        },
        PERIOD_METRICS,
    ),
    "fill": (
        {
            "select": {"dimensions": ["Order Date:month", "Region"], "measures": ["Revenue"]},
            "fill": True,
        },
        "",
    ),
    "fill_filtered_on_the_other_source": (
        {
            "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
            "where": [EU],
            "fill": True,
        },
        "",
    ),
    "fill_beside_a_running_total": (
        {
            "select": {
                "dimensions": ["Order Date:month", "Region"],
                "measures": ["Running Revenue"],
            },
            "fill": True,
        },
        PERIOD_METRICS,
    ),
}


CUSTOMER = "  Customer: {dataObject: Customers, column: Customer ID, resultType: int}\n"
REAGGREGATE_METRICS = """\
  Revenue per Customer:
    {type: reaggregate, measure: Revenue, per: [Customer], aggregation: avg}
  Revenue per Day:
    {type: reaggregate, measure: Revenue, per: [Order Date], aggregation: avg}
  Worst Month:
    {type: reaggregate, measure: Revenue, per: ['Order Date:month'], aggregation: min}
  Median Customer:
    {type: reaggregate, measure: Revenue, per: [Customer], aggregation: median}
  Big Customers:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: count
    having: [{field: Orders Count, op: '>', value: 1}]
  Every Customer:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: count
    population: dimension
  Best Region of a Customer:
    {type: reaggregate, measure: Revenue per Customer, per: [Region], aggregation: max}
  Twice per Customer: {expression: "{[Revenue per Customer]} * 2"}
  Running per Customer:
    {type: cumulative, measure: Revenue per Customer, timeDimension: Order Date}
  Customers per Day:
    {type: reaggregate, measure: Customers Count, per: [Order Date], aggregation: max}
  Carried:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: sum
    population: dimension
    carryForward: true
"""
BY_ORDER_MONTH_ONLY = ["Order Date:month"]
PER_CUSTOMER_ONLY = """\
  Revenue per Customer:
    {type: reaggregate, measure: Revenue, per: [Customer], aggregation: avg}
  Every Customer:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: count
    population: dimension
"""
REAGGREGATE_QUERIES = {
    "per_a_dimension_of_the_other_source": {
        "select": {"dimensions": BY_ORDER_MONTH_ONLY, "measures": ["Revenue per Customer"]}
    },
    "per_a_dimension_of_the_fact": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue per Day"]}
    },
    "without_dimensions": {"select": {"measures": ["Revenue per Customer", "Revenue"]}},
    "per_a_grain_of_a_dimension_selected_at_another": {
        "select": {"dimensions": ["Order Date:year", "Region"], "measures": ["Worst Month"]}
    },
    "a_median_of_the_first_stage": {
        "select": {"dimensions": ["Region"], "measures": ["Median Customer"]}
    },
    "a_having_of_the_metric": {
        "select": {"dimensions": BY_ORDER_MONTH_ONLY, "measures": ["Big Customers"]}
    },
    "the_population_of_the_dimension": {
        "select": {"dimensions": ["Region"], "measures": ["Every Customer"]},
        "where": [LATE],
    },
    "filtered_on_the_other_source": {
        "select": {"dimensions": BY_ORDER_MONTH_ONLY, "measures": ["Revenue per Customer"]},
        "where": [EU],
    },
    "nested": {
        "select": {"dimensions": BY_ORDER_MONTH_ONLY, "measures": ["Best Region of a Customer"]}
    },
    "under_a_derived_metric": {
        "select": {"dimensions": ["Region"], "measures": ["Twice per Customer"]}
    },
    "under_a_cumulative_metric": {
        "select": {"dimensions": BY_ORDER_MONTH_ONLY, "measures": ["Running per Customer"]}
    },
    "carried_forward": {
        "select": {"dimensions": BY_MONTH_REGION, "measures": ["Carried", "Revenue"]}
    },
    "carried_forward_past_the_time_filter": {
        "select": {"dimensions": BY_MONTH_REGION, "measures": ["Carried"]},
        "where": [FEB_ON],
    },
    "only_ordered_by": {
        "select": {"dimensions": BY_ORDER_MONTH_ONLY, "measures": ["Revenue"]},
        "orderBy": [{"field": "Revenue per Customer", "direction": "desc"}],
    },
}


class TestReaggregateMetrics:
    """The combine step computes a reaggregate metric's two stages, each legs
    returning the ``per`` dimensions beside the query's."""

    @pytest.mark.parametrize("query", REAGGREGATE_QUERIES.values(), ids=REAGGREGATE_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        assert_same(files, query, metric=REAGGREGATE_METRICS, dimension=CUSTOMER)

    def test_the_customers_leg_returns_the_per_dimension(self) -> None:
        """By order month, the query joins no customers; the first stage,
        a query of its own on one database, does."""
        query = REAGGREGATE_QUERIES["per_a_dimension_of_the_other_source"]

        compiled = _compile(
            model_yaml(federated=True, metric=REAGGREGATE_METRICS, dimension=CUSTOMER), query
        )

        plan = compiled.federation
        assert plan is not None
        assert [leg.data_source for leg in plan.legs] == ["shop", "crm"]
        assert '"customers"' in plan.legs[1].sql

    def test_per_a_dimension_on_the_many_side_is_refused(self) -> None:
        """The customers' count per order date: one database's first stage
        reads it from the orders, which the customers' rows do not join."""
        query = {"select": {"dimensions": ["Region"], "measures": ["Customers per Day"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(
                model_yaml(federated=True, metric=REAGGREGATE_METRICS, dimension=CUSTOMER), query
            )

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_METRIC"
        assert "groups its first stage by 'Order Date', of Orders" in error.message

    @pytest.mark.parametrize(
        ("measure", "dimension", "refused"),
        [
            ("Worst Month", "Region", True),
            ("Revenue per Day", "Region", False),
            ("Revenue per Customer", "Order Date:month", False),
        ],
        ids=["per_its_month", "per_the_timestamp", "per_another_dimension_by_month"],
    )
    def test_per_a_zoned_timestamp_at_a_grain_is_refused(
        self, measure: str, dimension: str, refused: bool
    ) -> None:
        """Per its month, the legs return the timestamp as stored and the
        combine step would truncate it in its own time zone; per the timestamp
        itself, it groups it as it is; per customer, by month, the orders'
        leg truncates it in its own."""
        yaml = (
            model_yaml(federated=True, metric=REAGGREGATE_METRICS, dimension=CUSTOMER)
            .replace("abstractType: date}", "abstractType: timestamp_tz}")
            .replace(
                "column: Order Date, resultType: date}",
                "column: Order Date, resultType: timestamp_tz}",
            )
        )
        query = {"select": {"dimensions": [dimension], "measures": [measure]}}

        if not refused:
            assert _compile(yaml, query).federation is not None
            return
        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_METRIC"
        assert "reads a timestamp with a time zone" in error.message

    def test_a_required_join_to_another_source_limits_the_first_stage_alone(
        self, files: dict[str, str]
    ) -> None:
        """The combine step joins the customers' leg where the first stage
        reads it: order 6, of no customer, is March's revenue still."""
        query = {
            "select": {
                "dimensions": BY_ORDER_MONTH_ONLY,
                "measures": ["Revenue per Customer", "Revenue"],
            }
        }
        yaml = {
            federated: model_yaml(
                federated=federated, metric=REAGGREGATE_METRICS, dimension=CUSTOMER
            ).replace(
                "        joinTo: Customers\n", "        joinTo: Customers\n        required: true\n"
            )
            for federated in (False, True)
        }

        assert _compile(yaml[True], query).federation is not None
        rows = _run_on(files, yaml[True], query)
        assert rows == _run_on(files, yaml[False], query)
        assert (date(2024, 3, 1), Decimal("7.58"), Decimal("55.15")) in rows

    @pytest.mark.parametrize(
        ("measure", "required", "message"),
        [
            ("Revenue per Customer", True, "reached by a required join to Customers"),
            ("Every Customer", False, "its population would hold only the members with rows"),
        ],
        ids=["a_required_join", "the_population_of_the_dimension"],
    )
    def test_per_a_dimension_beside_the_measure_is_refused_where_the_fact_leg_would_limit_it(
        self, measure: str, required: bool, message: str
    ) -> None:
        """The customers beside the orders, the calendar on a source of its
        own: the orders' leg joins the customers, its required join dropping
        the orders of no customer from every measure, and holds only the
        customers with orders."""
        yaml = calendar_yaml(True, "geo", metric=PER_CUSTOMER_ONLY, dimension=CUSTOMER).replace(
            "    code: customers\n    dataSource: crm\n",
            "    code: customers\n    dataSource: shop\n",
        )
        if required:
            yaml = yaml.replace(
                "        joinTo: Customers\n", "        joinTo: Customers\n        required: true\n"
            )
        query = {"select": {"dimensions": ["Calendar Date"], "measures": ["Revenue", measure]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_METRIC"
        assert message in error.message
        unlimited = {
            "select": {"dimensions": ["Calendar Date"], "measures": ["Revenue per Customer"]}
        }
        assert _compile(yaml.replace("        required: true\n", ""), unlimited).federation


def bridged_model() -> str:
    """Orders on the shop, reaching segments through a required bridge; the
    customers in the CRM, reaching tiers by a required join and regions."""
    return """\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: orders
    dataSource: shop
    columns:
      Order Customer: {code: customer_id, abstractType: int}
      Order Bridge: {code: bridge_id, abstractType: int}
      Amount: {code: amount, abstractType: decimal}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [Order Customer],
         columnsTo: [Customer ID]}
      - {joinType: many-to-one, joinTo: Bridge, columnsFrom: [Order Bridge],
         columnsTo: [Bridge ID], required: true}
  Bridge:
    code: bridge
    dataSource: shop
    columns:
      Bridge ID: {code: bridge_id, abstractType: int}
      Bridge Segment: {code: segment_id, abstractType: string}
    joins:
      - {joinType: many-to-one, joinTo: Segments, columnsFrom: [Bridge Segment],
         columnsTo: [Segment ID]}
  Segments:
    code: segments
    dataSource: shop
    columns:
      Segment ID: {code: segment_id, abstractType: string}
  Customers:
    code: customers
    dataSource: crm
    columns:
      Customer ID: {code: customer_id, abstractType: int}
      Region: {code: region, abstractType: string}
      Customer Tier: {code: tier_id, abstractType: int}
      Customer Area: {code: area_id, abstractType: string}
    joins:
      - {joinType: many-to-one, joinTo: Tiers, columnsFrom: [Customer Tier],
         columnsTo: [Tier ID], required: true}
      - {joinType: many-to-one, joinTo: Areas, columnsFrom: [Customer Area],
         columnsTo: [Area ID]}
  Tiers:
    code: tiers
    dataSource: crm
    columns:
      Tier ID: {code: tier_id, abstractType: int}
  Areas:
    code: areas
    dataSource: crm
    columns:
      Area ID: {code: area_id, abstractType: string}
dimensions:
  Region: {dataObject: Customers, column: Region, resultType: string}
  Segment: {dataObject: Segments, column: Segment ID, resultType: string}
  Tier: {dataObject: Tiers, column: Tier ID, resultType: int}
  Area: {dataObject: Areas, column: Area ID, resultType: string}
  Customer: {dataObject: Customers, column: Customer ID, resultType: int}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum, defaultValue: 0}
metrics:
  Best Segment: {type: reaggregate, measure: Revenue, per: [Segment], aggregation: max}
  Best Tier: {type: reaggregate, measure: Revenue, per: [Tier], aggregation: max}
  Per Customer: {type: reaggregate, measure: Revenue, per: [Customer], aggregation: avg}
  Every Area:
    {type: reaggregate, measure: Revenue, per: [Area], aggregation: count, population: dimension}
"""


class TestReaggregatePerDimensionsTheLegsLimit:
    """A per dimension a leg the query reads too would limit, or list in part."""

    @pytest.mark.parametrize(
        ("dimensions", "measures", "message"),
        [
            (["Region"], ["Best Segment"], "a required join to Bridge the query does not make"),
            (["Region"], ["Best Tier"], "a required join to Tiers the query does not make"),
            ([], ["Best Tier", "Per Customer"], "a required join to Tiers the query does not"),
            ([], ["Every Area"], "read beside Customers on its data source"),
        ],
        ids=[
            "required_on_the_way",
            "required_in_a_source_the_query_reads",
            "required_in_a_source_another_metric_reads",
            "population_reached",
        ],
    )
    def test_is_refused(self, dimensions: list[str], measures: list[str], message: str) -> None:
        """The bridge's required join drops the orders without one from the
        shop's leg, the tiers' the customers without one from the CRM's, which
        the query reads by region, or the average per customer reads; the
        CRM's leg lists the areas its customers reach, not an area none does."""
        query = {"select": {"dimensions": dimensions, "measures": ["Revenue", *measures]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(bridged_model(), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_METRIC"
        assert message in error.message

    @pytest.mark.parametrize("measure", ["Best Tier", "Per Customer"])
    def test_a_source_one_first_stage_alone_reads_is_planned(self, measure: str) -> None:
        """The CRM's leg is the tiers' first stage's alone, joined where it reads it."""
        query = {"select": {"measures": ["Revenue", measure]}}

        assert _compile(bridged_model(), query).federation is not None


class TestQueryOptions:
    """asOf, fill, dimensionsExclude and a dimension at two grains run in the combine step."""

    @pytest.mark.parametrize(
        ("query", "metric"), OPTION_QUERIES.values(), ids=OPTION_QUERIES.keys()
    )
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any], metric: str
    ) -> None:
        assert_same(files, query, metric=metric)

    @pytest.mark.parametrize(
        ("dimensions", "dimension"),
        [
            (["Order Date", "Order Date:month", "Region"], ""),
            (["Order Date:month", "Order Date", "Region"], ""),
            (["Order Month", "Order Month:quarter", "Region"], ORDER_MONTH),
        ],
        ids=["the_date_beside_its_month", "the_month_beside_its_date", "a_declared_grain_beside"],
    )
    def test_a_dimension_as_declared_beside_a_grain(
        self, files: dict[str, str], dimensions: list[str], dimension: str
    ) -> None:
        """Order Date beside Order Date:month is one date at two grains too."""
        query = {"select": {"dimensions": dimensions, "measures": ["Revenue"]}}

        assert_same(files, query, dimension=dimension)

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Order Stamp:month", "Order Stamp", "Region"]}},
            {"select": {"dimensions": ["Order Stamp", "Order Stamp:month", "Region"]}},
        ],
        ids=["the_month_then_the_stamp", "the_stamp_then_the_month"],
    )
    def test_a_zoned_timestamp_returned_as_stored_and_truncated_is_refused(
        self, query: dict[str, Any]
    ) -> None:
        """Truncated in the combine step's time zone, an order of Los Angeles'
        New Year's Eve would move into January, whichever reference comes last."""
        stamp = (
            "  Order Stamp: {dataObject: Orders, column: Order Date, resultType: timestamp_tz}\n"
        )
        yaml = model_yaml(federated=True, dimension=stamp).replace(
            "code: order_date, abstractType: date", "code: order_date, abstractType: timestamp_tz"
        )

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert "'Order Stamp' reads a timestamp with a time zone" in error.message

    def test_fill_spans_the_periods_its_filter_bounds(self, files: dict[str, str]) -> None:
        """From December, before the first order: the filter runs in the combine
        step, which fills December too, as one database does."""
        query = {
            "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
            "where": [{"field": "Order Date", "op": ">=", "value": "2023-12-01"}, KNOWN_REGION],
            "fill": True,
        }

        assert_same(files, query)
        assert (date(2023, 12, 1), None) in run_federated(files, query)

    def test_dimensions_exclude_over_one_group(self, files: dict[str, str]) -> None:
        """The months and regions the orders reach are one group: none of their
        pairs is missing, on one database as federated."""
        query = {
            "select": {"dimensions": ["Order Date:month", "Region"]},
            "dimensionsExclude": True,
        }

        assert run_federated(files, query) == run_single(files, query) == []


RAW_FIELDS = ["Orders.Order ID", "Customers.Region", "Orders.Amount"]
RAW_QUERIES = {
    "fields_of_both_sources": ({"select": {"fields": RAW_FIELDS}}, {}),
    "filtered_on_the_facts_source": (
        {
            "select": {"fields": RAW_FIELDS},
            "where": [{"field": "Orders.Amount", "op": ">", "value": 10}],
        },
        {},
    ),
    "filtered_on_a_dimension_of_the_other_source": (
        {"select": {"fields": RAW_FIELDS}, "where": [EU]},
        {},
    ),
    "filtered_on_a_column_of_the_other_source": (
        {
            "select": {"fields": RAW_FIELDS},
            "where": [{"field": "Customers.Region", "op": "=", "value": "EU"}],
        },
        {},
    ),
    "a_missing_region": (
        {
            "select": {"fields": RAW_FIELDS},
            "where": [{"field": "Customers.Region", "op": "is_null"}],
        },
        {},
    ),
    "an_or_across_sources": (
        {
            "select": {"fields": RAW_FIELDS},
            "where": [_either(US, {"field": "Orders.Amount", "op": ">", "value": 50})],
        },
        {},
    ),
    "ordered_and_limited": (
        {
            "select": {"fields": RAW_FIELDS},
            "orderBy": [{"field": "Orders.Amount", "direction": "desc"}],
            "limit": 3,
        },
        {},
    ),
    "distinct": (
        {"select": {"fields": ["Orders.Order Customer", "Customers.Region"], "distinct": True}},
        {},
    ),
    "ordered_by_position": (
        {
            "select": {"fields": RAW_FIELDS},
            "orderBy": [{"field": "2", "direction": "desc"}, {"field": "1"}],
        },
        {},
    ),
    "a_static_filter_on_the_other_source": (
        {"select": {"fields": RAW_FIELDS}},
        {"tenant_filter": True},
    ),
    "an_enforced_static_filter_there": (
        {"select": {"fields": ["Orders.Order ID", "Orders.Amount"]}},
        {
            "static": "  - {dataObject: Customers, column: Region, operator: notequals, "
            "value: US, enforce: always}\n"
        },
    ),
    "filtered_on_a_dimension_declaring_its_grain": (
        {
            "select": {"fields": ["Orders.Order ID", "Customers.Region"]},
            "where": [_either({"field": "Order Month", "op": "=", "value": "2024-02-01"}, EU)],
        },
        {"dimension": ORDER_MONTH},
    ),
}


class TestRawMode:
    """Raw-mode rows across sources: each leg its rows, joined in the combine step."""

    @pytest.mark.parametrize(("query", "model"), RAW_QUERIES.values(), ids=RAW_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any], model: dict[str, Any]
    ) -> None:
        assert_same(files, query, **model)

    def test_the_fields_keep_their_names(self, files: dict[str, str]) -> None:
        query = {"select": {"fields": RAW_FIELDS}}

        plan = _compile(model_yaml(federated=True), query).federation

        assert '"Orders.Order ID"' in plan.combine_sql
        assert all(f'AS "{field}"' in plan.combine_sql for field in RAW_FIELDS)

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"fields": ["Orders.Amount", "Regions.Region Name"]}},
            {
                "select": {
                    "fields": ["Orders.Amount", "Customers.Customer Region", "Regions.Region Name"]
                },
                "where": [{"field": "Region Name", "op": "is_null"}],
            },
        ],
        ids=["", "missing_on_the_far_source"],
    )
    def test_a_chain_of_sources(self, chain_files: dict[str, str], query: dict[str, Any]) -> None:
        chains = TestSourceChains()

        single = chains._run(chain_files, None, query)

        assert chains._run(chain_files, ("shop", "crm", "geo"), query) == single
        assert single

    def test_a_dimension_named_like_a_generated_key(self, files: dict[str, str]) -> None:
        """A key takes a name no declared dimension has: ``__fk0`` stays the amount."""
        dimension = "  __fk0: {dataObject: Orders, column: Amount, resultType: decimal}\n"
        query = {
            "select": {"fields": ["Orders.Order ID", "Customers.Region"]},
            "where": [{"field": "__fk0", "op": ">", "value": 50}],
        }

        assert_same(files, query, dimension=dimension)
        assert run_single(files, query, dimension=dimension) == [(1, "EU"), (4, None)]

    def test_an_exists_on_the_facts_source(self, subquery_files: dict[str, str]) -> None:
        """Orders with a return: the subquery runs in the fact leg."""
        query = {
            "select": {"fields": ["Orders.Order ID", "Customers.Region"]},
            "where": [_returns("exists")],
        }

        exists = TestExists()

        single = exists._run(subquery_files, False, query)

        assert sorted(exists._run(subquery_files, True, query), key=repr) == sorted(
            single, key=repr
        )
        assert single

    def test_an_exists_on_the_other_source_is_refused(self) -> None:
        query = {
            "select": {"fields": ["Orders.Order ID", "Customers.Region"]},
            "where": [_tickets("exists")],
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(subquery_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_FILTER"

    def test_independent_facts_are_refused(self) -> None:
        """Fields of the orders and the returns: one database unions them."""
        from tests.unit.test_dimension_only_roots import MODEL_YAML

        yaml = MODEL_YAML.replace(
            "  Customers:\n    code: customers\n",
            "  Customers:\n    code: customers\n    dataSource: crm\n",
        )
        for obj in ("Products", "Calendar", "Orders", "Returns"):
            yaml = yaml.replace(
                f"  {obj}:\n    code: {obj.lower()}\n",
                f"  {obj}:\n    code: {obj.lower()}\n    dataSource: shop\n",
            )
        query = {"select": {"fields": ["Orders.Order Id", "Returns.Return Id", "Customers.Region"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert "cannot read independent facts" in error.message


def _over_amount(name: str, override: str, aggregation: str = "sum") -> str:
    return f"  {name}: {{columns: [{AMOUNT}], aggregation: {aggregation}, {override}}}\n"


CONTEXT_MEASURES = (
    _over_amount("Total Revenue", "grain: {mode: FIXED}")
    + _over_amount("Region Revenue", "grain: {mode: FIXED, include: [Region]}")
    + _over_amount("Region Average", "grain: {mode: FIXED, include: [Region]}", "avg")
    + _over_amount("Unfiltered Revenue", "filterContext: {mode: FIXED}")
    + _over_amount("Revenue Any Region", "filterContext: {exclude: [Region]}")
    + _over_amount("Revenue Any Amount", "filterContext: {exclude: [Amount]}")
    + _over_amount("Revenue Any Tenant", "filterContext: {exclude: [Tenant]}")
    + _over_amount("Revenue Only Tenant", "filterContext: {keepOnly: [Tenant]}")
    + _over_amount("Revenue Any Date", "filterContext: {exclude: [Order Date]}")
    + _over_amount(
        "EU Revenue",
        "filterContext: {mode: FIXED, include: [{field: Region, op: '=', value: EU}]}",
    )
    + _over_amount(
        "Revenue Since Valentine",
        "filterContext: {mode: FIXED, include: [{field: Order Date, op: '>=', "
        "value: '2024-02-14'}]}",
    )
    + _over_amount("Average Any Region", "filterContext: {exclude: [Region]}", "avg")
    + _over_amount("Median Any Region", "filterContext: {exclude: [Region]}", "median")
    + "  Orders Any Region: {columns: [{dataObject: Orders, column: Order ID}], "
    "aggregation: count, filterContext: {exclude: [Region]}}\n"
    + "  Customers Any Region: {columns: [{dataObject: Orders, column: Order Customer}], "
    "aggregation: count_distinct, filterContext: {exclude: [Region]}}\n"
    + _over_amount("Unfiltered Total", "grain: {mode: FIXED}, filterContext: {mode: FIXED}")
    + _over_amount(
        "Unfiltered Region Total",
        "grain: {mode: FIXED, include: [Region]}, filterContext: {mode: FIXED}",
    )
)
CONTEXT_DIMENSIONS = (
    "  Tenant: {dataObject: Customers, column: Tenant, resultType: string}\n"
    "  Amount: {dataObject: Orders, column: Amount, resultType: decimal}\n"
)
# Orders 3 (7.00) and 5 (3.15) fail it.
ABOVE_TEN = "  - {dataObject: Orders, column: Amount, operator: gt, value: 10}\n"
SHARE = '  Share: {expression: "{[Revenue]} / {[Unfiltered Revenue]}"}\n'
RUNNING_ANY_REGION = (
    "  Running Any Region:\n"
    "    {type: cumulative, measure: Revenue Any Region, timeDimension: Order Date}\n"
)
BY_ORDER_MONTH = ["Order Date:month"]


def _in_context(
    dimensions: list[str], measures: list[str], *where: dict[str, Any], **model: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A query over the measures with a grain or filter context, and its model."""
    query: dict[str, Any] = {"select": {"dimensions": dimensions, "measures": measures}}
    if where:
        query["where"] = list(where)
    return query, {"extra": CONTEXT_MEASURES, "dimension": CONTEXT_DIMENSIONS, **model}


CONTEXT_QUERIES = {
    "a_grand_total": _in_context(["Region"], ["Revenue", "Total Revenue"]),
    "a_grain_on_the_other_source": _in_context(
        ["Order Date:month", "Region"], ["Revenue", "Region Revenue"], LATE
    ),
    "an_average_at_a_grain_from_rows": _in_context(
        ["Order Date:month", "Region"], ["Region Average"]
    ),
    "a_fixed_context": _in_context(["Region"], ["Revenue", "Unfiltered Revenue"], EU),
    "a_context_without_one_filter": _in_context(
        BY_ORDER_MONTH, ["Revenue", "Revenue Any Region"], EU, LATE
    ),
    "an_included_filter": _in_context(BY_ORDER_MONTH, ["Revenue", "EU Revenue"], LATE),
    "an_included_filter_on_the_day_of_a_selected_month": _in_context(
        ["Order Date:month", "Region"], ["Revenue", "Revenue Since Valentine"]
    ),
    "averages_medians_and_counts": _in_context(
        BY_ORDER_MONTH,
        [
            "Revenue",
            "Average Any Region",
            "Median Any Region",
            "Orders Any Region",
            "Customers Any Region",
        ],
        EU,
    ),
    "a_context_and_a_grain": _in_context(
        ["Region", "Order Date:month"],
        ["Revenue", "Unfiltered Total", "Unfiltered Region Total"],
        LATE,
    ),
    "a_metric_over_a_context": _in_context(BY_ORDER_MONTH, ["Revenue", "Share"], EU, metric=SHARE),
    "a_running_total_over_a_context": _in_context(
        ["Order Date:month", "Region"],
        ["Revenue", "Running Any Region"],
        EU,
        metric=RUNNING_ANY_REGION,
    ),
    "a_filter_on_the_day_of_a_selected_month_left_out": _in_context(
        ["Order Date:month", "Region"], ["Revenue", "Revenue Any Date"], LATE
    ),
    "a_static_filter_a_context_drops": _in_context(
        ["Region"], ["Revenue", "Unfiltered Revenue"], static=ABOVE_TEN
    ),
    "a_static_filter_a_context_excludes": _in_context(
        ["Region"], ["Revenue", "Revenue Any Amount", "Revenue Any Region"], EU, static=ABOVE_TEN
    ),
    "a_static_filter_on_the_other_source_excluded": _in_context(
        BY_ORDER_MONTH, ["Revenue", "Revenue Any Tenant"], tenant_filter=True
    ),
    "a_static_filter_kept_only": _in_context(
        BY_ORDER_MONTH,
        ["Revenue", "Revenue Only Tenant"],
        LATE,
        tenant_filter=True,
        static=ABOVE_TEN,
    ),
}


class TestGrainAndFilterContext:
    """A measure's grain or filter context: the combine step computes it, over
    the legs' groups before any filter a context leaves out."""

    @pytest.mark.parametrize(
        ("query", "model"), CONTEXT_QUERIES.values(), ids=CONTEXT_QUERIES.keys()
    )
    def test_federated_returns_what_one_source_returns(
        self, files: dict[str, str], query: dict[str, Any], model: dict[str, Any]
    ) -> None:
        assert_same(files, query, **model)

    def test_only_a_filter_every_context_keeps_runs_in_a_leg(self) -> None:
        """The date filter in the fact leg, the region filter after the join."""
        query, model = CONTEXT_QUERIES["a_context_without_one_filter"]

        plan = _compile(model_yaml(federated=True, **model), query).federation

        fact, customers = plan.legs
        assert "2024-02-14" in fact.sql
        assert "WHERE" not in customers.sql
        assert "'EU'" in plan.combine_sql
        assert not plan.injections

    def test_a_static_filter_a_context_drops_runs_after_the_join(self) -> None:
        query, model = CONTEXT_QUERIES["a_static_filter_a_context_drops"]

        plan = _compile(model_yaml(federated=True, **model), query).federation

        assert "WHERE" not in plan.legs[0].sql

    def test_an_exists_every_context_keeps(self, subquery_files: dict[str, str]) -> None:
        """Orders with a return, beside the revenue of every region: 100.50 in
        January, not the 120.75 of all its orders."""
        query, model = _in_context(
            BY_ORDER_MONTH, ["Revenue", "Revenue Any Region"], _returns("exists"), EU
        )
        exists = TestExists()

        single = exists._run(subquery_files, False, query, **model)

        assert sorted(exists._run(subquery_files, True, query, **model), key=repr) == sorted(
            single, key=repr
        )
        assert single

    def test_an_exists_a_context_leaves_out_is_refused(self) -> None:
        query, model = _in_context(
            ["Region"], ["Revenue", "Unfiltered Revenue"], _returns("exists")
        )

        with pytest.raises(ResolutionError) as caught:
            _compile(subquery_model(True, **model), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_FILTER"
        assert "filterContext leaves out" in error.message

    def test_a_filter_on_a_column_the_context_leaves_out_stays_out(
        self, files: dict[str, str]
    ) -> None:
        """The dimension takes the filter's name, which the context names too."""
        extra = _over_amount("Revenue Any Region", "filterContext: {exclude: [Customers.Region]}")
        query = {
            "select": {
                "dimensions": ["Order Date:month"],
                "measures": ["Revenue Any Region", "Revenue"],
            },
            "where": [{"field": "Customers.Region", "op": "=", "value": "EU"}],
        }

        single = _run_on(files, model_yaml(federated=False, extra=extra), query)

        assert _run_on(files, model_yaml(federated=True, extra=extra), query) == single
        assert any(any_region != revenue for _, any_region, revenue in single)

    def test_a_filter_on_a_date_column_filters_as_on_one_database(
        self, files: dict[str, str]
    ) -> None:
        query = {
            "select": {"dimensions": ["Order Date:month", "Region"], "measures": ["Revenue"]},
            "where": [{"field": "Orders.Order Date", "op": ">=", "value": "2024-02-01"}],
        }

        single = _run_on(files, model_yaml(federated=False), query)

        assert _run_on(files, model_yaml(federated=True), query) == single
        assert single

    @pytest.mark.parametrize(
        ("model", "where", "measures"),
        [
            (
                {"extra": _over_amount("Orders.Amount", "resultType: decimal")},
                [{"field": "Orders.Amount", "op": ">", "value": 50}],
                ["Revenue"],
            ),
            (
                {
                    "extra": _over_amount("Orders.Amount", "resultType: decimal")
                    + _over_amount("Any Amount", "filterContext: {exclude: [Orders.Amount]}")
                },
                [
                    {"field": "Orders.Amount", "op": ">", "value": 50},
                    {"field": "Customers.Region", "op": "=", "value": "EU"},
                ],
                ["Any Amount", "Revenue"],
            ),
            (
                {"order_columns": "      AMOUNT: {code: amount, abstractType: decimal}\n"},
                [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Orders.Amount", "op": ">", "value": 50},
                            {"field": "Orders.AMOUNT", "op": "<", "value": 10},
                            {"field": "Customers.Region", "op": "=", "value": "US"},
                        ],
                    }
                ],
                ["Revenue"],
            ),
        ],
        ids=["named_as_a_measure", "beside_another_column", "another_without_its_case"],
    )
    def test_a_column_whose_name_is_taken_stays_refused(
        self, model: dict[str, str], where: list[Any], measures: list[str]
    ) -> None:
        """A measure ``Orders.Amount``, or a column ``AMOUNT`` beside ``Amount``,
        which DuckDB names alike: no column becomes a dimension, and the query
        is refused as without them, also beside a filter on another column."""
        query = {"select": {"dimensions": ["Region"], "measures": measures}, "where": where}

        with pytest.raises(ResolutionError) as caught:
            _compile(model_yaml(federated=True, **model), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_FILTER"

    def test_an_included_filter_on_a_column_filters_as_on_one_database(
        self, files: dict[str, str]
    ) -> None:
        """The column is a dimension over it, which the filter context includes."""
        extra = _over_amount(
            "Large Revenue",
            "filterContext: {include: [{field: Orders.Amount, op: '>', value: 50}]}",
        )
        query = {"select": {"dimensions": ["Region"], "measures": ["Large Revenue", "Revenue"]}}

        single = _run_on(files, model_yaml(federated=False, extra=extra), query)

        assert _run_on(files, model_yaml(federated=True, extra=extra), query) == single
        assert single


class TestRefusals:
    @pytest.mark.parametrize(("extra", "query", "code"), REFUSALS.values(), ids=REFUSALS.keys())
    def test_a_query_that_does_not_split_exactly_is_refused_by_name(
        self, extra: str, query: dict[str, Any], code: str
    ) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(model_yaml(federated=True, extra=extra), query)

        assert [e.code for e in caught.value.errors] == [code]


class TestLegDialects:
    def test_each_leg_is_written_for_its_sources_dialect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("ob_flight")
        monkeypatch.setenv("OBSL_DATABASES", "shop,crm")
        monkeypatch.setenv("DB_SHOP_TYPE", "mysql")
        monkeypatch.setenv("DB_CRM_TYPE", "postgres")

        plan = _compile(
            model_yaml(federated=True),
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}},
        ).federation

        assert [leg.dialect for leg in plan.legs] == ["mysql", "postgres"]
        assert "`orders`" in plan.legs[0].sql
        assert '"customers"' in plan.legs[1].sql

    def test_an_unconfigured_source_gets_the_dialect_compiled_for(self) -> None:
        store = ModelStore()
        model_id = store.load_model(model_yaml(federated=True)).model_id
        query = {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}}

        plan = store.compile_query(model_id, QueryObject.model_validate(query), "postgres")

        assert [leg.dialect for leg in plan.federation.legs] == ["postgres", "postgres"]
        assert plan.dialect == "duckdb"


class TestRollup:
    def test_subtotals_are_computed_over_the_combined_rows(self, files: dict[str, str]) -> None:
        assert_same(
            files,
            {
                "select": {"dimensions": ["Region", "Order Date:month"], "measures": MEASURES},
                "grouping": "rollup",
            },
        )


class TestJoinShape:
    def test_measures_on_the_one_side_grouped_by_the_many_side(self, files: dict[str, str]) -> None:
        """The customers' rows by order date: counted once per date in the
        combine step, from the CRM's leg."""
        query = {"select": {"dimensions": ["Order Date"], "measures": ["Customer Rows"]}}

        single = _run_on(files, model_yaml(federated=False, extra=ON_CUSTOMERS), query)

        assert _run_on(files, model_yaml(federated=True, extra=ON_CUSTOMERS), query) == single
        assert single

    def test_measures_of_a_query_of_their_own_on_the_one_side_are_refused(self) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(
                model_yaml(federated=True, extra=UNFILTERED_CUSTOMERS),
                {"select": {"dimensions": ["Order Date"], "measures": ["All Customer Rows"]}},
            )

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert "on the many side of their join" in error.message


class TestSurfaces:
    async def test_query_sql_returns_the_legs_and_the_combine_query(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from orionbelt.api.app import create_app
        from orionbelt.api.deps import init_session_manager, reset_session_manager
        from orionbelt.service.session_manager import SessionManager
        from orionbelt.settings import Settings

        app = create_app(settings=Settings(session_ttl_seconds=3600, session_cleanup_interval=9999))
        mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
        init_session_manager(mgr)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                session = (await c.post("/v1/sessions")).json()["session_id"]
                model_id = (
                    await c.post(
                        f"/v1/sessions/{session}/models",
                        json={"model_yaml": model_yaml(federated=True)},
                    )
                ).json()["model_id"]
                response = await c.post(
                    f"/v1/sessions/{session}/query/sql",
                    json={
                        "model_id": model_id,
                        "query": {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}},
                    },
                )
        finally:
            reset_session_manager()
            mgr.stop()

        body = response.json()
        assert response.status_code == 200, body
        assert body["dialect"] == "duckdb"
        assert '"leg_0"' in body["sql"]
        assert [(leg["name"], leg["data_source"]) for leg in body["federation"]["legs"]] == [
            ("leg_0", "shop"),
            ("leg_1", "crm"),
        ]

    def test_obsl_compile_prints_each_leg_then_the_combine_query(self, tmp_path: Any) -> None:
        import json

        from typer.testing import CliRunner

        from orionbelt.cli.main import app

        model = tmp_path / "model.yaml"
        model.write_text(model_yaml(federated=True))
        query = tmp_path / "query.json"
        query.write_text(
            json.dumps({"select": {"dimensions": ["Region"], "measures": ["Revenue"]}})
        )

        result = CliRunner().invoke(app, ["compile", str(model), "-q", str(query)])

        assert result.exit_code == 0, result.output
        assert "-- leg_0: shop (duckdb), reads Orders" in result.output
        assert "-- leg_1: crm (duckdb), reads Customers" in result.output
        assert result.output.index("-- combine") > result.output.index("-- leg_1")


FILTERED_WITH_DEFAULT = """\
  First Order Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: decimal
    defaultValue: 10
    filters:
      - column: {dataObject: Orders, column: Order ID}
        operator: equals
        values: [{dataType: int, valueInt: 1}]
"""
NARROW = (
    "  Revenue One Place: {columns: [{dataObject: Orders, column: Amount}], "
    "aggregation: sum, resultType: decimal, dataType: 'decimal(10, 1)'}\n"
)


class TestReviewFindings:
    """Each case returned a different number federated than in one database."""

    def test_an_enforced_dimension_side_filter_applies_without_its_dimensions(
        self, files: dict[str, str]
    ) -> None:
        static = (
            "  - {dataObject: Customers, column: Tenant, operator: equals, value: acme, "
            "enforce: always}\n"
        )
        query = {"select": {"measures": ["Revenue"]}}

        assert_same(files, query, static=static)
        ((total,),) = run_federated(files, query, static=static)
        assert float(total) == pytest.approx(194.85)

    def test_a_static_null_filter_keeps_the_orphan_order(self, files: dict[str, str]) -> None:
        static = "  - {dataObject: Customers, column: Region, operator: is_null}\n"
        query = {"select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]}}

        assert_same(files, query, static=static)
        total = sum(float(r[1]) for r in run_federated(files, query, static=static))
        assert total == pytest.approx(55.10 + 40.00)

    def test_a_default_applies_after_the_partials_are_combined(self, files: dict[str, str]) -> None:
        query = {"select": {"dimensions": ["Region"], "measures": ["First Order Revenue"]}}

        assert_same(files, query, extra=FILTERED_WITH_DEFAULT)
        rows = dict(run_federated(files, query, extra=FILTERED_WITH_DEFAULT))
        assert float(rows["EU"]) == pytest.approx(100.50)

    def test_partials_are_not_rounded_before_they_are_added(self, files: dict[str, str]) -> None:
        query = {"select": {"dimensions": ["Region"], "measures": ["Revenue One Place"]}}

        assert_same(files, query, extra=NARROW)
        rows = dict(run_federated(files, query, extra=NARROW))
        assert float(rows["EU"]) == pytest.approx(123.9)

    def test_a_count_over_no_rows_is_zero(self, files: dict[str, str]) -> None:
        query = {
            "select": {"measures": ["Orders Count", "Revenue"]},
            "where": [{"field": "Region", "op": "=", "value": "nowhere"}],
        }

        assert_same(files, query)
        ((count, _),) = run_federated(files, query)
        assert count == 0

    def test_a_deferred_filter_on_a_dimension_selected_at_a_grain_reads_its_day(
        self, files: dict[str, str]
    ) -> None:
        """Order 2 is of January 20: its month, January 1, is before the 15th."""
        query = {
            "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
            "where": [
                {
                    "logic": "or",
                    "filters": [
                        {"field": "Region", "op": "=", "value": "US"},
                        {"field": "Order Date", "op": "<", "value": "2024-01-15"},
                    ],
                }
            ],
        }

        assert_same(files, query)
        assert (date(2024, 1, 1), Decimal("100.50")) in run_federated(files, query)


def chain_yaml(shop: str, crm: str, regions: str) -> str:
    """Orders -> Customers -> Regions, each on the data source given; the
    embargoes on some regions with the regions."""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: orders
    dataSource: {shop}
    columns:
      Order Customer: {{code: customer_id, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Customers:
    code: customers
    dataSource: {crm}
    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
      Customer Region: {{code: region_id, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region]
        columnsTo: [Region ID]
  Regions:
    code: regions
    dataSource: {regions}
    columns:
      Region ID: {{code: region_id, abstractType: int}}
      Region Name: {{code: name, abstractType: string}}
  Embargoes:
    code: embargoes
    dataSource: {regions}
    columns:
      Embargoed Region: {{code: region_id, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Embargoed Region]
        columnsTo: [Region ID]
dimensions:
  Region Name: {{dataObject: Regions, column: Region Name, resultType: string}}
  Order Customer: {{dataObject: Orders, column: Order Customer, resultType: int}}
  Customer Region: {{dataObject: Customers, column: Customer Region, resultType: int}}
measures:
  Revenue: {{columns: [{AMOUNT}], aggregation: sum, resultType: decimal}}
"""


class TestIntermediateObjects:
    QUERY = {"select": {"dimensions": ["Region Name"], "measures": ["Revenue"]}}

    def test_an_object_the_join_passes_through_is_planned_on_its_source(self) -> None:
        plan = _compile(chain_yaml("shop", "shop", "crm"), self.QUERY).federation

        assert [(leg.data_source, leg.data_objects) for leg in plan.legs] == [
            ("shop", ("Customers", "Orders")),
            ("crm", ("Regions",)),
        ]

    def test_a_join_passing_through_another_source_comes_back_on_its_own_leg(self) -> None:
        """The regions are of the shop, reached through the CRM's customers:
        a leg of their own on the shop, joined from the customers' leg."""
        plan = _compile(chain_yaml("shop", "crm", "shop"), self.QUERY).federation

        assert [(leg.data_source, leg.data_objects) for leg in plan.legs] == [
            ("shop", ("Orders",)),
            ("crm", ("Customers",)),
            ("shop", ("Regions",)),
        ]


# Customer 12 has no region and region 3 of customer 13 is missing; order 6's
# customer 99 is missing. Region 2 is under an embargo.
CHAIN_TABLES = {
    "orders": "CREATE TABLE orders AS SELECT * FROM (VALUES (10, 100.50), (10, 20.25),"
    " (11, 7.00), (12, 55.10), (13, 3.15), (99, 40.00)) t(customer_id, amount)",
    "customers": "CREATE TABLE customers AS SELECT * FROM (VALUES (10, 1), (11, 2), (12, NULL),"
    " (13, 3)) t(customer_id, region_id)",
    "regions": "CREATE TABLE regions AS SELECT * FROM (VALUES (1, 'EU'), (2, 'US'))"
    " t(region_id, name)",
    "embargoes": "CREATE TABLE embargoes AS SELECT * FROM (VALUES (2)) t(region_id)",
}


@pytest.fixture(scope="module")
def chain_files(tmp_path_factory: Any) -> dict[str, str]:
    """Every table in ``all``; the regions and embargoes on ``geo``, and on the shop too."""
    root = tmp_path_factory.mktemp("chain")
    tables = {
        "all": list(CHAIN_TABLES),
        "shop": ["orders", "regions", "embargoes"],
        "crm": ["customers", "regions"],
        "geo": ["regions", "embargoes"],
    }
    paths = {name: str(root / f"{name}.duckdb") for name in tables}
    for name, names in tables.items():
        con = duckdb.connect(paths[name])
        for table in names:
            con.execute(CHAIN_TABLES[table])
        con.close()
    return paths


EU_REGION = {"field": "Region Name", "op": "=", "value": "EU"}
CHAIN_QUERIES = {
    "by_the_far_source": {"select": {"dimensions": ["Region Name"], "measures": ["Revenue"]}},
    "beside_the_middle_source": {
        "select": {"dimensions": ["Customer Region", "Region Name"], "measures": ["Revenue"]}
    },
    "filtered_on_the_far_source": {
        "select": {"dimensions": ["Customer Region"], "measures": ["Revenue"]},
        "where": [EU_REGION],
    },
    "missing_on_the_far_source": {
        "select": {"dimensions": ["Order Customer"], "measures": ["Revenue"]},
        "where": [{"field": "Region Name", "op": "is_null"}],
    },
    "or_across_three_sources": {
        "select": {"dimensions": ["Region Name"], "measures": ["Revenue"]},
        "where": [
            _either(
                {"field": "Order Customer", "op": "=", "value": 12},
                {"field": "Customer Region", "op": "=", "value": 2},
                EU_REGION,
            )
        ],
    },
    "nonexists_on_the_far_source": {
        "select": {"dimensions": ["Order Customer"], "measures": ["Revenue"]},
        "where": [
            {
                "field": "Regions.Region ID",
                "op": "nonexists",
                "subquery": {"dataObject": "Embargoes"},
            }
        ],
    },
    "without_measures": {"select": {"dimensions": ["Order Customer", "Region Name"]}},
}


class TestSourceChains:
    """A source reached through another dimension source: a leg joined from that one's."""

    def _run(
        self, paths: dict[str, str], sources: tuple[str, str, str] | None, query: dict[str, Any]
    ) -> list[Any]:
        compiled = _compile(chain_yaml(*(sources or ("all", "all", "all"))), query)
        if sources is None:
            assert compiled.federation is None
            con = duckdb.connect(paths["all"], read_only=True)
            try:
                return sorted(_rows(con, compiled.sql), key=repr)
            finally:
                con.close()
        combine = legs_registered(paths, compiled.federation)
        return sorted(_rows(combine, compiled.sql), key=repr)

    @pytest.mark.parametrize("query", CHAIN_QUERIES.values(), ids=CHAIN_QUERIES.keys())
    @pytest.mark.parametrize(
        "sources", [("shop", "crm", "geo"), ("shop", "crm", "shop")], ids=["three", "back"]
    )
    def test_federated_returns_what_one_source_returns(
        self, chain_files: dict[str, str], query: dict[str, Any], sources: tuple[str, str, str]
    ) -> None:
        single = self._run(chain_files, None, query)

        assert self._run(chain_files, sources, query) == single
        assert single

    def test_the_far_leg_is_joined_from_the_middle_one(self) -> None:
        query = {"select": {"dimensions": ["Region Name"], "measures": ["Revenue"]}}

        plan = _compile(chain_yaml("shop", "crm", "geo"), query).federation

        orders, customers, regions = plan.legs
        assert "region_id" not in orders.sql
        assert '"Customers"."region_id" AS "__fk1_0"' in customers.sql
        assert 'LEFT JOIN "leg_2" AS "leg_2" ON "leg_1"."__fk1_0" = "leg_2"."__fk1_0"' in (
            plan.combine_sql
        )

    def test_only_a_filter_on_the_middle_source_narrows_the_fact_leg(self) -> None:
        """The fact leg holds the customers' keys, not the regions'."""
        middle = {"field": "Customer Region", "op": "=", "value": 1}
        query = {"select": {"dimensions": ["Region Name"], "measures": ["Revenue"]}}

        far = _compile(chain_yaml("shop", "crm", "geo"), {**query, "where": [EU_REGION]})
        near = _compile(chain_yaml("shop", "crm", "geo"), {**query, "where": [middle]})

        assert far.federation.injections == ()
        assert [i.leg for i in near.federation.injections] == ["leg_1"]


TYPED_ORDERS = """
CREATE TABLE orders AS SELECT * FROM (VALUES
    (10, 0.60::DECIMAL(38, 20), 0.00000000000000000001::DECIMAL(38, 20),
        500000000000000000000000000000::DECIMAL(38, 0)),
    (11, 0.60::DECIMAL(38, 20), 0.00000000000000000001::DECIMAL(38, 20),
        500000000000000000000000000000::DECIMAL(38, 0))
) t(customer_id, amount, tiny, big)
"""
TYPED_CUSTOMERS = (
    "CREATE TABLE customers AS SELECT * FROM (VALUES (10, 'EU'), (11, 'EU')) t(customer_id, region)"
)


def typed_yaml(*, federated: bool) -> str:
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""

    def measure(name: str, column: str, data_type: str) -> str:
        return (
            f"  {name}: {{columns: [{{dataObject: Orders, column: {column}}}], "
            f"aggregation: sum, resultType: decimal, dataType: '{data_type}'}}\n"
        )

    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: orders
{shop}    columns:
      Order Customer: {{code: customer_id, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal}}
      Tiny: {{code: tiny, abstractType: decimal}}
      Big: {{code: big, abstractType: decimal}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Customers:
    code: customers
{crm}    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
      Region: {{code: region, abstractType: string}}
dimensions:
  Region: {{dataObject: Customers, column: Region, resultType: string}}
measures:
{measure("Whole Amount", "Amount", "integer")}{measure("Tiny Sum", "Tiny", "decimal(38, 20)")}\
{measure("Big Sum", "Big", "decimal(38, 0)")}\
  Counted:
    columns: [{{dataObject: Orders, column: Order Customer}}]
    aggregation: count
    resultType: int
    defaultValue: 10
"""


@pytest.fixture(scope="module")
def typed_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("fed_typed")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [TYPED_ORDERS, TYPED_CUSTOMERS]),
        ("shop", [TYPED_ORDERS]),
        ("crm", [TYPED_CUSTOMERS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


def _run_typed(files: dict[str, str], query: dict[str, Any], *, federated: bool) -> list[Any]:
    compiled = _compile(typed_yaml(federated=federated), query)
    if not federated:
        con = duckdb.connect(files["all"], read_only=True)
        try:
            return _rows(con, compiled.sql)
        finally:
            con.close()
    combine = legs_registered(files, compiled.federation)
    return _rows(combine, compiled.sql)


class TestPartialTypes:
    """A partial is neither rounded nor bounded before the partials are added."""

    QUERY = {
        "select": {"dimensions": ["Region"], "measures": ["Whole Amount", "Tiny Sum", "Big Sum"]}
    }

    def test_the_combined_values_are_the_single_sums(self, typed_files: dict[str, str]) -> None:
        single = _run_typed(typed_files, self.QUERY, federated=False)
        federated = _run_typed(typed_files, self.QUERY, federated=True)

        assert federated == single
        ((_, whole, tiny, big),) = federated
        assert whole == 1  # 1.20, rounded once; rounding each 0.60 first gave 2
        assert str(tiny) == "2E-20"  # a fixed partial scale would round this to 0
        assert big == 10**30  # a fractional partial scale would overflow this

    def test_the_fact_leg_leaves_its_sums_uncast(self) -> None:
        plan = _compile(typed_yaml(federated=True), self.QUERY).federation

        assert "CAST(SUM" not in plan.legs[0].sql
        assert "AS INTEGER" in plan.combine_sql

    def test_a_count_declaring_a_default_is_still_zero_over_no_rows(
        self, typed_files: dict[str, str]
    ) -> None:
        query = {
            "select": {"measures": ["Counted"]},
            "where": [{"field": "Region", "op": "=", "value": "nowhere"}],
        }

        assert _run_typed(typed_files, query, federated=True) == [(0,)]
        assert _run_typed(typed_files, query, federated=False) == [(0,)]


THREE_OBJECTS = """\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: orders
{shop}    columns:
      Order ID: {{code: order_id, abstractType: int}}
      Order Customer: {{code: customer_id, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal, numClass: additive}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Customers:
    code: customers
{shop}    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
      Customer Country: {{code: country_id, abstractType: int}}
      Credit: {{code: credit, abstractType: decimal, numClass: additive}}
      First Order: {{abstractType: int, expression: "{{[Orders].[Order ID]}}"}}
    joins:
      - joinType: many-to-one
        joinTo: Countries
        columnsFrom: [Customer Country]
        columnsTo: [Country ID]
  Countries:
    code: countries
{crm}    columns:
      Country ID: {{code: country_id, abstractType: int}}
      Country: {{code: name, abstractType: string}}
dimensions:
  Country: {{dataObject: Countries, column: Country, resultType: string}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
  Customers Counted:
    columns: [{{dataObject: Customers, column: Customer ID}}]
    aggregation: count_distinct
    resultType: int
  Orders Counted:
    columns: [{{dataObject: Orders, column: Order ID}}]
    aggregation: count_distinct
    resultType: int
  Credit Total: {{columns: [{{dataObject: Customers, column: Credit}}], aggregation: sum}}
  First Orders Counted:
    columns: [{{dataObject: Customers, column: First Order}}]
    aggregation: count_distinct
    resultType: int
"""
THREE_DATA = {
    "orders": "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 10, 50.00), (2, 10, 30.00), "
    "(3, 11, 20.00)) t(order_id, customer_id, amount)",
    # Customer 12 has no orders, and is the only customer in country 3.
    "customers": "CREATE TABLE customers AS SELECT * FROM (VALUES (10, 1, 100.00), "
    "(11, 2, 40.00), (12, 3, 60.00)) t(customer_id, country_id, credit)",
    "countries": "CREATE TABLE countries AS SELECT * FROM (VALUES (1, 'AT'), (2, 'SI'), "
    "(3, 'HR')) t(country_id, name)",
}


def three_objects(federated: bool) -> str:
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    return THREE_OBJECTS.format(shop=shop, crm=crm)


@pytest.fixture(scope="module")
def three_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("three")
    tables = {"all": THREE_DATA, "shop": ("orders", "customers"), "crm": ("countries",)}
    paths = {name: str(root / f"{name}.duckdb") for name in tables}
    for name, which in tables.items():
        con = duckdb.connect(paths[name])
        for table in which:
            con.execute(THREE_DATA[table])
        con.close()
    return paths


class TestCountDistinctBesideOtherObjects:
    """A count of distinct values beside measures of another data object."""

    def test_alone_it_keeps_the_rows_without_a_match(self, three_files: dict[str, str]) -> None:
        """Customer 12 has no orders: counted, and its country HR kept."""
        query = {"select": {"dimensions": ["Country"], "measures": ["Customers Counted"]}}
        single_compiled = _compile(three_objects(False), query)
        con = duckdb.connect(three_files["all"], read_only=True)
        try:
            single = sorted(_rows(con, single_compiled.sql))
        finally:
            con.close()
        compiled = _compile(three_objects(True), query)
        combine = legs_registered(three_files, compiled.federation)

        assert sorted(_rows(combine, compiled.sql)) == single == [("AT", 1), ("HR", 1), ("SI", 1)]

    @pytest.mark.parametrize(
        "measures",
        [
            ["Revenue", "Customers Counted"],
            ["Credit Total", "Orders Counted"],
        ],
        ids=["sum_over_orders_and_distinct_customers", "customer_sum_and_distinct_orders"],
    )
    def test_with_measures_of_another_object_it_comes_from_rows(
        self, three_files: dict[str, str], measures: list[str]
    ) -> None:
        """Grouping a fact leg of partial results by the counted column would
        repeat a one-side sum per value, and the rows kept for one measure are
        not the other's: from rows, each is counted as on one source."""
        query = {"select": {"dimensions": ["Country"], "measures": measures}}
        single_compiled = _compile(three_objects(False), query)
        con = duckdb.connect(three_files["all"], read_only=True)
        try:
            single = sorted(_rows(con, single_compiled.sql), key=repr)
        finally:
            con.close()
        compiled = _compile(three_objects(True), query)
        assert compiled.federation.legs[0].rows is not None
        combine = legs_registered(three_files, compiled.federation)

        assert sorted(_rows(combine, compiled.sql), key=repr) == single
        assert single

    def test_a_customer_column_computed_from_orders_is_refused(self) -> None:
        """Per order, not per customer: one source refuses it too
        (``FILTER_ADMITS_ROWS_WITHOUT``)."""
        query = {
            "select": {
                "dimensions": ["Country"],
                "measures": ["Credit Total", "First Orders Counted"],
            }
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(three_objects(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_AGGREGATION"
        assert "which one row of 'Customers' can have several values of" in error.message


EQUALITY = """\
version: 1.0
dataObjects:
  Facts:
    code: t
    dataSource: facts
    columns:
      Key: {code: k, abstractType: int}
      Name: {code: n, abstractType: string}
      F: {code: f, abstractType: float}
    joins:
      - {joinType: many-to-one, joinTo: Groups, columnsFrom: [Key], columnsTo: [Group Key]}
  Groups:
    code: g
    dataSource: labels
    columns:
      Group Key: {code: k, abstractType: int}
      Label: {code: l, abstractType: string}
dimensions:
  Label: {dataObject: Groups, column: Label, resultType: string}
  Name: {dataObject: Facts, column: Name, resultType: string}
measures:
  Rows: {columns: [{dataObject: Facts, column: Key}], aggregation: count, resultType: int}
  Names: {columns: [{dataObject: Facts, column: Name}], aggregation: count_distinct}
  Floats: {columns: [{dataObject: Facts, column: F}], aggregation: count_distinct}
  Median F: {columns: [{dataObject: Facts, column: F}], aggregation: median, resultType: float}
  Least Name: {columns: [{dataObject: Facts, column: Name}], aggregation: min, resultType: string}
  Mode Name: {columns: [{dataObject: Facts, column: Name}], aggregation: mode, resultType: string}
  Mode F: {columns: [{dataObject: Facts, column: F}], aggregation: mode, resultType: float}
  Median A F:
    columns: [{dataObject: Facts, column: F}]
    aggregation: median
    resultType: float
    filters:
      - column: {dataObject: Facts, column: Name}
        operator: equals
        values: [{dataType: string, valueString: a}]
  Median Like A F:
    columns: [{dataObject: Facts, column: F}]
    aggregation: median
    resultType: float
    filters:
      - column: {dataObject: Facts, column: Name}
        operator: like
        values: [{dataType: string, valueString: a}]
  Median One F:
    columns: [{dataObject: Facts, column: F}]
    aggregation: median
    resultType: float
    filters:
      - column: {dataObject: Facts, column: Name}
        operator: equals
        values: [{dataType: int, valueInt: 1}]
"""


class TestEngineEquality:
    """Values compared in the combine step compare as their own engine compares them."""

    @pytest.fixture
    def facts_on(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        pytest.importorskip("ob_flight")
        monkeypatch.setenv("OBSL_DATABASES", "facts,labels")
        monkeypatch.setenv("DB_LABELS_TYPE", "postgres")

        def plan(dialect: str, query: dict[str, Any]) -> Any:
            monkeypatch.setenv("DB_FACTS_TYPE", dialect)
            return _compile(EQUALITY, query).federation

        return plan

    def test_a_mysql_string_dimension_is_one_member_of_its_class(self, facts_on: Any) -> None:
        """'a' and 'A' are one value to MySQL: the leg returns the same member
        for both, so the combine step groups them as one."""
        plan = facts_on(
            "mysql", {"select": {"dimensions": ["Label", "Name"], "measures": ["Rows"]}}
        )

        facts, labels = plan.legs
        assert (
            "FIRST_VALUE(`__leg`.`Name`) OVER (PARTITION BY `__leg`.`Name` "
            "ORDER BY CAST(`__leg`.`Name` AS BINARY) ASC) AS `Name`"
        ) in facts.sql
        # Only the compared value is rewritten; keys and measures pass through.
        assert "`__leg`.`__fk0_0` AS `__fk0_0`" in facts.sql
        assert "__leg" not in labels.sql

    def test_a_least_mysql_string_is_ranked_by_its_collation(self, facts_on: Any) -> None:
        """MySQL orders 'é' before 'f': the leg ranks the minima in its engine."""
        plan = facts_on("mysql", {"select": {"dimensions": ["Label"], "measures": ["Least Name"]}})

        assert ("DENSE_RANK() OVER (ORDER BY `__leg`.`Least Name` ASC) AS `__rank0`") in plan.legs[
            0
        ].sql

    def test_a_count_of_distinct_mysql_strings_counts_classes(self, facts_on: Any) -> None:
        plan = facts_on(
            "mysql", {"select": {"dimensions": ["Label"], "measures": ["Names", "Floats"]}}
        )

        facts = plan.legs[0]
        rewritten = [c for c in dict(facts.columns) if f"PARTITION BY `__leg`.`{c}`" in facts.sql]
        # The counted string, not the counted float: MySQL counts -0.0 as 0.0.
        assert len(rewritten) == 1
        assert dict(facts.columns)[rewritten[0]] == "string"

    def test_a_clickhouse_float_is_counted_by_its_text(self, facts_on: Any) -> None:
        """ClickHouse counts 0.0 and -0.0 as two values, DuckDB as one."""
        plan = facts_on(
            "clickhouse", {"select": {"dimensions": ["Label"], "measures": ["Names", "Floats"]}}
        )

        facts = plan.legs[0]
        texts = [c for c in dict(facts.columns) if f'toString("__leg"."{c}")' in facts.sql]
        assert len(texts) == 1
        assert dict(facts.columns)[texts[0]] == "string"

    def test_from_rows_a_count_of_distinct_mysql_strings_still_counts_classes(
        self, facts_on: Any
    ) -> None:
        plan = facts_on(
            "mysql", {"select": {"dimensions": ["Label"], "measures": ["Median F", "Names"]}}
        )

        facts = plan.legs[0]
        assert facts.rows is not None
        rewritten = [c for c in dict(facts.columns) if f"PARTITION BY `__leg`.`{c}`" in facts.sql]
        assert len(rewritten) == 1

    @pytest.mark.parametrize(
        ("dialect", "measure", "column"),
        [
            ("mysql", "Least Name", "Name"),
            ("mysql", "Mode Name", "Name"),
            ("mysql", "Median Like A F", "Name"),
            ("mysql", "Median One F", "Name"),
            ("clickhouse", "Mode F", "F"),
        ],
    )
    def test_from_rows_values_their_engine_compares_otherwise_are_refused(
        self, facts_on: Any, dialect: str, measure: str, column: str
    ) -> None:
        """The combine step would compare them: 'a' and 'A' as two values,
        where MySQL sees one; 0.0 and -0.0 as one, where ClickHouse counts two.
        Nor can the leg evaluate a pattern, or a comparison with a number,
        over its groups: MySQL groups 'a' with 'a ' under utf8mb4_general_ci,
        and LIKE 'a' matches only the first; '1' with '１' under
        utf8mb4_0900_ai_ci, and = 1 converts only the first to 1."""
        with pytest.raises(ResolutionError) as caught:
            facts_on(
                dialect, {"select": {"dimensions": ["Label"], "measures": ["Median F", measure]}}
            )

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_AGGREGATION"
        assert f"compares the values of 'Facts.{column}' otherwise than {dialect}" in (
            error.message
        )

    def test_from_rows_a_filter_on_a_mysql_string_runs_in_the_leg(self, facts_on: Any) -> None:
        """'A' equals 'a' to MySQL: the leg evaluates the measure's filter in
        MySQL, and the combine step reads its outcome; keys injected keep it."""
        query = {
            "select": {"dimensions": ["Label"], "measures": ["Median A F"]},
            "where": [{"field": "Label", "op": "=", "value": "x"}],
        }

        plan = facts_on("mysql", query)

        predicate = "CASE WHEN `__leg`.`__value1` = 'a' THEN 1 ELSE 0 END AS `__mf0`"
        assert predicate in plan.legs[0].sql
        assert dict(plan.legs[0].columns)["__mf0"] == "int"
        assert 'CASE WHEN "leg_0"."__mf0" = 1 THEN' in plan.combine_sql
        assert plan.inject is not None
        assert predicate in plan.inject({"__fk0_0": [1]}).sql

    def test_from_rows_a_filter_its_engine_compares_as_duckdb_runs_in_the_combine(
        self, facts_on: Any
    ) -> None:
        plan = facts_on(
            "postgres", {"select": {"dimensions": ["Label"], "measures": ["Median A F"]}}
        )

        assert "__mf" not in plan.legs[0].sql
        assert "= 'a'" in plan.combine_sql

    def test_a_value_of_another_source_its_engine_compares_otherwise_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filter on a MySQL label would compare 'a' and 'A' as two values in
        the combine step, where MySQL sees one."""
        pytest.importorskip("ob_flight")
        monkeypatch.setenv("OBSL_DATABASES", "facts,labels")
        monkeypatch.setenv("DB_FACTS_TYPE", "postgres")
        monkeypatch.setenv("DB_LABELS_TYPE", "mysql")
        yaml = EQUALITY + (
            "  A Rows:\n"
            "    columns: [{dataObject: Facts, column: Key}]\n"
            "    aggregation: count\n"
            "    filters:\n"
            "      - column: {dataObject: Groups, column: Label}\n"
            "        operator: equals\n"
            "        values: [{dataType: string, valueString: a}]\n"
        )

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, {"select": {"dimensions": ["Name"], "measures": ["A Rows"]}})

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_AGGREGATION"
        assert "values of 'Groups.Label' otherwise than mysql" in error.message

    @pytest.mark.parametrize(
        ("dialect", "measure"),
        [("mysql", "Mode F"), ("clickhouse", "Mode Name"), ("clickhouse", "Least Name")],
    )
    def test_from_rows_values_their_engine_compares_as_duckdb_combine(
        self, facts_on: Any, dialect: str, measure: str
    ) -> None:
        plan = facts_on(
            dialect, {"select": {"dimensions": ["Label"], "measures": ["Median F", measure]}}
        )

        assert plan.legs[0].rows is not None

    def test_a_clickhouse_dimension_is_left_alone(self, facts_on: Any) -> None:
        plan = facts_on(
            "clickhouse", {"select": {"dimensions": ["Label", "Name"], "measures": ["Rows"]}}
        )

        assert "__leg" not in plan.legs[0].sql

    def test_a_combine_filter_on_a_class_runs_in_its_leg(self, facts_on: Any) -> None:
        """A condition after the join on a MySQL string runs in its leg, by
        MySQL's collation, which returns its outcome for the combine step to
        test; the dimension shown and grouped is its class."""
        plan = facts_on(
            "mysql",
            {
                "select": {"dimensions": ["Label", "Name"], "measures": ["Rows"]},
                "where": [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Label", "op": "=", "value": "x"},
                            {"field": "Name", "op": "=", "value": "a"},
                        ],
                    }
                ],
            },
        )

        facts = plan.legs[0]
        (tested,) = [c for c in dict(facts.columns) if c.startswith("__wf")]
        assert (
            f"CASE WHEN `__leg`.`Name` = 'a' THEN 1 WHEN NOT `__leg`.`Name` = 'a' THEN 0 END"
            f" AS `{tested}`"
        ) in facts.sql
        assert dict(facts.columns)[tested] == "int"
        assert f'"leg_0"."{tested}" = 1' in plan.combine_sql
        assert "'a'" not in plan.combine_sql
        # Label is read from a source comparing as DuckDB: the combine step compares it.
        assert '"leg_1"."Label" = \'x\'' in plan.combine_sql
        assert all(not c.startswith("__wf") for c in dict(plan.legs[1].columns))

    def test_a_dimension_only_filtered_is_not_rewritten(self, facts_on: Any) -> None:
        plan = facts_on(
            "mysql",
            {
                "select": {"dimensions": ["Label"], "measures": ["Rows"]},
                "where": [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Label", "op": "=", "value": "x"},
                            {"field": "Name", "op": "=", "value": "a"},
                        ],
                    }
                ],
            },
        )

        assert "FIRST_VALUE" not in plan.legs[0].sql
        assert "`__leg`.`Name` AS `Name`" in plan.legs[0].sql

    @pytest.mark.parametrize("dialect", ["postgres", "snowflake", "bigquery", "databricks"])
    def test_an_engine_comparing_as_duckdb_is_left_alone(self, facts_on: Any, dialect: str) -> None:
        plan = facts_on(
            dialect, {"select": {"dimensions": ["Label", "Name"], "measures": ["Names", "Floats"]}}
        )

        assert all("__leg" not in leg.sql for leg in plan.legs)


class TestGeneratedNames:
    """A column a leg gets beyond the model's takes a name the model does not use."""

    def test_a_kept_value_does_not_take_a_dimensions_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dimension named ``__eqv0``, an integer, selected beside a MySQL
        string a combine filter compares (a pattern, which the grouped fact leg
        cannot evaluate): the string's kept value took that name, and the
        integer column showed strings."""
        pytest.importorskip("ob_flight")
        monkeypatch.setenv("OBSL_DATABASES", "facts,labels")
        monkeypatch.setenv("DB_FACTS_TYPE", "mysql")
        monkeypatch.setenv("DB_LABELS_TYPE", "postgres")
        yaml = EQUALITY.replace(
            "dimensions:\n",
            "dimensions:\n  __eqv0: {dataObject: Facts, column: Key, resultType: int}\n",
        )

        plan = _compile(
            yaml,
            {
                # Label filtered, not selected: Name is the only value kept.
                "select": {"dimensions": ["Name", "__eqv0"], "measures": ["Rows"]},
                "where": [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Label", "op": "=", "value": "x"},
                            {"field": "Name", "op": "like", "value": "a%"},
                        ],
                    }
                ],
            },
        ).federation

        facts = plan.legs[0]
        names = [name for name, _ in facts.columns]
        assert len(names) == len(set(names))
        assert dict(facts.columns)["__eqv0"] == "int"
        (kept,) = [n for n in names if "eqv" in n and n != "__eqv0"]
        assert f"`__leg`.`Name` AS `{kept}`" in facts.sql
        assert f'"leg_0"."{kept}" LIKE \'a%\'' in plan.combine_sql
        assert '"leg_0"."__eqv0" AS "__eqv0"' in plan.combine_sql

    def test_keys_and_static_filters_do_not_take_a_dimensions_name(
        self, files: dict[str, str]
    ) -> None:
        """Dimensions named like the edge key and the static filter's column."""

        def yaml(federated: bool) -> str:
            return model_yaml(federated=federated, tenant_filter=True).replace(
                "dimensions:\n",
                "dimensions:\n"
                "  __fk0_0: {dataObject: Orders, column: Order ID, resultType: int}\n"
                "  __sf0_0: {dataObject: Orders, column: Order Date, resultType: date}\n",
            )

        query = {
            "select": {"dimensions": ["Region", "__fk0_0", "__sf0_0"], "measures": ["Revenue"]}
        }
        single = _compile(yaml(False), query)
        con = duckdb.connect(files["all"], read_only=True)
        try:
            expected = sorted(_rows(con, single.sql), key=repr)
        finally:
            con.close()
        federated = _compile(yaml(True), query)
        combine = duckdb.connect()
        for leg in federated.federation.legs:
            names = [name for name, _ in leg.columns]
            assert len(names) == len(set(names))
            con = duckdb.connect(files[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()

        assert sorted(_rows(combine, federated.sql), key=repr) == expected
        assert expected


# Customers' measures over their sales: one database counts each customer once
# per group (``grain_dedup``). Sales reach products on another source.
REPEATED = """\
version: 1.0
dataObjects:
  Sales:
    code: sales
{facts}    columns:
      ID: {{code: id, abstractType: int, primaryKey: true}}
      Customer: {{code: customer, abstractType: int}}
      Product: {{code: product, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Customers, columnsFrom: [Customer], columnsTo: [ID]}}
      - {{joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [ID]}}
  Customers:
    code: customers
{facts}    columns:
      ID: {{code: id, abstractType: int, primaryKey: true}}
      Age: {{code: age, abstractType: int}}
      Region: {{code: region, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Regions, columnsFrom: [Region], columnsTo: [ID]}}
  Products:
    code: products
{labels}    columns:
      ID: {{code: id, abstractType: int, primaryKey: true}}
      Category: {{code: category, abstractType: string}}
  Regions:
    code: regions
{labels}    columns:
      ID: {{code: id, abstractType: int, primaryKey: true}}
      Name: {{code: name, abstractType: string}}
dimensions:
  Category: {{dataObject: Products, column: Category, resultType: string}}
  Region: {{dataObject: Regions, column: Name, resultType: string}}
  Product ID: {{dataObject: Sales, column: Product, resultType: int}}
  Age: {{dataObject: Customers, column: Age, resultType: int}}
measures:
  Ages: {{columns: [{{dataObject: Customers, column: Age}}], aggregation: {aggregation}}}
  Ages Total:
    columns: [{{dataObject: Customers, column: Age}}]
    aggregation: {aggregation}
    total: true
  Doubled Ages: {{expression: "{{[Customers].[Age]}} * 2", aggregation: {aggregation}}}
  Adult Ages:
    columns: [{{dataObject: Customers, column: Age}}]
    aggregation: {aggregation}
    filters:
      - column: {{dataObject: Customers, column: Age}}
        operator: gte
        values: [{{dataType: int, valueInt: 18}}]
metrics:
  Ages per Sale: {{expression: "{{[Ages]}} / {{[Sales Count]}}"}}
"""

REPEATED_TABLES = [
    # Sale 5's customer is missing.
    "CREATE TABLE sales AS SELECT * FROM (VALUES (1, 1, 10), (2, 1, 11), (3, 2, 10), (4, 3, 11),"
    " (5, 9, 10)) t(id, customer, product)",
    # Customer 4 has no sales.
    "CREATE TABLE customers AS SELECT * FROM (VALUES (1, 10, 1), (2, 20, 1), (3, 40, 2),"
    " (4, 70, 2)) t(id, age, region)",
    "CREATE TABLE products AS SELECT * FROM (VALUES (10, 'A'), (11, 'A')) t(id, category)",
    "CREATE TABLE regions AS SELECT * FROM (VALUES (1, 'north'), (2, 'south')) t(id, name)",
]


def _repeated(
    aggregation: str,
    query: dict[str, Any],
    change: Callable[[str], str] = lambda yaml: yaml,
    tables: list[str] = REPEATED_TABLES,
) -> tuple[list[Any], list[Any]]:
    """*query* over the customers' ages on one source, and across two.

    *change* rewrites the model, both ways; *tables* hold its rows.
    """

    def yaml(federated: bool) -> str:
        facts = "    dataSource: facts\n" if federated else ""
        labels = "    dataSource: labels\n" if federated else ""
        return change(REPEATED.format(facts=facts, labels=labels, aggregation=aggregation))

    single = _compile(yaml(False), query)
    federated = _compile(yaml(True), query)
    con = duckdb.connect()
    try:
        for statement in tables:
            con.execute(statement)
        expected = sorted(_rows(con, single.sql), key=repr)
        combine = duckdb.connect()
        for leg in federated.federation.legs:
            combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
        return expected, sorted(_rows(combine, federated.sql), key=repr)
    finally:
        con.close()


def _assert_same_rows(got: list[Any], expected: list[Any]) -> None:
    """*got* is *expected*, a double to twelve digits: DuckDB adds a variance's
    doubles in no fixed order, so the grand total of 700 came back
    699.9999999999999 or 700.0000000000001 from one run to the next."""
    assert len(got) == len(expected)
    for row, want in zip(got, expected, strict=True):
        assert row == tuple(
            pytest.approx(v, rel=1e-12) if isinstance(v, float) else v for v in want
        )
    assert expected


class TestRepeatedRows:
    """A measure counted once per row of an object the joins repeat."""

    @pytest.mark.parametrize("aggregation", ["sum", "count", "avg", "variance", "median"])
    @pytest.mark.parametrize("beside", [[], ["Sales Count"]])
    def test_keys_on_the_many_side_come_from_rows(
        self, aggregation: str, beside: list[str]
    ) -> None:
        """Customer 1's sales reach products 10 and 11, both category A: one
        database counts the customer once in A, a fact leg of partial results
        once per product (the sum of ages 10 and 20 was 40, the variance 33.3
        for 50). From rows, the combine step counts each customer once."""
        query = {"select": {"dimensions": ["Category"], "measures": ["Ages", *beside]}}

        expected, got = _repeated(aggregation, query)

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("aggregation", ["sum", "count", "avg"])
    def test_an_expression_a_filter_and_a_metric_from_rows(self, aggregation: str) -> None:
        query = {
            "select": {
                "dimensions": ["Category"],
                "measures": ["Doubled Ages", "Adult Ages", "Ages per Sale", "Sales Count"],
            }
        }

        expected, got = _repeated(aggregation, query)

        _assert_same_rows(got, expected)

    def test_measures_of_two_objects_from_rows(self) -> None:
        """One database counts each customer and each product once, and adds
        none without sales: it does that for one such object, not several."""

        def products_with_sales(yaml: str) -> str:
            return yaml.replace(
                "  Products:\n    code: products\n    dataSource: labels\n",
                "  Products:\n    code: products\n    dataSource: facts\n",
            ).replace(
                "metrics:\n",
                "  Products Counted: {columns: [{dataObject: Products, column: ID}], "
                "aggregation: count}\nmetrics:\n",
            )

        query = {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Ages", "Products Counted", "Sales Count"],
            }
        }

        expected, got = _repeated("sum", query, products_with_sales)

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("aggregation", ["min", "max", "first", "last", "count_distinct"])
    def test_another_measure_of_the_object_keeps_its_rows_without_sales(
        self, aggregation: str
    ) -> None:
        """Read from the fact rows, customer 4, who has no sales, was left out:
        a count of distinct ages of no category was 0 for 1."""
        other = (
            "  Other Age:\n    columns: [{dataObject: Customers, column: Age}]\n"
            f"    aggregation: {aggregation}\n    resultType: int\n"
        )
        if aggregation in ("first", "last"):
            # Ordered by the customer's identity, which the relation holds too.
            other += "    withinGroup: {column: {dataObject: Customers, column: ID}}\n"
        query = {"select": {"dimensions": ["Category"], "measures": ["Ages", "Other Age"]}}

        expected, got = _repeated(
            "sum", query, lambda yaml: yaml.replace("metrics:\n", other + "metrics:\n")
        )

        assert got == expected
        assert (None, 70, 70) in expected or (None, 70, 1) in expected

    def test_a_measure_filtered_on_an_object_it_joins_many_to_one(self) -> None:
        """The filter's region column is the customer's: the relation holds it."""

        def regions_with_customers(yaml: str) -> str:
            return yaml.replace(
                "  Regions:\n    code: regions\n    dataSource: labels\n",
                "  Regions:\n    code: regions\n    dataSource: facts\n",
            ).replace(
                "      - column: {dataObject: Customers, column: Age}\n"
                "        operator: gte\n"
                "        values: [{dataType: int, valueInt: 18}]\n",
                "      - column: {dataObject: Regions, column: Name}\n"
                "        operator: equals\n"
                "        values: [{dataType: string, valueString: south}]\n",
            )

        query = {"select": {"dimensions": ["Category"], "measures": ["Adult Ages", "Sales Count"]}}

        expected, got = _repeated("sum", query, regions_with_customers)

        assert got == expected
        assert (None, 70, 0) in expected

    @pytest.mark.parametrize(
        "condition",
        [
            "      - column: {dataObject: Sales, column: Product}\n"
            "        operator: gte\n"
            "        values: [{dataType: int, valueInt: 11}]\n",
            "      - logic: or\n"
            "        filters:\n"
            "          - column: {dataObject: Customers, column: Age}\n"
            "            operator: gte\n"
            "            values: [{dataType: int, valueInt: 40}]\n"
            "          - column: {dataObject: Sales, column: Product}\n"
            "            operator: equals\n"
            "            values: [{dataType: int, valueInt: 11}]\n",
            "      - column: {dataObject: Sales, column: Product}\n        operator: notset\n",
        ],
        ids=["a_sale_column", "or_a_customer_column", "unset"],
    )
    def test_a_measure_filtered_on_the_many_side(self, condition: str) -> None:
        """Ages of the customers with a sale of product 11: one database
        filters the joined rows, then counts each customer once per group;
        the combine step filters the fact rows, the customers' rows once
        each holding none of the sales' columns."""

        def on_sales(yaml: str) -> str:
            return yaml.replace(
                "      - column: {dataObject: Customers, column: Age}\n"
                "        operator: gte\n"
                "        values: [{dataType: int, valueInt: 18}]\n",
                condition,
            )

        query = {"select": {"dimensions": ["Category"], "measures": ["Adult Ages", "Ages"]}}

        expected, got = _repeated("sum", query, on_sales)

        assert got == expected
        assert expected
        yaml = on_sales(
            REPEATED.format(
                facts="    dataSource: facts\n",
                labels="    dataSource: labels\n",
                aggregation="sum",
            )
        )
        combine_sql = _compile(yaml, query).sql
        once = combine_sql[combine_sql.index('"leg_0_once0" AS (') :]
        assert once[: once.index("FROM")].count('"__value') == 2

    def test_a_dimension_read_through_two_such_objects_is_refused(self) -> None:
        """The owners' region beside the accounts' credits and the owners'
        ages: one database adds each measure's own rows no sale reaches, an
        account without sales to its owner's region; one relation of the
        owners' rows cannot hold both."""
        yaml = """\
version: 1.0
dataObjects:
  Sales:
    code: sales
    dataSource: facts
    columns:
      ID: {code: id, abstractType: int, primaryKey: true}
      Account: {code: account, abstractType: int}
      Product: {code: product, abstractType: int}
    joins:
      - {joinType: many-to-one, joinTo: Accounts, columnsFrom: [Account], columnsTo: [ID]}
      - {joinType: many-to-one, joinTo: Products, columnsFrom: [Product], columnsTo: [ID]}
  Accounts:
    code: accounts
    dataSource: facts
    columns:
      ID: {code: id, abstractType: int, primaryKey: true}
      Owner: {code: owner, abstractType: int}
      Credit: {code: credit, abstractType: int}
    joins:
      - {joinType: many-to-one, joinTo: Owners, columnsFrom: [Owner], columnsTo: [ID]}
  Owners:
    code: owners
    dataSource: facts
    columns:
      ID: {code: id, abstractType: int, primaryKey: true}
      Age: {code: age, abstractType: int}
      Region: {code: region, abstractType: string}
  Products:
    code: products
    dataSource: labels
    columns:
      ID: {code: id, abstractType: int, primaryKey: true}
      Category: {code: category, abstractType: string}
dimensions:
  Category: {dataObject: Products, column: Category, resultType: string}
  Region: {dataObject: Owners, column: Region, resultType: string}
measures:
  Ages: {columns: [{dataObject: Owners, column: Age}], aggregation: sum}
  Credits: {columns: [{dataObject: Accounts, column: Credit}], aggregation: sum}
"""
        query = {
            "select": {
                "dimensions": ["Region", "Category"],
                "measures": ["Ages", "Credits", "Sales Count"],
            }
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_AGGREGATION"
        assert "reads dimension 'Region' through each of Accounts, Owners" in error.message

    def test_the_combine_step_reads_the_objects_rows_once_each(self) -> None:
        yaml = REPEATED.format(
            facts="    dataSource: facts\n", labels="    dataSource: labels\n", aggregation="sum"
        )
        compiled = _compile(yaml, {"select": {"dimensions": ["Category"], "measures": ["Ages"]}})

        fact = compiled.federation.legs[0]
        assert fact.rows == "__rows"
        # The customer's identity and age, once each, joined from the fact rows.
        assert (
            'SELECT DISTINCT "__value0", "__value1"\nFROM "leg_0"\nWHERE "__value1" IS NOT NULL'
        ) in compiled.sql
        assert 'LEFT JOIN "leg_0_once0" AS "leg_0_once0"' in compiled.sql
        assert compiled.warnings == []

    @pytest.mark.parametrize("aggregation", ["min", "max", "count_distinct"])
    def test_what_repeated_rows_do_not_change_combines(self, aggregation: str) -> None:
        expected, got = _repeated(
            aggregation, {"select": {"dimensions": ["Category"], "measures": ["Ages"]}}
        )

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("aggregation", ["sum", "count", "avg", "variance"])
    def test_a_filter_after_the_join_on_the_many_side_comes_from_rows(
        self, aggregation: str
    ) -> None:
        """The join starts at customers, but the OR runs after the join, so the
        fact leg groups by the product ID it reads too: customer 1 has two."""
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Ages"]},
            "where": [
                {
                    "logic": "or",
                    "filters": [
                        {"field": "Product ID", "op": ">", "value": 0},
                        {"field": "Region", "op": "=", "value": "nowhere"},
                    ],
                }
            ],
        }

        expected, got = _repeated(aggregation, query)

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("aggregation", ["sum", "count", "avg", "variance"])
    def test_a_filter_after_the_join_on_the_object_itself_combines(self, aggregation: str) -> None:
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Ages", "Sales Count"]},
            "where": [
                {
                    "logic": "or",
                    "filters": [
                        {"field": "Age", "op": "<", "value": 30},
                        {"field": "Region", "op": "=", "value": "south"},
                    ],
                }
            ],
        }

        expected, got = _repeated(aggregation, query)

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("grouping", ["rollup", "cube"])
    @pytest.mark.parametrize("aggregation", ["sum", "variance"])
    def test_subtotals_across_the_many_side_come_from_rows(
        self, grouping: str, aggregation: str
    ) -> None:
        """Per region and product a leg's groups are right, but the region's
        subtotal added customer 1's two product groups: ages summed to 40 for
        30, their variance 33.3 for 50."""
        query = {
            "select": {"dimensions": ["Region", "Product ID"], "measures": ["Ages"]},
            "grouping": grouping,
        }

        _assert_same_rows(*reversed(_repeated(aggregation, query)))

    @pytest.mark.parametrize("grouping", ["rollup", "cube"])
    @pytest.mark.parametrize("aggregation", ["sum", "variance"])
    def test_subtotals_across_the_object_itself_combine(
        self, grouping: str, aggregation: str
    ) -> None:
        query = {
            "select": {"dimensions": ["Region", "Age"], "measures": ["Ages", "Sales Count"]},
            "grouping": grouping,
        }

        _assert_same_rows(*reversed(_repeated(aggregation, query)))

    @pytest.mark.parametrize("dimension", ["Product ID", "Age"])
    def test_a_total(self, dimension: str) -> None:
        """A total adds every group, as a grand total does: across products,
        partial results made it 80 for 70."""
        query = {"select": {"dimensions": ["Region", dimension], "measures": ["Ages Total"]}}

        expected, got = _repeated("sum", query)

        _assert_same_rows(got, expected)

    def test_a_grain(self) -> None:
        """A grain adds the groups across the others, as a total does: across
        products, partial results made a region's ages 40 for 30."""
        grain = (
            "  Ages by Region:\n"
            "    columns: [{dataObject: Customers, column: Age}]\n"
            "    aggregation: sum\n"
            "    grain: {mode: FIXED, include: [Region]}\n"
        )
        query = {"select": {"dimensions": ["Region", "Product ID"], "measures": ["Ages by Region"]}}

        expected, got = _repeated(
            "sum", query, lambda yaml: yaml.replace("metrics:\n", grain + "metrics:\n", 1)
        )

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("aggregation", ["sum", "avg", "variance", "count"])
    def test_a_filter_context(self, aggregation: str) -> None:
        """The ages of customers buying in any category, beside those in A."""
        context = (
            "  Ages Any Category:\n"
            "    columns: [{dataObject: Customers, column: Age}]\n"
            f"    aggregation: {aggregation}\n"
            "    filterContext: {exclude: [Category]}\n"
        )
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Ages", "Ages Any Category"]},
            "where": [{"field": "Category", "op": "=", "value": "A"}],
        }

        expected, got = _repeated(
            aggregation, query, lambda yaml: yaml.replace("metrics:\n", context + "metrics:\n", 1)
        )

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("aggregation", ["sum", "count"])
    def test_a_filter_context_beside_the_sales_count(self, aggregation: str) -> None:
        """The query's own deduplication leaves a filter context's measure to
        its scan: as partial results across products, customer 1's age came
        out 40 for 30 in the north, its count 3 for 2."""
        context = (
            "  Ages Any Category:\n"
            "    columns: [{dataObject: Customers, column: Age}]\n"
            f"    aggregation: {aggregation}\n"
            "    filterContext: {exclude: [Category]}\n"
        )
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Sales Count", "Ages Any Category"]},
            "where": [{"field": "Category", "op": "=", "value": "A"}],
        }

        expected, got = _repeated(
            aggregation, query, lambda yaml: yaml.replace("metrics:\n", context + "metrics:\n", 1)
        )

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize(
        ("dimension", "static"),
        [
            ("Region", "{dataObject: Sales, column: ID, operator: gte, value: 1}"),
            ("Category", "{dataObject: Customers, column: Age, operator: gte, value: 15}"),
        ],
        ids=["on_the_sales", "on_the_customers"],
    )
    def test_a_static_filter_a_context_leaves_out(self, dimension: str, static: str) -> None:
        """The fact leg groups by the filter's column too. Each sale apart
        made customer 1's age 40 for 30. The customers' filter is the
        customers' own: customer 4, without sales, keeps its group."""
        unfiltered = (
            "  Ages Unfiltered:\n"
            "    columns: [{dataObject: Customers, column: Age}]\n"
            "    aggregation: sum\n"
            "    filterContext: {mode: FIXED}\n"
        )
        query = {
            "select": {
                "dimensions": [dimension],
                "measures": ["Sales Count", "Ages", "Ages Unfiltered"],
            }
        }

        expected, got = _repeated(
            "sum",
            query,
            lambda yaml: (
                yaml.replace("metrics:\n", unfiltered + "metrics:\n", 1)
                + f"filters:\n  - {static}\n"
            ),
        )

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("aggregation", ["sum", "count", "avg", "variance"])
    def test_keys_of_the_object_itself_combine(self, aggregation: str) -> None:
        """The join to regions starts from customers: each has one key."""
        query = {"select": {"dimensions": ["Region"], "measures": ["Ages", "Sales Count"]}}

        expected, got = _repeated(aggregation, query)

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("aggregation", ["sum", "count", "avg", "variance"])
    def test_a_filter_after_the_join_on_the_day_of_a_month_comes_from_rows(
        self, aggregation: str
    ) -> None:
        """Customer 1 bought on two days of January: the fact leg groups by the
        day the filter reads too, and counted the customer twice (40 for 30)."""
        query = {
            "select": {"dimensions": ["Sale Date:month"], "measures": ["Ages", "Sales Count"]},
            "where": [
                _either({"field": "Sale Date", "op": ">", "value": "2024-01-06"}, NORTH),
            ],
        }

        expected, got = _repeated(aggregation, query, _dated, DATED_TABLES)

        _assert_same_rows(got, expected)

    @pytest.mark.parametrize("metric", ["Running Ages", "Ranked Ages"])
    def test_beside_a_metric_across_periods_the_days_count_once(self, metric: str) -> None:
        """The fact leg groups the sales by their dates, finer than the month:
        customer 1 bought on two days of January, and counted twice (40 for 30)."""

        def with_metrics(yaml: str) -> str:
            return _dated(yaml).replace(
                "metrics:\n",
                "metrics:\n"
                "  Running Ages: {type: cumulative, measure: Ages, timeDimension: Sale Date}\n"
                "  Ranked Ages: {type: window, measure: Ages, windowFunction: dense_rank}\n",
            )

        query = {
            "select": {
                "dimensions": ["Sale Date:month", "Region"],
                "measures": ["Ages", "Sales Count", metric],
            }
        }

        expected, got = _repeated("sum", query, with_metrics, DATED_TABLES)

        _assert_same_rows(got, expected)

    def test_a_filter_after_the_join_on_a_parents_month_reads_it_once(self) -> None:
        """Customer 4 signed up in June and bought nothing: one database
        groups it in June, from the customers' relation, which holds the day
        the filter reads too."""
        query = {
            "select": {
                "dimensions": ["Signup Date:month"],
                "measures": ["Ages", "Median Age", "Sales Count"],
            },
            "where": [
                _either(
                    {"field": "Signup Date", "op": ">=", "value": "2023-06-10"},
                    {"field": "Region", "op": "=", "value": "nowhere"},
                )
            ],
        }

        expected, got = _repeated("sum", query, _dated, DATED_TABLES)

        assert got == expected == [(date(2023, 6, 1), 70, 70.0, 0)]


# Each sale with its day, each customer with the day it signed up.
DATED_TABLES = [
    "CREATE TABLE sales AS SELECT * FROM (VALUES (1, 1, 10, DATE '2024-01-05'),"
    " (2, 1, 11, DATE '2024-01-20'), (3, 2, 10, DATE '2024-01-05'),"
    " (4, 3, 11, DATE '2024-02-03'), (5, 9, 10, DATE '2024-02-10'))"
    " t(id, customer, product, sale_date)",
    "CREATE TABLE customers AS SELECT * FROM (VALUES (1, 10, 1, DATE '2023-05-02'),"
    " (2, 20, 1, DATE '2023-05-20'), (3, 40, 2, DATE '2023-06-01'),"
    " (4, 70, 2, DATE '2023-06-15')) t(id, age, region, signup)",
    *REPEATED_TABLES[2:],
]
NORTH = {"field": "Region", "op": "=", "value": "north"}


def _dated(yaml: str) -> str:
    """The sales and customers model with each sale's day and customer's signup."""
    return (
        yaml.replace(
            "      Product: {code: product, abstractType: int}\n",
            "      Product: {code: product, abstractType: int}\n"
            "      Sale Date: {code: sale_date, abstractType: date}\n",
        )
        .replace(
            "      Region: {code: region, abstractType: int}\n",
            "      Region: {code: region, abstractType: int}\n"
            "      Signup: {code: signup, abstractType: date}\n",
        )
        .replace(
            "dimensions:\n",
            "dimensions:\n"
            "  Sale Date: {dataObject: Sales, column: Sale Date, resultType: date}\n"
            "  Signup Date: {dataObject: Customers, column: Signup, resultType: date}\n",
        )
        .replace(
            "metrics:\n",
            "  Median Age: {columns: [{dataObject: Customers, column: Age}], aggregation: median}\n"
            "metrics:\n",
        )
    )


PRICED = """\
version: 1.0
settings:
  defaultDialect: duckdb
  queryTimezone: Europe/Berlin
dataObjects:
  Lines:
    code: lines
{shop}    columns:
      Line ID: {{code: line_id, abstractType: int}}
      Line Product: {{code: product_id, abstractType: int}}
      Line Batch: {{code: batch_id, abstractType: int}}
      Qty: {{code: qty, abstractType: int, numClass: additive}}
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Line Product]
        columnsTo: [Product ID]
      - {{joinType: many-to-one, joinTo: Batches, columnsFrom: [Line Batch], columnsTo: [Batch ID]}}
  Batches:
    code: batches
{shop}    columns:
      Batch ID: {{code: batch_id, abstractType: int}}
      Weight: {{code: weight, abstractType: int}}
  Products:
    code: products
{catalog}    columns:
      Product ID: {{code: product_id, abstractType: int}}
      Price: {{code: price, abstractType: decimal, sqlPrecision: 10, sqlScale: 2}}
      Category: {{code: category, abstractType: string}}
      Listed: {{code: listed, abstractType: timestamp_tz}}
dimensions:
  Category: {{dataObject: Products, column: Category, resultType: string}}
  Batch: {{dataObject: Batches, column: Batch ID, resultType: int}}
measures:
  Units: {{columns: [{{dataObject: Lines, column: Qty}}], aggregation: sum, resultType: int}}
  Sales:
    expression: "{{[Lines].[Qty]}} * {{[Products].[Price]}}"
    aggregation: sum
    resultType: decimal
  Average Line Value:
    expression: "{{[Lines].[Qty]}} * {{[Products].[Price]}}"
    aggregation: avg
    resultType: decimal
  Median Line Value:
    expression: "{{[Lines].[Qty]}} * {{[Products].[Price]}}"
    aggregation: median
    resultType: decimal
  Tool Units:
    columns: [{{dataObject: Lines, column: Qty}}]
    aggregation: sum
    filters:
      - column: {{dataObject: Products, column: Category}}
        operator: equals
        values: [{{dataType: string, valueString: tools}}]
    resultType: int
  First Listed Qty:
    columns: [{{dataObject: Lines, column: Qty}}]
    aggregation: first
    withinGroup: {{column: {{dataObject: Products, column: Listed}}}}
    resultType: int
  Tool Weight:
    columns: [{{dataObject: Batches, column: Weight}}]
    aggregation: sum
    filters:
      - column: {{dataObject: Products, column: Category}}
        operator: equals
        values: [{{dataType: string, valueString: tools}}]
    resultType: int
metrics:
  Price per Unit: {{expression: "{{[Sales]}} / {{[Units]}}"}}
"""
LINES = (
    "CREATE TABLE lines AS SELECT * FROM (VALUES"
    " (1, 1, 1, 2), (2, 1, 1, 3), (3, 2, 2, 1), (4, 3, 2, 5),"
    " (5, 99, 1, 4), (6, 2, 2, 2), (7, 4, 1, 6)) t(line_id, product_id, batch_id, qty)"
)
BATCHES = "CREATE TABLE batches AS SELECT * FROM (VALUES (1, 10), (2, 20)) t(batch_id, weight)"
# Products 1 and 2 are listed at 02:30 in Berlin both, the night it falls back.
PRODUCTS = (
    "CREATE TABLE products AS SELECT * FROM (VALUES"
    " (1, 10.50::DECIMAL(10, 2), 'tools', TIMESTAMPTZ '2024-10-27 00:30:00+00'),"
    " (2, 3.25, 'toys', TIMESTAMPTZ '2024-10-27 01:30:00+00'),"
    " (3, NULL, 'tools', TIMESTAMPTZ '2024-10-28 09:00:00+00'),"
    " (4, 7.00, NULL, NULL)) t(product_id, price, category, listed)"
)


@pytest.fixture(scope="module")
def priced(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("priced")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "catalog")}
    for name, statements in (
        ("all", [LINES, BATCHES, PRODUCTS]),
        ("shop", [LINES, BATCHES]),
        ("catalog", [PRODUCTS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


def _priced(federated: bool) -> str:
    shop, catalog = ("    dataSource: shop\n", "    dataSource: catalog\n")
    return PRICED.format(shop=shop if federated else "", catalog=catalog if federated else "")


ACROSS_QUERIES = {
    "by_the_other_sources_dimension": {
        "select": {
            "dimensions": ["Category"],
            "measures": ["Units", "Sales", "Tool Units", "Price per Unit"],
        }
    },
    "by_none": {"select": {"measures": ["Sales", "Tool Units", "First Listed Qty"]}},
    "by_a_fact_side_dimension": {
        "select": {
            "dimensions": ["Batch"],
            "measures": ["Sales", "Average Line Value", "Median Line Value"],
        }
    },
    "filtered_on_the_other_source": {
        "select": {"dimensions": ["Batch"], "measures": ["Sales", "Tool Units"]},
        "where": [{"field": "Category", "op": "!=", "value": "toys"}],
    },
    "ordered_and_limited": {
        "select": {"dimensions": ["Category"], "measures": ["Sales"]},
        "orderBy": [{"field": "Sales", "direction": "desc"}],
        "limit": 2,
    },
}


class TestMeasuresReadingAnotherSource:
    """A measure reading a column of another source: its dimension leg returns
    the column, and the combine step computes the measure after the join."""

    def _run(self, priced: dict[str, str], federated: bool, query: dict[str, Any]) -> list[Any]:
        compiled = _compile(_priced(federated), query)
        if not federated:
            assert compiled.federation is None
            con = duckdb.connect(priced["all"], read_only=True)
            try:
                return _rows(con, compiled.sql)
            finally:
                con.close()
        combine = legs_registered(priced, compiled.federation)
        return _rows(combine, compiled.sql)

    @pytest.mark.parametrize("query", ACROSS_QUERIES.values(), ids=ACROSS_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, priced: dict[str, str], query: dict[str, Any]
    ) -> None:
        single = self._run(priced, False, query)
        federated = self._run(priced, True, query)

        if "orderBy" in query:
            assert federated == single
        else:
            assert sorted(federated, key=repr) == sorted(single, key=repr)
        assert single

    def test_the_other_sources_leg_returns_the_columns_beside_its_keys(self) -> None:
        plan = _compile(
            _priced(True), {"select": {"dimensions": ["Batch"], "measures": ["Sales", "Units"]}}
        ).federation

        lines, products = plan.legs
        assert lines.rows is not None
        assert '"price"' in products.sql and '"price"' not in lines.sql
        assert [kind for c, kind in products.columns if c.startswith("__value")] == ["decimal"]
        assert '"leg_0"."__value0" * "leg_1"."__value1"' in plan.combine_sql

    def test_an_ordering_key_there_is_read_in_its_own_time(self, priced: dict[str, str]) -> None:
        """Read in Berlin time, products 1 and 2 were both listed at 02:30, and
        the tie went to the smaller quantity: the first came back 1, not 2."""
        query = {"select": {"measures": ["First Listed Qty"]}}

        assert self._run(priced, True, query) == self._run(priced, False, query) == [(2,)]
        products = _compile(_priced(True), query).federation.legs[1]
        assert '"Products"."listed" AS' in products.sql

    def test_a_measure_counted_once_per_row_of_its_object_is_refused(self) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(
                _priced(True), {"select": {"dimensions": ["Batch"], "measures": ["Tool Weight"]}}
            )

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_MEASURE_ACROSS_SOURCES"
        assert "counted once per row of 'Batches'" in error.message

    def test_dimensions_alone_of_two_other_sources_join_through_the_facts(
        self, priced: dict[str, str], tmp_path: Any
    ) -> None:
        """No dimension of the lines: one source reads them all the same, to
        pair each category with the batches its lines reach."""
        plant = str(tmp_path / "plant.duckdb")
        con = duckdb.connect(plant)
        con.execute(BATCHES)
        con.close()
        yaml = _priced(True).replace(
            "  Batches:\n    code: batches\n    dataSource: shop\n",
            "  Batches:\n    code: batches\n    dataSource: plant\n",
        )
        query = {"select": {"dimensions": ["Category", "Batch"]}}
        compiled = _compile(yaml, query)
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect({**priced, "plant": plant}[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()

        federated = sorted(_rows(combine, compiled.sql), key=repr)

        assert [leg.data_source for leg in compiled.federation.legs] == ["shop", "plant", "catalog"]
        assert federated == sorted(self._run(priced, False, query), key=repr)
        assert federated == sorted([("tools", 1), ("tools", 2), ("toys", 2), (None, 1)], key=repr)

    def test_a_column_computed_from_another_source_is_refused(self) -> None:
        yaml = (
            _priced(True)
            .replace(
                "      Qty: {code: qty, abstractType: int, numClass: additive}\n",
                "      Qty: {code: qty, abstractType: int, numClass: additive}\n"
                "      Gross:\n"
                '        expression: "{Qty} * {[Products].[Price]}"\n'
                "        abstractType: decimal\n",
            )
            .replace(
                "measures:\n",
                "measures:\n"
                "  Gross Sales: {columns: [{dataObject: Lines, column: Gross}], aggregation: sum,"
                " resultType: decimal}\n",
            )
        )

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, {"select": {"dimensions": ["Batch"], "measures": ["Gross Sales"]}})

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_MEASURE_ACROSS_SOURCES"
        assert "'Lines.Gross', which is computed from Products" in error.message


# Customer 10 has two tickets, 12 one; ticket 4's customer 98 is not a
# customer. Customer 10's tickets are raised under the inactive tenant
# 'other', on the basic plan. Orders 1 and 4 have returns. Order 6's
# customer 99 is missing: one database keeps it under nonexists, its
# subject row being NULL.
TICKETS = (
    "CREATE TABLE tickets AS SELECT * FROM (VALUES (1, 10, 'high', 'other'),"
    " (2, 10, 'low', 'other'), (3, 12, 'low', 'acme'), (4, 98, 'high', 'acme'))"
    " t(ticket_id, customer_id, severity, tenant)"
)
TENANTS = (
    "CREATE TABLE tenants AS SELECT * FROM (VALUES ('acme', true, 'gold'),"
    " ('other', false, 'basic')) t(tenant, active, plan)"
)
PLANS = (
    "CREATE TABLE plans AS SELECT * FROM (VALUES ('gold', 'Gold'), ('basic', 'Basic'))"
    " t(plan, tier)"
)
RETURNS = (
    "CREATE TABLE returns AS SELECT * FROM (VALUES (1, 1, 'broken'), (2, 4, 'late'),"
    " (3, 4, 'broken')) t(return_id, order_id, reason)"
)
REASONS = (
    "CREATE TABLE reasons AS SELECT * FROM (VALUES ('broken', 'Damaged'), ('late', 'Delayed'))"
    " t(reason, label)"
)
SUBQUERY_OBJECTS = """\
  Tickets:
    code: tickets
{crm}    columns:
      Ticket ID: {{code: ticket_id, abstractType: int}}
      Ticket Customer: {{code: customer_id, abstractType: int}}
      Severity: {{code: severity, abstractType: string}}
      Ticket Tenant: {{code: tenant, abstractType: string}}
      Ticket Tier: {{expression: "{{[Plans].[Tier]}}", abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Ticket Customer]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Tenants
        columnsFrom: [Ticket Tenant]
        columnsTo: [Tenant Code]
  Tenants:
    code: tenants
{crm}    columns:
      Tenant Code: {{code: tenant, abstractType: string}}
      Active: {{code: active, abstractType: boolean}}
      Plan: {{code: plan, abstractType: string}}
    joins:
      - {{joinType: many-to-one, joinTo: Plans, columnsFrom: [Plan], columnsTo: [Plan Code]}}
  Plans:
    code: plans
{crm}    columns:
      Plan Code: {{code: plan, abstractType: string}}
      Tier: {{code: tier, abstractType: string}}
  Returns:
    code: returns
{shop}    columns:
      Return ID: {{code: return_id, abstractType: int}}
      Return Order: {{code: order_id, abstractType: int}}
      Reason: {{code: reason, abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom: [Return Order]
        columnsTo: [Order ID]
      - {{joinType: many-to-one, joinTo: Reasons, columnsFrom: [Reason], columnsTo: [Reason Code]}}
  Reasons:
    code: reasons
{crm}    columns:
      Reason Code: {{code: reason, abstractType: string}}
      Reason Label: {{code: label, abstractType: string}}
"""


def subquery_model(federated: bool, **model: Any) -> str:
    """The orders and customers model with tickets on the CRM, returns on the shop."""
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    objects = SUBQUERY_OBJECTS.format(shop=shop, crm=crm)
    tenant = "      Tenant: {code: tenant, abstractType: string}\n"
    tenant_join = (
        "    joins:\n      - {joinType: many-to-one, joinTo: Tenants, columnsFrom: [Tenant],"
        " columnsTo: [Tenant Code]}\n"
    )
    return (
        model_yaml(federated=federated, **model)
        .replace(tenant, tenant + tenant_join, 1)
        .replace("dimensions:\n", objects + "dimensions:\n", 1)
    )


@pytest.fixture(scope="module")
def subquery_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("subquery")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [ORDERS, CUSTOMERS, TICKETS, RETURNS, REASONS, TENANTS, PLANS]),
        ("shop", [ORDERS, RETURNS]),
        ("crm", [CUSTOMERS, TICKETS, REASONS, TENANTS, PLANS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


def _tickets(op: str, *filters: dict[str, Any]) -> dict[str, Any]:
    subquery = {"dataObject": "Tickets", "filter": list(filters)}
    return {"field": "Customers.Customer ID", "op": op, "subquery": subquery}


def _returns(op: str) -> dict[str, Any]:
    return {"field": "Orders.Order ID", "op": op, "subquery": {"dataObject": "Returns"}}


HIGH = {"field": "Severity", "op": "=", "value": "high"}
GOLD = {"field": "Ticket Tier", "op": "=", "value": "Gold"}
ACTIVE = (
    "  - {dataObject: Tenants, column: Active, operator: equals, value: true, enforce: always}\n"
)
BY_MONTH = {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Orders Count"]}
BY_REGION = {"dimensions": ["Region"], "measures": ["Revenue", "Orders Count"]}

SUBQUERY_QUERIES = {
    "exists_on_the_other_source": {"select": BY_MONTH, "where": [_tickets("exists")]},
    "nonexists_on_the_other_source": {"select": BY_MONTH, "where": [_tickets("nonexists")]},
    "exists_filtered_in_its_subquery": {"select": BY_REGION, "where": [_tickets("exists", HIGH)]},
    "nonexists_filtered_in_its_subquery": {
        "select": BY_REGION,
        "where": [_tickets("nonexists", HIGH)],
    },
    "exists_on_the_facts_source": {"select": BY_REGION, "where": [_returns("exists")]},
    "nonexists_on_the_facts_source": {"select": BY_REGION, "where": [_returns("nonexists")]},
    "nonexists_beside_a_filter_there": {
        "select": BY_MONTH,
        "where": [_tickets("nonexists"), {"field": "Region", "op": "=", "value": "US"}],
    },
    "exists_in_a_group_on_its_source": {
        "select": BY_MONTH,
        "where": [
            {
                "logic": "or",
                "filters": [_tickets("exists"), {"field": "Region", "op": "=", "value": "US"}],
            }
        ],
    },
    "nonexists_on_both_sources": {
        "select": BY_REGION,
        "where": [_tickets("nonexists"), _returns("nonexists")],
    },
    "nonexists_without_measures": {
        "select": {"dimensions": ["Order Date:month", "Region"]},
        "where": [_tickets("nonexists")],
    },
}


class TestExists:
    """An exists subquery runs on its subject's data source, as one database runs it."""

    def _run(
        self, paths: dict[str, str], federated: bool, query: dict[str, Any], **model: Any
    ) -> list[Any]:
        compiled = _compile(subquery_model(federated, **model), query)
        if not federated:
            assert compiled.federation is None
            con = duckdb.connect(paths["all"], read_only=True)
            try:
                return _rows(con, compiled.sql)
            finally:
                con.close()
        combine = legs_registered(paths, compiled.federation)
        return _rows(combine, compiled.sql)

    @pytest.mark.parametrize("query", SUBQUERY_QUERIES.values(), ids=SUBQUERY_QUERIES.keys())
    @pytest.mark.parametrize("tenant_filter", [False, True], ids=["", "tenant"])
    def test_federated_returns_what_one_source_returns(
        self, subquery_files: dict[str, str], query: dict[str, Any], tenant_filter: bool
    ) -> None:
        single = self._run(subquery_files, False, query, tenant_filter=tenant_filter)
        federated = self._run(subquery_files, True, query, tenant_filter=tenant_filter)

        assert sorted(federated, key=repr) == sorted(single, key=repr)
        assert single

    @pytest.mark.parametrize("op", ["exists", "nonexists"])
    def test_an_enforced_filter_the_subquery_reads_applies_in_it_too(
        self, subquery_files: dict[str, str], op: str
    ) -> None:
        """Customers and tickets both join tenants: the filter on tenants runs
        after the join for the customers, and in the subquery for the tickets,
        whose tenant is not always their customer's."""
        query = {"select": BY_MONTH, "where": [_tickets(op)]}

        single = self._run(subquery_files, False, query, static=ACTIVE)
        federated = self._run(subquery_files, True, query, static=ACTIVE)

        assert sorted(federated, key=repr) == sorted(single, key=repr)
        # Customer 10's January orders: its tickets are of the inactive tenant.
        assert (Decimal("120.75") in {revenue for _, revenue, _ in single}) is (op == "nonexists")

    @pytest.mark.parametrize("op", ["exists", "nonexists"])
    def test_a_subquery_filter_computed_two_joins_away(
        self, subquery_files: dict[str, str], op: str
    ) -> None:
        """A ticket's tier is its tenant's plan's: the subquery joins both."""
        query = {"select": BY_REGION, "where": [_tickets(op, GOLD)]}

        single = self._run(subquery_files, False, query)
        federated = self._run(subquery_files, True, query)

        assert sorted(federated, key=repr) == sorted(single, key=repr)
        assert single

    def test_the_order_without_a_customer_is_kept_under_nonexists(
        self, subquery_files: dict[str, str]
    ) -> None:
        query = {"select": {"measures": ["Orders Count"]}, "where": [_tickets("nonexists")]}

        # Orders 3, 7 (customer 11), 5 (13) and 6 (99, missing).
        assert self._run(subquery_files, True, query) == [(4,)]

    def test_nonexists_on_the_other_source_reads_the_keys_it_holds_for(self) -> None:
        plan = _compile(
            subquery_model(True), {"select": BY_MONTH, "where": [_tickets("nonexists", HIGH)]}
        ).federation

        orders, customers, holding = plan.legs
        assert "EXISTS" not in orders.sql and "EXISTS" not in customers.sql
        assert holding.data_source == "crm" and "WHERE EXISTS (" in holding.sql
        assert "'high'" in holding.sql
        assert set(holding.physical_tables) == {"customers", "tickets"}
        assert f'"{holding.name}"."__fk0_0" IS NULL' in plan.combine_sql

    def test_exists_on_the_other_source_runs_in_its_leg_and_narrows_the_facts(self) -> None:
        plan = _compile(
            subquery_model(True), {"select": BY_MONTH, "where": [_tickets("exists")]}
        ).federation

        orders, customers = plan.legs
        assert "WHERE EXISTS (" in customers.sql
        assert [i.leg for i in plan.injections] == ["leg_1"]

    def test_exists_on_the_facts_source_runs_in_the_fact_leg(self) -> None:
        plan = _compile(
            subquery_model(True), {"select": BY_REGION, "where": [_returns("nonexists")]}
        ).federation

        orders, customers = plan.legs
        assert "WHERE NOT EXISTS (" in orders.sql
        assert "returns" in orders.physical_tables

    @pytest.mark.parametrize(
        "where",
        [
            [{"logic": "or", "filters": [_tickets("exists", HIGH), LATE]}],
            [{"logic": "or", "filters": [_tickets("nonexists"), LATE]}],
            [
                {
                    "logic": "or",
                    "filters": [_tickets("exists"), {"field": "Region", "op": "is_null"}],
                }
            ],
            [{"logic": "or", "negated": True, "filters": [_tickets("exists"), LATE]}],
            [{"logic": "or", "filters": [_tickets("exists", HIGH), _tickets("nonexists"), LATE]}],
            [
                {
                    "logic": "or",
                    "filters": [{"logic": "and", "filters": [_tickets("exists"), EU]}, LATE],
                }
            ],
        ],
        ids=[
            "an_exists_or_an_order_date",
            "a_nonexists_or_an_order_date",
            "in_a_group_true_where_the_customer_is_missing",
            "negated",
            "two_of_them",
            "in_an_and_inside_an_or",
        ],
    )
    def test_beside_a_condition_on_another_source_it_tests_a_leg_of_keys(
        self, subquery_files: dict[str, str], where: list[Any]
    ) -> None:
        """The combine step runs no subquery: a leg on the CRM returns the
        customers the subquery holds for, and the combine step tests a fact
        row's customer among them, set for exists, not for nonexists."""
        query = {"select": BY_REGION, "where": where}

        single = _run_on(subquery_files, subquery_model(False), query)

        assert _run_on(subquery_files, subquery_model(True), query) == single
        assert single

    @pytest.mark.parametrize("op", ["exists", "nonexists"])
    def test_a_context_keeping_its_field_keeps_the_test(
        self, subquery_files: dict[str, str], op: str
    ) -> None:
        """A measure keeping only the filters on ``Customers.Customer ID`` keeps
        the ``or`` its exists is in, which the combine step tests by another
        name: it dropped the group, and counted every order."""
        extra = _over_amount(
            "Customer Revenue", "filterContext: {keepOnly: [Customers.Customer ID]}"
        )
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Customer Revenue", "Revenue"]},
            "where": [{"logic": "or", "filters": [_tickets(op), LATE]}],
        }

        single = _run_on(subquery_files, subquery_model(False, extra=extra), query)

        assert _run_on(subquery_files, subquery_model(True, extra=extra), query) == single
        assert single

    def test_the_leg_of_keys_is_joined_and_tested(self) -> None:
        query = {
            "select": BY_REGION,
            "where": [{"logic": "or", "filters": [_tickets("exists", HIGH), LATE]}],
        }

        plan = _compile(subquery_model(True), query).federation

        keys = plan.legs[-1]
        assert "WHERE EXISTS (" in keys.sql
        assert keys.data_source == "crm"
        assert '"leg_2"."__fk0_0" IS NOT NULL' in plan.combine_sql

    def test_on_the_facts_source_beside_a_condition_on_another_it_is_refused(self) -> None:
        """The fact leg returns groups, not the orders an exists tests."""
        query = {
            "select": BY_REGION,
            "where": [{"logic": "or", "filters": [_returns("exists"), EU]}],
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(subquery_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_FILTER"
        assert "An exists filter on the facts' data source that runs after the join" in (
            error.message
        )

    def test_a_subquery_reading_another_source_than_its_subject_is_refused(self) -> None:
        tickets_of_orders = {
            "field": "Orders.Order ID",
            "op": "exists",
            "subquery": {"dataObject": "Tickets"},
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(subquery_model(True), {"select": BY_REGION, "where": [tickets_of_orders]})

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_FILTER"
        assert "reads Customers, Tickets, of another data source than 'Orders'" in error.message

    def test_a_subquery_filter_on_another_source_is_not_compiled_for_one(self) -> None:
        """The orders returned as damaged, the reasons kept in the CRM: the
        query read only the shop's objects besides, so it was compiled for the
        shop alone, with the CRM's reasons table in its SQL."""
        returns = {
            "field": "Orders.Order ID",
            "op": "exists",
            "subquery": {
                "dataObject": "Returns",
                "filter": [{"field": "Reasons.Reason Label", "op": "=", "value": "Damaged"}],
            },
        }
        query = {"select": {"dimensions": ["Order Date"], "measures": ["Revenue"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(subquery_model(True), {**query, "where": [returns]})

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_FILTER"
        assert "reads Reasons, of another data source than 'Orders'" in error.message


INDEPENDENT_MEASURES = (
    "  Ticket Ids: {columns: [{dataObject: Tickets, column: Ticket ID}], aggregation: sum,"
    " resultType: int}\n"
    f"  Average Order: {{columns: [{AMOUNT}], aggregation: avg}}\n"
    "  Average Ticket: {columns: [{dataObject: Tickets, column: Ticket ID}], aggregation: avg}\n"
    "  Severities: {columns: [{dataObject: Tickets, column: Severity}],"
    " aggregation: count_distinct, resultType: int}\n"
    f"  Revenue Total: {{columns: [{AMOUNT}], aggregation: sum, total: true}}\n"
    f"  Median Order: {{columns: [{AMOUNT}], aggregation: median}}\n"
    "  Ticket Total: {columns: [{dataObject: Tickets, column: Ticket ID}], aggregation: count,"
    " total: true, resultType: int}\n"
    f"  Average Total: {{columns: [{AMOUNT}], aggregation: avg, total: true}}\n"
    f"  Region Revenue: {{columns: [{AMOUNT}], aggregation: sum,"
    " grain: {mode: FIXED, include: [Region]}}\n"
    f"  Revenue Any Region: {{columns: [{AMOUNT}], aggregation: sum,"
    " filterContext: {exclude: [Region]}}\n"
    "  Tickets Any Region: {columns: [{dataObject: Tickets, column: Ticket ID}],"
    " aggregation: count, resultType: int, filterContext: {exclude: [Region]}}\n"
    f"  Revenue All: {{columns: [{AMOUNT}], aggregation: sum, filterContext: {{mode: FIXED}}}}\n"
    f"  Date Revenue: {{columns: [{AMOUNT}], aggregation: sum,"
    " grain: {mode: FIXED, include: [Order Date]}}\n"
    f"  Revenue Not By Date: {{columns: [{AMOUNT}], aggregation: sum,"
    " grain: {exclude: [Order Date]}}\n"
)
INDEPENDENT_MODEL = {
    "extra": INDEPENDENT_MEASURES,
    "metric": '  Revenue per Ticket: {expression: "{[Revenue]} / {[Tickets Count]}"}\n'
    "  Running Revenue: {type: cumulative, measure: Revenue, timeDimension: Order Date}\n"
    "  Rolling Revenue: {type: cumulative, measure: Revenue, timeDimension: Order Date,"
    " window: 2}\n"
    "  Revenue to Date: {type: cumulative, measure: Revenue, timeDimension: Order Date,"
    " grainToDate: year}\n"
    "  Revenue in 60 Days: {type: cumulative, measure: Revenue, timeDimension: Order Date,"
    " windowSpan: {count: 60, unit: day}}\n"
    "  Running Tickets: {type: cumulative, measure: Tickets Count, timeDimension: Order Date}\n"
    "  Revenue Rank: {type: window, windowFunction: dense_rank, measure: Revenue,"
    " partitionBy: [Region]}\n"
    "  Prior Revenue: {type: window, windowFunction: lag, measure: Revenue, offset: 1,"
    " timeDimension: Order Date}\n"
    "  Prior Tickets: {type: window, windowFunction: lag, measure: Tickets Count, offset: 1,"
    " timeDimension: Order Date, partitionBy: [Region]}\n"
    "  Next Revenue: {type: window, windowFunction: lead, measure: Revenue, offset: 1,"
    " timeDimension: Order Date}\n"
    "  Ticket Half: {type: window, windowFunction: ntile, measure: Tickets Count, buckets: 2}\n"
    "  Running Orders: {type: cumulative, measure: Orders Count, timeDimension: Order Date}\n"
    "  Rolling Orders: {type: cumulative, measure: Orders Count, timeDimension: Order Date,"
    " window: 2}\n"
    "  Orders to Date: {type: cumulative, measure: Orders Count, timeDimension: Order Date,"
    " grainToDate: year}\n"
    "  Revenue per Customer: {type: reaggregate, measure: Revenue, per: [Customer],"
    " aggregation: avg}\n"
    "  Tickets per Customer: {type: reaggregate, measure: Tickets Count, per: [Customer],"
    " aggregation: avg}\n"
    "  Best Region Average: {type: reaggregate, measure: Revenue per Customer, per: [Region],"
    " aggregation: max}\n"
    '  Average per Ticket: {expression: "{[Revenue per Customer]} / {[Tickets Count]}"}\n',
    "dimension": "  Severity: {dataObject: Tickets, column: Severity, resultType: string}\n"
    "  Order Month: {dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}\n"
    "  Order Day: {dataObject: Orders, column: Order Date, resultType: date}\n"
    "  Customer: {dataObject: Customers, column: Customer ID, resultType: int}\n",
}
REVENUE_AND_TICKETS = ["Revenue", "Tickets Count"]
BY_REGION_BOTH = {"dimensions": ["Region"], "measures": REVENUE_AND_TICKETS}
BY_REGION_AND_MONTH = {
    "dimensions": ["Region", "Order Date:month"],
    "measures": REVENUE_AND_TICKETS,
}
INDEPENDENT_QUERIES = {
    "by_region": {"select": BY_REGION_BOTH},
    "beside_a_measure_of_the_customers": {
        "select": {"dimensions": ["Region"], "measures": [*REVENUE_AND_TICKETS, "Customers Count"]}
    },
    "by_a_dimension_one_fact_reaches": {
        "select": {"dimensions": ["Order Date:month", "Region"], "measures": REVENUE_AND_TICKETS}
    },
    "by_a_dimension_of_each": {
        "select": {"dimensions": ["Severity", "Region"], "measures": REVENUE_AND_TICKETS}
    },
    "without_dimensions": {"select": {"measures": REVENUE_AND_TICKETS}},
    "filtered_where_both_reach": {"select": BY_REGION_BOTH, "where": [EU]},
    "filtered_where_one_reaches": {"select": BY_REGION_BOTH, "where": [LATE]},
    "filtered_where_the_other_reaches": {"select": BY_REGION_BOTH, "where": [HIGH]},
    "an_and_group_of_conditions_of_each": {
        "select": BY_REGION_BOTH,
        "where": [{"logic": "and", "filters": [LATE, {"logic": "and", "filters": [HIGH]}]}],
    },
    "an_and_group_alone_in_an_or_group": {
        "select": BY_REGION_BOTH,
        "where": [{"logic": "or", "filters": [{"logic": "and", "filters": [LATE, HIGH]}]}],
    },
    "an_exists_both_reach": {"select": BY_REGION_BOTH, "where": [_tickets("exists")]},
    "a_metric_across_them": {
        "select": {
            "dimensions": ["Region"],
            "measures": [*REVENUE_AND_TICKETS, "Revenue per Ticket"],
        }
    },
    "having_on_a_metric": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue per Ticket"]},
        "having": [{"field": "Revenue per Ticket", "op": ">", "value": 10}],
    },
    "having_ordered_and_limited": {
        "select": BY_REGION_BOTH,
        "having": [{"field": "Tickets Count", "op": ">", "value": 0}],
        "orderBy": [{"field": "Revenue", "direction": "desc"}],
        "limit": 2,
    },
    "ordered_by_a_measure_of_one_of_them": {
        "select": BY_REGION_BOTH,
        "orderBy": [{"field": "Average Ticket", "direction": "desc"}, {"field": "Region"}],
    },
    "a_median_from_rows": {
        "select": {"dimensions": ["Region"], "measures": ["Median Order", "Tickets Count"]},
        "where": [LATE],
    },
    "averages_and_distinct_counts": {
        "select": {
            "dimensions": ["Region"],
            "measures": ["Average Order", "Average Ticket", "Severities", "Ticket Ids"],
        }
    },
    "a_rollup": {"select": BY_REGION_AND_MONTH, "grouping": "rollup"},
    "a_cube": {"select": BY_REGION_AND_MONTH, "grouping": "cube"},
    "a_rollup_from_rows": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Median Order", "Severities"],
        },
        "grouping": "rollup",
    },
    "a_rollup_sorted_and_limited": {
        "select": BY_REGION_AND_MONTH,
        "grouping": "rollup",
        "orderBy": [{"field": "Region"}, {"field": "Revenue", "direction": "desc"}],
        "limit": 5,
    },
    "a_cube_with_a_metric_and_having": {
        "select": {"dimensions": ["Region"], "measures": ["Tickets Count", "Revenue per Ticket"]},
        "grouping": "cube",
        "having": [{"field": "Tickets Count", "op": ">", "value": 0}],
    },
    "a_total_of_each": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue Total", "Ticket Total"]},
        "where": [LATE],
    },
    "a_total_of_an_average": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Average Total", "Tickets Count"],
        }
    },
    "a_grain": {
        "select": {
            "dimensions": ["Order Date:month", "Region"],
            "measures": ["Region Revenue", "Ticket Total"],
        }
    },
    "a_filter_context_of_each": {
        "select": {
            "dimensions": ["Region"],
            "measures": ["Revenue Any Region", "Tickets Any Region", "Revenue"],
        },
        "where": [EU],
    },
    "a_fixed_filter_context": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue All", "Tickets Count"]},
        "where": [LATE],
    },
    "filled": {
        "select": {"dimensions": ["Order Date:month"], "measures": REVENUE_AND_TICKETS},
        "where": [EU],
        "fill": True,
    },
    "at_two_grains": {
        "select": {
            "dimensions": ["Region", "Order Date:month", "Order Date:quarter"],
            "measures": REVENUE_AND_TICKETS,
        }
    },
    "as_of_a_date": {"select": BY_REGION_AND_MONTH, "asOf": "2024-02-15"},
    "at_a_coarser_grain_first": {
        "select": {
            "dimensions": ["Order Date:quarter", "Order Date:month"],
            "measures": REVENUE_AND_TICKETS,
        }
    },
    "at_two_grains_with_a_week": {
        "select": {
            "dimensions": ["Order Date:year", "Region", "Order Date:week"],
            "measures": REVENUE_AND_TICKETS,
        }
    },
    "at_two_grains_sorted_by_the_second_and_limited": {
        "select": {
            "dimensions": ["Order Date:quarter", "Order Date:month"],
            "measures": REVENUE_AND_TICKETS,
        },
        "orderBy": [{"field": "Order Date:month", "direction": "desc"}],
        "limit": 2,
    },
    "filled_between_bounds_beyond_the_data": {
        "select": {"dimensions": ["Order Date:month"], "measures": REVENUE_AND_TICKETS},
        "where": [
            {"field": "Order Date", "op": ">", "value": "2023-11-15"},
            {"field": "Order Date", "op": "<=", "value": "2024-05-20"},
        ],
        "fill": True,
    },
    "filled_and_sorted": {
        "select": {"dimensions": ["Order Date:month"], "measures": REVENUE_AND_TICKETS},
        "where": [{"field": "Order Date", "op": "between", "value": ["2023-12-01", "2024-05-31"]}],
        "orderBy": [{"field": "Order Date", "direction": "desc"}],
        "limit": 4,
        "fill": True,
    },
    "a_rollup_from_the_other_end": {
        "select": {"dimensions": ["Order Date:month", "Region"], "measures": REVENUE_AND_TICKETS},
        "grouping": "rollup",
    },
    "a_month_and_the_date_itself": {
        "select": {
            "dimensions": ["Order Date:month", "Order Date"],
            "measures": REVENUE_AND_TICKETS,
        }
    },
    "at_three_grains_with_the_date_itself": {
        "select": {
            "dimensions": ["Order Date:quarter", "Order Date:month", "Order Date"],
            "measures": REVENUE_AND_TICKETS,
        }
    },
    "sorted_by_the_first_of_two_grains": {
        "select": {
            "dimensions": ["Order Date:quarter", "Order Date:month"],
            "measures": REVENUE_AND_TICKETS,
        },
        "orderBy": [
            {"field": "Order Date", "direction": "desc"},
            {"field": "Order Date:month", "direction": "desc"},
        ],
        "limit": 2,
    },
    "a_grain_at_two_grains": {
        "select": {
            "dimensions": ["Region", "Order Date:month", "Order Date:quarter"],
            "measures": ["Region Revenue", "Tickets Count"],
        }
    },
    "filled_at_a_declared_grain": {
        "select": {"dimensions": ["Order Month"], "measures": REVENUE_AND_TICKETS},
        "where": [{"field": "Order Date", "op": ">=", "value": "2023-12-01"}],
        "fill": True,
    },
    "filled_between_periods_of_a_declared_grain": {
        "select": {"dimensions": ["Order Month"], "measures": REVENUE_AND_TICKETS},
        "where": [
            {"field": "Order Month", "op": ">=", "value": "2023-11-15"},
            {"field": "Order Month", "op": "<", "value": "2024-05-10"},
        ],
        "fill": True,
    },
    "filled_between_bounds_on_another_dimension": {
        "select": {"dimensions": ["Order Date:month"], "measures": REVENUE_AND_TICKETS},
        "where": [
            {"field": "Order Day", "op": ">=", "value": "2023-12-01"},
            {"field": "Order Day", "op": "<", "value": "2024-06-01"},
        ],
        "fill": True,
    },
    "a_grain_on_a_dimension_at_two_grains": {
        "select": {
            "dimensions": ["Order Date:quarter", "Order Date:month"],
            "measures": ["Date Revenue", "Tickets Count"],
        }
    },
    "a_grain_without_a_dimension_at_two_grains": {
        "select": {
            "dimensions": ["Region", "Order Date:quarter", "Order Date:month"],
            "measures": ["Revenue Not By Date", "Tickets Count"],
        }
    },
    "filled_beside_the_date_itself": {
        "select": {
            "dimensions": ["Order Date:month", "Order Date"],
            "measures": REVENUE_AND_TICKETS,
        },
        "where": [
            {"field": "Order Date", "op": ">=", "value": "2023-12-01"},
            {"field": "Order Date", "op": "<", "value": "2024-06-01"},
        ],
        "fill": True,
    },
    "the_date_itself_beside_a_filled_month": {
        "select": {
            "dimensions": ["Order Date", "Order Date:month"],
            "measures": REVENUE_AND_TICKETS,
        },
        "where": [{"field": "Order Date", "op": ">=", "value": "2023-12-01"}],
        "orderBy": [{"field": "Revenue", "direction": "desc"}],
        "limit": 3,
        "fill": True,
    },
    "a_running_total": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Running Revenue", "Tickets Count"],
        }
    },
    "a_running_total_by_region": {
        "select": {
            "dimensions": ["Order Date:month", "Region"],
            "measures": ["Running Revenue", "Tickets Count"],
        }
    },
    "a_running_total_of_the_tickets": {
        "select": {
            "dimensions": ["Order Date:month", "Region"],
            "measures": ["Running Tickets", "Revenue"],
        }
    },
    "a_running_total_filtered_elsewhere_sorted_and_limited": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Running Revenue", "Tickets Count"],
        },
        "where": [EU],
        "having": [{"field": "Running Revenue", "op": ">", "value": 100}],
        "orderBy": [{"field": "Order Date", "direction": "desc"}],
        "limit": 2,
    },
    "a_rolling_window": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Rolling Revenue", "Tickets Count"],
        }
    },
    "a_grain_to_date": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Revenue to Date", "Tickets Count"],
        }
    },
    "a_rank": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue Rank", "Tickets Count"],
        }
    },
    "a_lag": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Prior Revenue", "Tickets Count"],
        }
    },
    "a_lag_after_a_date_filter": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Prior Revenue", "Revenue"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    "running_counts_filled": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Running Orders", "Rolling Orders", "Orders to Date", "Tickets Count"],
        },
        "fill": True,
    },
    "filtered_on_a_column_of_each": {
        "select": BY_REGION_BOTH,
        "where": [
            {"field": "Customers.Region", "op": "=", "value": "EU"},
            {"field": "Tickets.Severity", "op": "=", "value": "high"},
        ],
    },
    "a_lead": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Next Revenue", "Tickets Count"],
        }
    },
    "an_ntile_of_the_tickets": {
        "select": {"dimensions": ["Region"], "measures": ["Ticket Half", "Revenue"]}
    },
    "a_lag_of_the_tickets_by_region": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Prior Tickets", "Revenue"],
        }
    },
    "a_rollup_of_no_rows": {
        "select": BY_REGION_AND_MONTH,
        "where": [{"field": "Region", "op": "=", "value": "nowhere"}],
        "grouping": "rollup",
    },
    "a_cube_of_no_rows": {
        "select": BY_REGION_AND_MONTH,
        "where": [{"field": "Region", "op": "=", "value": "nowhere"}],
        "grouping": "cube",
    },
    "a_reaggregate": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue per Customer", "Tickets Count"]}
    },
    "a_reaggregate_by_a_dimension_one_fact_reaches": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Revenue per Customer", "Tickets Count"],
        }
    },
    "a_reaggregate_by_no_dimension": {
        "select": {"dimensions": [], "measures": ["Revenue per Customer", "Tickets Count"]}
    },
    "a_reaggregate_filtered": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue per Customer", "Tickets Count"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
    "a_reaggregate_of_each": {
        "select": {
            "dimensions": ["Region"],
            "measures": ["Revenue per Customer", "Tickets per Customer"],
        }
    },
    "a_reaggregate_of_a_reaggregate": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Best Region Average", "Tickets Count"],
        }
    },
    "a_metric_over_a_reaggregate_and_the_tickets": {
        "select": {"dimensions": ["Region"], "measures": ["Average per Ticket"]}
    },
    "a_reaggregate_in_having_and_order_alone": {
        "select": {"dimensions": ["Region"], "measures": REVENUE_AND_TICKETS},
        "having": [{"field": "Revenue per Customer", "op": ">", "value": 20}],
        "orderBy": [{"field": "Revenue per Customer", "direction": "desc"}],
    },
}


@pytest.fixture(scope="module")
def independent_files(tmp_path_factory: Any) -> dict[str, dict[str, str]]:
    """The tickets beside the customers, beside the orders, on a source of their
    own, and none."""
    root = tmp_path_factory.mktemp("independent")
    crm = [CUSTOMERS, REASONS, TENANTS, PLANS]
    layouts = {
        "crm": {"shop": [ORDERS, RETURNS], "crm": [*crm, TICKETS]},
        "shop": {"shop": [ORDERS, RETURNS, TICKETS], "crm": crm},
        "support": {"shop": [ORDERS, RETURNS], "crm": crm, "support": [TICKETS]},
        "no_tickets": {"shop": [ORDERS, RETURNS], "crm": [*crm, f"{TICKETS} WHERE false"]},
    }
    paths: dict[str, dict[str, str]] = {}
    for layout, sources in layouts.items():
        sources = {"all": [s for statements in sources.values() for s in statements], **sources}
        paths[layout] = {}
        for name, statements in sources.items():
            paths[layout][name] = str(root / f"{layout}_{name}.duckdb")
            con = duckdb.connect(paths[layout][name])
            for statement in statements:
                con.execute(statement)
            con.close()
    return paths


def independent_model(federated: bool, tickets: str = "crm", **model: Any) -> str:
    """The subquery model with the tickets on *tickets*."""
    return subquery_model(federated, **{**INDEPENDENT_MODEL, **model}).replace(
        "  Tickets:\n    code: tickets\n    dataSource: crm\n",
        f"  Tickets:\n    code: tickets\n    dataSource: {tickets}\n",
    )


class TestMeasuresOfIndependentFacts:
    """Revenue of the shop's orders beside the CRM's tickets: one database unions
    the facts, each grouped by the dimensions it reaches; across sources each
    fact is a query of its own, and the combine step unions their results."""

    @pytest.mark.parametrize("query", INDEPENDENT_QUERIES.values(), ids=INDEPENDENT_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, independent_files: dict[str, dict[str, str]], query: dict[str, Any]
    ) -> None:
        paths = independent_files["crm"]
        sort = not query.get("orderBy")

        single = _run_on(paths, independent_model(False), query, sort=sort)

        assert _run_on(paths, independent_model(True), query, sort=sort) == single
        assert single

    @pytest.mark.parametrize(
        "tickets", ["shop", "support"], ids=["beside_the_orders", "on_a_source_of_their_own"]
    )
    def test_where_the_tickets_are(
        self, independent_files: dict[str, dict[str, str]], tickets: str
    ) -> None:
        """Two facts of one source, joined there to nothing they share, and
        three sources."""
        paths = independent_files[tickets]
        query = {"select": BY_REGION_BOTH, "where": [LATE]}

        single = _run_on(paths, independent_model(False), query)

        assert _run_on(paths, independent_model(True, tickets), query) == single
        assert single

    def test_each_fact_is_a_part(self) -> None:
        query = {"select": BY_REGION_BOTH, "where": [LATE]}

        compiled = _compile(independent_model(True), query)

        plan = compiled.federation
        assert [leg.name for leg in plan.legs] == [
            "__part0_leg_0",
            "__part0_leg_1",
            "__part1_leg_0",
        ]
        assert not plan.injections
        assert "2024-02-14" in plan.legs[0].sql
        assert "2024-02-14" not in plan.legs[2].sql

    @pytest.mark.parametrize(
        ("where", "model", "codes"),
        [
            ([LATE], {}, ["FILTER_NOT_APPLIED"]),
            ([], {"static": ABOVE_TEN}, ["STATIC_FILTER_NOT_APPLIED"]),
            (
                [{"logic": "and", "filters": [LATE, HIGH]}],
                {},
                ["FILTER_NOT_APPLIED", "FILTER_NOT_APPLIED"],
            ),
        ],
        ids=["a_query_filter", "a_static_filter", "conditions_of_an_and_group"],
    )
    def test_a_filter_one_fact_does_not_reach_warns_as_on_one_source(
        self,
        independent_files: dict[str, dict[str, str]],
        where: list[dict[str, Any]],
        model: dict[str, Any],
        codes: list[str],
    ) -> None:
        query = {"select": BY_REGION_BOTH, "where": where}

        single = _compile(independent_model(False, **model), query)
        federated = _compile(independent_model(True, **model), query)

        assert [(w.code, w.message) for w in federated.warnings] == [
            (w.code, w.message) for w in single.warnings
        ]
        assert [w.code for w in single.warnings] == codes
        paths = independent_files["crm"]
        assert _run_on(paths, independent_model(True, **model), query) == _run_on(
            paths, independent_model(False, **model), query
        )

    @pytest.mark.parametrize(
        ("layout", "where"),
        [("no_tickets", []), ("crm", [{"field": "Severity", "op": "=", "value": "none"}])],
        ids=["no_tickets", "no_ticket_passes_the_filter"],
    )
    def test_a_fact_without_rows_adds_no_group(
        self, independent_files: dict[str, dict[str, str]], layout: str, where: list[Any]
    ) -> None:
        """By order month, a group of the tickets' is one of NULLs: their part
        returned it, a count of 0, for no tickets too, where one database's
        union leg returns no row."""
        paths = independent_files[layout]
        query = {
            "select": {"dimensions": ["Order Date:month"], "measures": REVENUE_AND_TICKETS},
            "where": where,
        }

        single = _run_on(paths, independent_model(False), query)

        assert _run_on(paths, independent_model(True), query) == single
        assert single
        assert None not in [month for month, _, _ in single]

    def test_a_fact_of_more_rows_than_an_integer_holds(
        self, independent_files: dict[str, dict[str, str]]
    ) -> None:
        """By order month, the tickets' part counts them, to add no group for
        none: as an ``integer``, a count over 2,147,483,647 failed the query,
        which asks for no count. Each leg's count of that many, as returned."""
        paths = independent_files["crm"]
        query = {
            "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Ticket Ids"]}
        }
        compiled = _compile(independent_model(True), query)
        assert compiled.federation is not None
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            sql = leg.sql
            if '"__present"' in sql:
                sql = f'SELECT * REPLACE (2147483648 AS "__present") FROM ({sql})'
            con = duckdb.connect(paths[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(sql).to_arrow_table())
            finally:
                con.close()

        rows = sorted(_rows(combine, compiled.sql), key=repr)

        assert rows == _run_on(paths, independent_model(False), query)
        assert (None, None, 10) in rows

    @pytest.mark.parametrize("grouping", ["rollup", "cube"])
    @pytest.mark.parametrize(
        "dimensions",
        [["Region", "Order Date:month"], ["Order Date:month", "Region"]],
        ids=["region_first", "month_first"],
    )
    def test_a_fact_without_rows_adds_no_subtotal(
        self, independent_files: dict[str, dict[str, str]], grouping: str, dimensions: list[str]
    ) -> None:
        """Without tickets, their part's grand total stood for no rows, and
        gave the cube a group of all regions in no month."""
        paths = independent_files["no_tickets"]
        query = {
            "select": {"dimensions": dimensions, "measures": REVENUE_AND_TICKETS},
            "grouping": grouping,
        }

        single = _run_on(paths, independent_model(False), query)

        assert _run_on(paths, independent_model(True), query) == single
        assert single

    @pytest.mark.parametrize(
        ("query", "message"),
        [
            (
                {
                    "select": {
                        "dimensions": ["Region"],
                        "measures": ["Revenue Total", "Tickets Count"],
                    },
                    "grouping": "rollup",
                },
                "has a total, grain or filterContext beside a rollup or cube",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Order Date:year", "Order Date:month"],
                        "measures": REVENUE_AND_TICKETS,
                    },
                    "grouping": "rollup",
                },
                "selects one dimension at two grains beside a rollup or cube",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Order Date:month"],
                        "measures": [*REVENUE_AND_TICKETS, "Customers Count"],
                    }
                },
                "is of 'Customers', which one database counts through 'Orders'",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Severity"],
                        "measures": [*REVENUE_AND_TICKETS, "Customers Count"],
                    }
                },
                "is of 'Customers', which one database counts through 'Tickets'",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Order Date:month"],
                        "measures": ["Running Revenue", "Tickets Count"],
                    },
                    "where": [{"field": "Order Day", "op": ">=", "value": "2024-02-01"}],
                },
                "which it reads before the filter's first period",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Region"],
                        "measures": ["Running Revenue", "Tickets Count"],
                    }
                },
                "periods of a time dimension the query does not select",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Order Date:month"],
                        "measures": ["Running Revenue", "Tickets Count"],
                    },
                    "grouping": "rollup",
                },
                "reads values across periods beside a rollup",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Order Date:month"],
                        "measures": ["Revenue in 60 Days", "Tickets Count"],
                    }
                },
                "a cumulative metric reads a span of rows",
            ),
        ],
        ids=[
            "a_total_beside_a_rollup",
            "two_grains_beside_a_rollup",
            "the_customers_by_order_month",
            "the_customers_by_severity",
            "a_running_total_after_a_date_filter",
            "a_running_total_without_its_date",
            "a_running_total_beside_a_rollup",
            "a_running_span",
        ],
    )
    def test_what_is_not_combined_yet_is_refused(self, query: dict[str, Any], message: str) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(independent_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert message in error.message

    @pytest.mark.parametrize(
        ("query", "code"),
        [
            (
                {
                    "select": {
                        "dimensions": ["Severity"],
                        "measures": ["Revenue per Customer", "Tickets Count"],
                    }
                },
                "UNREACHABLE_REQUIRED_OBJECT",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Region"],
                        "measures": ["Revenue per Customer", "Tickets Count"],
                    },
                    "where": [{"field": "Severity", "op": "=", "value": "high"}],
                },
                "UNREACHABLE_FILTER_OBJECT",
            ),
        ],
        ids=["by_a_dimension_of_the_tickets", "filtered_on_the_tickets"],
    )
    def test_a_reaggregate_one_database_refuses_stays_refused(
        self, query: dict[str, Any], code: str
    ) -> None:
        """One database groups a reaggregate's first stage by every dimension of
        the query and filters it by every filter, which the orders do not reach:
        the part of the orders left them out, and returned a value."""
        with pytest.raises(ResolutionError) as single:
            _compile(independent_model(False), query)
        with pytest.raises(ResolutionError) as federated:
            _compile(independent_model(True), query)

        assert [e.code for e in federated.value.errors] == [code]
        assert [e.message for e in federated.value.errors] == [
            e.message for e in single.value.errors
        ]

    @pytest.mark.parametrize(
        ("dimension", "measure"),
        [("Region", "Tickets Count"), ("Order Date:month", "Customers Count")],
        ids=["another_fact", "a_parent"],
    )
    def test_ordered_by_a_measure_of_another_source_is_refused(
        self, dimension: str, measure: str
    ) -> None:
        """Ordered by the tickets' count, the fact leg counted a ticket of no
        table, in the shop; by the customers' count, it read no customers."""
        query = {
            "select": {"dimensions": [dimension], "measures": ["Revenue"]},
            "orderBy": [{"field": measure, "direction": "desc"}],
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(independent_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_MULTI_FACT"

    def test_ordered_by_a_measure_of_a_third_fact_is_refused(
        self, independent_files: dict[str, dict[str, str]]
    ) -> None:
        """The returns join the orders, which reach none of them: their count,
        selected, is a part of its own."""
        query = {
            "select": BY_REGION_BOTH,
            "orderBy": [{"field": "Returns Count", "direction": "desc"}],
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(independent_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert error.hint == "Select the measure, or order by a measure of a selected fact."
        paths = independent_files["crm"]
        selected = {
            "select": {
                "dimensions": ["Region"],
                "measures": [*REVENUE_AND_TICKETS, "Returns Count"],
            }
        }
        single = _run_on(paths, independent_model(False), selected)
        assert _run_on(paths, independent_model(True), selected) == single
        assert single


# Customers 14 and 15 have no orders: one database counts them in the
# groups they belong to, by their region, and in the NULL group of an order
# column.
CUSTOMERS_WITHOUT_ORDERS = CUSTOMERS.replace(
    "    (13, 'EU', 'other')\n",
    "    (13, 'EU', 'other'),\n    (14, 'US', 'other'),\n    (15, NULL, 'acme')\n",
)
CUSTOMER_MEASURES = (
    ON_CUSTOMERS + "  Customer Id Sum: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: sum, resultType: int}\n"
    "  Tenants: {columns: [{dataObject: Customers, column: Tenant}], "
    "aggregation: count_distinct, resultType: int}\n"
    "  Top Customer: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: max, resultType: int}\n"
    "  Average Customer Id: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: avg}\n"
    "  All Customers: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: count, total: true, resultType: int}\n"
    "  EU Customers: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: count, resultType: int, filters: [{column: {dataObject: Customers, "
    "column: Region}, operator: equals, values: [{dataType: string, valueString: EU}]}]}\n"
    f"  Median Order: {{columns: [{AMOUNT}], aggregation: median}}\n"
    "  Fanned Customers: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: count, resultType: int, allowFanOut: true}\n"
    "  Fanned Id Sum: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: sum, resultType: int, allowFanOut: true}\n"
)
REVENUE_AND_CUSTOMERS = {"dimensions": ["Region"], "measures": ["Revenue", "Customers Count"]}
CUSTOMER_QUERIES = {
    "by_region": {"select": REVENUE_AND_CUSTOMERS},
    "other_aggregations": {
        "select": {
            "dimensions": ["Region"],
            "measures": [
                "Revenue",
                "Customer Rows",
                "Customer Id Sum",
                "Tenants",
                "Top Customer",
                "Average Customer Id",
            ],
        }
    },
    "by_order_month": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "Customers Count"]}
    },
    "by_region_and_order_month": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue", "Customers Count"],
        }
    },
    "filtered_on_the_orders": {
        "select": REVENUE_AND_CUSTOMERS,
        "where": [{"field": "Order Date", "op": "<", "value": "2024-02-01"}],
    },
    "filtered_on_the_region": {
        "select": REVENUE_AND_CUSTOMERS,
        "where": [{"field": "Region", "op": "=", "value": "EU"}],
    },
    "an_or_across_sources": {
        "select": REVENUE_AND_CUSTOMERS,
        "where": [
            {
                "logic": "or",
                "filters": [
                    {"field": "Region", "op": "=", "value": "EU"},
                    {"field": "Order Date", "op": ">", "value": "2024-03-05"},
                ],
            }
        ],
    },
    "a_metric": {
        "select": {
            "dimensions": ["Region"],
            "measures": ["Revenue per Customer", "Customers Count"],
        }
    },
    "having": {
        "select": REVENUE_AND_CUSTOMERS,
        "having": [{"field": "Customers Count", "op": ">", "value": 1}],
    },
    "sorted_by_the_customers": {
        "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
        "orderBy": [
            {"field": "Customers Count", "direction": "desc"},
            {"field": "Region", "direction": "asc"},
        ],
    },
    "no_dimensions": {"select": {"dimensions": [], "measures": ["Revenue", "Customers Count"]}},
    "a_rollup": {
        "select": {
            "dimensions": ["Region", "Order Date:month"],
            "measures": ["Revenue", "Customers Count"],
        },
        "grouping": "rollup",
    },
    "a_total": {"select": {"dimensions": ["Region"], "measures": ["Revenue", "All Customers"]}},
    "a_measure_filter": {
        "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue", "EU Customers"]}
    },
    "beside_a_median": {
        "select": {
            "dimensions": ["Order Date:month"],
            "measures": ["Median Order", "Customer Id Sum"],
        }
    },
    "the_customers_alone": {
        "select": {"dimensions": ["Order Date"], "measures": ["Customers Count"]}
    },
    "allowing_fan_out": {
        "select": {
            "dimensions": ["Region"],
            "measures": ["Revenue", "Fanned Customers", "Fanned Id Sum"],
        }
    },
}


def customer_model(federated: bool) -> str:
    return model_yaml(
        federated=federated,
        extra=CUSTOMER_MEASURES,
        metric='  Revenue per Customer: {expression: "{[Revenue]} / {[Customers Count]}"}\n',
    )


@pytest.fixture(scope="module")
def customer_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("customers")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [ORDERS, CUSTOMERS_WITHOUT_ORDERS]),
        ("shop", [ORDERS]),
        ("crm", [CUSTOMERS_WITHOUT_ORDERS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestMeasuresOfJoinedObjects:
    """The customers' count beside the orders' revenue: one database counts
    each customer once per group (``grain_dedup``), those without orders too;
    across sources the CRM's leg returns each customer once, and the combine
    step counts them as one database does."""

    @pytest.mark.parametrize("query", CUSTOMER_QUERIES.values(), ids=CUSTOMER_QUERIES.keys())
    def test_federated_returns_what_one_source_returns(
        self, customer_files: dict[str, str], query: dict[str, Any]
    ) -> None:
        sort = not query.get("orderBy")

        single = _run_on(customer_files, customer_model(False), query, sort=sort)

        assert _run_on(customer_files, customer_model(True), query, sort=sort) == single
        assert single

    def test_the_customers_without_orders_are_counted(self, customer_files: dict[str, str]) -> None:
        """US has customer 11, and 14 without orders; no region 12, and 15."""
        rows = _run_on(customer_files, customer_model(True), {"select": REVENUE_AND_CUSTOMERS})

        assert rows == [
            ("EU", Decimal("123.90"), 2),
            ("US", Decimal("19.00"), 2),
            (None, Decimal("95.10"), 2),
        ]

    def test_the_crm_leg_returns_each_customer(self) -> None:
        plan = _compile(customer_model(True), {"select": REVENUE_AND_CUSTOMERS}).federation

        fact, customers = plan.legs
        assert '"Orders"."customer_id" AS "__fk0_0"' in fact.sql
        assert '"Customers"."customer_id" AS "__value0"' in customers.sql
        assert "NOT EXISTS" in plan.combine_sql
        assert "_reached" not in plan.combine_sql

    def test_filtered_on_the_orders_the_combine_step_reads_the_customers_they_reach(
        self, customer_files: dict[str, str]
    ) -> None:
        """One database adds no customer without an order a filter on the
        orders keeps: the combine step reads the customers the fact leg's
        keys reach, so it adds none either."""
        query = CUSTOMER_QUERIES["filtered_on_the_orders"]

        sql = _compile(customer_model(True), query).sql

        assert '"leg_1_reached" AS (' in sql
        assert _run_on(customer_files, customer_model(True), query) == [
            ("EU", Decimal("120.75"), 1)
        ]

    def test_allowing_fan_out_the_fact_leg_returns_its_rows(
        self, customer_files: dict[str, str]
    ) -> None:
        """One database counts a customer once per order, as the measure allows:
        the aggregated fact leg counted it once per region."""
        query = CUSTOMER_QUERIES["allowing_fan_out"]

        fact = _compile(customer_model(True), query).federation.legs[0]

        assert '"__rows"' in fact.sql
        assert _run_on(customer_files, customer_model(True), query) == [
            ("EU", Decimal("123.90"), 3, 33),
            ("US", Decimal("19.00"), 2, 22),
            (None, Decimal("95.10"), 1, 12),
        ]

    @pytest.mark.parametrize("op", ["nonexists", "exists"])
    def test_customers_without_orders_keep_an_exists_on_their_tickets(
        self, tmp_path: Any, op: str
    ) -> None:
        """The customers without tickets: those without orders too, 14 and 15,
        tested against the tickets as one database tests them."""
        paths = {name: str(tmp_path / f"{name}.duckdb") for name in ("all", "shop", "crm")}
        for name, statements in (
            ("all", [ORDERS, CUSTOMERS_WITHOUT_ORDERS, TICKETS]),
            ("shop", [ORDERS]),
            ("crm", [CUSTOMERS_WITHOUT_ORDERS, TICKETS]),
        ):
            con = duckdb.connect(paths[name])
            for statement in statements:
                con.execute(statement)
            con.close()
        query = {
            "select": REVENUE_AND_CUSTOMERS,
            "where": [
                {"field": "Customers.Customer ID", "op": op, "subquery": {"dataObject": "Tickets"}}
            ],
        }

        single = _run_on(paths, subquery_model(False), query)

        assert _run_on(paths, subquery_model(True), query) == single
        assert single

    @pytest.mark.parametrize(
        ("measure", "column"),
        [("Tenants", "Tenant"), ("Acme Customers", "Tenant")],
        ids=["a_distinct_count", "a_filter"],
    )
    def test_a_value_the_crm_compares_otherwise_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, measure: str, column: str
    ) -> None:
        """On MySQL 'acme' and 'ACME' are one tenant: DuckDB, counting them
        from the leg's values, would see two."""
        pytest.importorskip("ob_flight")
        monkeypatch.setenv("OBSL_DATABASES", "shop,crm")
        monkeypatch.setenv("DB_SHOP_TYPE", "postgres")
        monkeypatch.setenv("DB_CRM_TYPE", "mysql")
        yaml = model_yaml(
            federated=True,
            extra=CUSTOMER_MEASURES
            + "  Acme Customers: {columns: [{dataObject: Customers, column: Customer ID}], "
            "aggregation: count, resultType: int, filters: [{column: {dataObject: Customers, "
            "column: Tenant}, operator: equals, values: [{dataType: string, "
            "valueString: acme}]}]}\n",
        )
        query = {"select": {"dimensions": ["Region"], "measures": ["Revenue", measure]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_AGGREGATION"
        assert f"values of 'Customers.{column}' otherwise than mysql" in error.message
        assert _compile(yaml, {"select": REVENUE_AND_CUSTOMERS}).federation is not None

    def test_customers_without_orders_a_filter_on_the_orders_picks_are_refused(self) -> None:
        """``Order Date is_null`` holds for a customer without orders, whom one
        database finds among all orders; the fact leg returns those it keeps."""
        query = {
            "select": REVENUE_AND_CUSTOMERS,
            "where": [{"field": "Order Date", "op": "is_null"}],
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(customer_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_MULTI_FACT"
        assert "a filter on Orders decides which those are" in error.message
        assert error.hint == (
            "Query the measure separately, or filter on a dimension of 'Customers'."
        )

    def test_a_measure_of_an_object_the_customers_join_is_refused(self) -> None:
        """The tenants' rows: the CRM's leg returns a row per customer."""
        yaml = subquery_model(
            True,
            extra="  Tenant Rows: {columns: [{dataObject: Tenants, column: Tenant Code}], "
            "aggregation: count, resultType: int}\n",
        )
        query = {"select": {"dimensions": ["Region"], "measures": ["Revenue", "Tenant Rows"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_MULTI_FACT"
        assert "Measure 'Tenant Rows' is of 'Tenants', which no data object of" in error.message


# Charge c1's labels name owners prod and env, c2's ops, c3 has none: dev is
# named by no label. Owners prod and dev are of team 1, ops and env of 2.
NESTED_TABLES = {
    "billing": [
        "CREATE TABLE owners AS SELECT * FROM (VALUES ('prod', 1), ('dev', 1), ('ops', 2),"
        " ('env', 2)) t(owner_key, team_id)",
        'CREATE TABLE charges (id VARCHAR, labels STRUCT("Key" VARCHAR, "Value" VARCHAR)[])',
        "INSERT INTO charges VALUES ('c1', [{'Key': 'team', 'Value': 'prod'},"
        " {'Key': 'env', 'Value': 'env'}]), ('c2', [{'Key': 'team', 'Value': 'ops'}]),"
        " ('c3', [])",
    ],
    "crm": [
        "CREATE TABLE teams AS SELECT * FROM (VALUES (1, 'Platform'), (2, 'Data')) t(team_id, name)"
    ],
}


def nested_model(federated: bool) -> str:
    """Owners and the charges whose labels name them on billing, teams in the CRM."""
    billing = "    dataSource: billing\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Owners:
    code: owners
{billing}    columns:
      Owner Key: {{code: owner_key, abstractType: string, primaryKey: true}}
      Owner Team: {{code: team_id, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Teams, columnsFrom: [Owner Team], columnsTo: [Team ID]}}
  Charges:
    code: charges
{billing}    columns:
      Charge ID: {{code: id, abstractType: string, primaryKey: true}}
  Charge Labels:
    nestedIn: {{dataObject: Charges, column: Labels}}
    columns:
      Label Key: {{code: Key, abstractType: string}}
      Label Value: {{code: Value, abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Owners
        columnsFrom: [Label Value]
        columnsTo: [Owner Key]
  Teams:
    code: teams
{crm}    columns:
      Team ID: {{code: team_id, abstractType: int}}
      Team Name: {{code: name, abstractType: string}}
dimensions:
  Team Name: {{dataObject: Teams, column: Team Name, resultType: string}}
"""


@pytest.fixture(scope="module")
def nested_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("nested")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "billing", "crm")}
    for name, statements in (
        ("all", [*NESTED_TABLES["billing"], *NESTED_TABLES["crm"]]),
        ("billing", NESTED_TABLES["billing"]),
        ("crm", NESTED_TABLES["crm"]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestExistsOverNested:
    """An exists subquery over a nested object reads its parent's table there."""

    @pytest.mark.parametrize(
        ("op", "expected"),
        [("exists", [("Data", 2), ("Platform", 1)]), ("nonexists", [("Platform", 1)])],
    )
    def test_a_nested_target_reached_by_a_join_walked_backwards(
        self, nested_files: dict[str, str], op: str, expected: list[Any]
    ) -> None:
        """Owners some charge's label names, by team: the body reads the
        charges and unnests their labels, on billing."""
        named = {
            "field": "Owners.Owner Key",
            "op": op,
            "subquery": {"dataObject": "Charge Labels"},
        }
        query = {
            "select": {"dimensions": ["Team Name"], "measures": ["Owners Count"]},
            "where": [named],
        }
        single = _compile(nested_model(False), query)
        con = duckdb.connect(nested_files["all"], read_only=True)
        try:
            assert sorted(_rows(con, single.sql)) == expected
        finally:
            con.close()

        plan = _compile(nested_model(True), query).federation
        combine = legs_registered(nested_files, plan)
        assert sorted(_rows(combine, plan.combine_sql)) == expected


# Orders on the shop; customers in the CRM with their tags (an array), a
# profile each (one-to-one) and links to topics (many-to-many), and each tag's
# tier (one-to-one); profiles and tiers on a source of their own where asked.
# Customer 10 is tagged vip and gold, both in January, 11 vip in February,
# 12 has no tags and 13 none at all; order 6's customer 99 is missing. A vip
# is top tier, a gold one mid.
ISLAND_TABLES = {
    "shop": [
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 10, 100.50, 'paid'),"
        " (2, 10, 20.25, 'Paid'), (3, 11, 7.00, 'open'), (4, 12, 55.10, 'PAID'),"
        " (5, 13, 3.15, 'paid'), (6, 99, 40.00, 'open'))"
        " t(order_id, customer_id, amount, status)"
    ],
    "crm": [
        "CREATE TABLE customers"
        " (customer_id INT, region VARCHAR, tags STRUCT(Name VARCHAR, Tagged DATE)[])",
        "INSERT INTO customers VALUES"
        " (10, 'EU', [{'Name': 'vip', 'Tagged': DATE '2024-01-05'},"
        " {'Name': 'gold', 'Tagged': DATE '2024-01-20'}]),"
        " (11, 'US', [{'Name': 'vip', 'Tagged': DATE '2024-02-01'}]), (12, NULL, []),"
        " (13, 'EU', NULL)",
        "CREATE TABLE links AS SELECT * FROM (VALUES (10, 'x'), (10, 'y'), (11, 'x'))"
        " t(customer_id, topic)",
    ],
    "geo": [
        "CREATE TABLE profiles AS SELECT * FROM (VALUES (10, 'a'), (11, 'b'), (13, 'c'))"
        " t(customer_id, segment)",
        "CREATE TABLE tiers AS SELECT * FROM (VALUES ('vip', 'top'), ('gold', 'mid')) t(tag, tier)",
    ],
}


# An order over 90, or one of a customer tagged gold: one database's
# ``Region`` keeps orders 1 and 2 of customer 10, once each.
AMOUNT_OR_GOLD = {
    "logic": "or",
    "filters": [
        {"field": "Order Amount", "op": ">", "value": 90},
        {"field": "Tag", "op": "=", "value": "gold"},
    ],
}


def island_model(federated: bool, geo: str = "crm", *, required: bool = False) -> str:
    """Orders, and customers with what they join one-to-one, many-to-many and unnest.

    The profiles and the tiers on *geo*; the orders' join to their customers
    *required* where asked.
    """

    def source(name: str) -> str:
        return f"    dataSource: {name}\n" if federated else ""

    inner = "        required: true\n" if required else ""

    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: orders
{source("shop")}    columns:
      Order ID: {{code: order_id, abstractType: int, primaryKey: true}}
      Order Customer: {{code: customer_id, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal}}
      Status: {{code: status, abstractType: string}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
{inner}        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Customers:
    code: customers
{source("crm")}    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
      Region: {{code: region, abstractType: string}}
      Tag Copy: {{expression: "{{[Customer Tags].[Tag]}}", abstractType: string}}
    joins:
      - joinType: one-to-one
        joinTo: Profiles
        columnsFrom: [Customer ID]
        columnsTo: [Profile Customer]
      - joinType: many-to-many
        joinTo: Links
        columnsFrom: [Customer ID]
        columnsTo: [Link Customer]
  Customer Tags:
    nestedIn: {{dataObject: Customers, column: Tags}}
    columns:
      Tag: {{code: Name, abstractType: string}}
      Tagged: {{code: Tagged, abstractType: date}}
    joins:
      - joinType: one-to-one
        joinTo: Tiers
        columnsFrom: [Tag]
        columnsTo: [Tier Tag]
  Tiers:
    code: tiers
{source(geo)}    columns:
      Tier Tag: {{code: tag, abstractType: string}}
      Tier: {{code: tier, abstractType: string}}
  Profiles:
    code: profiles
{source(geo)}    columns:
      Profile Customer: {{code: customer_id, abstractType: int}}
      Segment: {{code: segment, abstractType: string}}
  Links:
    code: links
{source("crm")}    columns:
      Link Customer: {{code: customer_id, abstractType: int}}
      Topic: {{code: topic, abstractType: string}}
dimensions:
  Region: {{dataObject: Customers, column: Region, resultType: string}}
  Tag: {{dataObject: Customer Tags, column: Tag, resultType: string}}
  Tagged On: {{dataObject: Customer Tags, column: Tagged, resultType: date}}
  Tagged Month: {{dataObject: Customer Tags, column: Tagged, resultType: date, timeGrain: month}}
  Segment: {{dataObject: Profiles, column: Segment, resultType: string}}
  Topic: {{dataObject: Links, column: Topic, resultType: string}}
  Tier: {{dataObject: Tiers, column: Tier, resultType: string}}
  Order Amount: {{dataObject: Orders, column: Amount, resultType: decimal}}
  Order Status: {{dataObject: Orders, column: Status, resultType: string}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
  Tagged Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filters:
      - logic: or
        filters:
          - column: {{dataObject: Customer Tags, column: Tag}}
            operator: equals
            values: [{{dataType: string, valueString: vip}}]
          - column: {{dataObject: Customer Tags, column: Tag}}
            operator: equals
            values: [{{dataType: string, valueString: gold}}]
  Revenue Tagged Since 2024:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filters:
      - column: {{dataObject: Customer Tags, column: Tagged}}
        operator: gte
        values: [{{dataType: date, valueDate: "2024-01-01"}}]
  Revenue by Tag Copy:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filters:
      - logic: or
        filters:
          - column: {{dataObject: Customers, column: Tag Copy}}
            operator: equals
            values: [{{dataType: string, valueString: vip}}]
          - column: {{dataObject: Customers, column: Tag Copy}}
            operator: equals
            values: [{{dataType: string, valueString: gold}}]
  Revenue Any Region:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext: {{exclude: [Region]}}
  EU Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext:
      mode: FIXED
      include: [{{field: Region, op: "=", value: EU}}]
  No Region Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext: {{mode: FIXED, include: [{{field: Region, op: blank}}]}}
  Revenue Any Tag:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext: {{exclude: [Tag]}}
  Median Order: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: median}}
  Revenue Total:
    {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum, total: true}}
  Revenue per Region:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    grain: {{mode: FIXED, include: [Region]}}
metrics:
  Revenue per Tag: {{type: reaggregate, measure: Revenue, per: [Tag], aggregation: avg}}
  Running Revenue: {{type: cumulative, measure: Revenue, timeDimension: Tagged Month}}
"""


@pytest.fixture(scope="module")
def island_files(tmp_path_factory: Any) -> dict[str, dict[str, str]]:
    """The tables on one source, and on the shop, the CRM and the profiles' and tiers' own."""
    root = tmp_path_factory.mktemp("islands")
    every = [statement for statements in ISLAND_TABLES.values() for statement in statements]
    layouts = {
        "crm": {
            "shop": ISLAND_TABLES["shop"],
            "crm": [*ISLAND_TABLES["crm"], *ISLAND_TABLES["geo"]],
        },
        "geo": ISLAND_TABLES,
    }
    paths: dict[str, dict[str, str]] = {}
    for layout, sources in layouts.items():
        paths[layout] = {}
        for name, statements in {"all": every, **sources}.items():
            paths[layout][name] = str(root / f"{layout}_{name}.duckdb")
            con = duckdb.connect(paths[layout][name])
            for statement in statements:
                con.execute(statement)
            con.close()
    return paths


AMOUNT_OR_EU = {
    "logic": "or",
    "filters": [
        {"field": "Order Amount", "op": ">", "value": 90},
        {"field": "Region", "op": "=", "value": "EU"},
    ],
}


AMOUNT_OR_BLANK = {
    "logic": "or",
    "filters": [
        {"field": "Order Amount", "op": ">", "value": 90},
        {"field": "Region", "op": "blank"},
    ],
}


ZONE_OR_CATEGORY = {
    "logic": "or",
    "filters": [
        {"field": "Zone", "op": "=", "value": "south"},
        {"field": "Category", "op": "=", "value": "Z"},
    ],
}


class TestConditionsInTheirLeg:
    """Conditions after the join on strings an engine compares otherwise than
    DuckDB run in their leg, which returns their outcome for the combine step
    to test. DuckDB's legs stand in for such an engine here, comparing as
    DuckDB does: the answers stay one database's, NULLs, negations, missing
    rows and filter contexts included. ``test_federated_shapes_exec.py``
    runs them on MySQL's collation."""

    @pytest.fixture(autouse=True)
    def strings_compared_otherwise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def key(_: Any, column: Any, abstract_type: str, *, counted: bool) -> Any:
            return (column, abstract_type) if abstract_type == "string" else None

        monkeypatch.setattr(DuckDBDialect, "equality_key", key)

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"measures": ["Revenue"]}, "where": [AMOUNT_OR_EU]},
            {"select": {"measures": ["Revenue"]}, "where": [{**AMOUNT_OR_EU, "negated": True}]},
            {
                "select": {"measures": ["Revenue"]},
                "where": [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Order Amount", "op": ">", "value": 90},
                            {"field": "Region", "op": "!=", "value": "EU"},
                        ],
                    }
                ],
            },
            {
                "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
                "where": [
                    {
                        "logic": "or",
                        "filters": [
                            {"field": "Order Status", "op": "=", "value": "paid"},
                            {"field": "Region", "op": "like", "value": "U%"},
                        ],
                    }
                ],
            },
            {
                "select": {"measures": ["Revenue", "Revenue Any Region"]},
                "where": [{"field": "Region", "op": "in", "value": ["EU"]}],
            },
            {"select": {"measures": ["Revenue", "EU Revenue"]}},
            {
                "select": {"fields": ["Orders.Order ID", "Customers.Region"]},
                "where": [AMOUNT_OR_EU],
            },
            {"select": {"measures": ["Revenue"]}, "where": [AMOUNT_OR_BLANK]},
            {
                "select": {"measures": ["Revenue"]},
                "where": [{"negated": True, "filters": [{"field": "Region", "op": "notblank"}]}],
            },
            {"select": {"dimensions": ["Order Status"], "measures": ["No Region Revenue"]}},
            {
                "select": {"fields": ["Orders.Order ID", "Customers.Region"]},
                "where": [AMOUNT_OR_BLANK],
            },
        ],
        ids=[
            "beside_another_source",
            "negated",
            "unknown_where_null",
            "on_the_facts_and_a_pattern",
            "a_filter_context_drops",
            "a_filter_context_includes",
            "raw_rows",
            "blank",
            "not_blank_negated",
            "a_filter_context_includes_blanks",
            "raw_rows_blank",
        ],
    )
    def test_a_condition_on_a_string_after_the_join(
        self, island_files: dict[str, dict[str, str]], query: dict[str, Any]
    ) -> None:
        """Order 6's customer is missing, customer 12 has no region: there a
        comparison is unknown, and a blank test holds, as in one database."""
        paths = island_files["crm"]
        plan = _compile(island_model(True), query).federation

        single = _run_on(paths, island_model(False), query)

        assert plan is not None
        tested = [c for leg in plan.legs for c, _ in leg.columns if c.startswith("__wf")]
        assert tested
        assert all(
            any(f'"{c}" {test}' in plan.combine_sql for test in ("= 1", "IS NULL", "IS NOT NULL"))
            for c in tested
        )
        assert _run_on(paths, island_model(True), query) == single
        assert single

    @pytest.mark.parametrize(
        ("condition", "outcome"),
        [
            ("operator: notequals, value: US", "THEN 1 WHEN NOT"),
            ("operator: blank", "THEN 1 END"),
        ],
        ids=["compared", "blank"],
    )
    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Order Status"], "measures": ["Revenue"]}},
            {"select": {"fields": ["Orders.Order ID", "Customers.Region"]}},
        ],
        ids=["grouped", "raw_rows"],
    )
    def test_a_static_filter_on_a_string_after_the_join(
        self,
        island_files: dict[str, dict[str, str]],
        query: dict[str, Any],
        condition: str,
        outcome: str,
    ) -> None:
        """A static filter on the customers runs after the join, in their leg:
        its column there is its outcome, which a blank test's keeps order 6,
        whose customer is missing."""
        static = f"filters:\n  - {{dataObject: Customers, column: Region, {condition}}}\n"
        paths = island_files["crm"]
        plan = _compile(island_model(True) + static, query).federation

        single = _run_on(paths, island_model(False) + static, query)

        assert plan is not None
        assert outcome in plan.legs[1].sql
        assert _run_on(paths, island_model(True) + static, query) == single
        assert single

    def test_a_pattern_on_the_grouped_facts_stays_after_the_join(self) -> None:
        """The fact leg groups the values its engine calls equal: a pattern may
        hold for one of them and not another, so the combine step evaluates it."""
        query = {
            "select": {"measures": ["Revenue"]},
            "where": [
                {
                    "logic": "or",
                    "filters": [
                        {"field": "Order Status", "op": "like", "value": "pa%"},
                        {"field": "Region", "op": "=", "value": "US"},
                    ],
                }
            ],
        }

        plan = _compile(island_model(True), query).federation

        assert plan is not None
        assert all(not c.startswith("__wf") for c, _ in plan.legs[0].columns)
        assert "LIKE 'pa%'" in plan.combine_sql
        assert any(c.startswith("__wf") for c, _ in plan.legs[1].columns)

    @pytest.mark.parametrize("aggregation", ["sum", "avg", "median"])
    @pytest.mark.parametrize(
        "query",
        [
            {
                "select": {"dimensions": ["Category"], "measures": ["Ages", "Sales Count"]},
                "where": [ZONE_OR_CATEGORY],
            },
            {
                "select": {"dimensions": ["Region"], "measures": ["Ages"]},
                "where": [{**ZONE_OR_CATEGORY, "negated": True}],
            },
            {"select": {"dimensions": ["Category"], "measures": ["Ages", "South Ages"]}},
        ],
        ids=["or", "negated", "a_filter_context_includes"],
    )
    def test_a_condition_on_an_object_read_once(
        self, aggregation: str, query: dict[str, Any]
    ) -> None:
        """The customers' zones are read once per customer, as their ages: so
        is the outcome of a condition on them. Customer 4 in the south has no
        sales, one database groups its age under no category."""

        def zoned(yaml: str) -> str:
            yaml = yaml.replace(
                "      Region: {code: region, abstractType: int}\n",
                "      Region: {code: region, abstractType: int}\n"
                "      Zone: {code: zone, abstractType: string}\n",
            ).replace(
                "dimensions:\n",
                "dimensions:\n  Zone: {dataObject: Customers, column: Zone, resultType: string}\n",
            )
            return yaml.replace(
                "metrics:\n",
                "  South Ages:\n"
                "    columns: [{dataObject: Customers, column: Age}]\n"
                f"    aggregation: {aggregation}\n"
                "    filterContext:\n"
                "      {mode: FIXED, include: [{field: Zone, op: '=', value: south}]}\n"
                "metrics:\n",
            )

        tables = [
            "CREATE TABLE customers AS SELECT * FROM (VALUES (1, 10, 1, 'north'),"
            " (2, 20, 1, 'north'), (3, 40, 2, 'south'), (4, 70, 2, 'south'))"
            " t(id, age, region, zone)"
            if statement.startswith("CREATE TABLE customers")
            else statement
            for statement in REPEATED_TABLES
        ]
        sources = {"facts": "    dataSource: facts\n", "labels": "    dataSource: labels\n"}
        federated = _compile(
            zoned(REPEATED.format(**sources, aggregation=aggregation)), query
        ).federation

        expected, got = _repeated(aggregation, query, zoned, tables)

        assert federated is not None
        assert any(c.startswith("__wf") for c, _ in federated.legs[0].columns)
        assert got == expected
        assert expected


# Customer 10 tagged at 00:30 and at 01:30 UTC on the night Berlin's clocks
# go back, both shown as 02:30 there, their hours 0 and 1 as 2; each moment's
# tier, and each hour's, on a source of their own, both top.
MOMENT_TABLES = {
    "shop": ISLAND_TABLES["shop"],
    "crm": [
        "CREATE TABLE customers (customer_id INT, tags STRUCT(Name VARCHAR, Stamp TIMESTAMPTZ)[])",
        "INSERT INTO customers VALUES (10, [{'Name': 'vip', 'Stamp': '2024-10-27 00:30:00+00'},"
        " {'Name': 'gold', 'Stamp': '2024-10-27 01:30:00+00'}]),"
        " (11, [{'Name': 'vip', 'Stamp': '2024-11-01 10:00:00+00'}])",
    ],
    "geo": [
        "CREATE TABLE moments AS SELECT * FROM (VALUES"
        " (TIMESTAMPTZ '2024-10-27 00:30:00+00', 'top'),"
        " (TIMESTAMPTZ '2024-10-27 01:30:00+00', 'top'),"
        " (TIMESTAMPTZ '2024-11-01 10:00:00+00', 'mid')) t(moment, tier)",
        "CREATE TABLE hours AS SELECT * FROM (VALUES (0, 'top'), (1, 'top'), (10, 'mid'))"
        " t(hour, tier)",
    ],
}


def moments_model(federated: bool, zone: bool) -> str:
    """Orders, their customers' tags and each tag's moment's tier, and its
    hour's; in Berlin where *zone*. The orders' join to their customers is
    required, so a column computed from the tags has a value only where they do."""

    def source(name: str) -> str:
        return f"    dataSource: {name}\n" if federated else ""

    berlin = "  queryTimezone: Europe/Berlin\n" if zone else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
{berlin}dataObjects:
  Orders:
    code: orders
{source("shop")}    columns:
      Order ID: {{code: order_id, abstractType: int, primaryKey: true}}
      Order Customer: {{code: customer_id, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        required: true
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Customers:
    code: customers
{source("crm")}    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
  Customer Tags:
    nestedIn: {{dataObject: Customers, column: Tags}}
    columns:
      Tagged At: {{code: Stamp, abstractType: timestamp_tz}}
      Tagged Hour: {{expression: "extract('hour', {{Tagged At}})", abstractType: int}}
    joins:
      - joinType: one-to-one
        joinTo: Moments
        columnsFrom: [Tagged At]
        columnsTo: [Moment]
      - joinType: one-to-one
        joinTo: Hours
        columnsFrom: [Tagged Hour]
        columnsTo: [Hour]
  Moments:
    code: moments
{source("geo")}    columns:
      Moment: {{code: moment, abstractType: timestamp_tz}}
      Tier: {{code: tier, abstractType: string}}
  Hours:
    code: hours
{source("geo")}    columns:
      Hour: {{code: hour, abstractType: int}}
      Hour Tier: {{code: tier, abstractType: string}}
dimensions:
  Tagged At: {{dataObject: Customer Tags, column: Tagged At, resultType: timestamp_tz}}
  Tier: {{dataObject: Moments, column: Tier, resultType: string}}
  Tagged Hour: {{dataObject: Customer Tags, column: Tagged Hour, resultType: int}}
  Hour Tier: {{dataObject: Hours, column: Hour Tier, resultType: string}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
"""


@pytest.fixture(scope="module")
def moment_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("moments")
    every = [statement for statements in MOMENT_TABLES.values() for statement in statements]
    paths = {}
    for name, statements in {"all": every, **MOMENT_TABLES}.items():
        paths[name] = str(root / f"{name}.duckdb")
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestDimensionSourceJoins:
    """Joins inside a dimension source, and from one, that a leg returns as
    one database joins them, or that it would return otherwise."""

    @pytest.mark.parametrize("profiles", ["crm", "geo"], ids=["inside_the_crm", "between_sources"])
    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Segment"], "measures": ["Revenue", "Median Order"]}},
            {
                "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
                "where": [{"field": "Segment", "op": "=", "value": "a"}],
            },
        ],
        ids=["by_segment", "filtered_on_segment"],
    )
    def test_a_one_to_one_join_repeats_no_rows(
        self, island_files: dict[str, dict[str, str]], profiles: str, query: dict[str, Any]
    ) -> None:
        """A customer's one profile: refused as if it repeated the customer."""
        paths = island_files[profiles]

        single = _run_on(paths, island_model(False), query)

        assert _compile(island_model(True, profiles), query).federation is not None
        assert _run_on(paths, island_model(True, profiles), query) == single
        assert single

    def test_a_many_to_many_join_is_refused(self) -> None:
        """One database repeats an order once per link of its customer; the
        links' leg returns each customer's topic once."""
        query = {"select": {"dimensions": ["Topic"], "measures": ["Revenue"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(island_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert "'Customers' -> 'Links' would repeat the rows of 'Customers'" in error.message
        assert error.hint == (
            "Leave out the dimensions reached through this join, or keep these data objects "
            "in one data source."
        )

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Tag"], "measures": ["Revenue", "Median Order"]}},
            {"select": {"dimensions": ["Tag", "Region"], "measures": ["Revenue"]}},
            {
                "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
                "where": [{"field": "Tag", "op": "in", "value": ["vip", "gold"]}],
            },
            {"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Median Order"]}},
            {
                "select": {"dimensions": ["Tag"], "measures": ["Revenue"]},
                "where": [AMOUNT_OR_GOLD],
            },
            {"select": {"dimensions": ["Tag"], "measures": ["Tagged Revenue"]}},
            {"select": {"dimensions": ["Tagged Month"], "measures": ["Revenue"]}},
            {"select": {"dimensions": ["Tagged On"], "measures": ["Revenue Tagged Since 2024"]}},
        ],
        ids=[
            "by_tag",
            "by_tag_and_region",
            "filtered_on_tags",
            "by_tier",
            "by_tag_filtered_after_the_join",
            "by_tag_filtered_by_a_measure",
            "by_a_declared_month",
            "by_date_filtered_by_a_measure",
        ],
    )
    def test_a_dimension_unnested_on_another_source(
        self, island_files: dict[str, dict[str, str]], query: dict[str, Any]
    ) -> None:
        """Customer 10's orders under vip and under gold, once each, and so
        under each tag's tier; filtered on either tag, once. Filtered after
        the join, or by a measure, each tag's group holds them once too."""
        paths = island_files["crm"]

        single = _run_on(paths, island_model(False), query)

        assert _run_on(paths, island_model(True), query) == single
        assert single

    def test_a_join_on_from_an_unnested_object(
        self, island_files: dict[str, dict[str, str]]
    ) -> None:
        """The tiers on a source of their own, joined on from the tags: by tag
        and tier, an order once per tag of its customer."""
        query = {"select": {"dimensions": ["Tag", "Tier"], "measures": ["Revenue"]}}
        paths = island_files["geo"]

        single = _run_on(paths, island_model(False), query)

        assert _run_on(paths, island_model(True, "geo"), query) == single
        assert single

    @pytest.mark.parametrize(
        ("query", "dimension", "why"),
        [
            (
                {"select": {"dimensions": ["Tag"], "measures": ["Revenue"]}, "grouping": "rollup"},
                "Dimension 'Tag' unnests 'Customer Tags'",
                "a rollup subtotal",
            ),
            (
                {"select": {"dimensions": ["Tag"], "measures": ["Revenue", "Revenue Total"]}},
                "Dimension 'Tag' unnests 'Customer Tags'",
                "measure 'Revenue Total', a total",
            ),
            (
                {"select": {"dimensions": ["Tag", "Region"], "measures": ["Revenue per Region"]}},
                "Dimension 'Tag' unnests 'Customer Tags'",
                "measure 'Revenue per Region', at its own grain",
            ),
            (
                {"select": {"dimensions": ["Tier"], "measures": ["Revenue"]}, "grouping": "rollup"},
                "Dimension 'Tier' reads 'Tiers' through the unnest of 'Customer Tags'",
                "a rollup subtotal",
            ),
            (
                {"select": {"dimensions": ["Tier"], "measures": ["Revenue", "Revenue Total"]}},
                "Dimension 'Tier' reads 'Tiers' through the unnest of 'Customer Tags'",
                "measure 'Revenue Total', a total",
            ),
        ],
        ids=["a_rollup", "a_total", "a_grain", "a_rollup_by_tier", "a_total_by_tier"],
    )
    def test_an_aggregate_across_an_unnested_dimension_is_refused(
        self, query: dict[str, Any], dimension: str, why: str
    ) -> None:
        """Across the tags, customer 10's orders came twice: a total of 346.75
        for 226.00. Across the tiers too, the tags' a tier joins one-to-one."""
        with pytest.raises(ResolutionError) as caught:
            _compile(island_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert dimension in error.message
        assert why in error.message

    @pytest.mark.parametrize(
        ("query", "geo", "reader"),
        [
            (
                {
                    "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
                    "where": [AMOUNT_OR_GOLD],
                },
                "crm",
                "Dimension 'Tag' unnests 'Customer Tags'",
            ),
            (
                {"select": {"measures": ["Revenue"]}, "where": [AMOUNT_OR_GOLD]},
                "crm",
                "Dimension 'Tag' unnests 'Customer Tags'",
            ),
            (
                {
                    "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
                    "where": [
                        {
                            "logic": "or",
                            "filters": [
                                {"field": "Order Amount", "op": ">", "value": 90},
                                {"field": "Tier", "op": "=", "value": "top"},
                            ],
                        }
                    ],
                },
                "crm",
                "Dimension 'Tier' reads 'Tiers' through the unnest of 'Customer Tags'",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Region"],
                        "measures": ["Revenue", "Revenue Any Tag"],
                    },
                    "where": [{"field": "Tag", "op": "in", "value": ["vip", "gold"]}],
                },
                "crm",
                "Dimension 'Tag' unnests 'Customer Tags'",
            ),
            (
                {"select": {"dimensions": ["Region"], "measures": ["Tagged Revenue"]}},
                "crm",
                "A measure unnests 'Customer Tags'",
            ),
            (
                {"select": {"dimensions": ["Tier"], "measures": ["Revenue"]}},
                "geo",
                "The join 'Customer Tags' -> 'Tiers' unnests 'Customer Tags'",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Tagged Month"],
                        "measures": ["Revenue Tagged Since 2024"],
                    }
                },
                "crm",
                "A measure unnests 'Customer Tags'",
            ),
            (
                {
                    "select": {
                        "dimensions": ["Region"],
                        "measures": ["Tagged Revenue", "Revenue per Tag"],
                    }
                },
                "crm",
                "Dimension 'Tag' unnests 'Customer Tags'",
            ),
            (
                {"select": {"dimensions": ["Tagged Month"], "measures": ["Running Revenue"]}},
                "crm",
                "Dimension 'Tagged Month' unnests 'Customer Tags'",
            ),
        ],
        ids=[
            "filtered_after_the_join",
            "filtered_after_the_join_ungrouped",
            "filtered_through_a_one_to_one_join",
            "filtered_beside_a_filter_context",
            "a_measure_filtered_on_tags",
            "joined_on_to_another_source",
            "a_measure_filtered_on_dates_by_their_declared_month",
            "a_measure_filtered_on_tags_beside_a_reaggregate_per_tag",
            "a_running_total_by_a_declared_month",
        ],
    )
    def test_a_value_read_through_an_unnest_ungrouped_is_refused(
        self, query: dict[str, Any], geo: str, reader: str
    ) -> None:
        """The leg returns a row per tag that the query does not group by: by
        region, filtered on an amount over 90 or the gold tag, customer 10's
        first order came once per tag, 221.25 for 120.75. With the tiers on a
        source of their own, two tags of one tier would count an order twice:
        were gold a top tier too, 248.50 for 127.75. By month, customer 10's
        two January tags are two dates, and a reaggregate per tag groups only
        its own first stage: 241.50 for 120.75 each. Beside a running total,
        the leg returns the dates as stored: 346.75 for 226.00."""
        with pytest.raises(ResolutionError) as caught:
            _compile(island_model(True, geo), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert reader in error.message
        assert "which the query does not group by" in error.message

    def test_a_static_filter_on_an_unnested_object(
        self, island_files: dict[str, dict[str, str]]
    ) -> None:
        """A static filter on the tags runs after the join: by tag, each tag's
        group holds an order once; by region, customer 10's orders came once
        per tag, 241.50 for 120.75."""
        static = (
            "filters:\n"
            "  - {dataObject: Customer Tags, column: Tag, operator: notequals, value: x}\n"
        )
        by_tag = {"select": {"dimensions": ["Tag"], "measures": ["Revenue"]}}
        by_region = {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}}
        paths = island_files["crm"]

        single = _run_on(paths, island_model(False) + static, by_tag)

        assert _run_on(paths, island_model(True) + static, by_tag) == single
        assert single
        with pytest.raises(ResolutionError) as caught:
            _compile(island_model(True) + static, by_region)
        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert "A model filter unnests 'Customer Tags'" in error.message

    @pytest.mark.parametrize(
        ("dimensions", "join"),
        [
            (["Tagged At", "Tier"], "'Customer Tags' -> 'Moments'"),
            (["Tagged Hour", "Hour Tier"], "'Customer Tags' -> 'Hours'"),
        ],
        ids=["on_the_timestamp", "on_its_hour"],
    )
    def test_a_timestamp_joined_on_from_an_unnested_object(
        self, moment_files: dict[str, str], dimensions: list[str], join: str
    ) -> None:
        """By moment and tier, customer 10's orders once per moment: as stored,
        00:30 and 01:30 UTC are two values, as the join to their tiers keeps
        them, and so are their hours, 0 and 1. In Berlin both show 02:30, and
        both hours 2, a group the join's two keys would count customer 10's
        orders twice in, 241.50 for 120.75: refused."""
        query = {"select": {"dimensions": dimensions, "measures": ["Revenue"]}}
        utc = {"source_zone": "UTC", "combine_zone": "UTC"}

        single = _run_on(moment_files, moments_model(False, zone=False), query, **utc)

        assert _run_on(moment_files, moments_model(True, zone=False), query, **utc) == single
        assert single
        with pytest.raises(ResolutionError) as caught:
            _compile(moments_model(True, zone=True), query)
        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert f"The join {join} unnests 'Customer Tags'" in error.message

    @pytest.mark.parametrize(
        ("dimensions", "join"),
        [
            (["Tagged At", "Tier"], "'Customer Tags' -> 'Moments'"),
            (["Tagged Hour", "Hour Tier"], "'Customer Tags' -> 'Hours'"),
        ],
        ids=["on_the_timestamp", "on_its_hour"],
    )
    def test_a_timestamp_its_leg_converts_joined_on_from_an_unnested_object(
        self, monkeypatch: pytest.MonkeyPatch, dimensions: list[str], join: str
    ) -> None:
        """As above, the tags stored without a zone in UTC, a source's, and read
        in Berlin's, the model's ``defaultTimezone``: the leg converts them, and
        both show 02:30, both hours 2."""
        from orionbelt.service import datasource_selection

        monkeypatch.setattr(datasource_selection, "source_timezone", lambda name, dialect: "UTC")
        naive = (
            moments_model(True, zone=False)
            .replace("timestamp_tz", "timestamp")
            .replace("  defaultDialect: duckdb\n", "  defaultDialect: duckdb\n" + BERLIN)
        )
        query = {"select": {"dimensions": dimensions, "measures": ["Revenue"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(naive, query)
        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert f"The join {join} unnests 'Customer Tags'" in error.message

    @pytest.mark.parametrize(
        ("static", "measure", "reader"),
        [
            ("", "Revenue by Tag Copy", "A measure unnests 'Customer Tags'"),
            (
                "filters:\n"
                "  - {dataObject: Customers, column: Tag Copy, operator: notequals, value: x}\n",
                "Revenue",
                "A model filter unnests 'Customer Tags'",
            ),
        ],
        ids=["a_measure_filter", "a_static_filter"],
    )
    def test_a_column_computed_from_an_unnested_object_is_refused(
        self, static: str, measure: str, reader: str
    ) -> None:
        """A customer column computed from their tags is one per tag: by
        region, customer 10's orders came once per tag, 241.50 for 120.75. The
        orders' join required, the combine step reads it where it has a value."""
        query = {"select": {"dimensions": ["Region"], "measures": [measure]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(island_model(True, required=True) + static, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_QUERY"
        assert reader in error.message

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Topic"], "measures": ["Revenue"]}},
            {"select": {"dimensions": ["Topic", "Region"]}},
        ],
        ids=["with_a_measure", "dimensions_alone"],
    )
    def test_a_many_to_many_join_between_sources_is_refused(self, query: dict[str, Any]) -> None:
        """The links on a source of their own: one database repeats an order
        once per link of its customer, across sources each order joins one."""
        with pytest.raises(ResolutionError) as caught:
            _compile(_links_apart(island_model(True)), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert "'Customers' -> 'Links' between data sources is not many-to-one" in error.message
        assert error.hint == (
            "Leave out the dimensions reached through this join, or keep these data objects "
            "in one data source."
        )

    def test_an_exists_over_a_nested_object_there_is_refused(self) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(_keyed(island_model(True)), TAGGED_VIP)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert "Nested data object 'Customer Tags'" in error.message
        assert error.hint is not None and "Filter on a dimension" in error.hint

    def test_a_filter_on_the_nested_dimension_keeps_the_orders_exists_keeps(
        self, island_files: dict[str, dict[str, str]]
    ) -> None:
        """The hint's query: orders of customers tagged vip, each once."""
        paths = island_files["crm"]
        by_tag = {**TAGGED_VIP, "where": [{"field": "Tag", "op": "=", "value": "vip"}]}

        single = _run_on(paths, _keyed(island_model(False)), TAGGED_VIP)

        assert _run_on(paths, _keyed(island_model(True)), by_tag) == single
        assert single == [("EU", Decimal("120.75")), ("US", Decimal("7.00"))]

    @pytest.mark.parametrize(
        "where",
        [
            [{"field": "Orders.Amount", "op": ">", "value": 50}],
            [{"field": "Customers.Region", "op": "=", "value": "EU"}],
            [
                {
                    "logic": "or",
                    "filters": [
                        {"field": "Customers.Region", "op": "=", "value": "US"},
                        {"field": "Orders.Amount", "op": ">", "value": 50},
                    ],
                }
            ],
            [{"field": "Customers.Region", "op": "is_null"}],
        ],
        ids=["of_the_facts", "of_the_customers", "in_an_or_across_sources", "is_null"],
    )
    def test_a_filter_on_a_column_filters_as_on_one_database(
        self, island_files: dict[str, dict[str, str]], where: list[Any]
    ) -> None:
        """One database compares the column as it compares a dimension over it."""
        paths = island_files["crm"]
        query = {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}, "where": where}

        single = _run_on(paths, island_model(False), query)

        assert _run_on(paths, island_model(True), query) == single
        assert single


# The orders of customers some tag of whom is vip, by region.
TAGGED_VIP = {
    "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
    "where": [
        {
            "field": "Customers.Customer ID",
            "op": "exists",
            "subquery": {
                "dataObject": "Customer Tags",
                "filter": [{"field": "Customer Tags.Tag", "op": "=", "value": "vip"}],
            },
        }
    ],
}


def _keyed(yaml: str) -> str:
    """*yaml* with the customers keyed, which an exists over their tags matches on."""
    return yaml.replace(
        "Customer ID: {code: customer_id, abstractType: int}",
        "Customer ID: {code: customer_id, abstractType: int, primaryKey: true}",
    )


def _links_apart(yaml: str) -> str:
    """*yaml* with the customers' links on a source of their own."""
    return yaml.replace(
        "  Links:\n    code: links\n    dataSource: crm\n",
        "  Links:\n    code: links\n    dataSource: geo\n",
    )


# Orders on the shop, employees and customers in the CRM. Order 3 has no
# support employee, order 5's support employee 9 and customer 999 are missing,
# and customer 102 has no account manager.
ROLE_TABLES = {
    "shop": [
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 1, 2, 10.50, 100), (2, 1, 1, 20.25, 100),"
        " (3, 2, NULL, 5.00, 101), (4, 3, 2, 7.00, 102), (5, 2, 9, 1.25, 999))"
        " t(order_id, sales_id, support_id, amount, customer_id)"
    ],
    "crm": [
        "CREATE TABLE employees AS SELECT * FROM (VALUES (1, 'Ann', 'east', DATE '2020-03-01'),"
        " (2, 'Bob', 'west', DATE '2021-07-15'), (3, 'Cid', NULL, DATE '2021-01-10'))"
        " t(employee_id, name, team, hired)",
        "CREATE TABLE customers AS SELECT * FROM (VALUES (100, 2), (101, 1), (102, NULL))"
        " t(customer_id, manager_id)",
    ],
}


def roles_model(federated: bool, static: str = "", employees: str = "crm") -> str:
    """Orders with a sales and a support employee, customers with an account manager.

    The employees in the CRM, or on *employees*' source.
    """
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    staff = f"    dataSource: {employees}\n" if federated else ""
    filters = f"filters:\n{static}" if static else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: orders
{shop}    columns:
      Order ID: {{code: order_id, abstractType: int}}
      Sales Employee ID: {{code: sales_id, abstractType: int}}
      Support Employee ID: {{code: support_id, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal, numClass: additive}}
      Order Customer: {{code: customer_id, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Employees
        pathName: sales
        columnsFrom: [Sales Employee ID]
        columnsTo: [Employee ID]
      - joinType: many-to-one
        joinTo: Employees
        secondary: true
        pathName: support
        columnsFrom: [Support Employee ID]
        columnsTo: [Employee ID]
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Customers:
    code: customers
{crm}    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
      Manager ID: {{code: manager_id, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Employees
        pathName: manager
        columnsFrom: [Manager ID]
        columnsTo: [Employee ID]
  Employees:
    code: employees
{staff}    columns:
      Employee ID: {{code: employee_id, abstractType: int}}
      Name: {{code: name, abstractType: string}}
      Team: {{code: team, abstractType: string}}
      Hired: {{code: hired, abstractType: date}}
dimensions:
  Employee Name: {{dataObject: Employees, column: Name, resultType: string}}
  Sales Employee:
    {{dataObject: Employees, column: Name, resultType: string, via: Orders, pathName: sales}}
  Support Employee:
    {{dataObject: Employees, column: Name, resultType: string, via: Orders, pathName: support}}
  Support Team:
    {{dataObject: Employees, column: Team, resultType: string, via: Orders, pathName: support}}
  Account Manager:
    {{dataObject: Employees, column: Name, resultType: string, via: Customers, pathName: manager}}
  Order Employee: {{dataObject: Employees, column: Name, resultType: string, via: Orders}}
  Support Hired:
    {{dataObject: Employees, column: Hired, resultType: date, via: Orders, pathName: support}}
  Order ID: {{dataObject: Orders, column: Order ID, resultType: int}}
measures:
  Revenue:
    {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum, resultType: decimal}}
{filters}"""


@pytest.fixture(scope="module")
def role_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("roles")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [*ROLE_TABLES["shop"], *ROLE_TABLES["crm"]]),
        # The employees on the shop too, where a test keeps them beside the orders.
        ("shop", [*ROLE_TABLES["shop"], ROLE_TABLES["crm"][0]]),
        ("crm", ROLE_TABLES["crm"]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


ROLE_QUERIES = {
    "two_roles_of_one_join_target": {
        "select": {"dimensions": ["Sales Employee", "Support Employee"], "measures": ["Revenue"]}
    },
    "a_role_beside_the_plain_dimension": {
        "select": {"dimensions": ["Employee Name", "Support Employee"], "measures": ["Revenue"]}
    },
    "two_columns_of_one_role": {
        "select": {"dimensions": ["Support Employee", "Support Team"], "measures": ["Revenue"]}
    },
    "filtered_on_another_role": {
        "select": {"dimensions": ["Support Employee"], "measures": ["Revenue"]},
        "where": [{"field": "Sales Employee", "op": "=", "value": "Ann"}],
    },
    "a_role_missing_or_another": {
        "select": {"dimensions": ["Sales Employee"], "measures": ["Revenue"]},
        "where": [
            _either(
                {"field": "Support Employee", "op": "is_null"},
                {"field": "Sales Employee", "op": "=", "value": "Ann"},
            )
        ],
    },
    "or_across_sources": {
        "select": {"dimensions": ["Sales Employee"], "measures": ["Revenue"]},
        "where": [
            _either(
                {"field": "Support Team", "op": "=", "value": "east"},
                {"field": "Order ID", "op": ">", "value": 4},
            )
        ],
    },
    "a_role_within_the_dimension_source": {
        "select": {"dimensions": ["Account Manager", "Sales Employee"], "measures": ["Revenue"]}
    },
    "without_measures": {"select": {"dimensions": ["Sales Employee", "Support Employee"]}},
    "a_role_selected_at_a_grain_filtered_after_the_join": {
        "select": {"dimensions": ["Support Hired:year"], "measures": ["Revenue"]},
        "where": [
            _either(
                {"field": "Support Hired", "op": ">=", "value": "2021-07-01"},
                {"field": "Order ID", "op": "<", "value": 3},
            )
        ],
    },
    "via_without_a_path_name": {
        "select": {"dimensions": ["Order Employee", "Support Employee"], "measures": ["Revenue"]}
    },
}
TEAM_NOT_WEST = "  - {dataObject: Employees, column: Team, operator: notequals, value: west}\n"


class TestRoles:
    """Each role of a dimension source is a leg of its own, as one database joins it."""

    def _run(
        self, paths: dict[str, str], federated: bool, query: dict[str, Any], **model: Any
    ) -> list[Any]:
        compiled = _compile(roles_model(federated, **model), query)
        if not federated:
            assert compiled.federation is None
            con = duckdb.connect(paths["all"], read_only=True)
            try:
                return sorted(_rows(con, compiled.sql), key=repr)
            finally:
                con.close()
        combine = legs_registered(paths, compiled.federation)
        return sorted(_rows(combine, compiled.sql), key=repr)

    @pytest.mark.parametrize("query", ROLE_QUERIES.values(), ids=ROLE_QUERIES.keys())
    @pytest.mark.parametrize(
        "static",
        ["", TEAM_NOT_WEST, TEAM_NOT_WEST.replace("}\n", ", enforce: always}\n")],
        ids=["", "static", "enforced"],
    )
    def test_federated_returns_what_one_source_returns(
        self, role_files: dict[str, str], query: dict[str, Any], static: str
    ) -> None:
        single = self._run(role_files, False, query, static=static)

        assert self._run(role_files, True, query, static=static) == single
        assert single

    def test_a_static_filter_reads_its_object_not_a_roles_copy(
        self, role_files: dict[str, str]
    ) -> None:
        """The filter is on Employees as declared, joined through the primary
        (sales) join. Order 1's support employee Bob is of the west team, its
        sales employee Ann is not: the order stays, as on one database."""
        static = TEAM_NOT_WEST.replace("}\n", ", enforce: always}\n")
        query = {
            "select": {
                "dimensions": ["Sales Employee", "Support Employee"],
                "measures": ["Revenue"],
            }
        }

        rows = self._run(role_files, True, query, static=static)

        assert rows == self._run(role_files, False, query, static=static)
        assert ("Ann", "Bob", Decimal("10.50")) in rows

    @pytest.mark.parametrize("employees", ["crm", "shop"], ids=["across", "within_a_source"])
    def test_a_secondary_path_the_query_picks(
        self, role_files: dict[str, str], employees: str
    ) -> None:
        """Employee Name by the orders' support employee, not the sales one: on
        another source, the edge; beside the orders, the shop leg's join."""
        query = {
            "select": {"dimensions": ["Employee Name", "Account Manager"], "measures": ["Revenue"]},
            "usePathNames": [{"source": "Orders", "target": "Employees", "pathName": "support"}],
        }

        single = self._run(role_files, False, query)

        assert self._run(role_files, True, query, employees=employees) == single
        # Order 1's support employee is Bob; by sales, Ann would have 30.75.
        assert ("Bob", "Bob", Decimal("10.50")) in single

    @pytest.mark.parametrize(
        "dimensions",
        [
            [{"coalesce": ["Sales Employee", "Support Employee"], "as": "Employee"}],
            [{"coalesce": ["Sales Employee", "Account Manager"], "as": "Employee"}],
            [
                {"coalesce": ["Sales Employee", "Support Employee"], "as": "Employee"},
                "Sales Employee",
            ],
            [
                {"coalesce": ["Sales Employee", "Support Employee"], "as": "Employee"},
                {"coalesce": ["Sales Employee", "Account Manager"], "as": "Owner"},
            ],
        ],
        ids=[
            "two_roles",
            "roles_of_two_sources",
            "a_member_selected_too",
            "a_member_in_two_groups",
        ],
    )
    def test_a_coalesce_group(self, role_files: dict[str, str], dimensions: list[Any]) -> None:
        """The legs return each member once; the combine query keeps the groups."""
        query = {"select": {"dimensions": dimensions, "measures": ["Revenue"]}}

        single = self._run(role_files, False, query)

        assert self._run(role_files, True, query) == single
        assert single

    def test_each_role_reads_the_target_under_its_own_name(self) -> None:
        query = {
            "select": {
                "dimensions": ["Sales Employee", "Support Employee"],
                "measures": ["Revenue"],
            }
        }

        plan = _compile(roles_model(True), query).federation

        orders, sales, support = plan.legs
        assert (sales.data_source, support.data_source) == ("crm", "crm")
        assert 'FROM "employees" AS "Employees__Orders__sales"' in sales.sql
        assert 'FROM "employees" AS "Employees__Orders__support"' in support.sql
        assert '"Orders"."sales_id" AS "__fk0_0"' in orders.sql
        assert '"Orders"."support_id" AS "__fk1_0"' in orders.sql


def computed_chain_yaml(*sources: str, direct: bool = False) -> str:
    """The chain with each customer's region name computed on the customers,
    and with *direct*, the orders joined to the regions too."""
    yaml = chain_yaml(*sources).replace(
        "      Customer Region: {code: region_id, abstractType: int}\n",
        "      Customer Region: {code: region_id, abstractType: int}\n"
        '      Home Region: {expression: "{[Regions].[Region Name]}", abstractType: string}\n',
    )
    yaml = yaml.replace(
        "dimensions:\n",
        "dimensions:\n"
        "  Home Region: {dataObject: Customers, column: Home Region, resultType: string}\n",
    )
    if direct:
        yaml = yaml.replace(
            "      Amount: {code: amount, abstractType: decimal}\n    joins:\n",
            "      Amount: {code: amount, abstractType: decimal}\n"
            "      Order Region: {code: region_id, abstractType: int}\n    joins:\n"
            "      - joinType: many-to-one\n        joinTo: Regions\n"
            "        columnsFrom: [Order Region]\n        columnsTo: [Region ID]\n",
        )
    return yaml


HOME_EU = (
    "filters:\n  - {dataObject: Customers, column: Home Region, operator: equals, value: EU}\n"
)


class TestComputedDimensions:
    """A dimension computed from another data object's column is computed in its leg."""

    QUERY = {"select": {"dimensions": ["Home Region"], "measures": ["Revenue"]}}

    def test_from_an_object_joined_within_its_source(self, chain_files: dict[str, str]) -> None:
        """The customers' regions in the CRM too: one leg joins both."""
        single = _compile(computed_chain_yaml("all", "all", "all"), self.QUERY)
        compiled = _compile(computed_chain_yaml("shop", "crm", "crm"), self.QUERY)
        con = duckdb.connect(chain_files["all"], read_only=True)
        try:
            expected = sorted(_rows(con, single.sql), key=repr)
        finally:
            con.close()
        combine = legs_registered(chain_files, compiled.federation)

        assert sorted(_rows(combine, compiled.sql), key=repr) == expected
        assert ("EU", Decimal("120.75")) in expected

    @pytest.mark.parametrize(
        ("sources", "direct"),
        [(("shop", "crm", "geo"), False), (("shop", "crm", "crm"), True)],
        ids=["on_another_source", "through_another_join"],
    )
    def test_from_an_object_joined_apart_is_refused(
        self, sources: tuple[str, str, str], direct: bool
    ) -> None:
        """Its leg would name a table it does not read. Joined to the orders
        directly, the regions are reached that way, on one database too: not
        through the customers."""
        with pytest.raises(ResolutionError) as caught:
            _compile(computed_chain_yaml(*sources, direct=direct), self.QUERY)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert error.message.startswith(
            "Dimension 'Home Region' is computed from Regions, which the query does not "
            "join to 'Customers' within its data source"
        )

    def test_a_static_filter_computed_within_its_source(self, chain_files: dict[str, str]) -> None:
        """The customers' leg filters on their region, which it joins."""
        query = {"select": {"dimensions": ["Customer Region"], "measures": ["Revenue"]}}
        single = _compile(computed_chain_yaml("all", "all", "all") + HOME_EU, query)
        compiled = _compile(computed_chain_yaml("shop", "crm", "crm") + HOME_EU, query)
        con = duckdb.connect(chain_files["all"], read_only=True)
        try:
            expected = sorted(_rows(con, single.sql), key=repr)
        finally:
            con.close()
        combine = legs_registered(chain_files, compiled.federation)

        assert sorted(_rows(combine, compiled.sql), key=repr) == expected
        assert expected == [(1, Decimal("120.75"))]

    @pytest.mark.parametrize(
        ("sources", "direct"),
        [(("shop", "crm", "geo"), False), (("shop", "crm", "crm"), True)],
        ids=["on_another_source", "through_another_join"],
    )
    def test_a_static_filter_computed_from_an_object_joined_apart_is_refused(
        self, sources: tuple[str, str, str], direct: bool
    ) -> None:
        """Its leg named a table it does not read: DuckDB, ``Referenced table
        "Regions" not found``."""
        query = {"select": {"dimensions": ["Customer Region"], "measures": ["Revenue"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(computed_chain_yaml(*sources, direct=direct) + HOME_EU, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert error.message.startswith(
            "The static filter on 'Customers.Home Region' is computed from Regions, which "
            "the query does not join to 'Customers' within its data source"
        )


# Orders on the shop with their rep; customers, employees and tickets in the
# CRM. Acme's tickets went to Ann and Bob, Beta's to Bob, Cora's to Cid; order
# 5's customer 99 is missing.
SHARED_TABLES = {
    "shop": [
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 10, 1, 100.50), (2, 10, 2, 20.25),"
        " (3, 11, 1, 7.00), (4, 12, 3, 55.10), (5, 99, 2, 3.15))"
        " t(order_id, customer_id, rep_id, amount)"
    ],
    "crm": [
        "CREATE TABLE customers AS SELECT * FROM (VALUES (10, 'Acme'), (11, 'Beta'), (12, 'Cora'))"
        " t(customer_id, name)",
        "CREATE TABLE employees AS SELECT * FROM (VALUES (1, 'Ann'), (2, 'Bob'), (3, 'Cid'))"
        " t(employee_id, name)",
        "CREATE TABLE tickets AS SELECT * FROM (VALUES (10, 1), (10, 2), (11, 2), (12, 3))"
        " t(customer_id, agent_id)",
    ],
}


def shared_yaml(federated: bool, static: str = "") -> str:
    """Orders joined to their customer and to their rep, an employee; tickets
    joined to the customer and to their agent, an employee too."""
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    filters = f"filters:\n{static}" if static else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: orders
{shop}    columns:
      Order ID: {{code: order_id, abstractType: int}}
      Order Customer: {{code: customer_id, abstractType: int}}
      Order Rep: {{code: rep_id, abstractType: int}}
      Amount: {{code: amount, abstractType: decimal}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Employees
        columnsFrom: [Order Rep]
        columnsTo: [Employee ID]
  Customers:
    code: customers
{crm}    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
      Customer Name: {{code: name, abstractType: string}}
  Employees:
    code: employees
{crm}    columns:
      Employee ID: {{code: employee_id, abstractType: int}}
      Name: {{code: name, abstractType: string}}
  Tickets:
    code: tickets
{crm}    columns:
      Ticket Customer: {{code: customer_id, abstractType: int}}
      Ticket Agent: {{code: agent_id, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Ticket Customer]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Employees
        columnsFrom: [Ticket Agent]
        columnsTo: [Employee ID]
dimensions:
  Customer Name: {{dataObject: Customers, column: Customer Name, resultType: string}}
  Rep: {{dataObject: Employees, column: Name, resultType: string}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
{filters}"""


@pytest.fixture(scope="module")
def shared_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("shared")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [*SHARED_TABLES["shop"], *SHARED_TABLES["crm"]]),
        ("shop", SHARED_TABLES["shop"]),
        ("crm", SHARED_TABLES["crm"]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


def _agent(op: str, name: str) -> dict[str, Any]:
    """The orders of customers with (or without) a ticket handled by *name*."""
    subquery = {
        "dataObject": "Tickets",
        "filter": [{"field": "Employees.Name", "op": "=", "value": name}],
    }
    return {"field": "Customers.Customer ID", "op": op, "subquery": subquery}


SHARED_QUERIES = {
    "exists_by_the_rep": {
        "select": {"dimensions": ["Rep"], "measures": ["Revenue"]},
        "where": [_agent("exists", "Ann")],
    },
    "nonexists_by_the_rep": {
        "select": {"dimensions": ["Rep"], "measures": ["Revenue"]},
        "where": [_agent("nonexists", "Ann")],
    },
    "exists_by_customer_and_rep": {
        "select": {"dimensions": ["Customer Name", "Rep"], "measures": ["Revenue"]},
        "where": [_agent("exists", "Cid")],
    },
    "exists_beside_a_filter_on_the_rep": {
        "select": {"dimensions": ["Customer Name"], "measures": ["Revenue"]},
        "where": [_agent("exists", "Ann"), {"field": "Rep", "op": "=", "value": "Ann"}],
    },
}
NOT_BOB = "  - {dataObject: Employees, column: Name, operator: notequals, value: Bob}\n"


class TestExistsReadingAnOuterObject:
    """A subquery reading an object the outer query joins elsewhere reads it in its own leg."""

    @pytest.mark.parametrize("query", SHARED_QUERIES.values(), ids=SHARED_QUERIES.keys())
    @pytest.mark.parametrize(
        "static",
        ["", NOT_BOB, NOT_BOB.replace("}\n", ", enforce: always}\n")],
        ids=["", "static", "enforced"],
    )
    def test_federated_returns_what_one_source_returns(
        self, shared_files: dict[str, str], query: dict[str, Any], static: str
    ) -> None:
        """The employees are joined twice, as the orders' rep and as the
        tickets' agent: their leg, and the customers' leg for the subquery."""
        single = _compile(shared_yaml(False, static), query)
        con = duckdb.connect(shared_files["all"], read_only=True)
        try:
            expected = sorted(_rows(con, single.sql), key=repr)
        finally:
            con.close()
        compiled = _compile(shared_yaml(True, static), query)
        combine = legs_registered(shared_files, compiled.federation)

        assert sorted(_rows(combine, compiled.sql), key=repr) == expected
        assert expected

    def test_a_static_filter_computed_from_the_shared_object_is_refused(self) -> None:
        """The customers' rep name is the employee the outer query joins, the
        order's rep: in the customers' leg it would read the tickets' agent."""
        yaml = shared_yaml(
            True,
            "  - {dataObject: Customers, column: Rep Name, operator: equals, value: Ann}\n",
        ).replace(
            "      Customer Name: {code: name, abstractType: string}\n",
            "      Customer Name: {code: name, abstractType: string}\n"
            '      Rep Name: {expression: "{[Employees].[Name]}", abstractType: string}\n',
        )

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, SHARED_QUERIES["exists_by_the_rep"])

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert error.message.startswith(
            "The static filter on 'Customers.Rep Name' is computed from Employees"
        )

    def test_the_subjects_leg_reads_the_object_for_the_subquery(self) -> None:
        query = SHARED_QUERIES["exists_by_the_rep"]

        plan = _compile(shared_yaml(True), query).federation

        legs = {leg.data_objects: leg for leg in plan.legs}
        assert set(legs) == {
            ("Orders",),
            ("Employees",),
            ("Customers", "Employees", "Tickets"),
        }
        customers = legs["Customers", "Employees", "Tickets"]
        assert 'INNER JOIN "employees" AS "Employees"' in customers.sql


def returns_chain_yaml(*sources: str, static: str = "") -> str:
    """The chain with order IDs, and the orders' returns, each with the region
    it was returned in, on the orders' source."""
    yaml = chain_yaml(*sources).replace(
        "      Order Customer: {code: customer_id, abstractType: int}\n",
        "      Order ID: {code: order_id, abstractType: int}\n"
        "      Order Customer: {code: customer_id, abstractType: int}\n",
    )
    returns = (
        f"  Returns:\n    code: returns\n    dataSource: {sources[0]}\n    columns:\n"
        "      Return Order: {code: order_id, abstractType: int}\n"
        "      Return Region: {code: region_id, abstractType: int}\n"
        "    joins:\n"
        "      - joinType: many-to-one\n        joinTo: Orders\n"
        "        columnsFrom: [Return Order]\n        columnsTo: [Order ID]\n"
        "      - joinType: many-to-one\n        joinTo: Regions\n"
        "        columnsFrom: [Return Region]\n        columnsTo: [Region ID]\n"
    )
    return yaml.replace("dimensions:\n", returns + "dimensions:\n", 1) + static


RETURN_TABLES = {
    **CHAIN_TABLES,
    "orders": "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 10, 100.50), (2, 10, 20.25),"
    " (3, 11, 7.00), (4, 12, 55.10), (5, 13, 3.15), (6, 99, 40.00))"
    " t(order_id, customer_id, amount)",
    # Order 1 was returned in the EU, order 2 in the US, order 3 in the EU.
    "returns": "CREATE TABLE returns AS SELECT * FROM (VALUES (1, 1), (2, 2), (3, 1))"
    " t(order_id, region_id)",
}


@pytest.mark.parametrize(
    "static",
    [
        "",
        "filters:\n  - {dataObject: Regions, column: Region Name, operator: notequals,"
        " value: US, enforce: always}\n",
    ],
    ids=["", "enforced"],
)
def test_a_subquery_on_the_facts_source_reads_the_object_with_injected_keys(
    tmp_path: Any, static: str
) -> None:
    """The orders returned in the EU, of customers in region 1, by the
    customers' region: the regions are read through the CRM's customers, a
    leg of their own on the shop, and by the returns' subquery in the fact
    leg. The fact leg reads only the keys the customers' leg returns, and
    leaves the regions' static filter to their own leg there too."""
    query = {
        "select": {"dimensions": ["Region Name"], "measures": ["Revenue"]},
        "where": [
            {
                "field": "Orders.Order ID",
                "op": "exists",
                "subquery": {
                    "dataObject": "Returns",
                    "filter": [{"field": "Regions.Region Name", "op": "=", "value": "EU"}],
                },
            },
            {"field": "Customer Region", "op": "=", "value": 1},
        ],
    }
    layout = {
        "all": list(RETURN_TABLES),
        "shop": ["orders", "regions", "returns"],
        "crm": ["customers"],
    }
    paths = {name: str(tmp_path / f"{name}.duckdb") for name in layout}
    for name, tables in layout.items():
        con = duckdb.connect(paths[name])
        for table in tables:
            con.execute(RETURN_TABLES[table])
        con.close()
    single = _compile(returns_chain_yaml("all", "all", "all", static=static), query)
    con = duckdb.connect(paths["all"], read_only=True)
    try:
        expected = sorted(_rows(con, single.sql), key=repr)
    finally:
        con.close()

    compiled = _compile(returns_chain_yaml("shop", "crm", "shop", static=static), query)
    plan = compiled.federation
    assert plan.legs[0].data_objects == ("Orders", "Regions", "Returns")
    tables = {}
    for leg in plan.legs:
        con = duckdb.connect(paths[leg.data_source], read_only=True)
        try:
            tables[leg.name] = con.sql(leg.sql).to_arrow_table()
        finally:
            con.close()
    (injection,) = plan.injections
    values = tables[injection.leg].column(injection.values or injection.column)
    keys = sorted(v for v in values.to_pylist() if v)
    con = duckdb.connect(paths["shop"], read_only=True)
    try:
        tables["leg_0"] = con.sql(plan.inject({injection.column: keys}).sql).to_arrow_table()
    finally:
        con.close()
    combine = duckdb.connect()
    for name, table in tables.items():
        combine.register(name, table)

    assert sorted(_rows(combine, compiled.sql), key=repr) == expected
    assert expected == [("EU", Decimal("100.50"))]


# Orders on the shop, their products' lines in a catalog and the lines' names
# on a third source, each joined on a string key; the reviews of a product in
# the catalog; the orders' list prices and the products' stock in a warehouse,
# on their SKUs too. Order 3's product is missing, order 4 has none.
KEYED = """\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Orders:
    code: orders
{shop}    columns:
      Order ID: {{code: order_id, abstractType: int}}
      Order SKU: {{code: sku, abstractType: string}}
      Amount: {{code: amount, abstractType: decimal}}
    joins:
      - {{joinType: many-to-one, joinTo: Products, columnsFrom: [Order SKU], columnsTo: [SKU]}}
      - {{joinType: many-to-one, joinTo: Prices, columnsFrom: [Order SKU], columnsTo: [Price SKU]}}
  Products:
    code: products
{catalog}    columns:
      SKU: {{code: sku, abstractType: string}}
      Product Line: {{code: line, abstractType: string}}
    joins:
      - {{joinType: many-to-one, joinTo: Lines, columnsFrom: [Product Line], columnsTo: [Code]}}
      - {{joinType: many-to-one, joinTo: Stock, columnsFrom: [SKU], columnsTo: [Stock SKU]}}
  Lines:
    code: lines
{lines}    columns:
      Code: {{code: code, abstractType: string}}
      Line Name: {{code: name, abstractType: string}}
  Reviews:
    code: reviews
{catalog}    columns:
      Review SKU: {{code: sku, abstractType: string}}
      Stars: {{code: stars, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Products, columnsFrom: [Review SKU], columnsTo: [SKU]}}
  Stock:
    code: stock
{warehouse}    columns:
      Stock SKU: {{code: sku, abstractType: string}}
      Qty: {{code: qty, abstractType: int}}
  Prices:
    code: prices
{warehouse}    columns:
      Price SKU: {{code: sku, abstractType: string}}
      List Price: {{code: price, abstractType: decimal}}
dimensions:
  Line: {{dataObject: Products, column: Product Line, resultType: string}}
  Line Name: {{dataObject: Lines, column: Line Name, resultType: string}}
  List Price: {{dataObject: Prices, column: List Price, resultType: decimal}}
  Qty: {{dataObject: Stock, column: Qty, resultType: int}}
measures:
  Revenue: {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum}}
"""
KEYED_TABLES = {
    "shop": [
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 'a1', 10.00), (2, 'b2', 20.00),"
        " (3, 'zz', 5.00), (4, NULL, 7.00), (5, 'a1', 1.50)) t(order_id, sku, amount)"
    ],
    "catalog": [
        "CREATE TABLE products AS SELECT * FROM (VALUES ('a1', 'L1'), ('b2', 'L2')) t(sku, line)",
        "CREATE TABLE reviews AS SELECT * FROM (VALUES ('a1', 5), ('b2', 2)) t(sku, stars)",
    ],
    "lines": [
        "CREATE TABLE lines AS SELECT * FROM (VALUES ('L1', 'First'), ('L2', 'Second'))"
        " t(code, name)"
    ],
    "warehouse": [
        "CREATE TABLE stock AS SELECT * FROM (VALUES ('a1', 3), ('b2', 4)) t(sku, qty)",
        "CREATE TABLE prices AS SELECT * FROM (VALUES ('a1', 9.99), ('b2', 19.99)) t(sku, price)",
    ],
}


def keyed_model(federated: bool) -> str:
    def source(name: str) -> str:
        return f"    dataSource: {name}\n" if federated else ""

    return KEYED.format(
        shop=source("shop"),
        catalog=source("catalog"),
        lines=source("lines"),
        warehouse=source("warehouse"),
    )


@pytest.fixture(scope="module")
def keyed_files(tmp_path_factory: Any) -> dict[str, str]:
    """The tables on one source, and on the shop, the catalog and the lines' own."""
    root = tmp_path_factory.mktemp("keyed")
    every = [statement for statements in KEYED_TABLES.values() for statement in statements]
    paths: dict[str, str] = {}
    for name, statements in {"all": every, **KEYED_TABLES}.items():
        paths[name] = str(root / f"{name}.duckdb")
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestKeysMatchedByTheirEngine:
    """Join keys both of whose legs run on one engine that compares strings
    otherwise than DuckDB (MySQL's collations) are returned as that engine
    matches them, by both legs (``Dialect.match_key``); the fact leg still
    reads the keys as stored where a dimension leg's filter injects them."""

    @pytest.fixture
    def sources(self, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
        pytest.importorskip("ob_flight")
        monkeypatch.setenv("OBSL_DATABASES", "shop,catalog,lines")

        def plan(shop: str, catalog: str, query: dict[str, Any]) -> Any:
            monkeypatch.setenv("DB_SHOP_TYPE", shop)
            monkeypatch.setenv("DB_CATALOG_TYPE", catalog)
            monkeypatch.setenv("DB_LINES_TYPE", catalog)
            return _compile(keyed_model(True), query).federation

        return plan

    def test_mysql_legs_match_keys_by_their_weight(self, sources: Callable[..., Any]) -> None:
        plan = sources(
            "mysql",
            "mysql",
            {
                "select": {"dimensions": ["Line Name"], "measures": ["Revenue"]},
                "where": [{"field": "Line", "op": "=", "value": "L1"}],
            },
        )

        weight = "HEX(CASE WHEN CONCAT(`__leg`.`{0}`, ' ') = `__leg`.`{0}` THEN "
        orders, products, lines = plan.legs
        assert weight.format("__fk0_0") in orders.sql
        assert weight.format("__fk0_0") in products.sql
        assert weight.format("__fk1_0") in products.sql
        assert weight.format("__fk1_0") in lines.sql
        # The fact leg reads the products' keys as stored, a column of their own.
        (injection,) = plan.injections
        assert (injection.column, injection.values) == ("__fk0_0", "__fv0")
        assert "`__leg`.`__fk0_0` AS `__fv0`" in products.sql

    def test_legs_on_two_engines_match_keys_bytewise(self, sources: Callable[..., Any]) -> None:
        plan = sources(
            "mysql", "postgres", {"select": {"dimensions": ["Line Name"], "measures": ["Revenue"]}}
        )

        assert all("WEIGHT_STRING" not in leg.sql for leg in plan.legs)

    @pytest.fixture
    def hexed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DuckDB's legs return their string keys in hex: matched as DuckDB
        matches them, through every path a match key takes."""

        def key(_: Any, column: Any, abstract_type: str) -> Any:
            return FunctionCall(name="hex", args=[column]) if abstract_type == "string" else None

        monkeypatch.setattr(DuckDBDialect, "match_key", key)

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Line Name"], "measures": ["Revenue"]}},
            {
                "select": {"dimensions": ["Line"], "measures": ["Revenue"]},
                "where": [
                    {
                        "field": "Products.SKU",
                        "op": "nonexists",
                        "subquery": {
                            "dataObject": "Reviews",
                            "filter": [{"field": "Stars", "op": ">=", "value": 4}],
                        },
                    }
                ],
            },
            {"select": {"fields": ["Orders.Order ID", "Lines.Line Name"]}},
        ],
        ids=["a_chain", "nonexists", "raw_rows"],
    )
    @pytest.mark.usefixtures("hexed")
    def test_keys_matched_alike(self, keyed_files: dict[str, str], query: dict[str, Any]) -> None:
        plan = _compile(keyed_model(True), query).federation

        single = _run_on(keyed_files, keyed_model(False), query)

        assert plan is not None
        assert all("hex(" in leg.sql for leg in plan.legs)
        assert _run_on(keyed_files, keyed_model(True), query) == single
        assert single

    @pytest.mark.parametrize(
        "query",
        [
            {
                "select": {
                    "fields": ["Orders.Order ID", "Products.Product Line", "Prices.List Price"]
                }
            },
            {"select": {"fields": ["Orders.Order ID", "Products.Product Line", "Stock.Qty"]}},
            {"select": {"dimensions": ["Line", "List Price"], "measures": ["Revenue"]}},
            {"select": {"dimensions": ["Line Name", "Qty"], "measures": ["Revenue"]}},
        ],
        ids=["raw_rows_branching", "raw_rows_chained", "branching", "chained"],
    )
    @pytest.mark.usefixtures("hexed")
    def test_a_key_joined_to_legs_on_two_engines(
        self,
        keyed_files: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        query: dict[str, Any],
    ) -> None:
        """A SKU joins the catalog's products, on DuckDB, as DuckDB matches it
        (in hex here), and the warehouse's prices or stock, on PostgreSQL, as
        stored: each join reads a column of its own."""
        pytest.importorskip("ob_flight")
        monkeypatch.setenv("OBSL_DATABASES", "warehouse")
        monkeypatch.setenv("DB_WAREHOUSE_TYPE", "postgres")
        plan = _compile(keyed_model(True), query).federation

        single = _run_on(keyed_files, keyed_model(False), query)

        assert plan is not None
        assert "postgres" in [leg.dialect for leg in plan.legs]
        assert _run_on(keyed_files, keyed_model(True), query) == single
        assert single

    @pytest.mark.usefixtures("hexed")
    def test_injected_keys_are_read_as_stored(self, keyed_files: dict[str, str]) -> None:
        """The products' leg runs the filter on their line: the fact leg reads
        the SKUs it returned, as stored, and not in hex."""
        query = {
            "select": {"dimensions": ["Line Name"], "measures": ["Revenue"]},
            "where": [{"field": "Line", "op": "=", "value": "L1"}],
        }
        compiled = _compile(keyed_model(True), query)
        plan = compiled.federation
        tables = {}
        for leg in plan.legs[1:]:
            con = duckdb.connect(keyed_files[leg.data_source], read_only=True)
            try:
                tables[leg.name] = con.sql(leg.sql).to_arrow_table()
            finally:
                con.close()
        (injection,) = plan.injections
        keys = tables[injection.leg].column(injection.values).to_pylist()
        con = duckdb.connect(keyed_files["shop"], read_only=True)
        try:
            fact = plan.inject({injection.column: keys})
            tables["leg_0"] = con.sql(fact.sql).to_arrow_table()
        finally:
            con.close()
        combine = duckdb.connect()
        for name, table in tables.items():
            combine.register(name, table)

        assert keys == ["a1"]
        assert sorted(_rows(combine, compiled.sql), key=repr) == [("First", Decimal("11.50"))]
        assert _run_on(keyed_files, keyed_model(False), query) == [("First", Decimal("11.50"))]


# -- What one database's joins make of a missing or zoned row -----------------


def _run_on(
    paths: dict[str, str],
    yaml: str,
    query: dict[str, Any],
    *,
    source_zone: str | None = None,
    combine_zone: str | None = None,
    sort: bool = True,
) -> list[Any]:
    """*query* on *yaml*: on ``all`` if it reads one data source, else each leg on its own.

    Each source's session, and the combine step's, in the time zone given.
    The rows sorted, unless *sort* is false.
    """

    def ordered(rows: list[Any]) -> list[Any]:
        return sorted(rows, key=repr) if sort else rows

    def connect(path: str) -> Any:
        con = duckdb.connect(path, read_only=True)
        if source_zone:
            con.execute(f"SET TimeZone = '{source_zone}'")
        return con

    compiled = _compile(yaml, query)
    if compiled.federation is None:
        con = connect(paths["all"])
        try:
            return ordered(_rows(con, compiled.sql))
        finally:
            con.close()
    combine = duckdb.connect()
    if combine_zone:
        combine.execute(f"SET TimeZone = '{combine_zone}'")
    for leg in compiled.federation.legs:
        con = connect(paths[leg.data_source])
        try:
            combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
        finally:
            con.close()
    return ordered(_rows(combine, compiled.sql))


def _regions_required(yaml: str) -> str:
    return yaml.replace(
        "        joinTo: Regions\n        columnsFrom: [Customer Region]",
        "        joinTo: Regions\n        required: true\n        columnsFrom: [Customer Region]",
    )


class TestAnInnerJoinInALeg:
    """Orders left-joined to their customers and inner-joined on to the
    customers' regions: one database drops the orders whose customer has no
    region (12, 13) or is missing (99), and so does the combine step, where
    the customers' leg holds only those with a region."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            (
                {"select": {"fields": ["Orders.Amount", "Regions.Region Name"]}},
                [(Decimal("100.50"), "EU"), (Decimal("20.25"), "EU"), (Decimal("7.00"), "US")],
            ),
            (
                {"select": {"dimensions": ["Region Name"], "measures": ["Revenue"]}},
                [("EU", Decimal("120.75")), ("US", Decimal("7.00"))],
            ),
        ],
        ids=["raw", "aggregated"],
    )
    def test_drops_the_fact_rows_one_database_drops(
        self, chain_files: dict[str, str], query: dict[str, Any], expected: list[Any]
    ) -> None:
        single = _run_on(chain_files, _regions_required(chain_yaml("all", "all", "all")), query)

        federated = _run_on(chain_files, _regions_required(chain_yaml("shop", "crm", "crm")), query)

        assert federated == single == expected


# Two orders an hour apart, in the hour Berlin's clocks fall back: one wall-clock
# time there, two instants. Each order's shift starts at the order's instant.
DST_ORDERS = (
    "CREATE TABLE orders AS SELECT * FROM (VALUES"
    " (1, TIMESTAMPTZ '2024-10-27 00:30:00+00', 10.00),"
    " (2, TIMESTAMPTZ '2024-10-27 01:30:00+00', 20.00)) t(order_id, placed, amount)"
)
DST_SHIFTS = (
    "CREATE TABLE shifts AS SELECT * FROM (VALUES"
    " (TIMESTAMPTZ '2024-10-27 00:30:00+00', 'early'),"
    " (TIMESTAMPTZ '2024-10-27 01:30:00+00', 'late')) t(starts, label)"
)


def shifts_yaml(federated: bool) -> str:
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
  queryTimezone: Europe/Berlin
dataObjects:
  Orders:
    code: orders
{shop}    columns:
      Order ID: {{code: order_id, abstractType: int}}
      Placed: {{code: placed, abstractType: timestamp_tz}}
      Amount: {{code: amount, abstractType: decimal}}
    joins:
      - joinType: many-to-one
        joinTo: Shifts
        columnsFrom: [Placed]
        columnsTo: [Starts]
  Shifts:
    code: shifts
{crm}    columns:
      Starts: {{code: starts, abstractType: timestamp_tz}}
      Label: {{code: label, abstractType: string}}
dimensions:
  Shift: {{dataObject: Shifts, column: Label, resultType: string}}
  Placed: {{dataObject: Orders, column: Placed, resultType: timestamp_tz}}
measures:
  Revenue:
    {{columns: [{{dataObject: Orders, column: Amount}}], aggregation: sum, resultType: decimal}}
"""


@pytest.fixture(scope="module")
def shift_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("shifts")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [DST_ORDERS, DST_SHIFTS]),
        ("shop", [DST_ORDERS]),
        ("crm", [DST_SHIFTS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestKeysInTheirOwnTime:
    """A timestamp join key is compared as stored, as one database's join
    compares it, while the query reads the same column in Berlin's time."""

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"fields": ["Orders.Order ID", "Shifts.Label"]}},
            {"select": {"fields": ["Orders.Order ID", "Orders.Placed", "Shifts.Label"]}},
            {"select": {"dimensions": ["Shift"], "measures": ["Revenue"]}},
            {"select": {"dimensions": ["Placed", "Shift"], "measures": ["Revenue"]}},
        ],
        ids=["raw", "raw_with_the_key_as_a_field", "aggregated", "aggregated_by_the_key"],
    )
    def test_two_instants_of_one_wall_clock_time_stay_two_keys(
        self, shift_files: dict[str, str], query: dict[str, Any]
    ) -> None:
        single = _run_on(shift_files, shifts_yaml(False), query)

        assert _run_on(shift_files, shifts_yaml(True), query) == single
        assert len(single) == 2


# The orders and shifts above without a zone, stored alike on both sources.
NAIVE_ORDERS = (
    "CREATE TABLE orders AS SELECT * FROM (VALUES"
    " (1, TIMESTAMP '2024-10-27 00:30:00', 10.00),"
    " (2, TIMESTAMP '2024-10-27 01:30:00', 20.00)) t(order_id, placed, amount)"
)
NAIVE_SHIFTS = (
    "CREATE TABLE shifts AS SELECT * FROM (VALUES"
    " (TIMESTAMP '2024-10-27 00:30:00', 'early'),"
    " (TIMESTAMP '2024-10-27 01:30:00', 'late')) t(starts, label)"
)
# Orders placed at 11:00 and 12:00 in Berlin, stored without a zone, and
# shifts starting at those instants, with one.
BERLIN_ORDERS = (
    "CREATE TABLE orders AS SELECT * FROM (VALUES"
    " (1, TIMESTAMP '2024-01-05 11:00:00', 10.00),"
    " (2, TIMESTAMP '2024-01-05 12:00:00', 20.00)) t(order_id, placed, amount)"
)
ZONED_SHIFTS = (
    "CREATE TABLE shifts AS SELECT * FROM (VALUES"
    " (TIMESTAMPTZ '2024-01-05 10:00:00+00', 'early'),"
    " (TIMESTAMPTZ '2024-01-05 11:00:00+00', 'late')) t(starts, label)"
)


def _files(root: Any, orders: str, shifts: str) -> dict[str, str]:
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (("all", [orders, shifts]), ("shop", [orders]), ("crm", [shifts])):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


def _sources_in(monkeypatch: pytest.MonkeyPatch, zone: str) -> None:
    """Every data source's sessions running in *zone*."""
    from orionbelt.service import datasource_selection

    monkeypatch.setattr(datasource_selection, "source_timezone", lambda name, dialect: zone)


STARTS_COPY = '      Starts Copy: {expression: "{[Shifts].[Starts]}", abstractType: timestamp}\n'
KEY_QUERIES = [
    {"select": {"dimensions": ["Shift"], "measures": ["Revenue"]}},
    {"select": {"fields": ["Orders.Order ID", "Shifts.Label"]}},
]


class TestKeysAsStored:
    """A timestamp join key between sources compares as stored, as one database
    holding both tables compares it, while the legs read the columns themselves
    in the query's zone: a key computed from a column joins as the column, and
    one without a zone meets one with a zone as one database's session reads it."""

    @pytest.mark.parametrize("query", KEY_QUERIES, ids=["aggregated", "raw"])
    def test_a_computed_key_joins_as_its_column(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, query: dict[str, Any]
    ) -> None:
        # Both sources in New York, read in Berlin's time.
        _sources_in(monkeypatch, "America/New_York")
        files = _files(tmp_path, NAIVE_ORDERS, NAIVE_SHIFTS)

        def model(federated: bool) -> str:
            return (
                shifts_yaml(federated)
                .replace("timestamp_tz", "timestamp")
                .replace("  queryTimezone: Europe/Berlin\n", BERLIN)
                .replace(
                    "      Label: {code: label, abstractType: string}\n",
                    "      Label: {code: label, abstractType: string}\n" + STARTS_COPY,
                )
                .replace("columnsTo: [Starts]", "columnsTo: [Starts Copy]")
            )

        single = _run_on(files, model(False), query)

        assert _run_on(files, model(True), query) == single
        assert len(single) == 2

    @pytest.mark.parametrize("query", KEY_QUERIES, ids=["aggregated", "raw"])
    def test_a_key_without_a_zone_meets_one_with_a_zone(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, query: dict[str, Any]
    ) -> None:
        # Sessions in Berlin, read in UTC: one database reads the order's
        # stored time in its session's zone to compare it with a shift's.
        _sources_in(monkeypatch, "Europe/Berlin")
        files = _files(tmp_path, BERLIN_ORDERS, ZONED_SHIFTS)

        def model(federated: bool) -> str:
            return (
                shifts_yaml(federated)
                .replace("  queryTimezone: Europe/Berlin\n", "  defaultTimezone: UTC\n")
                .replace(
                    "      Placed: {code: placed, abstractType: timestamp_tz}",
                    "      Placed: {code: placed, abstractType: timestamp}",
                )
            )

        berlin = {"source_zone": "Europe/Berlin", "combine_zone": "Europe/Berlin"}
        single = _run_on(files, model(False), query, **berlin)

        assert _run_on(files, model(True), query, **berlin) == single
        assert len(single) == 2


def labelled_yaml(federated: bool, required: bool = False) -> str:
    """The test model, with customer columns computed from the region."""
    yaml = model_yaml(federated=federated).replace(
        "      Tenant: {code: tenant, abstractType: string}\n",
        "      Tenant: {code: tenant, abstractType: string}\n"
        "      Region Label:\n"
        "        {code: region_label, abstractType: string,"
        " expression: \"COALESCE({[Customers].[Region]}, 'unknown')\"}\n"
        "      Region Code:\n"
        "        {code: region_code, abstractType: string,"
        ' expression: "UPPER({[Customers].[Region]})"}\n',
    )
    yaml = yaml.replace(
        "dimensions:\n",
        "dimensions:\n"
        "  Region Label: {dataObject: Customers, column: Region Label, resultType: string}\n"
        "  Region Code: {dataObject: Customers, column: Region Code, resultType: string}\n",
        1,
    )
    if required:
        yaml = yaml.replace(
            "        joinTo: Customers\n",
            "        joinTo: Customers\n        required: true\n",
            1,
        )
    return yaml


def _label(value: str) -> dict[str, Any]:
    return {"field": "Region Label", "op": "=", "value": value}


class TestComputedOverAMissingRow:
    """Order 6's customer 99 is not in the CRM. One database computes the
    customer's columns over NULLs, and COALESCE makes 'unknown' of them; the
    customers' leg has no row to compute it from."""

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Region Label"], "measures": ["Revenue"]}},
            {"select": {"measures": ["Revenue"]}, "where": [_label("unknown")]},
            {"select": {"fields": ["Orders.Order ID", "Customers.Region Label"]}},
            {"select": {"fields": ["Orders.Order ID"]}, "where": [_label("unknown")]},
            {
                "select": {"fields": ["Orders.Order ID"]},
                "where": [{"field": "Customers.Region Label", "op": "=", "value": "EU"}],
            },
        ],
        ids=["selected", "filtered", "raw_field", "raw_filter", "raw_column_filter"],
    )
    def test_a_value_over_nulls_is_refused(self, query: dict[str, Any]) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(labelled_yaml(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert "Region Label" in error.message
        assert "would read as NULL" in error.message

    def test_a_filter_a_context_includes_is_refused(self) -> None:
        """Order 6 is 'unknown' on one database: the unknown revenue is 95.10,
        not the 55.10 of order 4 alone."""
        unknown = (
            "  Unknown Revenue:\n"
            "    columns: [{dataObject: Orders, column: Amount}]\n"
            "    aggregation: sum\n"
            "    filterContext:\n"
            "      mode: FIXED\n"
            "      include: [{field: Region Label, op: '=', value: unknown}]\n"
        )
        yaml = labelled_yaml(True).replace("metrics:\n", unknown + "metrics:\n", 1)
        query = {"select": {"dimensions": ["Region"], "measures": ["Revenue", "Unknown Revenue"]}}

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert "Region Label" in error.message

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Region Code"], "measures": ["Revenue"]}},
            {"select": {"fields": ["Orders.Order ID", "Customers.Region Code"]}},
        ],
        ids=["aggregated", "raw"],
    )
    def test_null_over_nulls_is_computed(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        """UPPER of no region is NULL, as the combine step reads it."""
        single = _run_on(files, labelled_yaml(False), query)

        assert _run_on(files, labelled_yaml(True), query) == single
        assert any(None in row for row in single)

    @pytest.mark.parametrize(
        "query",
        [
            {"select": {"dimensions": ["Region Label"], "measures": ["Revenue"]}},
            {"select": {"fields": ["Orders.Order ID", "Customers.Region Label"]}},
        ],
        ids=["aggregated", "raw"],
    )
    def test_through_a_required_join_it_is_never_read_missing(
        self, files: dict[str, str], query: dict[str, Any]
    ) -> None:
        single = _run_on(files, labelled_yaml(False, required=True), query)

        assert _run_on(files, labelled_yaml(True, required=True), query) == single
        assert single


def keyed_chain_yaml(sources: tuple[str, str, str], expression: str, required: bool) -> str:
    """The chain, the customers joined to their regions by a key computed by *expression*."""
    yaml = chain_yaml(*sources).replace(
        "      Customer Region: {code: region_id, abstractType: int}\n",
        "      Customer Region: {code: region_id, abstractType: int}\n"
        f"      Region Key: {{code: region_key, abstractType: int, expression: '{expression}'}}\n",
    )
    return yaml.replace(
        "        joinTo: Regions\n        columnsFrom: [Customer Region]",
        "        joinTo: Regions\n"
        + ("        required: true\n" if required else "")
        + "        columnsFrom: [Region Key]",
    )


class TestAComputedJoinKeyOverAMissingRow:
    """``COALESCE(region, -1)`` joins an order whose customer is missing to
    the unknown region (-1) in one database; the customers' leg has no row
    to compute the key from, and an inner join to the regions within it
    does not drop that order there."""

    COALESCED = "COALESCE({[Customers].[Customer Region]}, -1)"
    QUERIES = [
        {"select": {"dimensions": ["Region Name"], "measures": ["Revenue"]}},
        {"select": {"fields": ["Orders.Amount", "Regions.Region Name"]}},
    ]

    @pytest.mark.parametrize("query", QUERIES, ids=["aggregated", "raw"])
    @pytest.mark.parametrize("required", [False, True], ids=["left", "required"])
    @pytest.mark.parametrize(
        "sources",
        [("shop", "crm", "geo"), ("shop", "crm", "crm")],
        ids=["across_sources", "within_a_source"],
    )
    def test_is_refused(
        self, sources: tuple[str, str, str], required: bool, query: dict[str, Any]
    ) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(keyed_chain_yaml(sources, self.COALESCED, required), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_JOIN"
        assert "The join key 'Customers.Region Key'" in error.message

    @pytest.mark.parametrize("query", QUERIES, ids=["aggregated", "raw"])
    @pytest.mark.parametrize("required", [False, True], ids=["left", "required"])
    def test_one_null_over_nulls_is_computed(
        self, chain_files: dict[str, str], required: bool, query: dict[str, Any]
    ) -> None:
        expression = "{[Customers].[Customer Region]} * 1"
        single = _run_on(
            chain_files, keyed_chain_yaml(("all", "all", "all"), expression, required), query
        )

        federated = _run_on(
            chain_files, keyed_chain_yaml(("shop", "crm", "crm"), expression, required), query
        )

        assert federated == single
        assert single


# 00:30 UTC on New Year's Day is New Year's Eve in Los Angeles.
ZONED_ORDERS = (
    "CREATE TABLE orders AS SELECT * FROM (VALUES"
    " (1, TIMESTAMPTZ '2024-01-01 00:30:00+00', 10),"
    " (2, TIMESTAMPTZ '2024-01-15 12:00:00+00', 11),"
    " (3, TIMESTAMPTZ '2023-12-10 12:00:00+00', 99)) t(order_id, placed, customer_id)"
)
ZONED_CUSTOMERS = (
    "CREATE TABLE customers AS SELECT * FROM (VALUES (10, 'EU'), (11, 'US')) t(customer_id, region)"
)


def zoned_yaml(federated: bool, query_zone: bool) -> str:
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    zone = "  queryTimezone: America/Los_Angeles\n" if query_zone else ""
    return f"""\
version: 1.0
settings:
  defaultDialect: duckdb
{zone}dataObjects:
  Orders:
    code: orders
{shop}    columns:
      Order ID: {{code: order_id, abstractType: int}}
      Placed: {{code: placed, abstractType: timestamp_tz}}
      Order Customer: {{code: customer_id, abstractType: int}}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer]
        columnsTo: [Customer ID]
  Customers:
    code: customers
{crm}    columns:
      Customer ID: {{code: customer_id, abstractType: int}}
      Region: {{code: region, abstractType: string}}
dimensions:
  Region: {{dataObject: Customers, column: Region, resultType: string}}
  Order Month: {{dataObject: Orders, column: Placed, resultType: date, timeGrain: month}}
"""


@pytest.fixture(scope="module")
def zoned_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("zoned")
    paths = {name: str(root / f"{name}.duckdb") for name in ("all", "shop", "crm")}
    for name, statements in (
        ("all", [ZONED_ORDERS, ZONED_CUSTOMERS]),
        ("shop", [ZONED_ORDERS]),
        ("crm", [ZONED_CUSTOMERS]),
    ):
        con = duckdb.connect(paths[name])
        for statement in statements:
            con.execute(statement)
        con.close()
    return paths


class TestZonedDatesAfterTheJoin:
    """A raw-mode filter after the join on December, of orders placed in Los
    Angeles: the combine step, in UTC, would put order 1 in January."""

    QUERY = {
        "select": {"fields": ["Orders.Order ID", "Customers.Region"]},
        "where": [_either({"field": "Order Month", "op": "=", "value": "2023-12-01"}, US)],
    }

    def test_without_a_query_time_zone_it_is_refused(self) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(zoned_yaml(True, query_zone=False), self.QUERY)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_FILTER"
        assert "Order Month" in error.message

    def test_in_the_query_time_zone_the_leg_returns_its_wall_clock(
        self, zoned_files: dict[str, str]
    ) -> None:
        zones = {"source_zone": "America/Los_Angeles", "combine_zone": "UTC"}
        yaml = zoned_yaml(False, query_zone=True)
        single = _run_on(zoned_files, yaml, self.QUERY, **zones)

        federated = _run_on(zoned_files, zoned_yaml(True, query_zone=True), self.QUERY, **zones)

        assert federated == single == [(1, "EU"), (2, "US"), (3, None)]
