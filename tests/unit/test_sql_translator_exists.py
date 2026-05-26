"""OBSQL EXISTS / NOT EXISTS translation (v2.7.5, follow-up to #82 release).

v2.7.0 added EXISTS / NONEXISTS to QueryObject. OBSQL (the BI-style SQL
surface that compiles to QueryObject) didn't get matching support, so
``WHERE EXISTS (SELECT 1 FROM "OrderItems")`` was rejected with
``UNSUPPORTED_SQL_FEATURE`` even though the underlying machinery was in
place. This test set covers the translation contract.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.sql_translator import SQLTranslationError, translate_sql_to_query
from orionbelt.models.query import FilterOperator
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_MODEL_YAML = """\
version: 1.0
dataObjects:
  Customers:
    code: CUSTOMERS
    columns:
      Customer ID: {code: CID, abstractType: string, primaryKey: true}
      Country: {code: COUNTRY, abstractType: string}
  Orders:
    code: ORDERS
    columns:
      Order ID: {code: OID, abstractType: string, primaryKey: true}
      Order Customer ID: {code: CID, abstractType: string}
      Amount: {code: AMT, abstractType: float, numClass: additive}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
  OrderItems:
    code: ORDER_ITEMS
    columns:
      Item ID: {code: IID, abstractType: string, primaryKey: true}
      Item Order ID: {code: OID, abstractType: string}
      Status: {code: STATUS, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Orders
        columnsFrom: [Item Order ID]
        columnsTo: [Order ID]
dimensions:
  Customer Country: {dataObject: Customers, column: Country, resultType: string}
  Order ID: {dataObject: Orders, column: Order ID, resultType: string}
measures:
  Total Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
"""


@pytest.fixture
def model() -> SemanticModel:
    loader = TrackedLoader()
    raw, source_map = loader.load_string(_MODEL_YAML)
    sm, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return sm


# --- happy path ---------------------------------------------------------------


def test_exists_translates_to_query_filter(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Order ID", "Total Revenue" FROM m WHERE EXISTS (SELECT 1 FROM "OrderItems")',
        model,
    )
    assert len(q.where) == 1
    qf = q.where[0]
    assert qf.op == FilterOperator.EXISTS
    assert qf.field == "Order ID"
    assert qf.subquery is not None
    assert qf.subquery.data_object == "OrderItems"
    assert qf.subquery.filter == []


def test_not_exists_translates_to_nonexists(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Order ID", "Total Revenue" FROM m WHERE NOT EXISTS (SELECT 1 FROM "OrderItems")',
        model,
    )
    qf = q.where[0]
    assert qf.op == FilterOperator.NONEXISTS
    assert qf.subquery is not None
    assert qf.subquery.data_object == "OrderItems"


def test_subject_is_first_dimension(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Customer Country", "Order ID", "Total Revenue" FROM m '
        'WHERE EXISTS (SELECT 1 FROM "OrderItems")',
        model,
    )
    assert q.where[0].field == "Customer Country"


def test_subject_falls_back_to_measure(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Total Revenue" FROM m WHERE EXISTS (SELECT 1 FROM "OrderItems")',
        model,
    )
    assert q.where[0].field == "Total Revenue"


# --- subquery filter ---------------------------------------------------------


def test_exists_with_subquery_filter(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Order ID", "Total Revenue" FROM m '
        'WHERE EXISTS (SELECT 1 FROM "OrderItems" WHERE "Status" = \'shipped\')',
        model,
    )
    qf = q.where[0]
    assert qf.subquery is not None
    assert len(qf.subquery.filter) == 1
    sub = qf.subquery.filter[0]
    assert sub.field == "Status"
    assert sub.op == FilterOperator.EQUALS
    assert sub.value == "shipped"


def test_exists_with_in_list_subquery_filter(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Order ID", "Total Revenue" FROM m '
        "WHERE EXISTS (SELECT 1 FROM \"OrderItems\" WHERE \"Status\" IN ('shipped', 'returned'))",
        model,
    )
    sub = q.where[0].subquery.filter[0]
    assert sub.op == FilterOperator.IN_LIST
    assert sub.value == ["shipped", "returned"]


def test_exists_with_multiple_subquery_filters(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Order ID", "Total Revenue" FROM m '
        "WHERE EXISTS ("
        'SELECT 1 FROM "OrderItems" '
        'WHERE "Status" = \'shipped\' AND "Item ID" IS NOT NULL'
        ")",
        model,
    )
    subs = q.where[0].subquery.filter
    assert len(subs) == 2
    assert subs[0].field == "Status"
    assert subs[1].field == "Item ID"
    assert subs[1].op == FilterOperator.IS_NOT_NULL


# --- combined with outer filters --------------------------------------------


def test_exists_combined_with_outer_filter(model: SemanticModel) -> None:
    q = translate_sql_to_query(
        'SELECT "Order ID", "Total Revenue" FROM m '
        'WHERE "Customer Country" = \'US\' AND EXISTS (SELECT 1 FROM "OrderItems")',
        model,
    )
    assert len(q.where) == 2
    # Order of predicates in the AND chain
    codes = {f.op for f in q.where}
    assert FilterOperator.EQUALS in codes
    assert FilterOperator.EXISTS in codes


# --- rejections --------------------------------------------------------------


def test_exists_without_select_subject_errors(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        # SELECT * is rejected upstream — fall back to a literal
        translate_sql_to_query(
            'SELECT 1 FROM m WHERE EXISTS (SELECT 1 FROM "OrderItems")',
            model,
        )
    # Either UNSUPPORTED_SELECT_ITEM (the literal) or
    # UNSUPPORTED_SQL_FEATURE for no-subject — both acceptable here.
    msg = str(exc.value)
    assert "EXISTS" in msg or "SELECT" in msg


def test_exists_with_group_by_in_subquery_errors(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Order ID", "Total Revenue" FROM m '
            'WHERE EXISTS (SELECT 1 FROM "OrderItems" GROUP BY "Status")',
            model,
        )
    assert "GROUP" in str(exc.value).upper()


def test_exists_with_join_in_subquery_errors(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Order ID", "Total Revenue" FROM m '
            "WHERE EXISTS ("
            'SELECT 1 FROM "OrderItems" JOIN "Orders" ON 1 = 1'
            ")",
            model,
        )
    assert "JOIN" in str(exc.value).upper()


def test_nested_exists_in_subquery_errors(model: SemanticModel) -> None:
    with pytest.raises(SQLTranslationError) as exc:
        translate_sql_to_query(
            'SELECT "Order ID", "Total Revenue" FROM m '
            "WHERE EXISTS ("
            'SELECT 1 FROM "OrderItems" '
            'WHERE EXISTS (SELECT 1 FROM "Orders")'
            ")",
            model,
        )
    assert "Nested EXISTS" in str(exc.value) or "EXISTS" in str(exc.value)
