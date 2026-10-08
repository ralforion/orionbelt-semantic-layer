"""Reaggregate metrics executed on DuckDB against answers worked out by hand.

The rows are few enough to check on paper, and include a NULL country: a NULL
dimension value is a group of its own, and the second stage has to find it when
it joins back to the query grain.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryObject, QueryOrderBy, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

MODEL_YAML = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    schema: PUBLIC
    columns:
      Order ID: {code: ORDER_ID, abstractType: string}
      Customer: {code: CUSTOMER_ID, abstractType: string}
      Country: {code: COUNTRY, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float, numClass: additive}
dimensions:
  Customer: {dataObject: Orders, column: Customer, resultType: string}
  Country: {dataObject: Orders, column: Country, resultType: string}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
metrics:
  Avg Revenue per Customer:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: avg
  Best Customer Revenue:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: max
  Customers:
    type: reaggregate
    measure: Revenue
    per: [Customer]
    aggregation: count
  Avg Orders per Customer:
    type: reaggregate
    measure: Orders Count
    per: [Customer]
    aggregation: avg
"""

# Per customer:  DE c1 = 10 + 20 = 30, DE c2 = 50, DE c7 = 1,
#                FR c3 = 5, FR c6 = 1000,
#                NULL c4 = 7 + 3 = 10, NULL c5 = 30.
# Ranked by average FR leads; ranked by customer count DE does - so ordering
# by one metric cannot pass for ordering by the other.
_ROWS = [
    ("o1", "c1", "DE", 10),
    ("o2", "c1", "DE", 20),
    ("o3", "c2", "DE", 50),
    ("o8", "c7", "DE", 1),
    ("o4", "c3", "FR", 5),
    ("o9", "c6", "FR", 1000),
    ("o5", "c4", None, 7),
    ("o6", "c4", None, 3),
    ("o7", "c5", None, 30),
]


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture(scope="module")
def conn() -> Any:
    con = duckdb.connect()
    con.execute('CREATE SCHEMA "PUBLIC"')
    con.execute(
        'CREATE TABLE "PUBLIC"."ORDERS" '
        "(ORDER_ID VARCHAR, CUSTOMER_ID VARCHAR, COUNTRY VARCHAR, AMOUNT DOUBLE)"
    )
    con.executemany('INSERT INTO "PUBLIC"."ORDERS" VALUES (?, ?, ?, ?)', _ROWS)
    yield con
    con.close()


def _run(model: SemanticModel, conn: Any, query: QueryObject) -> list[dict[str, Any]]:
    cur = conn.execute(CompilationPipeline().compile(query, model, "duckdb").sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def test_by_country_including_null(model: SemanticModel, conn: Any) -> None:
    rows = _run(
        model,
        conn,
        QueryObject(
            select=QuerySelect(
                dimensions=["Country"],
                measures=[
                    "Revenue",
                    "Avg Revenue per Customer",
                    "Best Customer Revenue",
                    "Customers",
                    "Avg Orders per Customer",
                ],
            )
        ),
    )
    by_country = {row.pop("Country"): row for row in rows}
    assert by_country == {
        "DE": {
            "Revenue": Decimal("81.00"),
            "Avg Revenue per Customer": Decimal("27.00"),
            "Best Customer Revenue": Decimal("50.00"),
            "Customers": 3,
            "Avg Orders per Customer": Decimal("1.33"),
        },
        "FR": {
            "Revenue": Decimal("1005.00"),
            "Avg Revenue per Customer": Decimal("502.50"),
            "Best Customer Revenue": Decimal("1000.00"),
            "Customers": 2,
            "Avg Orders per Customer": Decimal("1.00"),
        },
        None: {
            "Revenue": Decimal("40.00"),
            "Avg Revenue per Customer": Decimal("20.00"),
            "Best Customer Revenue": Decimal("30.00"),
            "Customers": 2,
            "Avg Orders per Customer": Decimal("1.50"),
        },
    }


def test_ungrouped(model: SemanticModel, conn: Any) -> None:
    rows = _run(
        model,
        conn,
        QueryObject(select=QuerySelect(measures=["Avg Revenue per Customer", "Customers"])),
    )
    # (30 + 50 + 1 + 5 + 1000 + 10 + 30) / 7 customers = 160.857...
    assert rows == [{"Avg Revenue per Customer": Decimal("160.86"), "Customers": 7}]


def test_only_the_metric(model: SemanticModel, conn: Any) -> None:
    rows = _run(
        model,
        conn,
        QueryObject(select=QuerySelect(dimensions=["Country"], measures=["Customers"])),
    )
    assert {r["Country"]: r["Customers"] for r in rows} == {"DE": 3, "FR": 2, None: 2}


@pytest.mark.parametrize(
    ("order_field", "first"),
    [("Customers", "DE"), ("Avg Revenue per Customer", "FR")],
)
def test_order_by_names_the_metric_asked_for(
    model: SemanticModel, conn: Any, order_field: str, first: str
) -> None:
    """Two metrics over one measure each order by their own value."""
    rows = _run(
        model,
        conn,
        QueryObject(
            select=QuerySelect(
                dimensions=["Country"], measures=["Avg Revenue per Customer", "Customers"]
            ),
            order_by=[QueryOrderBy(field=order_field, direction="desc")],
            limit=1,
        ),
    )
    assert [r["Country"] for r in rows] == [first]


_INTEGER_AGGREGATIONS = ["sum", "min", "max", "any_value", "mode"]

INTEGER_MODEL_YAML = (
    """\
version: 1.0
dataObjects:
  Shipments:
    code: SHIPMENTS
    schema: PUBLIC
    columns:
      Customer: {code: CUSTOMER_ID, abstractType: string}
      Units: {code: UNITS, abstractType: int, numClass: additive}
dimensions:
  Customer: {dataObject: Shipments, column: Customer, resultType: string}
measures:
"""
    + "".join(
        f"""  Units {agg}:
    columns: [{{dataObject: Shipments, column: Units}}]
    resultType: int
    aggregation: {agg}
"""
        for agg in _INTEGER_AGGREGATIONS
    )
    + "metrics:\n"
    + "".join(
        f"""  Avg Units {agg}:
    type: reaggregate
    measure: Units {agg}
    per: [Customer]
    aggregation: avg
"""
        for agg in _INTEGER_AGGREGATIONS
    )
)


@pytest.fixture(scope="module")
def integer_model(conn: Any) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(INTEGER_MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    conn.execute('CREATE TABLE "PUBLIC"."SHIPMENTS" (CUSTOMER_ID VARCHAR, UNITS BIGINT)')
    conn.executemany(
        'INSERT INTO "PUBLIC"."SHIPMENTS" VALUES (?, ?)',
        [("a", 9007199254740991), ("b", 9007199254740990)],
    )
    return model


@pytest.mark.parametrize("agg", _INTEGER_AGGREGATIONS)
def test_integer_average_is_exact(integer_model: SemanticModel, conn: Any, agg: str) -> None:
    """Per-customer integer values past 2^53 average exactly, as an avg measure does.

    Each customer has one row, so every first-stage aggregate answers that row's
    value; a floating-point AVG answers 9007199254740989.44.
    """
    metric = f"Avg Units {agg}"
    rows = _run(integer_model, conn, QueryObject(select=QuerySelect(measures=[metric])))
    assert rows == [{metric: Decimal("9007199254740990.50")}]
