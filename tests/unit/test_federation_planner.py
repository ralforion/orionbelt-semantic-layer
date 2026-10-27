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


def model_yaml(
    *,
    federated: bool,
    tenant_filter: bool = False,
    extra: str = "",
    static: str = "",
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

    def test_a_deferred_filter_on_a_dimension_selected_at_a_grain_is_refused(self) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(
                model_yaml(federated=True),
                {
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
                },
            )

        assert [e.code for e in caught.value.errors] == ["FEDERATION_UNSUPPORTED_FILTER"]


def chain_yaml(shop: str, crm: str, regions: str) -> str:
    """Orders -> Customers -> Regions, each on the data source given."""
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
dimensions:
  Region Name: {{dataObject: Regions, column: Region Name, resultType: string}}
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

    def test_a_join_passing_through_another_source_is_federated(self) -> None:
        with pytest.raises(ResolutionError) as caught:
            _compile(chain_yaml("shop", "crm", "shop"), self.QUERY)

        assert [e.code for e in caught.value.errors] == ["FEDERATION_UNSUPPORTED_JOIN"]


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
