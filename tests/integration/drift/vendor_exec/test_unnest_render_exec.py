"""Every dialect's unnest rendering, executed against a real engine.

The shapes are not variations on one another - a comma-lateral, an ``ARRAY
JOIN``, a ``LATERAL VIEW``, a ``JSON_TABLE`` - so a rendering that looks right
is worth nothing until the engine parses it. This module runs each dialect's
own fragment against that dialect.

Two rows, the second carrying an empty array, so both halves of ``outer`` are
checked: the inner form drops that parent and the outer form keeps it. Empty
arrays are the common case rather than an edge one - 61% of the charges in a
real billing export carry no labels - which is why ``outer`` is the default.

Dremio is absent because it has no FROM-clause unnest at all: ``FLATTEN`` is a
projection function and needs a derived table, which the planner will build.
``render_unnest`` refuses there, and ``test_nested_data_objects`` pins that.
"""

from __future__ import annotations

import contextlib

import pytest

from orionbelt.ast.nodes import Unnest
from orionbelt.dialect.registry import DialectRegistry

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

#: How each engine spells "a two-row table with an array-of-struct column, the
#: second row's array empty". No portable literal exists for this, which is the
#: reason the nested work needs seeded fixtures rather than expression tests.
SOURCES = {
    "duckdb": """(SELECT 1 AS @ID@, 100 AS @COST@,
                         [{'Key':'team','Value':'core'},{'Key':'env','Value':'prod'}] AS @LABELS@
                  UNION ALL SELECT 2, 50, [])""",
    "postgres": """(SELECT 1 AS @ID@, 100 AS @COST@,
                           ARRAY[ROW('team','core'),ROW('env','prod')]::kvpair[] AS @LABELS@
                    UNION ALL SELECT 2, 50, ARRAY[]::kvpair[])""",
    "clickhouse": """(SELECT 1 AS @ID@, 100 AS @COST@,
                             CAST([('team','core'),('env','prod')] AS
                                  Array(Tuple(Key String, Value String))) AS @LABELS@
                      UNION ALL SELECT 2, 50,
                             CAST([] AS Array(Tuple(Key String, Value String))))""",
    "mysql": """(SELECT 1 AS @ID@, 100 AS @COST@,
                        CAST('[{"Key":"team","Value":"core"},
                                {"Key":"env","Value":"prod"}]' AS JSON) AS @LABELS@
                 UNION ALL SELECT 2, 50, CAST('[]' AS JSON))""",
    "bigquery": """(SELECT 1 AS @ID@, 100 AS @COST@,
                           [STRUCT('team' AS Key,'core' AS Value), STRUCT('env','prod')] AS @LABELS@
                    UNION ALL SELECT 2, 50, [])""",
    "snowflake": """(SELECT 1 AS @ID@, 100 AS @COST@,
                            ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('Key','team','Value','core'),
                                            OBJECT_CONSTRUCT('Key','env','Value','prod'))
                                AS @LABELS@
                     UNION ALL SELECT 2, 50, ARRAY_CONSTRUCT())""",
    "databricks": """(SELECT 1 AS @ID@, 100 AS @COST@,
                             array(named_struct('Key','team','Value','core'),
                                   named_struct('Key','env','Value','prod')) AS @LABELS@
                      UNION ALL SELECT 2, 50, array())""",
}

#: How the element's ``Key`` field is read once unnested. Snowflake hands back a
#: VARIANT, so it is the one engine that needs a path rather than a field.
KEY_EXPR = {
    "snowflake": '"L".value:Key::string',
    "clickhouse": '"L"."Key"',
    "duckdb": '"L"."Key"',
    "postgres": '"L"."Key"',
    "mysql": "`L`.`Key`",
    "bigquery": "`L`.`Key`",
    "databricks": "`L`.`Key`",
}


def _prepare(target: VendorTarget) -> None:
    """Postgres needs a composite type declared before an array of it exists."""
    if target.dialect != "postgres":
        return
    create = 'CREATE TYPE kvpair AS ("Key" text, "Value" text)'
    for stmt in ("DROP TYPE IF EXISTS kvpair CASCADE", create):
        # DDL returns no cursor description for the fixture to read
        with contextlib.suppress(TypeError):
            target.execute(stmt)


def _run(target: VendorTarget, outer: bool) -> list[dict]:
    dialect = DialectRegistry.get(target.dialect)
    node = Unnest(
        parent_alias="C",
        column="x_Labels",
        alias="L",
        columns=(("Key", "VARCHAR(64)"), ("Value", "VARCHAR(64)")),
        outer=outer,
    )
    fragment = dialect.render_unnest(node)
    q = dialect.quote_identifier
    source = (
        SOURCES[target.dialect]
        .replace("@ID@", q("id"))
        .replace("@COST@", q("cost"))
        .replace("@LABELS@", q("x_Labels"))
    )
    sql = (
        f"SELECT {q('C')}.{q('id')} AS {q('id')}, {KEY_EXPR[target.dialect]} AS {q('k')} "
        f"FROM {source} AS {q('C')} {fragment} ORDER BY 1, 2"
    )
    return target.execute(sql)


def _assert_both_forms(target: VendorTarget) -> None:
    _prepare(target)

    def ids(rows: list[dict]) -> list[int]:
        return [next(v for k, v in r.items() if k.lower() == "id") for r in rows]

    inner = _run(target, outer=False)
    assert ids(inner) == [1, 1], (
        f"{target.name}: the inner form should drop the parent whose array is empty: {inner}"
    )

    outer = _run(target, outer=True)
    assert ids(outer) == [1, 1, 2], (
        f"{target.name}: the outer form should keep it, with a NULL child: {outer}"
    )


def test_duckdb_unnest(vendor_duckdb: VendorTarget) -> None:
    _assert_both_forms(vendor_duckdb)


def test_postgres_unnest(vendor_postgres: VendorTarget) -> None:
    _assert_both_forms(vendor_postgres)


def test_mysql_unnest(vendor_mysql: VendorTarget) -> None:
    _assert_both_forms(vendor_mysql)


def test_clickhouse_unnest(vendor_clickhouse: VendorTarget) -> None:
    _assert_both_forms(vendor_clickhouse)


def test_bigquery_unnest(vendor_bigquery: VendorTarget) -> None:
    _assert_both_forms(vendor_bigquery)


def test_snowflake_unnest(vendor_snowflake: VendorTarget) -> None:
    _assert_both_forms(vendor_snowflake)


def test_databricks_unnest(vendor_databricks: VendorTarget) -> None:
    _assert_both_forms(vendor_databricks)
