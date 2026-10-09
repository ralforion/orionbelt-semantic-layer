"""Reaggregate metrics compiled and run on each of the eight engines.

``tests/integration/test_reaggregate_execution.py`` checks the arithmetic on
DuckDB. The two-stage plan leans on things each engine spells its own way: the
NULL-safe join back to the query grain, the CROSS JOIN when there is no
dimension, the cast of the second-stage result, the HAVING wrapper, and the
exact integer AVG, which takes a different route on almost every engine. So
the same answers, worked out by hand, are asserted on all of them.

The rows are built per engine rather than read from the corpus seed: the seed
has no NULL group and no integers past a double's mantissa, and Dremio has no
seed at all.

Per customer:  DE c1 = 10 + 20 = 30, DE c2 = 50, DE c7 = 1,
               FR c3 = 5, FR c6 = 1000,
               NULL c4 = 7 + 3 = 10, NULL c5 = 30.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryFilter, QueryObject, QueryOrderBy, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from ._seed import SCHEMA as SEED_SCHEMA
from .conftest import VendorTarget

pytestmark = pytest.mark.docker

ORDERS = "reagg_orders"
SHIPMENTS = "reagg_shipments"

#: Where each engine keeps the tables. The containers use their connection's
#: default; the cloud engines the seeded schema; Dremio its writable scratch.
SCHEMAS = {
    "bigquery": SEED_SCHEMA,
    "snowflake": SEED_SCHEMA,
    "databricks": SEED_SCHEMA,
    "dremio": "$scratch",
}

#: (string, double, bigint) as each engine spells them in a CAST.
TYPES: dict[str, tuple[str, str, str]] = {
    "duckdb": ("VARCHAR", "DOUBLE", "BIGINT"),
    "postgres": ("TEXT", "DOUBLE PRECISION", "BIGINT"),
    "mysql": ("CHAR(8)", "DOUBLE", "SIGNED"),
    "clickhouse": ("Nullable(String)", "Float64", "Int64"),
    "snowflake": ("VARCHAR", "DOUBLE", "BIGINT"),
    "bigquery": ("STRING", "FLOAT64", "INT64"),
    "databricks": ("STRING", "DOUBLE", "BIGINT"),
    "dremio": ("VARCHAR", "DOUBLE", "BIGINT"),
}

ORDER_ROWS: list[tuple[str, str | None, int]] = [
    ("c1", "DE", 10),
    ("c1", "DE", 20),
    ("c2", "DE", 50),
    ("c7", "DE", 1),
    ("c3", "FR", 5),
    ("c6", "FR", 1000),
    ("c4", None, 7),
    ("c4", None, 3),
    ("c5", None, 30),
]

#: One row per customer, so every first-stage aggregate answers that row's
#: value; a floating-point AVG of the two answers 9007199254740989.44.
SHIPMENT_ROWS: list[tuple[str, int]] = [("a", 9007199254740991), ("b", 9007199254740990)]

MODEL_YAML = """
version: "1.0"
name: reaggregate_vendor
dataObjects:
  Orders:
    code: {orders}
    schema: '{schema}'
    columns:
      Customer: {{code: customer_id, abstractType: string}}
      Country: {{code: country, abstractType: string}}
      Amount: {{code: amount, abstractType: float}}
  Shipments:
    code: {shipments}
    schema: '{schema}'
    columns:
      Customer: {{code: customer_id, abstractType: string}}
      Units: {{code: units, abstractType: int}}
dimensions:
  Customer: {{dataObject: Orders, column: Customer, resultType: string}}
  Country: {{dataObject: Orders, column: Country, resultType: string}}
  Shipment Customer: {{dataObject: Shipments, column: Customer, resultType: string}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
  Units Sum:
    columns: [{{dataObject: Shipments, column: Units}}]
    resultType: int
    aggregation: sum
  Units Min:
    columns: [{{dataObject: Shipments, column: Units}}]
    resultType: int
    aggregation: min
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
  Avg Revenue Share:
    expression: '{{[Avg Revenue per Customer]}} / {{[Revenue]}}'
  Avg Units Sum:
    type: reaggregate
    measure: Units Sum
    per: [Shipment Customer]
    aggregation: avg
  Avg Units Min:
    type: reaggregate
    measure: Units Min
    per: [Shipment Customer]
    aggregation: avg
"""


def _model(target: VendorTarget) -> SemanticModel:
    yaml_text = MODEL_YAML.format(
        orders=ORDERS, shipments=SHIPMENTS, schema=SCHEMAS.get(target.dialect, "")
    )
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _literal(value: str | int | None, type_name: str) -> str:
    text = "NULL" if value is None else f"'{value}'" if isinstance(value, str) else str(value)
    return f"CAST({text} AS {type_name})"


def _create(
    target: VendorTarget, table: str, columns: list[tuple[str, int]], rows: list[tuple[Any, ...]]
) -> None:
    """``CREATE TABLE ... AS`` over literal rows, every cell cast.

    Casting every cell, not just the first leg's, keeps each engine from
    picking its own supertype for a column whose first value is NULL.
    """
    dialect = DialectRegistry.get(target.dialect)
    types = TYPES[target.dialect]
    schema = SCHEMAS.get(target.dialect)
    ref = dialect.quote_identifier(table)
    if schema:
        ref = f"{dialect.quote_identifier(schema)}.{ref}"
    legs = " UNION ALL ".join(
        "SELECT "
        + ", ".join(
            f"{_literal(value, types[type_index])} AS {dialect.quote_identifier(name)}"
            for value, (name, type_index) in zip(row, columns, strict=True)
        )
        for row in rows
    )
    engine = " ENGINE = Memory" if target.dialect == "clickhouse" else ""
    for statement in (f"DROP TABLE IF EXISTS {ref}", f"CREATE TABLE {ref}{engine} AS {legs}"):
        # DDL returns no cursor description for the fixture to read.
        with contextlib.suppress(TypeError):
            target.execute(statement)


def _prepare(target: VendorTarget) -> SemanticModel:
    _create(target, ORDERS, [("customer_id", 0), ("country", 0), ("amount", 1)], ORDER_ROWS)
    _create(target, SHIPMENTS, [("customer_id", 0), ("units", 2)], SHIPMENT_ROWS)
    return _model(target)


def _run(target: VendorTarget, model: SemanticModel, query: QueryObject) -> list[dict[str, Any]]:
    rows = target.execute(CompilationPipeline().compile(query, model, target.dialect).sql)
    # Result keys are the aliases the model declares, but engines differ on
    # case, so they are matched insensitively.
    return [{str(k).lower(): v for k, v in row.items()} for row in rows]


def _number(value: Any) -> Decimal | None:
    """A cell as a Decimal, from whatever numeric type the driver hands back."""
    return None if value is None else Decimal(str(value))


def _by_country(
    target: VendorTarget, model: SemanticModel, measures: list[str]
) -> dict[str | None, tuple[Decimal | None, ...]]:
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Country"], measures=measures)),
    )
    return {row["country"]: tuple(_number(row[m.lower()]) for m in measures) for row in rows}


def _assert_grouped(target: VendorTarget, model: SemanticModel) -> None:
    """Every aggregation, including the NULL country, which is a group of its own."""
    measures = [
        "Revenue",
        "Avg Revenue per Customer",
        "Best Customer Revenue",
        "Customers",
        "Avg Orders per Customer",
    ]
    got = _by_country(target, model, measures)
    # Decimal equality ignores trailing zeros, so 27 and 27.00 agree; 1.33 is
    # only reached if the second stage is cast to the model's decimal type.
    want = {
        "DE": (81, 27, 50, 3, Decimal("1.33")),
        "FR": (1005, Decimal("502.5"), 1000, 2, 1),
        None: (40, 20, 30, 2, Decimal("1.5")),
    }
    assert got == {k: tuple(Decimal(v) for v in vs) for k, vs in want.items()}, (
        f"{target.name}: {got}"
    )


def _assert_ungrouped(target: VendorTarget, model: SemanticModel) -> None:
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(measures=["Avg Revenue per Customer", "Customers"])),
    )
    # (30 + 50 + 1 + 5 + 1000 + 10 + 30) / 7 customers = 160.857...
    got = [(_number(r["avg revenue per customer"]), _number(r["customers"])) for r in rows]
    assert got == [(Decimal("160.86"), Decimal(7))], f"{target.name}: {got}"


def _assert_having_order_limit(target: VendorTarget, model: SemanticModel) -> None:
    """HAVING runs over the final rows, and ORDER BY still binds out there."""
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Country"], measures=["Avg Revenue per Customer", "Customers"]
            ),
            having=[QueryFilter(field="Customers", op=">=", value=3)],
            order_by=[QueryOrderBy(field="Avg Revenue per Customer", direction="desc")],
            limit=1,
        ),
    )
    # Only DE has three customers; without the HAVING, FR would lead.
    got = [(r["country"], _number(r["avg revenue per customer"])) for r in rows]
    assert got == [("DE", Decimal(27))], f"{target.name}: {got}"


def _assert_derived(target: VendorTarget, model: SemanticModel) -> None:
    got = _by_country(target, model, ["Avg Revenue Share"])
    rounded = {k: (round(v[0], 6) if v[0] is not None else None) for k, v in got.items()}
    want = {"DE": Decimal("0.333333"), "FR": Decimal("0.5"), None: Decimal("0.5")}
    assert rounded == want, f"{target.name}: {got}"


def _assert_exact_integer_average(target: VendorTarget, model: SemanticModel) -> None:
    measures = ["Avg Units Sum", "Avg Units Min"]
    rows = _run(target, model, QueryObject(select=QuerySelect(measures=measures)))
    got = [tuple(_number(r[m.lower()]) for m in measures) for r in rows]
    want = Decimal("9007199254740990.5")
    assert got == [(want, want)], f"{target.name}: {got}"


def _assert_all(target: VendorTarget) -> None:
    model = _prepare(target)
    _assert_grouped(target, model)
    _assert_ungrouped(target, model)
    _assert_having_order_limit(target, model)
    _assert_derived(target, model)
    _assert_exact_integer_average(target, model)


def test_duckdb_reaggregate(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_reaggregate(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_reaggregate(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_reaggregate(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_snowflake_reaggregate(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_bigquery_reaggregate(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_databricks_reaggregate(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)


def test_dremio_reaggregate(vendor_dremio: VendorTarget) -> None:
    _assert_all(vendor_dremio)
