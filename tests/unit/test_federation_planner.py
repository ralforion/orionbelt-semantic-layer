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

import gc
import weakref
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.service.compilation_cache import CompilationCache  # noqa: E402
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
      Tenant: {{code: tenant, abstractType: string}}
dimensions:
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


def run_federated(files: dict[str, str], query: dict[str, Any], **model: Any) -> list[Any]:
    compiled = _compile(model_yaml(federated=True, **model), query)
    plan = compiled.federation
    assert plan is not None
    combine = duckdb.connect()
    for leg in plan.legs:
        con = duckdb.connect(files[leg.data_source], read_only=True)
        try:
            combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
        finally:
            con.close()
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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(zoned[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()
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
        assert sql.endswith("LIMIT 1000001")

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

REFUSALS = {
    "measures_on_two_sources": (
        ON_CUSTOMERS,
        {"select": {"dimensions": ["Region"], "measures": ["Revenue", "Customer Rows"]}},
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
    def test_measures_on_the_one_side_grouped_by_the_many_side_are_refused(self) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(
                model_yaml(federated=True, extra=ON_CUSTOMERS),
                {"select": {"dimensions": ["Order Date"], "measures": ["Customer Rows"]}},
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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(paths[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()
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
    combine = duckdb.connect()
    for leg in compiled.federation.legs:
        con = duckdb.connect(files[leg.data_source], read_only=True)
        try:
            combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
        finally:
            con.close()
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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(three_files[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()

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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(three_files[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()

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
            ("clickhouse", "Mode F", "F"),
        ],
    )
    def test_from_rows_values_their_engine_compares_otherwise_are_refused(
        self, facts_on: Any, dialect: str, measure: str, column: str
    ) -> None:
        """The combine step would compare them: 'a' and 'A' as two values,
        where MySQL sees one; 0.0 and -0.0 as one, where ClickHouse counts two."""
        with pytest.raises(ResolutionError) as caught:
            facts_on(
                dialect, {"select": {"dimensions": ["Label"], "measures": ["Median F", measure]}}
            )

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_AGGREGATION"
        assert f"compares the values of 'Facts.{column}' otherwise than {dialect}" in (
            error.message
        )

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

    def test_a_combine_filter_reads_the_value_not_its_class(self, facts_on: Any) -> None:
        """A filter run in the combine step compares the value the leg had,
        as it did; the dimension shown and grouped is its class."""
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
        (kept,) = [c for c in dict(facts.columns) if c.startswith("__eqv")]
        assert f"`__leg`.`Name` AS `{kept}`" in facts.sql
        assert dict(facts.columns)[kept] == "string"
        assert f'"leg_0"."{kept}" = \'a\'' in plan.combine_sql
        assert '"leg_0"."Name" = \'a\'' not in plan.combine_sql
        # Label is read from a source comparing as DuckDB: no copy needed.
        assert all(not c.startswith("__eqv") for c in dict(plan.legs[1].columns))

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

        assert "__leg" not in plan.legs[0].sql

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
        string a combine filter reads: the string's kept value took that name,
        and the integer column showed strings."""
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
                            {"field": "Name", "op": "=", "value": "a"},
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
        assert f'"leg_0"."{kept}" = \'a\'' in plan.combine_sql
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

    def test_a_measure_reading_the_many_side_is_refused(self) -> None:
        """Ages filtered on the sale's product: one customer has several."""
        yaml = REPEATED.format(
            facts="    dataSource: facts\n", labels="    dataSource: labels\n", aggregation="sum"
        ).replace(
            "      - column: {dataObject: Customers, column: Age}\n        operator: gte\n",
            "      - column: {dataObject: Sales, column: Product}\n        operator: gte\n",
        )

        with pytest.raises(ResolutionError) as caught:
            _compile(yaml, {"select": {"dimensions": ["Category"], "measures": ["Adult Ages"]}})

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_AGGREGATION"
        assert "reads 'Sales.Product', which one row of 'Customers' can have several" in (
            error.message
        )

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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(priced[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()
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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(paths[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()
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

    def test_beside_a_condition_on_another_source_it_is_refused(self) -> None:
        after = {"field": "Order Date", "op": ">=", "value": "2024-03-01"}
        query = {
            "select": BY_REGION,
            "where": [{"logic": "or", "filters": [_tickets("exists"), after]}],
        }

        with pytest.raises(ResolutionError) as caught:
            _compile(subquery_model(True), query)

        (error,) = caught.value.errors
        assert error.code == "FEDERATION_UNSUPPORTED_FILTER"
        assert "An exists filter that runs after the join" in error.message

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
        combine = duckdb.connect()
        for leg in plan.legs:
            con = duckdb.connect(nested_files[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()
        assert sorted(_rows(combine, plan.combine_sql)) == expected


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


def roles_model(federated: bool, static: str = "") -> str:
    """Orders with a sales and a support employee, customers with an account manager."""
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
{crm}    columns:
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
        ("shop", ROLE_TABLES["shop"]),
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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(paths[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()
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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(chain_files[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()

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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(chain_files[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()

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
        combine = duckdb.connect()
        for leg in compiled.federation.legs:
            con = duckdb.connect(shared_files[leg.data_source], read_only=True)
            try:
                combine.register(leg.name, con.sql(leg.sql).to_arrow_table())
            finally:
                con.close()

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
    keys = sorted(v for v in tables[injection.leg].column(injection.column).to_pylist() if v)
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
