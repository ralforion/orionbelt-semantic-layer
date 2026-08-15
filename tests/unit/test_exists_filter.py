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


# A model whose subquery targets join onward, so a subquery filter has
# somewhere to traverse to: OrderItems → Products, Orders → Dates. Payments
# hangs off Orders on a many-to-one, which makes it unreachable *from*
# OrderItems — the negative case.
TRAVERSAL_MODEL = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID: {code: CUSTOMER_ID, abstractType: string}
      Country: {code: COUNTRY, abstractType: string}

  Dates:
    code: DATES
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Date Key: {code: DATE_KEY, abstractType: string}
      Year: {code: YEAR, abstractType: int}

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID: {code: ORDER_ID, abstractType: string}
      Order Customer ID: {code: CUSTOMER_ID, abstractType: string}
      Order Date Key: {code: DATE_KEY, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Dates
        columnsFrom: [Order Date Key]
        columnsTo: [Date Key]

  OrderItems:
    code: ORDER_ITEMS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Item ID: {code: ITEM_ID, abstractType: string}
      Item Order ID: {code: ORDER_ID, abstractType: string}
      Item Product ID: {code: PRODUCT_ID, abstractType: string}
      SKU: {code: SKU, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom: [Item Order ID]
        columnsTo: [Order ID]
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Item Product ID]
        columnsTo: [Product ID]

  Products:
    code: PRODUCTS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Product ID: {code: PRODUCT_ID, abstractType: string}
      Category: {code: CATEGORY, abstractType: string}
      Product SKU: {code: PRODUCT_SKU, abstractType: string}

  Payments:
    code: PAYMENTS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Payment ID: {code: PAYMENT_ID, abstractType: string}
      Payment Order ID: {code: ORDER_ID, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom: [Payment Order ID]
        columnsTo: [Order ID]

dimensions:
  Customer Country: {dataObject: Customers, column: Country, resultType: string}
  Order ID: {dataObject: Orders, column: Order ID, resultType: string}
  Product Category: {dataObject: Products, column: Category, resultType: string}
  Order Year: {dataObject: Dates, column: Year, resultType: int}
  SKU: {dataObject: Products, column: Product SKU, resultType: string}

measures:
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    resultType: int
    aggregation: count
"""


# Like TRAVERSAL_MODEL, but the objects hanging off OrderItems declare their
# joins the other way round: ItemDetails → OrderItems one-to-one and ItemTags
# → OrderItems many-to-many. Neither is a *directed* descendant of OrderItems,
# yet both are row-preserving and therefore legal traversals.
REVERSE_MODEL = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID: {code: CUSTOMER_ID, abstractType: string}
      Country: {code: COUNTRY, abstractType: string}

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID: {code: ORDER_ID, abstractType: string}
      Order Customer ID: {code: CUSTOMER_ID, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]

  OrderItems:
    code: ORDER_ITEMS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Item ID: {code: ITEM_ID, abstractType: string}
      Item Order ID: {code: ORDER_ID, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom: [Item Order ID]
        columnsTo: [Order ID]

  ItemDetails:
    code: ITEM_DETAILS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Detail Item ID: {code: DETAIL_ITEM_ID, abstractType: string}
      Gift Wrapped: {code: GIFT_WRAPPED, abstractType: boolean}
    joins:
      - joinType: one-to-one
        joinTo: OrderItems
        columnsFrom: [Detail Item ID]
        columnsTo: [Item ID]

  ItemTags:
    code: ITEM_TAGS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Tag Item ID: {code: TAG_ITEM_ID, abstractType: string}
      Tag Name: {code: TAG_NAME, abstractType: string}
    joins:
      - joinType: many-to-many
        joinTo: OrderItems
        columnsFrom: [Tag Item ID]
        columnsTo: [Item ID]

dimensions:
  Customer Country: {dataObject: Customers, column: Country, resultType: string}
  Order ID: {dataObject: Orders, column: Order ID, resultType: string}
  Gift Wrapped: {dataObject: ItemDetails, column: Gift Wrapped, resultType: boolean}

measures:
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    resultType: int
    aggregation: count
"""


def _exists_on_items(sub_filters: list[QueryFilter]) -> QueryObject:
    """Orders-by-country query semi-joined to OrderItems with *sub_filters*."""
    return QueryObject(
        select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
        where=[
            QueryFilter(
                field="Order ID",
                op=FilterOperator.EXISTS,
                subquery=Subquery(data_object="OrderItems", filter=sub_filters),
            )
        ],
    )


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
# Subquery filters that traverse the target's joins
# ---------------------------------------------------------------------------


class TestSubqueryFilterTraversal:
    """A ``Subquery.filter`` may name a dimension or a qualified column on any
    data object reachable from the subquery's target; the join it needs is
    emitted inside the ``EXISTS`` body."""

    def test_dimension_one_hop_joins_inside_exists(self) -> None:
        model = _load_model(TRAVERSAL_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="Product Category", op=FilterOperator.EQUALS, value="Toys")]
        )
        sql = PIPELINE.compile(query, model, "postgres").sql
        assert '"Products"."CATEGORY" = \'Toys\'' in sql
        # The join lands inside the subquery, as an INNER JOIN, and the outer
        # query is left untouched: Products appears exactly once.
        assert sql.count("PRODUCTS") == 1
        assert 'INNER JOIN "PUBLIC"."PRODUCTS" AS "Products"' in sql
        assert sql.index("EXISTS (") < sql.index("INNER JOIN")

    def test_qualified_column_one_hop(self) -> None:
        model = _load_model(TRAVERSAL_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="Products.Category", op=FilterOperator.EQUALS, value="Toys")]
        )
        sql = PIPELINE.compile(query, model, "postgres").sql
        assert '"Products"."CATEGORY" = \'Toys\'' in sql
        assert sql.count("PRODUCTS") == 1

    def test_target_column_wins_over_same_named_dimension(self) -> None:
        """``SKU`` is both a column of OrderItems and a dimension on Products.
        The target's own column takes precedence, so no join is added."""
        model = _load_model(TRAVERSAL_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="SKU", op=FilterOperator.EQUALS, value="sku-a")]
        )
        sql = PIPELINE.compile(query, model, "postgres").sql
        assert '"OrderItems"."SKU" = \'sku-a\'' in sql
        assert "PRODUCTS" not in sql

    def test_same_object_joined_once_for_two_filters(self) -> None:
        model = _load_model(TRAVERSAL_MODEL)
        query = _exists_on_items(
            [
                QueryFilter(field="Product Category", op=FilterOperator.EQUALS, value="Toys"),
                QueryFilter(field="Products.Product SKU", op=FilterOperator.EQUALS, value="p-1"),
            ]
        )
        sql = PIPELINE.compile(query, model, "postgres").sql
        assert sql.count('INNER JOIN "PUBLIC"."PRODUCTS"') == 1
        assert '"Products"."CATEGORY"' in sql
        assert '"Products"."PRODUCT_SKU"' in sql

    def test_semi_join_windowed_by_a_date_dimension(self) -> None:
        """The shape this unlocks: "customers who ordered in 2024", where the
        window lives on a Date object one join past the subquery's target."""
        model = _load_model(TRAVERSAL_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Customer Country",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(
                        data_object="Orders",
                        filter=[
                            QueryFilter(field="Order Year", op=FilterOperator.EQUALS, value=2024)
                        ],
                    ),
                )
            ],
        )
        sql = PIPELINE.compile(query, model, "postgres").sql
        assert 'INNER JOIN "PUBLIC"."DATES" AS "Dates"' in sql
        assert '"Dates"."YEAR" = 2024' in sql
        assert sql.index("EXISTS (") < sql.index("INNER JOIN")

    @pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
    def test_traversing_filter_compiles_per_dialect(self, dialect_name: str) -> None:
        model = _load_model(TRAVERSAL_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="Product Category", op=FilterOperator.EQUALS, value="Toys")]
        )
        sql = PIPELINE.compile(query, model, dialect_name).sql
        assert "EXISTS (" in sql, f"{dialect_name}: missing EXISTS"
        assert "INNER JOIN" in sql, f"{dialect_name}: subquery join not emitted"
        assert sql.index("EXISTS (") < sql.index("INNER JOIN"), (
            f"{dialect_name}: join landed outside the subquery"
        )

    def test_joined_object_tracked_in_physical_tables(self) -> None:
        """The cache key must cover tables only the EXISTS body reads."""
        model = _load_model(TRAVERSAL_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="Product Category", op=FilterOperator.EQUALS, value="Toys")]
        )
        refs = PIPELINE.compile(query, model, "postgres").physical_tables
        assert any(r.endswith(".PRODUCTS") for r in refs), refs
        assert any(r.endswith(".ORDER_ITEMS") for r in refs), refs


class TestSubqueryFilterReverseTraversal:
    """Row-preserving joins declared *towards* the subquery's target.

    ``EXISTS`` bodies reach filter objects with the same walker the outer
    query uses, and that walker treats one-to-one and many-to-many joins as
    bidirectional. A reachability rule that only followed declared direction
    would refuse these paths even though the walker would happily emit them.
    """

    def test_reverse_one_to_one_dimension(self) -> None:
        model = _load_model(REVERSE_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="Gift Wrapped", op=FilterOperator.EQUALS, value=True)]
        )
        sql = PIPELINE.compile(query, model, "postgres").sql
        assert 'INNER JOIN "PUBLIC"."ITEM_DETAILS" AS "ItemDetails"' in sql
        assert '"ItemDetails"."DETAIL_ITEM_ID" = "OrderItems"."ITEM_ID"' in sql
        assert '"ItemDetails"."GIFT_WRAPPED" = TRUE' in sql
        assert sql.index("EXISTS (") < sql.index("INNER JOIN")

    def test_reverse_many_to_many_qualified_column(self) -> None:
        model = _load_model(REVERSE_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="ItemTags.Tag Name", op=FilterOperator.EQUALS, value="gift")]
        )
        sql = PIPELINE.compile(query, model, "postgres").sql
        assert 'INNER JOIN "PUBLIC"."ITEM_TAGS" AS "ItemTags"' in sql
        assert '"ItemTags"."TAG_ITEM_ID" = "OrderItems"."ITEM_ID"' in sql
        assert '"ItemTags"."TAG_NAME" = \'gift\'' in sql

    def test_reverse_join_tracked_in_physical_tables(self) -> None:
        model = _load_model(REVERSE_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="Gift Wrapped", op=FilterOperator.EQUALS, value=True)]
        )
        refs = PIPELINE.compile(query, model, "postgres").physical_tables
        assert any(r.endswith(".ITEM_DETAILS") for r in refs), refs

    def test_reverse_many_to_one_still_refused(self) -> None:
        """Only row-preserving joins are bidirectional: Orders stays the
        subject, so nothing changes for the many-to-one direction."""
        model = _load_model(REVERSE_MODEL)
        with pytest.raises(ResolutionError) as excinfo:
            PIPELINE.compile(
                _exists_on_items(
                    [QueryFilter(field="Customers.Country", op=FilterOperator.EQUALS, value="DE")]
                ),
                model,
                "postgres",
            )
        assert "SUBQUERY_FILTER_OBJECT_NOT_JOINABLE" in {e.code for e in excinfo.value.errors}


class TestSubqueryFilterTraversalValidation:
    def _expect(self, query: QueryObject, model, code: str) -> None:
        with pytest.raises(ResolutionError) as excinfo:
            PIPELINE.compile(query, model, "postgres")
        codes = {e.code for e in excinfo.value.errors}
        assert code in codes, f"expected {code} in {codes}"

    def test_unreachable_object_rejected(self) -> None:
        """Payments joins *to* Orders many-to-one, so it is not reachable from
        OrderItems — filtering on it inside the subquery must not silently
        widen the semi-join."""
        model = _load_model(TRAVERSAL_MODEL)
        self._expect(
            _exists_on_items(
                [QueryFilter(field="Payments.Payment ID", op=FilterOperator.EQUALS, value="p1")]
            ),
            model,
            "UNREACHABLE_SUBQUERY_FILTER_OBJECT",
        )

    def test_subject_object_rejected(self) -> None:
        """Joining the correlation subject inside the body would shadow its
        outer alias and rebind the correlation predicate."""
        model = _load_model(TRAVERSAL_MODEL)
        self._expect(
            _exists_on_items([QueryFilter(field="Orders.Amount", op=FilterOperator.GT, value=10)]),
            model,
            "SUBQUERY_FILTER_OBJECT_NOT_JOINABLE",
        )

    def test_path_through_the_subject_rejected(self) -> None:
        """Customers is reachable from OrderItems only via Orders, the
        subject — same shadowing hazard, one hop further out."""
        model = _load_model(TRAVERSAL_MODEL)
        self._expect(
            _exists_on_items(
                [QueryFilter(field="Customers.Country", op=FilterOperator.EQUALS, value="DE")]
            ),
            model,
            "SUBQUERY_FILTER_OBJECT_NOT_JOINABLE",
        )

    def test_unknown_qualified_column_rejected(self) -> None:
        model = _load_model(TRAVERSAL_MODEL)
        self._expect(
            _exists_on_items(
                [QueryFilter(field="Products.No Such", op=FilterOperator.EQUALS, value="x")]
            ),
            model,
            "UNKNOWN_SUBQUERY_FILTER_COLUMN",
        )

    def test_unknown_data_object_in_qualified_field_rejected(self) -> None:
        model = _load_model(TRAVERSAL_MODEL)
        self._expect(
            _exists_on_items(
                [QueryFilter(field="Nope.Category", op=FilterOperator.EQUALS, value="x")]
            ),
            model,
            "UNKNOWN_SUBQUERY_FILTER_COLUMN",
        )


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


class TestTraversingSubqueryFilterExecution:
    """The joined-in filter must restrict the semi-join, not the outer rows."""

    def _setup_duckdb(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA PUBLIC")
        conn.execute("CREATE TABLE PUBLIC.CUSTOMERS (CUSTOMER_ID TEXT, COUNTRY TEXT)")
        conn.execute("CREATE TABLE PUBLIC.DATES (DATE_KEY TEXT, YEAR INTEGER)")
        conn.execute(
            "CREATE TABLE PUBLIC.ORDERS "
            "(ORDER_ID TEXT, CUSTOMER_ID TEXT, DATE_KEY TEXT, AMOUNT DOUBLE)"
        )
        conn.execute(
            "CREATE TABLE PUBLIC.ORDER_ITEMS "
            "(ITEM_ID TEXT, ORDER_ID TEXT, PRODUCT_ID TEXT, SKU TEXT)"
        )
        conn.execute(
            "CREATE TABLE PUBLIC.PRODUCTS (PRODUCT_ID TEXT, CATEGORY TEXT, PRODUCT_SKU TEXT)"
        )
        conn.execute("CREATE TABLE PUBLIC.PAYMENTS (PAYMENT_ID TEXT, ORDER_ID TEXT)")
        conn.execute("INSERT INTO PUBLIC.CUSTOMERS VALUES ('c1', 'Germany'), ('c2', 'France')")
        conn.execute("INSERT INTO PUBLIC.DATES VALUES ('d2023', 2023), ('d2024', 2024)")
        # o1 (Germany, 2024) has a Toys item; o2 (Germany, 2023) has a Books
        # item; o3 (France, 2024) has a Books item.
        conn.execute(
            "INSERT INTO PUBLIC.ORDERS VALUES "
            "('o1', 'c1', 'd2024', 100), ('o2', 'c1', 'd2023', 50), ('o3', 'c2', 'd2024', 200)"
        )
        conn.execute(
            "INSERT INTO PUBLIC.PRODUCTS VALUES "
            "('p1', 'Toys', 'sku-toy'), ('p2', 'Books', 'sku-book')"
        )
        conn.execute(
            "INSERT INTO PUBLIC.ORDER_ITEMS VALUES "
            "('i1', 'o1', 'p1', 'sku-a'), ('i2', 'o2', 'p2', 'sku-b'), "
            "('i3', 'o3', 'p2', 'sku-c')"
        )
        return conn

    def test_filter_on_joined_object_restricts_the_semi_join(self) -> None:
        conn = self._setup_duckdb()
        model = _load_model(TRAVERSAL_MODEL)
        query = _exists_on_items(
            [QueryFilter(field="Product Category", op=FilterOperator.EQUALS, value="Toys")]
        )
        result = PIPELINE.compile(query, model, "duckdb")
        rows = conn.execute(result.sql).fetchall()
        # Only o1 has a Toys item, and it belongs to Germany.
        assert {country: count for country, count in rows} == {"Germany": 1}

    def test_semi_join_windowed_by_a_date_dimension(self) -> None:
        conn = self._setup_duckdb()
        model = _load_model(TRAVERSAL_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Customer Country",
                    op=FilterOperator.EXISTS,
                    subquery=Subquery(
                        data_object="Orders",
                        filter=[
                            QueryFilter(field="Order Year", op=FilterOperator.EQUALS, value=2024)
                        ],
                    ),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "duckdb")
        rows = conn.execute(result.sql).fetchall()
        # Both customers ordered in 2024, so both are kept — and every one of
        # their orders is counted, 2023 included. That is the semi-join's
        # meaning: the window restricts *which customers qualify*, not which
        # of the outer rows survive.
        assert {country: count for country, count in rows} == {"Germany": 2, "France": 1}

    def test_nonexists_with_a_traversing_filter(self) -> None:
        conn = self._setup_duckdb()
        model = _load_model(TRAVERSAL_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count"]),
            where=[
                QueryFilter(
                    field="Order ID",
                    op=FilterOperator.NONEXISTS,
                    subquery=Subquery(
                        data_object="OrderItems",
                        filter=[
                            QueryFilter(
                                field="Product Category",
                                op=FilterOperator.EQUALS,
                                value="Toys",
                            )
                        ],
                    ),
                )
            ],
        )
        result = PIPELINE.compile(query, model, "duckdb")
        rows = conn.execute(result.sql).fetchall()
        # o2 and o3 carry no Toys item.
        assert {country: count for country, count in rows} == {"Germany": 1, "France": 1}
