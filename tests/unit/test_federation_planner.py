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

from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
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


def model_yaml(*, federated: bool, tenant_filter: bool = False, extra: str = "") -> str:
    shop = "    dataSource: shop\n" if federated else ""
    crm = "    dataSource: crm\n" if federated else ""
    filters = (
        "filters:\n  - {dataObject: Customers, column: Tenant, operator: equals, value: acme}\n"
        if tenant_filter
        else ""
    )
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
measures:
  Revenue: {{columns: [{AMOUNT}], aggregation: sum, resultType: decimal}}
  Top Order: {{columns: [{AMOUNT}], aggregation: max, resultType: decimal}}
  Smallest Order: {{columns: [{AMOUNT}], aggregation: min, resultType: decimal}}
{extra}metrics:
  Revenue per Order: {{expression: "{{[Revenue]}} / {{[Orders Count]}}"}}
{filters}"""


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

    def test_the_orphan_order_keeps_its_value_under_no_region(self, files: dict[str, str]) -> None:
        rows = dict(
            run_federated(files, {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}})
        )

        # Customer 12 has no region and order 6 no customer: both are NULL.
        assert float(rows[None]) == pytest.approx(55.10 + 40.00)


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

    def test_a_filter_on_the_dimension_side_runs_only_in_the_combine_step(self) -> None:
        plan = _compile(
            model_yaml(federated=True),
            {
                "select": {"dimensions": ["Order Date:month"], "measures": ["Revenue"]},
                "where": [{"field": "Region", "op": "is_null"}],
            },
        ).federation

        assert "IS NULL" not in plan.legs[1].sql
        assert "IS NULL" in plan.combine_sql


AVG = "  Average Order: {columns: [{dataObject: Orders, column: Amount}], aggregation: avg}\n"
DISTINCT = (
    "  Customers Ordering: {columns: [{dataObject: Orders, column: Order Customer}], "
    "aggregation: count_distinct, resultType: int}\n"
)
ON_CUSTOMERS = (
    "  Customer Rows: {columns: [{dataObject: Customers, column: Customer ID}], "
    "aggregation: count, resultType: int}\n"
)

REFUSALS = {
    "avg": (
        AVG,
        {"select": {"dimensions": ["Region"], "measures": ["Average Order"]}},
        "FEDERATION_UNSUPPORTED_AGGREGATION",
    ),
    "count_distinct": (
        DISTINCT,
        {"select": {"dimensions": ["Region"], "measures": ["Customers Ordering"]}},
        "FEDERATION_UNSUPPORTED_AGGREGATION",
    ),
    "measures_on_two_sources": (
        ON_CUSTOMERS,
        {"select": {"dimensions": ["Region"], "measures": ["Revenue", "Customer Rows"]}},
        "FEDERATION_MULTI_FACT",
    ),
    "no_measures": (
        "",
        {"select": {"dimensions": ["Region", "Order Date"]}},
        "FEDERATION_NO_MEASURES",
    ),
    "relative_filter_on_dimension_side": (
        "",
        {
            "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
            "where": [
                {
                    "logic": "or",
                    "filters": [
                        {"field": "Region", "op": "=", "value": "EU"},
                        {
                            "field": "Order Date",
                            "op": "relative",
                            "value": {"unit": "day", "count": 30, "direction": "past"},
                        },
                    ],
                }
            ],
        },
        "FEDERATION_UNSUPPORTED_FILTER",
    ),
}


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
