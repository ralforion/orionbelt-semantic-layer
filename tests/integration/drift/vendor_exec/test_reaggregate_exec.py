"""Reaggregate metrics compiled and run on each of the eight engines.

``tests/integration/test_reaggregate_execution.py`` checks the arithmetic on
DuckDB. The two-stage plan leans on things each engine spells its own way: the
NULL-safe join back to the query grain, the CROSS JOIN when there is no
dimension, the cast of the second-stage result, the HAVING wrapper, and the
exact integer AVG, which takes a different route on almost every engine. So
the same answers, worked out by hand, are asserted on all of them. A ``per``
with a time grain adds each engine's truncation, beside the query's own bucket
of the same date. Beside each of the other wrappers - a total, a filterContext,
a cumulative, period-over-period and window metric - the reaggregate pass wraps
what they built, which each engine has to accept as one query.

The rows are built per engine rather than read from the corpus seed: the seed
has no NULL group and no integers past a double's mantissa, and Dremio has no
seed at all.

Per customer:  DE c1 = 10 + 20 = 30, DE c2 = 50, DE c7 = 1,
               FR c3 = 5, FR c6 = 1000,
               NULL c4 = 7 + 3 = 10, NULL c5 = 30.
Per day:       Jan 5 = 30, Jan 20 = 50 + 5 = 55,
               Feb 3 = 1 + 1000 = 1001, Feb 10 = 10, Feb 28 = 30.
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

#: (string, double, bigint, date) as each engine spells them in a CAST.
TYPES: dict[str, tuple[str, str, str, str]] = {
    "duckdb": ("VARCHAR", "DOUBLE", "BIGINT", "DATE"),
    "postgres": ("TEXT", "DOUBLE PRECISION", "BIGINT", "DATE"),
    "mysql": ("CHAR(8)", "DOUBLE", "SIGNED", "DATE"),
    "clickhouse": ("Nullable(String)", "Float64", "Int64", "Date"),
    "snowflake": ("VARCHAR", "DOUBLE", "BIGINT", "DATE"),
    "bigquery": ("STRING", "FLOAT64", "INT64", "DATE"),
    "databricks": ("STRING", "DOUBLE", "BIGINT", "DATE"),
    "dremio": ("VARCHAR", "DOUBLE", "BIGINT", "DATE"),
}

ORDER_ROWS: list[tuple[str, str | None, int, str]] = [
    ("c1", "DE", 10, "2026-01-05"),
    ("c1", "DE", 20, "2026-01-05"),
    ("c2", "DE", 50, "2026-01-20"),
    ("c7", "DE", 1, "2026-02-03"),
    ("c3", "FR", 5, "2026-01-20"),
    ("c6", "FR", 1000, "2026-02-03"),
    ("c4", None, 7, "2026-02-10"),
    ("c4", None, 3, "2026-02-10"),
    ("c5", None, 30, "2026-02-28"),
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
      Order Date: {{code: order_date, abstractType: date}}
  Shipments:
    code: {shipments}
    schema: '{schema}'
    columns:
      Customer: {{code: customer_id, abstractType: string}}
      Units: {{code: units, abstractType: int}}
dimensions:
  Customer: {{dataObject: Orders, column: Customer, resultType: string}}
  Country: {{dataObject: Orders, column: Country, resultType: string}}
  Order Date: {{dataObject: Orders, column: Order Date, resultType: date}}
  Order Month: {{dataObject: Orders, column: Order Date, resultType: date, timeGrain: month}}
  Shipment Customer: {{dataObject: Shipments, column: Customer, resultType: string}}
measures:
  Revenue:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
  Last Customer:
    columns: [{{dataObject: Orders, column: Customer}}]
    aggregation: max
  Revenue Total:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    total: true
  Revenue All Countries:
    columns: [{{dataObject: Orders, column: Amount}}]
    aggregation: sum
    filterContext: {{mode: RELATIVE, exclude: [Country]}}
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
  Avg Daily Revenue:
    type: reaggregate
    measure: Revenue
    per: ['Order Date:day']
    aggregation: avg
  Avg Monthly Revenue:
    type: reaggregate
    measure: Revenue
    per: ['Order Date:month']
    aggregation: avg
  Avg Revenue per Month and Day:
    type: reaggregate
    measure: Revenue
    per: ['Order Date:month', 'Order Date:day']
    aggregation: avg
  Named Customers:
    type: reaggregate
    measure: Last Customer
    per: [Customer]
    aggregation: count
  Named Customers Doubled:
    expression: '{{[Named Customers]}} * 2'
  Running Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Month
  Revenue MoM:
    type: period_over_period
    expression: '{{[Revenue]}}'
    periodOverPeriod:
      timeDimension: Order Month
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
  Revenue Rank:
    type: window
    measure: Revenue
    windowFunction: rank
    orderDirection: desc
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
    _create(
        target,
        ORDERS,
        [("customer_id", 0), ("country", 0), ("amount", 1), ("order_date", 3)],
        ORDER_ROWS,
    )
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


def _assert_time_grained_per(target: VendorTarget, model: SemanticModel) -> None:
    """A ``per`` bucket finer than the query's bucket of the same date.

    Stage 1 groups by both truncations of one column, which the engine has to
    keep apart; only days with an order count, so January averages two.
    """
    by_month = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(dimensions=["Order Date:month"], measures=["Avg Daily Revenue"])
        ),
    )
    # Engines hand a month back as a date, a datetime or a string.
    got = {str(r["order date"])[:7]: _number(r["avg daily revenue"]) for r in by_month}
    want = {"2026-01": Decimal("42.5"), "2026-02": Decimal(347)}
    assert got == want, f"{target.name}: {got}"

    by_year = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(dimensions=["Order Date:year"], measures=["Avg Monthly Revenue"])
        ),
    )
    # (85 + 1041) / 2 months.
    got = {str(r["order date"])[:4]: _number(r["avg monthly revenue"]) for r in by_year}
    assert got == {"2026": Decimal(563)}, f"{target.name}: {got}"

    total = _run(target, model, QueryObject(select=QuerySelect(measures=["Avg Daily Revenue"])))
    # 1126 over five days.
    got_total = [_number(r["avg daily revenue"]) for r in total]
    assert got_total == [Decimal("225.2")], f"{target.name}: {got_total}"


def _assert_two_per_buckets_of_one_date(target: VendorTarget, model: SemanticModel) -> None:
    """Two buckets of one date, neither in the query, each a column of its own.

    Days nest in months, so the groups are the days: per country DE has three
    (30, 50, 1), FR two (5, 1000), NULL two (10, 30).
    """
    got = _by_country(target, model, ["Avg Revenue per Month and Day"])
    want = {"DE": (Decimal(27),), "FR": (Decimal("502.5"),), None: (Decimal(20),)}
    assert got == want, f"{target.name}: {got}"
    rows = _run(
        target, model, QueryObject(select=QuerySelect(measures=["Avg Revenue per Month and Day"]))
    )
    got_total = [_number(r["avg revenue per month and day"]) for r in rows]
    assert got_total == [Decimal("225.2")], f"{target.name}: {got_total}"


def _assert_beside_other_wrappers(target: VendorTarget, model: SemanticModel) -> None:
    """Each wrapper's own answer, unchanged by the reaggregate pass after it.

    Revenue by month: Jan 85, Feb 1041. Per customer by month: Jan c1 30, c2 50,
    c3 5; Feb c7 1, c6 1000, c4 10, c5 30.
    """
    measures = ["Avg Daily Revenue", "Running Revenue", "Revenue MoM", "Revenue Rank"]
    rows = _run(
        target,
        model,
        QueryObject(select=QuerySelect(dimensions=["Order Month"], measures=measures)),
    )
    got = {str(r["order month"])[:7]: tuple(_number(r[m.lower()]) for m in measures) for r in rows}
    want = {
        "2026-01": (Decimal("42.5"), Decimal(85), None, Decimal(2)),
        "2026-02": (Decimal(347), Decimal(1126), Decimal(956), Decimal(1)),
    }
    assert got == want, f"{target.name}: {got}"

    total = _by_country(target, model, ["Avg Revenue per Customer", "Revenue Total"])
    want_total = {
        "DE": (Decimal(27), Decimal(1126)),
        "FR": (Decimal("502.5"), Decimal(1126)),
        None: (Decimal(20), Decimal(1126)),
    }
    assert total == want_total, f"{target.name}: {total}"

    # The WHERE reaches the reaggregate metric and not the filterContext one.
    measures = ["Avg Revenue per Customer", "Revenue All Countries"]
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(dimensions=["Order Month"], measures=measures),
            where=[QueryFilter(field="Country", op="=", value="DE")],
        ),
    )
    got = {str(r["order month"])[:7]: tuple(_number(r[m.lower()]) for m in measures) for r in rows}
    want = {"2026-01": (Decimal(40), Decimal(85)), "2026-02": (Decimal(1), Decimal(1041))}
    assert got == want, f"{target.name}: {got}"

    # A formula over the reaggregate metric, beside a cumulative one. January's
    # average is 85 / 3, read at the model's decimal(18, 2): 28.33 / 85.
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Order Month"], measures=["Avg Revenue Share", "Running Revenue"]
            )
        ),
    )
    got_share = {
        str(r["order month"])[:7]: (
            round(_number(r["avg revenue share"]) or Decimal(0), 6),
            _number(r["running revenue"]),
        )
        for r in rows
    }
    want_share = {
        "2026-01": (Decimal("0.333294"), Decimal(85)),
        "2026-02": (Decimal("0.25"), Decimal(1126)),
    }
    assert got_share == want_share, f"{target.name}: {got_share}"

    # The same formula selected before a period-over-period metric, which
    # rebuilds its projection in the order the measures were asked for.
    rows = _run(
        target,
        model,
        QueryObject(
            select=QuerySelect(
                dimensions=["Order Month"], measures=["Avg Revenue Share", "Revenue MoM"]
            )
        ),
    )
    got_pop = {
        str(r["order month"])[:7]: (
            round(_number(r["avg revenue share"]) or Decimal(0), 6),
            _number(r["revenue mom"]),
        )
        for r in rows
    }
    want_pop = {
        "2026-01": (Decimal("0.333294"), None),
        "2026-02": (Decimal("0.25"), Decimal(956)),
    }
    assert got_pop == want_pop, f"{target.name}: {got_pop}"

    # A count over a string measure, inside a formula, beside a total: the
    # placeholder the wrappers carry has the count's type, not the string's.
    named = _by_country(target, model, ["Named Customers Doubled", "Revenue Total"])
    want_named = {
        "DE": (Decimal(6), Decimal(1126)),
        "FR": (Decimal(4), Decimal(1126)),
        None: (Decimal(4), Decimal(1126)),
    }
    assert named == want_named, f"{target.name}: {named}"


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
    _assert_time_grained_per(target, model)
    _assert_two_per_buckets_of_one_date(target, model)
    _assert_beside_other_wrappers(target, model)
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
