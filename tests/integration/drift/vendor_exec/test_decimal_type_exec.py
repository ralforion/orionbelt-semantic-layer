"""``decimal`` renders as a type every engine accepts, and keeps 0.1 + 0.2 exact.

Where an abstract type becomes a SQL type of its own (a typed NULL pad, a
nested field), ``float`` is the engine's binary float and 0.1 + 0.2 comes back
0.30000000000000004. ``decimal`` has to be an exact type the engine accepts in
a ``CAST``, and a NULL cast to it has to union with a value of it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from orionbelt.ast.nodes import BinaryOp, Cast, Literal
from orionbelt.dialect.registry import DialectRegistry

from .conftest import VendorTarget

pytestmark = pytest.mark.docker


def _assert_exact(target: VendorTarget) -> None:
    dialect = DialectRegistry.get(target.dialect)

    def as_decimal(value: Literal) -> Cast:
        return Cast(expr=value, type_name="decimal")

    total = dialect.compile_expr(
        BinaryOp(as_decimal(Literal.string("0.1")), "+", as_decimal(Literal.string("0.2")))
    )
    pad = dialect.compile_expr(as_decimal(Literal.null()))
    v = dialect.quote_identifier("v")
    rows = target.execute(f"SELECT {pad} AS {v} UNION ALL SELECT {total} AS {v}")
    values = {
        None if value is None else Decimal(str(value)) for row in rows for value in row.values()
    }
    assert values == {None, Decimal("0.3")}, f"{target.name}: {values}"


def test_duckdb_decimal_type(vendor_duckdb: VendorTarget) -> None:
    _assert_exact(vendor_duckdb)


def test_postgres_decimal_type(vendor_postgres: VendorTarget) -> None:
    _assert_exact(vendor_postgres)


def test_mysql_decimal_type(vendor_mysql: VendorTarget) -> None:
    _assert_exact(vendor_mysql)


def test_clickhouse_decimal_type(vendor_clickhouse: VendorTarget) -> None:
    _assert_exact(vendor_clickhouse)


def test_bigquery_decimal_type(vendor_bigquery: VendorTarget) -> None:
    _assert_exact(vendor_bigquery)


def test_snowflake_decimal_type(vendor_snowflake: VendorTarget) -> None:
    _assert_exact(vendor_snowflake)


def test_databricks_decimal_type(vendor_databricks: VendorTarget) -> None:
    _assert_exact(vendor_databricks)


def test_dremio_decimal_type(vendor_dremio: VendorTarget) -> None:
    _assert_exact(vendor_dremio)
