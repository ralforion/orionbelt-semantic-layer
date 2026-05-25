"""Tests for the ``exists`` / ``nonexists`` filter operators (v2.7.0).

See ``design/PLAN_exists_operator.md`` for the surface and validation
rules being verified here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryObject,
    QuerySelect,
    Subquery,
)
from orionbelt.models.semantic import FilterLogic
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

PIPELINE = CompilationPipeline()
LOADER = TrackedLoader()
RESOLVER = ReferenceResolver()

ALL_DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]


def _load_model(yaml_str: str):
    raw, sm = LOADER.load_string(yaml_str)
    model, result = RESOLVER.resolve(raw, sm)
    assert result.valid, f"Model has errors: {result.errors}"
    return model


# A small fact / dim / child model with one primary join (Orders → Customers,
# OrderItems → Orders) plus a Returns table joined to Orders both as a
# primary "returns" path and a secondary "returned_via_warehouse" path so
# pathName behaviour can be exercised.
BASE_MODEL = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Order Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Order Customer ID
        columnsTo:
          - Customer ID

  OrderItems:
    code: ORDER_ITEMS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Item ID:
        code: ITEM_ID
        abstractType: string
      Item Order ID:
        code: ORDER_ID
        abstractType: string
      SKU:
        code: SKU
        abstractType: string
      Is Returned:
        code: IS_RETURNED
        abstractType: boolean
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom:
          - Item Order ID
        columnsTo:
          - Order ID

  Returns:
    code: RETURNS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Return ID:
        code: RETURN_ID
        abstractType: string
      Return Order ID:
        code: ORDER_ID
        abstractType: string
      Return Warehouse ID:
        code: WAREHOUSE_ID
        abstractType: string
      Reason:
        code: REASON
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom:
          - Return Order ID
        columnsTo:
          - Order ID
      - joinType: many-to-one
        joinTo: Orders
        secondary: true
        pathName: viaWarehouse
        columnsFrom:
          - Return Warehouse ID
        columnsTo:
          - Order ID

  Payments:
    code: PAYMENTS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Payment ID:
        code: PAYMENT_ID
        abstractType: string
      Payment Order ID:
        code: ORDER_ID
        abstractType: string
      Paid Amount:
        code: PAID_AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom:
          - Payment Order ID
        columnsTo:
          - Order ID

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string
  Order ID:
    dataObject: Orders
    column: Order ID
    resultType: string

measures:
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count
"""


# ---------------------------------------------------------------------------
# Pydantic-level model validation
# ---------------------------------------------------------------------------


class TestQueryFilterValidation:
    """``exists`` / ``nonexists`` require ``subquery`` and reject ``value``."""

    def test_exists_requires_subquery(self) -> None:
        with pytest.raises(ValidationError, match="subquery"):
            QueryFilter(field="Order ID", op=FilterOperator.EXISTS)

    def test_nonexists_requires_subquery(self) -> None:
        with pytest.raises(ValidationError, match="subquery"):
            QueryFilter(field="Order ID", op=FilterOperator.NONEXISTS)

    def test_exists_rejects_value(self) -> None:
        with pytest.raises(ValidationError, match="value"):
            QueryFilter(
                field="Order ID",
                op=FilterOperator.EXISTS,
                value="x",
                subquery=Subquery(data_object="OrderItems"),
            )

    def test_equals_rejects_subquery(self) -> None:
        with pytest.raises(ValidationError, match="subquery"):
            QueryFilter(
                field="Order ID",
                op=FilterOperator.EQUALS,
                value="x",
                subquery=Subquery(data_object="OrderItems"),
            )

    def test_subquery_alias_round_trip(self) -> None:
        """Camel-case JSON aliases populate the snake-case Python fields."""
        sub = Subquery.model_validate(
            {"dataObject": "OrderItems", "pathName": "viaWarehouse", "filter": []}
        )
        assert sub.data_object == "OrderItems"
        assert sub.path_name == "viaWarehouse"


# ---------------------------------------------------------------------------
# SQL compilation
# ---------------------------------------------------------------------------


class TestExistsCompilation:
    """End-to-end: QueryObject → SQL contains a correlated ``EXISTS``."""

    def test_exists_emits_correlated_subquery(self) -> None:
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(data_object="OrderItems"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        sql = result.sql
        assert "EXISTS (" in sql
        # Correlation predicate: Orders.ORDER_ID = OrderItems.ORDER_ID
        # — note that the dialect quotes identifiers per its style.
        assert '"OrderItems"."ORDER_ID"' in sql
        assert '"Orders"."ORDER_ID"' in sql
        # The subquery's projection is SELECT 1.
        assert "SELECT 1" in sql

    def test_nonexists_emits_not_exists(self) -> None:
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.NONEXISTS,
                    subquery=Subquery(data_object="OrderItems"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "NOT EXISTS (" in result.sql

    def test_subquery_filter_landed_inside_exists(self) -> None:
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(
                        data_object="OrderItems",
                        filter=[
                            QueryFilter(
                                field="Is Returned",
                                op=FilterOperator.EQUALS,
                                value=True,
                            )
                        ],
                    ),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        # Both the correlation and the extra predicate should be inside
        # the EXISTS subquery, joined by AND.
        assert "EXISTS (" in result.sql
        assert '"IS_RETURNED"' in result.sql
        assert " AND " in result.sql

    def test_composite_or_two_nonexists(self) -> None:
        """``logic: or`` over two ``nonexists`` legs renders as OR-joined."""
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilterGroup(
                    logic=FilterLogic.OR,
                    filters=[
                        QueryFilter(
                            field="Order ID",
                            op=FilterOperator.NONEXISTS,
                            subquery=Subquery(data_object="OrderItems"),
                        ),
                        QueryFilter(
                            field="Order ID",
                            op=FilterOperator.NONEXISTS,
                            subquery=Subquery(data_object="Payments"),
                        ),
                    ],
                )
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert result.sql.count("NOT EXISTS (") == 2
        assert " OR " in result.sql

    def test_path_name_selects_secondary_join(self) -> None:
        """``pathName: viaWarehouse`` uses the secondary Returns→Orders join."""
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(data_object="Returns", path_name="viaWarehouse"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        # The secondary join uses Returns.WAREHOUSE_ID, not Returns.ORDER_ID.
        assert '"Returns"."WAREHOUSE_ID"' in result.sql
        # And the primary join column should NOT appear for this filter.
        assert '"Returns"."ORDER_ID"' not in result.sql

    def test_path_name_omitted_uses_primary_join(self) -> None:
        """Without ``pathName``, the primary Returns→Orders join is used."""
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(data_object="Returns"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert '"Returns"."ORDER_ID"' in result.sql
        assert '"Returns"."WAREHOUSE_ID"' not in result.sql


# ---------------------------------------------------------------------------
# Validation / error paths
# ---------------------------------------------------------------------------


class TestExistsValidation:
    def _expect(self, query: QueryObject, model, code: str) -> None:
        with pytest.raises(ResolutionError) as excinfo:
            PIPELINE.compile(query, model, "postgres")
        codes = {e.code for e in excinfo.value.errors}
        assert code in codes, f"expected {code} in {codes}"

    def test_unknown_target_raises_semantic_error(self) -> None:
        model = _load_model(BASE_MODEL)
        self._expect(
            QueryObject(
                select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
                where=[
                    QueryFilter(
                        field="Order ID",
                        op=FilterOperator.EXISTS,
                        subquery=Subquery(data_object="DoesNotExist"),
                    )
                ],
            ),
            model,
            "UNKNOWN_SUBQUERY_DATA_OBJECT",
        )

    def test_unknown_path_name_raises(self) -> None:
        model = _load_model(BASE_MODEL)
        self._expect(
            QueryObject(
                select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
                where=[
                    QueryFilter(
                        field="Order ID",
                        op=FilterOperator.EXISTS,
                        subquery=Subquery(data_object="Returns", path_name="doesNotExist"),
                    )
                ],
            ),
            model,
            "UNKNOWN_PATH_NAME",
        )

    def test_unknown_subquery_filter_column_raises(self) -> None:
        model = _load_model(BASE_MODEL)
        self._expect(
            QueryObject(
                select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
                where=[
                    QueryFilter(
                        field="Order ID",
                        op=FilterOperator.EXISTS,
                        subquery=Subquery(
                            data_object="OrderItems",
                            filter=[
                                QueryFilter(
                                    field="DoesNotExist",
                                    op=FilterOperator.EQUALS,
                                    value="x",
                                )
                            ],
                        ),
                    )
                ],
            ),
            model,
            "UNKNOWN_SUBQUERY_FILTER_COLUMN",
        )

    def test_no_join_path_raises(self) -> None:
        """A target disconnected from the subject in the join graph errors."""
        disconnected_model = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
  Standalone:
    code: STANDALONE
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Standalone ID:
        code: STANDALONE_ID
        abstractType: string
dimensions:
  Order ID:
    dataObject: Orders
    column: Order ID
    resultType: string
measures:
  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count
"""
        model = _load_model(disconnected_model)
        self._expect(
            QueryObject(
                select=QuerySelect(dimensions=["Order ID"], measures=["Order Count"]),
                where=[
                    QueryFilter(
                        field="Order ID",
                        op=FilterOperator.EXISTS,
                        subquery=Subquery(data_object="Standalone"),
                    )
                ],
            ),
            model,
            "NO_JOIN_PATH_TO_SUBQUERY",
        )

    def test_nested_exists_rejected(self) -> None:
        model = _load_model(BASE_MODEL)
        self._expect(
            QueryObject(
                select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
                where=[
                    QueryFilter(
                        field="Order ID",
                        op=FilterOperator.EXISTS,
                        subquery=Subquery(
                            data_object="OrderItems",
                            filter=[
                                QueryFilter(
                                    field="SKU",
                                    op=FilterOperator.EXISTS,
                                    subquery=Subquery(data_object="Returns"),
                                )
                            ],
                        ),
                    )
                ],
            ),
            model,
            "NESTED_SUBQUERY_NOT_SUPPORTED",
        )

    def test_exists_in_having_rejected(self) -> None:
        """v2.7 restricts EXISTS / NONEXISTS to WHERE only. HAVING is evaluated
        after GROUP BY, so the correlation predicate's row-level subject
        column is out of scope — every dialect would reject the resulting SQL.
        Measure-level EXISTS is a separate, deferred feature
        (``MeasureFilter.subquery``)."""
        model = _load_model(BASE_MODEL)
        self._expect(
            QueryObject(
                select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
                having=[
                    QueryFilter(
                        field="Order ID",
                        op=FilterOperator.EXISTS,
                        subquery=Subquery(data_object="OrderItems"),
                    )
                ],
            ),
            model,
            "INVALID_FILTER_OPERATOR",
        )

    def test_nonexists_in_having_rejected(self) -> None:
        model = _load_model(BASE_MODEL)
        self._expect(
            QueryObject(
                select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
                having=[
                    QueryFilter(
                        field="Order ID",
                        op=FilterOperator.NONEXISTS,
                        subquery=Subquery(data_object="OrderItems"),
                    )
                ],
            ),
            model,
            "INVALID_FILTER_OPERATOR",
        )


# ---------------------------------------------------------------------------
# 8-dialect snapshot — EXISTS is portable, every backend emits the operator.
# ---------------------------------------------------------------------------


class TestExistsDialects:
    @pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
    def test_exists_compiles_per_dialect(self, dialect_name: str) -> None:
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(data_object="OrderItems"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, dialect_name)
        assert "EXISTS (" in result.sql, f"{dialect_name}: missing EXISTS in compiled SQL"
        # Subquery must select a constant — that's the EXISTS idiom.
        assert "SELECT 1" in result.sql

    @pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
    def test_nonexists_compiles_per_dialect(self, dialect_name: str) -> None:
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.NONEXISTS,
                    subquery=Subquery(data_object="OrderItems"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, dialect_name)
        assert "NOT EXISTS (" in result.sql, f"{dialect_name}: missing NOT EXISTS in compiled SQL"


class TestExistsPhysicalTables:
    """``physical_tables`` must include EXISTS / NONEXISTS subquery targets so
    the cache key reflects every table the SQL reads — otherwise child-table
    edits would not invalidate cached results."""

    def test_exists_target_listed_in_physical_tables(self) -> None:
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(data_object="OrderItems"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert any(ref.endswith(".ORDER_ITEMS") for ref in result.physical_tables), (
            result.physical_tables
        )

    def test_nested_subquery_filter_targets_also_tracked(self) -> None:
        """A ``Subquery.filter`` may itself contain an EXISTS clause; the
        nested target must also appear in physical_tables."""
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(
                        data_object="OrderItems",
                        filter=[
                            QueryFilter(
                                field="SKU",
                                op=FilterOperator.EQUALS,
                                value="sku-a",
                            )
                        ],
                    ),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        refs = result.physical_tables
        assert any(r.endswith(".ORDERS") for r in refs), refs
        assert any(r.endswith(".ORDER_ITEMS") for r in refs), refs


# ---------------------------------------------------------------------------
# DuckDB execution — round-trip against an in-memory database to verify the
# generated SQL actually runs and returns the rows we expect.
# ---------------------------------------------------------------------------


class TestExistsExecution:
    def _setup_duckdb(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA PUBLIC")
        conn.execute("CREATE TABLE PUBLIC.CUSTOMERS (CUSTOMER_ID TEXT, COUNTRY TEXT)")
        conn.execute("CREATE TABLE PUBLIC.ORDERS (ORDER_ID TEXT, CUSTOMER_ID TEXT, AMOUNT DOUBLE)")
        conn.execute(
            "CREATE TABLE PUBLIC.ORDER_ITEMS ("
            "ITEM_ID TEXT, ORDER_ID TEXT, SKU TEXT, IS_RETURNED BOOLEAN)"
        )
        conn.execute("INSERT INTO PUBLIC.CUSTOMERS VALUES ('c1', 'Germany'), ('c2', 'France')")
        # c1 has o1 with items, o2 without; c2 has o3 with returned item.
        conn.execute(
            "INSERT INTO PUBLIC.ORDERS VALUES "
            "('o1', 'c1', 100), ('o2', 'c1', 50), ('o3', 'c2', 200)"
        )
        conn.execute(
            "INSERT INTO PUBLIC.ORDER_ITEMS VALUES "
            "('i1', 'o1', 'sku-a', FALSE), ('i2', 'o3', 'sku-b', TRUE)"
        )
        return conn

    def test_exists_returns_only_orders_with_items(self) -> None:
        conn = self._setup_duckdb()
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(data_object="OrderItems"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "duckdb")
        rows = conn.execute(result.sql).fetchall()
        # Germany has o1 (with items); France has o3 (with items). Each one
        # contributes a single distinct order; o2 (no items) is filtered out.
        by_country = {country: count for country, count in rows}
        assert by_country == {"Germany": 1, "France": 1}

    def test_nonexists_returns_only_orders_without_items(self) -> None:
        conn = self._setup_duckdb()
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.NONEXISTS,
                    subquery=Subquery(data_object="OrderItems"),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "duckdb")
        rows = conn.execute(result.sql).fetchall()
        by_country = {country: count for country, count in rows}
        # Only Germany's o2 has no items.
        assert by_country == {"Germany": 1}

    def test_exists_with_subquery_filter(self) -> None:
        """Only orders with at least one *returned* item count."""
        conn = self._setup_duckdb()
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(
                        data_object="OrderItems",
                        filter=[
                            QueryFilter(
                                field="Is Returned",
                                op=FilterOperator.EQUALS,
                                value=True,
                            )
                        ],
                    ),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "duckdb")
        rows = conn.execute(result.sql).fetchall()
        by_country = {country: count for country, count in rows}
        # Only o3 (France) has a returned item.
        assert by_country == {"France": 1}
