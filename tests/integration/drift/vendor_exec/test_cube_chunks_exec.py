"""A cube past its engine's grouping-set limit, as a union of chunks, on each engine.

Snowflake takes 128 grouping sets in one ``GROUP BY`` (a cube of 7
dimensions), BigQuery and PostgreSQL 4096 (12): past that, the dialect
renders the cube as one query per chunk of its grouping sets
(``Dialect.compile_select``). Each engine's answer is compared with DuckDB's
own cube of the same rows; the engines without a limit answer theirs with a
``CUBE``. MySQL has none.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore
from tests.integration.drift.vendor_exec.conftest import VendorTarget
from tests.integration.drift.vendor_exec.test_federated_shapes_exec import TYPES

pytestmark = pytest.mark.docker

COLUMNS = 13
# Three rows, two values in each column: each grouping set has two or three
# groups. A column of one value would be a constant Dremio's optimizer folds,
# returning it where its rollup has NULL.
AMOUNTS = ["10.50", "2.25", "1.00"]
# The fewest dimensions past each engine's limit, else a cube of 8.
DIMENSIONS = {"snowflake": 8, "bigquery": 13, "postgres": 13}

MODEL = (
    "version: 1.0\ndataObjects:\n  Facts:\n    code: f\n    columns:\n"
    + "".join(f"      K{i}: {{code: k{i}, abstractType: string}}\n" for i in range(COLUMNS))
    + "      Amount: {code: a, abstractType: decimal, numClass: additive}\n"
    + "dimensions:\n"
    + "".join(
        f"  D{i}: {{dataObject: Facts, column: K{i}, resultType: string}}\n" for i in range(COLUMNS)
    )
    + "measures:\n  Total: {columns: [{dataObject: Facts, column: Amount}], aggregation: sum}\n"
)


def _on_rows(dialect: str, sql: str) -> str:
    """*sql* reading :data:`ROWS` as table ``f`` of this engine."""
    _, decimal, text, _ = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier
    body = " UNION ALL ".join(
        "SELECT "
        + ", ".join(
            [
                *(f"CAST('v{(r + i) % 2}' AS {text}) AS {q(f'k{i}')}" for i in range(COLUMNS)),
                f"CAST('{amount}' AS {decimal}) AS {q('a')}",
            ]
        )
        for r, amount in enumerate(AMOUNTS)
    )
    rows = f"WITH {q('f')} AS ({body})"
    return f"{rows},\n{sql.removeprefix('WITH ')}" if sql.startswith("WITH ") else f"{rows}\n{sql}"


def _canonical(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    def value(v: Any) -> Any:
        if isinstance(v, (Decimal, float)) and not isinstance(v, bool):
            return Decimal(str(v)).normalize()
        return v

    return sorted(
        (
            tuple(value(v) for _, v in sorted((k.lower(), v) for k, v in row.items()))
            for row in rows
        ),
        key=repr,
    )


def _assert_cube(target: VendorTarget, duckdb: VendorTarget) -> None:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    n = DIMENSIONS.get(target.dialect, 8)
    query = QueryObject.model_validate(
        {
            "select": {
                "dimensions": [f"D{i}" for i in range(n)],
                "measures": ["Total", "Facts Count"],
            },
            "grouping": "cube",
        }
    )
    sql = store.compile_query(model_id, query, target.dialect).sql
    expected = duckdb.execute(
        _on_rows("duckdb", store.compile_query(model_id, query, "duckdb").sql)
    )

    rows = target.execute(_on_rows(target.dialect, sql))

    assert ("GROUPING SETS" in sql) == (target.dialect in DIMENSIONS)
    assert len(expected) == len(rows) > 2**n
    assert _canonical(rows) == _canonical(expected)


@pytest.mark.parametrize(
    "vendor", ["duckdb", "postgres", "clickhouse", "snowflake", "bigquery", "databricks", "dremio"]
)
def test_a_cube_past_the_limit_answers_as_duckdb(
    vendor: str, request: pytest.FixtureRequest, vendor_duckdb: VendorTarget
) -> None:
    _assert_cube(request.getfixturevalue(f"vendor_{vendor}"), vendor_duckdb)
