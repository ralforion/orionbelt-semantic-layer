"""A rollup or cube of a coalesced dimension, on every engine.

Beside two facts, a dimension coalesced from each fact's own
(``{coalesce: [Sales Region, Purchase Region], as: Region}``) is grouped by
the ``COALESCE`` of the two, and its ``GROUPING()`` flag named it by its
alias: PostgreSQL, DuckDB and MySQL refuse an alias there. Dremio read it,
until each key was returned through its flag (``CASE WHEN GROUPING(key) = 0
THEN key END``): the alias then named an expression holding ``GROUPING()``,
which Dremio refuses too. The flag now reads the key.

Rolled up or limited, a query is sorted by its dimensions: a coalesced one by
its alias, where its members, not in the outer query, were refused.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget
from .test_rollup_filter_context_exec import TYPES

pytestmark = pytest.mark.docker

MODEL = """\
version: 1.0
dataObjects:
  Sales:
    code: s
    columns:
      Region: {code: region, abstractType: string}
      Amount: {code: amount, abstractType: int}
  Purchases:
    code: p
    columns:
      Region: {code: region, abstractType: string}
      Cost: {code: cost, abstractType: int}
dimensions:
  Sales Region: {dataObject: Sales, column: Region, resultType: string}
  Purchase Region: {dataObject: Purchases, column: Region, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum}
  Spend: {columns: [{dataObject: Purchases, column: Cost}], aggregation: sum}
"""

# region, amount; region, cost: n has both, s sales only, e purchases only.
SALES: list[tuple[str, int]] = [("n", 10), ("n", 20), ("s", 40)]
PURCHASES: list[tuple[str, int]] = [("n", 5), ("e", 7)]
REGION = {"coalesce": ["Sales Region", "Purchase Region"], "as": "Region"}
# None, and the constant Dremio's planner carried through a rollup.
FILTERS: list[list[dict[str, Any]]] = [
    [],
    [{"field": "Sales Region", "op": "=", "value": "n"}],
]


def _with_rows(dialect: str, sql: str) -> str:
    """*sql*, a ``WITH`` query, with the facts' rows as CTEs ``s`` and ``p``."""
    whole, text = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def rows(values: list[tuple[str, int]], number: str) -> str:
        return " UNION ALL ".join(
            f"SELECT CAST('{region}' AS {text}) AS {q('region')}, "
            f"CAST('{value}' AS {whole}) AS {q(number)}"
            for region, value in values
        )

    assert sql.startswith("WITH ")
    sales, purchases = rows(SALES, "amount"), rows(PURCHASES, "cost")
    return f"WITH {q('s')} AS ({sales}), {q('p')} AS ({purchases}), {sql[len('WITH ') :]}"


def _sum(values: list[int]) -> Decimal | None:
    return Decimal(sum(values)) if values else None


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _rows(
    target: VendorTarget, store: ModelStore, model_id: str, raw: dict[str, Any]
) -> list[dict[str, Any]]:
    """The rows of query *raw* on *target*, their column names in lower case."""
    sql = store.compile_query(model_id, QueryObject.model_validate(raw), target.dialect).sql
    rows = target.execute(_with_rows(target.dialect, sql))
    return [{k.lower(): v for k, v in row.items()} for row in rows]


def _assert_coalesced(target: VendorTarget) -> None:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    select = {"dimensions": [REGION], "measures": ["Revenue", "Spend"]}
    wrong = []
    for grouping in ["rollup"] if target.dialect == "mysql" else ["rollup", "cube"]:
        for where in FILTERS:
            rows = _rows(
                target, store, model_id, {"select": select, "where": where, "grouping": grouping}
            )
            # A filter on the sales' region filters the sales; the purchases,
            # which do not reach it, are all counted.
            sales = [r for r in SALES if not where or r[0] == "n"]
            purchases = PURCHASES
            expected = {
                (region, 0): (
                    _sum([a for r, a in sales if r == region]),
                    _sum([c for r, c in purchases if r == region]),
                )
                for region in {r for r, _ in sales} | {r for r, _ in purchases}
            }
            expected[(None, 1)] = (_sum([a for _, a in sales]), _sum([c for _, c in purchases]))
            got = {
                (row["region"], int(row["_g_region"])): (
                    _number(row["revenue"]),
                    _number(row["spend"]),
                )
                for row in rows
            }
            # Sorted by the region, the grand total first.
            order = [None, *sorted(region for region, flag in expected if not flag)]
            regions = [row["region"] for row in rows]
            if got != expected or regions != order:
                wrong.append(f"{grouping} {where}: {got} != {expected}, {regions} != {order}")
    # Limited, the first two regions.
    rows = _rows(target, store, model_id, {"select": select, "limit": 2})
    got_rows = [(row["region"], _number(row["revenue"]), _number(row["spend"])) for row in rows]
    if got_rows != [("e", None, Decimal(7)), ("n", Decimal(30), Decimal(5))]:
        wrong.append(f"limit 2: {got_rows}")
    assert not wrong, f"{target.name}:\n" + "\n".join(wrong)


def test_duckdb_rollup_coalesced(vendor_duckdb: VendorTarget) -> None:
    _assert_coalesced(vendor_duckdb)


def test_postgres_rollup_coalesced(vendor_postgres: VendorTarget) -> None:
    _assert_coalesced(vendor_postgres)


def test_mysql_rollup_coalesced(vendor_mysql: VendorTarget) -> None:
    _assert_coalesced(vendor_mysql)


def test_clickhouse_rollup_coalesced(vendor_clickhouse: VendorTarget) -> None:
    _assert_coalesced(vendor_clickhouse)


def test_snowflake_rollup_coalesced(vendor_snowflake: VendorTarget) -> None:
    _assert_coalesced(vendor_snowflake)


def test_bigquery_rollup_coalesced(vendor_bigquery: VendorTarget) -> None:
    _assert_coalesced(vendor_bigquery)


def test_databricks_rollup_coalesced(vendor_databricks: VendorTarget) -> None:
    _assert_coalesced(vendor_databricks)


def test_dremio_rollup_coalesced(vendor_dremio: VendorTarget) -> None:
    _assert_coalesced(vendor_dremio)
