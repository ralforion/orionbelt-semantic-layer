"""A data source's schemas and tables, read from its catalog.

DuckDB answers for real: a schema with a composite primary key, a composite
foreign key, comments and a view. The other engines are read through a
stand-in runner, which checks what each catalog makes of its own rows: the
spellings of yes and no, Databricks' 0-based ordinals, Snowflake's ``SHOW``
columns, MySQL's ``VIEW`` comment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("ob_flight")
duckdb = pytest.importorskip("duckdb")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from ob_flight.datasources import DataSource, configured_datasources  # noqa: E402

from orionbelt.api.app import create_app  # noqa: E402
from orionbelt.api.deps import init_session_manager, reset_session_manager  # noqa: E402
from orionbelt.service.db_executor import ExecutionError  # noqa: E402
from orionbelt.service.schema_inspection import (  # noqa: E402
    SUPPORTED_DIALECTS,
    InspectionNotSupportedError,
    catalog_for,
    inspect_schema,
    list_schemas,
)
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402

SHOP_DDL = """
CREATE SCHEMA sales;
CREATE TABLE sales.region (id INTEGER, code VARCHAR, PRIMARY KEY (id, code));
COMMENT ON TABLE sales.region IS 'Sales regions';
CREATE TABLE sales.customer (
    id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL,
    region_id INTEGER,
    region_code VARCHAR,
    FOREIGN KEY (region_id, region_code) REFERENCES sales.region (id, code)
);
COMMENT ON COLUMN sales.customer.name IS 'Full name';
CREATE VIEW sales.customer_names AS SELECT name FROM sales.customer;
"""


@pytest.fixture
def shop_path(tmp_path: Any) -> str:
    path = str(tmp_path / "shop.duckdb")
    con = duckdb.connect(path)
    con.execute(SHOP_DDL)
    con.close()
    return path


@pytest.fixture
def shop(shop_path: str) -> DataSource:
    return configured_datasources(
        {"OBSL_DATABASES": "shop", "DB_SHOP_TYPE": "duckdb", "DB_SHOP_DUCKDB_DATABASE": shop_path}
    )[0]


class TestDuckDB:
    def test_schemas_leave_out_the_engines_own(self, shop: DataSource) -> None:
        assert list_schemas(shop) == ["main", "sales"]

    def test_tables_columns_and_keys(self, shop: DataSource) -> None:
        info = inspect_schema(shop, "sales")

        assert info is not None
        assert [(t.name, t.kind) for t in info.tables] == [
            ("customer", "table"),
            ("customer_names", "view"),
            ("region", "table"),
        ]
        customer, _, region = info.tables
        assert [(c.name, c.data_type, c.ordinal, c.nullable) for c in customer.columns] == [
            ("id", "INTEGER", 1, False),
            ("name", "VARCHAR", 2, False),
            ("region_id", "INTEGER", 3, True),
            ("region_code", "VARCHAR", 4, True),
        ]
        assert customer.columns[1].comment == "Full name"
        assert region.comment == "Sales regions"
        assert region.primary_key == ["id", "code"]
        assert [c.primary_key for c in region.columns] == [True, True]
        (fk,) = customer.foreign_keys
        assert fk.columns == ("region_id", "region_code")
        assert (fk.referred_schema, fk.referred_table) == ("sales", "region")
        assert fk.referred_columns == ("id", "code")
        assert info.warnings == []

    def test_view_definitions_only_when_asked(self, shop: DataSource) -> None:
        plain = inspect_schema(shop, "sales", tables=["customer_names"])
        full = inspect_schema(shop, "sales", tables=["customer_names"], view_definitions=True)

        assert plain is not None and full is not None
        assert plain.tables[0].view_definition is None
        assert "SELECT" in (full.tables[0].view_definition or "")

    def test_views_can_be_left_out(self, shop: DataSource) -> None:
        info = inspect_schema(shop, "sales", include_views=False)

        assert info is not None
        assert [t.name for t in info.tables] == ["customer", "region"]

    def test_a_table_filter(self, shop: DataSource) -> None:
        info = inspect_schema(shop, "sales", tables=["region"])

        assert info is not None
        assert [t.name for t in info.tables] == ["region"]

    def test_an_unknown_schema_is_none_and_an_empty_one_is_empty(self, shop: DataSource) -> None:
        assert inspect_schema(shop, "nowhere") is None
        main = inspect_schema(shop, "main")
        assert main is not None and main.tables == []

    def test_a_filter_matching_nothing_still_tells_the_two_apart(self, shop: DataSource) -> None:
        assert inspect_schema(shop, "nowhere", tables=["missing"]) is None
        sales = inspect_schema(shop, "sales", tables=["missing"])
        assert sales is not None and sales.tables == []

    def test_a_schema_name_cannot_break_out_of_its_literal(self, shop: DataSource) -> None:
        assert inspect_schema(shop, "sales' OR '1'='1") is None


class _Catalog:
    """A stand-in runner: answers each statement by the first marker it contains."""

    def __init__(self, answers: dict[str, list[dict[str, Any]] | Exception]) -> None:
        self.answers = answers
        self.statements: list[str] = []

    def __call__(self, sql: str) -> list[dict[str, Any]]:
        self.statements.append(sql)
        for marker, answer in self.answers.items():
            if marker in sql:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return []


def _source(dialect: str) -> DataSource:
    return DataSource(name=dialect, dialect=dialect, environ={})


class TestCatalogs:
    def test_every_dialect_has_a_catalog(self) -> None:
        assert set(SUPPORTED_DIALECTS) == {
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
        }

    def test_an_unknown_dialect_is_refused(self) -> None:
        with pytest.raises(InspectionNotSupportedError, match="supported: bigquery"):
            catalog_for("oracle")

    def test_snowflake_reads_keys_from_show(self) -> None:
        run = _Catalog(
            {
                "information_schema.tables": [
                    {"table_name": "ORDERS", "table_type": "BASE TABLE", "comment": ""}
                ],
                "information_schema.columns": [
                    {
                        "table_name": "ORDERS",
                        "column_name": "ID",
                        "data_type": "NUMBER",
                        "is_nullable": "NO",
                        "ordinal": 1,
                        "comment": None,
                    },
                    {
                        "table_name": "ORDERS",
                        "column_name": "CUSTOMER_ID",
                        "data_type": "NUMBER",
                        "is_nullable": "YES",
                        "ordinal": 2,
                        "comment": "Buyer",
                    },
                ],
                "SHOW PRIMARY KEYS": [
                    {"table_name": "ORDERS", "column_name": "ID", "key_sequence": 1}
                ],
                "SHOW IMPORTED KEYS": [
                    {
                        "fk_name": "FK_CUST",
                        "fk_table_name": "ORDERS",
                        "fk_column_name": "CUSTOMER_ID",
                        "key_sequence": 1,
                        "pk_schema_name": "CRM",
                        "pk_table_name": "CUSTOMERS",
                        "pk_column_name": "ID",
                    }
                ],
            }
        )

        info = inspect_schema(_source("snowflake"), "SALES", run=run)

        assert info is not None
        (orders,) = info.tables
        assert orders.comment is None
        assert [(c.name, c.nullable, c.primary_key) for c in orders.columns] == [
            ("ID", False, True),
            ("CUSTOMER_ID", True, False),
        ]
        (fk,) = orders.foreign_keys
        assert (fk.name, fk.columns, fk.referred_schema, fk.referred_table) == (
            "FK_CUST",
            ("CUSTOMER_ID",),
            "CRM",
            "CUSTOMERS",
        )
        assert 'SHOW PRIMARY KEYS IN SCHEMA "SALES"' in run.statements

    def test_databricks_ordinals_count_from_one(self) -> None:
        run = _Catalog(
            {
                "information_schema.tables": [
                    {"table_name": "t", "table_type": "MANAGED", "comment": None}
                ],
                "information_schema.columns": [
                    {"table_name": "t", "column_name": "b", "data_type": "INT", "ordinal": 1},
                    {"table_name": "t", "column_name": "a", "data_type": "INT", "ordinal": 0},
                ],
            }
        )

        info = inspect_schema(_source("databricks"), "s", run=run)

        assert info is not None
        assert [(c.name, c.ordinal) for c in info.tables[0].columns] == [("a", 1), ("b", 2)]
        assert "full_data_type AS data_type" in run.statements[1]

    def test_mysql_views_lose_their_placeholder_comment(self) -> None:
        run = _Catalog(
            {
                "information_schema.tables": [
                    {"table_name": "v", "table_type": "VIEW", "comment": "VIEW"},
                    {"table_name": "t", "table_type": "BASE TABLE", "comment": "Orders"},
                ],
            }
        )

        info = inspect_schema(_source("mysql"), "shop", run=run)

        assert info is not None
        assert [(t.name, t.kind, t.comment) for t in info.tables] == [
            ("t", "table", "Orders"),
            ("v", "view", None),
        ]

    def test_clickhouse_engines_and_nullable_flags(self) -> None:
        run = _Catalog(
            {
                # First: the key statement reads system.tables too.
                "is_in_primary_key": [{"table_name": "events", "column_name": "ts", "ordinal": 1}],
                "system.tables": [
                    {"table_name": "events", "table_type": "MergeTree", "comment": ""},
                    {"table_name": "daily", "table_type": "MaterializedView", "comment": ""},
                ],
                "system.columns": [
                    {
                        "table_name": "events",
                        "column_name": "ts",
                        "data_type": "DateTime",
                        "is_nullable": 0,
                        "ordinal": 1,
                    },
                    {
                        "table_name": "events",
                        "column_name": "note",
                        "data_type": "Nullable(String)",
                        "is_nullable": 1,
                        "ordinal": 2,
                    },
                ],
            }
        )

        info = inspect_schema(_source("clickhouse"), "analytics", run=run)

        assert info is not None
        assert [(t.name, t.kind) for t in info.tables] == [
            ("daily", "materialized_view"),
            ("events", "table"),
        ]
        events = info.tables[1]
        assert [(c.name, c.nullable) for c in events.columns] == [("ts", False), ("note", True)]
        assert events.primary_key == ["ts"]

    def test_dremio_asks_for_no_keys(self) -> None:
        run = _Catalog(
            {
                '"TABLES"': [{"table_name": "trips", "table_type": "TABLE"}],
                "INFORMATION_SCHEMA.COLUMNS": [
                    {"table_name": "trips", "column_name": "id", "data_type": "BIGINT"}
                ],
            }
        )

        info = inspect_schema(_source("dremio"), "lake.nyc", run=run)

        assert info is not None
        assert info.tables[0].columns[0].nullable is None
        assert len(run.statements) == 2

    def test_unreadable_keys_are_a_warning_not_a_failure(self) -> None:
        run = _Catalog(
            {
                "referential_constraints": ExecutionError("permission denied"),
                "table_constraints": ExecutionError("permission denied"),
                "information_schema.tables": [
                    {"table_name": "t", "table_type": "BASE TABLE", "comment": None}
                ],
                "information_schema.columns": [
                    {"table_name": "t", "column_name": "id", "data_type": "int", "ordinal": 1}
                ],
            }
        )

        info = inspect_schema(_source("databricks"), "s", run=run)

        assert info is not None
        assert [c.name for c in info.tables[0].columns] == ["id"]
        assert [w.split(":")[0] for w in info.warnings] == [
            "Could not read primary keys",
            "Could not read foreign keys",
        ]

    def test_a_failed_column_read_fails_the_request(self) -> None:
        run = _Catalog(
            {
                "information_schema.tables": [
                    {"table_name": "t", "table_type": "BASE TABLE", "comment": None}
                ],
                "information_schema.columns": ExecutionError("boom"),
            }
        )

        with pytest.raises(ExecutionError):
            inspect_schema(_source("mysql"), "s", run=run)


@pytest.fixture
async def client(shop_path: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    for variable in ("DUCKDB_DATABASE", "DUCKDB_DATABASE_PATH", "MOTHERDUCK_ACCESS_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OBSL_DATABASES", "shop")
    monkeypatch.setenv("DB_SHOP_TYPE", "duckdb")
    monkeypatch.setenv("DB_SHOP_DUCKDB_DATABASE", shop_path)
    app = create_app(settings=Settings(session_ttl_seconds=3600, session_cleanup_interval=9999))
    mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    init_session_manager(mgr, query_execute_enabled=True, db_vendor="duckdb")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        reset_session_manager()
        mgr.stop()


class TestRest:
    async def test_schemas(self, client: AsyncClient) -> None:
        response = await client.get("/v1/datasources/shop/schemas")

        assert response.status_code == 200, response.text
        assert response.json() == {
            "datasource": "shop",
            "dialect": "duckdb",
            "schemas": ["main", "sales"],
        }

    async def test_tables_in_camel_case(self, client: AsyncClient) -> None:
        response = await client.get(
            "/v1/datasources/shop/schemas/sales/tables",
            params={"table": ["customer", "customer_names"], "view_definitions": "true"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["schema"] == "sales"
        customer, view = body["tables"]
        assert customer["columns"][0] == {
            "name": "id",
            "dataType": "INTEGER",
            "ordinal": 1,
            "nullable": False,
            "comment": None,
            "primaryKey": True,
        }
        assert customer["primaryKey"] == ["id"]
        assert customer["foreignKeys"][0]["referredColumns"] == ["id", "code"]
        assert view["kind"] == "view" and "SELECT" in view["viewDefinition"]

    async def test_an_unknown_source_is_a_404(self, client: AsyncClient) -> None:
        response = await client.get("/v1/datasources/nowhere/schemas")

        assert response.status_code == 404
        assert "Configured data sources: shop" in response.json()["detail"]

    async def test_an_unknown_schema_is_a_404(self, client: AsyncClient) -> None:
        response = await client.get("/v1/datasources/shop/schemas/nowhere/tables")

        assert response.status_code == 404

    async def test_an_unknown_schema_is_a_404_with_a_table_filter_too(
        self, client: AsyncClient
    ) -> None:
        unknown = await client.get("/v1/datasources/shop/schemas/nowhere/tables?table=missing")
        known = await client.get("/v1/datasources/shop/schemas/sales/tables?table=missing")

        assert unknown.status_code == 404
        assert known.status_code == 200 and known.json()["tables"] == []

    async def test_without_query_execute_it_is_a_503(self, client: AsyncClient) -> None:
        mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
        init_session_manager(mgr, query_execute_enabled=False, db_vendor="duckdb")
        try:
            response = await client.get("/v1/datasources/shop/schemas")
        finally:
            mgr.stop()

        assert response.status_code == 503
        assert "QUERY_EXECUTE=true" in response.json()["detail"]
