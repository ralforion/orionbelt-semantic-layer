"""Schema inspection on a real ClickHouse: the catalog SQL itself, not only its rows.

The unit tests read ClickHouse's catalog through a stand-in runner, which
cannot tell whether a statement asks the right question. Here the statements
run on a ClickHouse container, through ``inspect_schema``'s ``run`` hook.

Opt-in, requires Docker::

    uv run pytest -m docker tests/integration/test_schema_inspection_clickhouse.py
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

pytest.importorskip("ob_flight")
pytest.importorskip("testcontainers.clickhouse", reason="testcontainers[clickhouse] required")
clickhouse_connect = pytest.importorskip("clickhouse_connect", reason="clickhouse-connect required")

from ob_flight.datasources import DataSource  # noqa: E402
from testcontainers.clickhouse import ClickHouseContainer  # noqa: E402

from orionbelt.service.schema_inspection import inspect_schema  # noqa: E402

pytestmark = pytest.mark.docker

Runner = Callable[[str], list[dict[str, Any]]]

SHOP_DDL = (
    "CREATE DATABASE shop",
    # The key's columns in another order than the table's.
    "CREATE TABLE shop.orders (a UInt32, b String, note LowCardinality(Nullable(String)), "
    "qty Nullable(Int32), tags Array(Nullable(String))) ENGINE = MergeTree ORDER BY (b, a)",
    # A primary key shorter than the sorting key.
    "CREATE TABLE shop.prefix (a UInt32, b UInt32, c UInt32) "
    "ENGINE = MergeTree PRIMARY KEY (c, a) ORDER BY (c, a, b)",
    # Expressions over columns, one of them over two.
    "CREATE TABLE shop.events (ts DateTime, id UInt64, x String) "
    "ENGINE = MergeTree ORDER BY (toDate(ts), id, cityHash64(x, id))",
    # A name ClickHouse backquotes in the key.
    "CREATE TABLE shop.quoted (a UInt32, `my col` UInt32) "
    "ENGINE = MergeTree ORDER BY (`my col`, a)",
    "CREATE TABLE shop.keyless (a UInt32) ENGINE = MergeTree ORDER BY tuple()",
    # A string literal holding a column's name.
    "CREATE TABLE shop.literal (a String, b UInt32) "
    "ENGINE = MergeTree ORDER BY (concat('b', a), b)",
    # Names quoted and escaped inside a literal.
    "CREATE TABLE shop.literal_quotes (a String, b UInt32, `my col` UInt8) "
    "ENGINE = MergeTree ORDER BY (concat('it\\'s `b` and \"b\"', a), `my col`, b)",
    # A number's exponent written like a column's name (1e100 stays 1e100).
    "CREATE TABLE shop.exponent (e100 UInt32, b UInt32) "
    "ENGINE = MergeTree ORDER BY (1e100 + b, e100)",
    # A function named like a column.
    "CREATE TABLE shop.function (`toDate` Date, ts DateTime) "
    "ENGINE = MergeTree ORDER BY (toDate(ts), `toDate`)",
)

SOURCE = DataSource(name="clickhouse", dialect="clickhouse", environ={})


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def run() -> Iterator[Runner]:
    if not _docker_available():
        pytest.skip("Docker is not running")

    with ClickHouseContainer("clickhouse/clickhouse-server:latest") as ch:
        client = clickhouse_connect.get_client(
            host=ch.get_container_host_ip(),
            port=int(ch.get_exposed_port(8123)),
            username=ch.username,
            password=ch.password,
        )
        for statement in SHOP_DDL:
            client.command(statement)

        def run_sql(sql: str) -> list[dict[str, Any]]:
            result = client.query(sql)
            names = [name.lower() for name in result.column_names]
            return [dict(zip(names, row, strict=True)) for row in result.result_rows]

        yield run_sql
        client.close()


def test_a_low_cardinality_nullable_column_is_nullable(run: Runner) -> None:
    info = inspect_schema(SOURCE, "shop", tables=["orders"], run=run)

    assert info is not None
    assert [(c.name, c.nullable) for c in info.tables[0].columns] == [
        ("a", False),
        ("b", False),
        ("note", True),
        ("qty", True),
        ("tags", False),
    ]


def test_primary_keys_in_the_order_the_table_declares(run: Runner) -> None:
    info = inspect_schema(SOURCE, "shop", run=run)

    assert info is not None
    assert {t.name: t.primary_key for t in info.tables} == {
        "events": ["ts", "id", "x"],
        "exponent": ["b", "e100"],
        "function": ["ts", "toDate"],
        "keyless": [],
        "literal": ["a", "b"],
        "literal_quotes": ["a", "my col", "b"],
        "orders": ["b", "a"],
        "prefix": ["c", "a"],
        "quoted": ["my col", "a"],
    }


def test_a_table_filter_keeps_the_key_order(run: Runner) -> None:
    info = inspect_schema(SOURCE, "shop", tables=["orders", "quoted"], run=run)

    assert info is not None
    assert [(t.name, t.primary_key) for t in info.tables] == [
        ("orders", ["b", "a"]),
        ("quoted", ["my col", "a"]),
    ]


def test_a_filter_matching_nothing_tells_an_unknown_schema_from_a_known_one(
    run: Runner,
) -> None:
    assert inspect_schema(SOURCE, "nowhere", tables=["missing"], run=run) is None
    known = inspect_schema(SOURCE, "shop", tables=["missing"], run=run)
    assert known is not None and known.tables == []
