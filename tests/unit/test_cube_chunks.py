"""A cube of more grouping sets than its engine takes, as a union of chunks of them.

Snowflake takes 128 grouping sets in one ``GROUP BY`` (a cube of 7
dimensions), BigQuery and PostgreSQL 4096 (``Dialect.max_grouping_sets``). Past that,
the dialect renders the cube as one query per chunk of its grouping sets,
each grouped by ``GROUPING SETS``, and sorts the union. Here DuckDB takes 4
for the test, so a cube of 3 dimensions (8 sets) is two chunks, compared with
DuckDB's own cube.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from orionbelt.ast.nodes import (
    AliasedExpr,
    ColumnRef,
    From,
    FunctionCall,
    OrderByItem,
    Select,
    WindowFunction,
)
from orionbelt.dialect.base import UnsupportedGroupingError
from orionbelt.dialect.duckdb import DuckDBDialect
from orionbelt.dialect.registry import DialectRegistry
from tests.unit.test_federation_planner import _compile, _rows, model_yaml
from tests.unit.test_federation_planner import files as files  # noqa: F401

TENANT = "  Tenant: {dataObject: Customers, column: Tenant, resultType: string}\n"
CUSTOMERS_COUNT = (
    "  Order Customers: {columns: [{dataObject: Orders, column: Order Customer}], "
    "aggregation: count_distinct, resultType: int}\n"
)
BY_THREE = ["Region", "Tenant", "Order Date:month"]
QUERIES: dict[str, dict[str, Any]] = {
    "a_cube": {"select": {"dimensions": BY_THREE, "measures": ["Revenue", "Orders Count"]}},
    "sorted_limited_and_offset": {
        "select": {"dimensions": BY_THREE, "measures": ["Revenue"]},
        "orderBy": [
            {"field": "Revenue", "direction": "desc"},
            {"field": "Region"},
            {"field": "Tenant"},
            {"field": "Order Date"},
        ],
        "limit": 5,
        "offset": 2,
    },
    "with_having": {
        "select": {"dimensions": BY_THREE, "measures": ["Revenue", "Orders Count"]},
        "having": [{"field": "Orders Count", "op": ">", "value": 1}],
    },
    "with_a_metric_and_a_distinct_count": {
        "select": {
            "dimensions": BY_THREE,
            "measures": ["Revenue per Order", "Order Customers"],
        }
    },
    "with_a_customer_measure": {
        "select": {"dimensions": BY_THREE, "measures": ["Revenue", "Customers Count"]}
    },
    "filtered": {
        "select": {"dimensions": BY_THREE, "measures": ["Revenue"]},
        "where": [{"field": "Order Date", "op": ">=", "value": "2024-02-01"}],
    },
}


def _taking(monkeypatch: pytest.MonkeyPatch, sets: int) -> None:
    """DuckDB taking *sets* grouping sets in one ``GROUP BY``."""
    monkeypatch.setattr(DuckDBDialect, "_MAX_GROUPING_SETS", sets)


@pytest.fixture
def chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    _taking(monkeypatch, 4)


def _model() -> str:
    return model_yaml(federated=False, dimension=TENANT, extra=CUSTOMERS_COUNT)


def _run(paths: dict[str, str], query: dict[str, Any]) -> list[Any]:
    con = duckdb.connect(paths["all"], read_only=True)
    try:
        rows = _rows(con, _compile(_model(), query).sql)
    finally:
        con.close()
    return rows if query.get("orderBy") else sorted(rows, key=repr)


@pytest.mark.parametrize("query", QUERIES.values(), ids=QUERIES.keys())
def test_chunks_return_what_the_cube_returns(
    files: dict[str, str], monkeypatch: pytest.MonkeyPatch, query: dict[str, Any]
) -> None:
    query = {**query, "grouping": "cube"}
    native = _run(files, query)

    _taking(monkeypatch, 4)

    assert "GROUPING SETS" in _compile(_model(), query).sql
    assert _run(files, query) == native
    assert native


@pytest.mark.usefixtures("chunked")
def test_a_cube_of_two_chunks_is_a_union_of_two_queries() -> None:
    sql = _compile(_model(), {**QUERIES["a_cube"], "grouping": "cube"}).sql

    assert sql.count("GROUP BY GROUPING SETS") == 2
    assert sql.count("UNION ALL") == 1
    assert "CUBE(" not in sql


@pytest.mark.usefixtures("chunked")
def test_a_cube_within_the_limit_stays_a_cube() -> None:
    query = {
        "select": {"dimensions": ["Region", "Tenant"], "measures": ["Revenue"]},
        "grouping": "cube",
    }

    sql = _compile(_model(), query).sql

    assert "CUBE(" in sql
    assert "GROUPING SETS" not in sql


@pytest.mark.usefixtures("chunked")
def test_a_rollup_stays_a_rollup() -> None:
    sql = _compile(_model(), {**QUERIES["a_cube"], "grouping": "rollup"}).sql

    assert "ROLLUP(" in sql
    assert "GROUPING SETS" not in sql


@pytest.mark.usefixtures("chunked")
def test_a_window_over_the_cube_is_refused() -> None:
    """A rank over the cube's rows would rank each chunk's rows alone."""
    a, b, c = (ColumnRef(name=name, table="t") for name in "abc")
    node = Select(
        columns=[
            *(AliasedExpr(expr=key, alias=key.name) for key in (a, b, c)),
            AliasedExpr(
                expr=WindowFunction(func_name="RANK", order_by=[OrderByItem(expr=a)]), alias="r"
            ),
        ],
        from_=From(source="t", alias="t"),
        group_by=[a, b, c],
        grouping="cube",
    )

    with pytest.raises(UnsupportedGroupingError):
        DialectRegistry.get("duckdb").compile_select(node)


A, B, C = (ColumnRef(name=name, table="t") for name in "abc")
TOTAL = FunctionCall(name="SUM", args=[ColumnRef(name="v", table="t")])
ROWS_T = (
    "CREATE TABLE t AS SELECT * FROM (VALUES ('x', 'p', 1, 1.0), ('x', 'q', 2, 2.0),"
    " ('y', 'p', 2, 40.0), ('y', 'q', 1, 195.0)) AS r(a, b, c, v)"
)


def _cube(columns: list[Any], **select: Any) -> Select:
    return Select(
        columns=columns,
        from_=From(source="t", alias="t"),
        group_by=[A, B, C],
        grouping="cube",
        **select,
    )


def _on_t(node: Select) -> list[tuple[Any, ...]]:
    con = duckdb.connect()
    try:
        con.execute(ROWS_T)
        return [
            tuple(r)
            for r in con.execute(DialectRegistry.get("duckdb").compile_select(node)).fetchall()
        ]
    finally:
        con.close()


def test_a_sort_key_takes_a_name_no_column_has(monkeypatch: pytest.MonkeyPatch) -> None:
    """A column named as the sort key's helper would have been sorted by instead."""
    node = _cube(
        [
            AliasedExpr(expr=A, alias="__cube_sort0"),
            AliasedExpr(expr=B, alias="b"),
            AliasedExpr(expr=C, alias="c"),
            AliasedExpr(expr=TOTAL, alias="total"),
        ],
        order_by=[OrderByItem(expr=TOTAL, desc=True)],
        limit=3,
    )
    native = _on_t(node)

    _taking(monkeypatch, 4)

    assert _on_t(node) == native
    assert native[0][3] == 238


def test_distinct_rows_are_distinct_across_the_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each chunk's rows were distinct, the union's were not."""
    node = _cube([AliasedExpr(expr=A, alias="a")], distinct=True)
    native = sorted(_on_t(node), key=repr)

    _taking(monkeypatch, 4)

    assert sorted(_on_t(node), key=repr) == native == [("x",), ("y",), (None,)]


@pytest.mark.usefixtures("chunked")
def test_a_window_in_a_sort_key_is_refused() -> None:
    node = _cube(
        [AliasedExpr(expr=A, alias="a"), AliasedExpr(expr=TOTAL, alias="total")],
        order_by=[
            OrderByItem(expr=WindowFunction(func_name="RANK", order_by=[OrderByItem(expr=TOTAL)]))
        ],
    )

    with pytest.raises(UnsupportedGroupingError):
        DialectRegistry.get("duckdb").compile_select(node)


@pytest.mark.usefixtures("chunked")
def test_a_projection_naming_no_column_is_refused() -> None:
    """``SELECT *`` over the chunks: the union's projection would list nothing."""
    with pytest.raises(UnsupportedGroupingError):
        DialectRegistry.get("duckdb").compile_select(_cube([]))
