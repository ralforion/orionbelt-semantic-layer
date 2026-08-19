"""A whole query over a nested data object, compiled and run on each engine.

``test_unnest_render_exec`` executes each dialect's unnest *fragment*. This
executes what the planner builds around it: the containment edge, the dedup CTE
that keeps a parent measure from counting a charge once per label, and the
projection that reads an element field the way its engine spells it. The
arithmetic is the assertion, because a rendering that parses can still answer
the wrong number - which is the whole reason the design plan spends its length
on multiplicities rather than on syntax.

Three charges, chosen so each behaviour appears exactly once:

===========  ======  ==========================  ====================
charge       cost    labels                      credits
===========  ======  ==========================  ====================
``c1``       100     ``team=prod``, ``env=prod`` two identical ``-5``
``c2``       100     ``team=prod``               one ``-1``
``c3``       50      *(empty)*                   *(empty)*
===========  ======  ==========================  ====================

``c1``'s two labels share a value, so a parent-side ``SUM`` counts it twice
without deduplication. Its two credits are byte-identical, so the same
deduplication applied to a nested-side ``SUM`` would lose one. ``c3`` has
neither array, so it only survives because the unnest is an outer one.

Dremio is absent: it has no FROM-clause unnest and reads the ``code`` fallback,
which is an ordinary join and needs no evidence of its own here.

**ClickHouse fills an unmatched row with the type's default rather than NULL** -
``''`` for a String, ``0`` for a Float - which ``DataObjectJoin.required``
already documents for an ordinary join. The helpers below normalise that one
difference and nothing else.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

from ._seed import SCHEMA as SEED_SCHEMA
from .conftest import VendorTarget

pytestmark = pytest.mark.docker

TABLE = "nested_charges"

#: The cloud engines run inside the seeded schema; the containers use their
#: connection's default, so the model qualifies nothing there.
QUALIFIED = {"bigquery", "snowflake", "databricks"}

#: One table per engine, built rather than inlined. Two dialects were nearly
#: written off during this work because a *literal* would not parse while the
#: engine itself was fine, so array-of-struct data is seeded as a real table.
DDL: dict[str, list[str]] = {
    "duckdb": [
        f"DROP TABLE IF EXISTS {TABLE}",
        f"CREATE TABLE {TABLE} (id VARCHAR, cost DOUBLE, "
        f'"Labels" STRUCT("Key" VARCHAR, "Value" VARCHAR)[], '
        f'"Credits" STRUCT("Type" VARCHAR, "Amount" DOUBLE)[])',
        f"""INSERT INTO {TABLE} VALUES
            ('c1', 100, [{{'Key':'team','Value':'prod'}}, {{'Key':'env','Value':'prod'}}],
                        [{{'Type':'CUD','Amount':-5}}, {{'Type':'CUD','Amount':-5}}]),
            ('c2', 100, [{{'Key':'team','Value':'prod'}}], [{{'Type':'SUD','Amount':-1}}]),
            ('c3',  50, [], [])""",
    ],
    "postgres": [
        f"DROP TABLE IF EXISTS {TABLE}",
        "DROP TYPE IF EXISTS ob_kv CASCADE",
        "DROP TYPE IF EXISTS ob_credit CASCADE",
        'CREATE TYPE ob_kv AS ("Key" text, "Value" text)',
        'CREATE TYPE ob_credit AS ("Type" text, "Amount" double precision)',
        f"CREATE TABLE {TABLE} (id text, cost double precision, "
        f'"Labels" ob_kv[], "Credits" ob_credit[])',
        f"""INSERT INTO {TABLE} VALUES
            ('c1', 100, ARRAY[ROW('team','prod')::ob_kv, ROW('env','prod')::ob_kv],
                        ARRAY[ROW('CUD',-5)::ob_credit, ROW('CUD',-5)::ob_credit]),
            ('c2', 100, ARRAY[ROW('team','prod')::ob_kv], ARRAY[ROW('SUD',-1)::ob_credit]),
            ('c3',  50, ARRAY[]::ob_kv[], ARRAY[]::ob_credit[])""",
    ],
    "mysql": [
        f"DROP TABLE IF EXISTS {TABLE}",
        f"CREATE TABLE {TABLE} (id VARCHAR(8), cost DOUBLE, `Labels` JSON, `Credits` JSON)",
        f"""INSERT INTO {TABLE} VALUES
            ('c1', 100,
             '[{{"Key":"team","Value":"prod"}},{{"Key":"env","Value":"prod"}}]',
             '[{{"Type":"CUD","Amount":-5}},{{"Type":"CUD","Amount":-5}}]'),
            ('c2', 100, '[{{"Key":"team","Value":"prod"}}]', '[{{"Type":"SUD","Amount":-1}}]'),
            ('c3',  50, '[]', '[]')""",
    ],
    "clickhouse": [
        f"DROP TABLE IF EXISTS {TABLE}",
        f"CREATE TABLE {TABLE} (id String, cost Float64, "
        f"`Labels` Array(Tuple(Key String, Value String)), "
        f"`Credits` Array(Tuple(Type String, Amount Float64))) ENGINE = Memory",
        f"""INSERT INTO {TABLE} VALUES
            ('c1', 100, [('team','prod'),('env','prod')], [('CUD',-5),('CUD',-5)]),
            ('c2', 100, [('team','prod')], [('SUD',-1)]),
            ('c3',  50, [], [])""",
    ],
    "bigquery": [
        f"DROP TABLE IF EXISTS `{SEED_SCHEMA}`.`{TABLE}`",
        f"""CREATE TABLE `{SEED_SCHEMA}`.`{TABLE}` AS
            SELECT 'c1' AS id, 100.0 AS cost,
                   [STRUCT('team' AS Key, 'prod' AS Value), STRUCT('env', 'prod')] AS Labels,
                   [STRUCT('CUD' AS Type, -5.0 AS Amount), STRUCT('CUD', -5.0)] AS Credits
            UNION ALL SELECT 'c2', 100.0, [STRUCT('team', 'prod')], [STRUCT('SUD', -1.0)]
            UNION ALL SELECT 'c3', 50.0,
                   CAST([] AS ARRAY<STRUCT<Key STRING, Value STRING>>),
                   CAST([] AS ARRAY<STRUCT<Type STRING, Amount FLOAT64>>)""",
    ],
    "snowflake": [
        f'DROP TABLE IF EXISTS "{SEED_SCHEMA}"."{TABLE}"',
        # Quoted throughout: an unquoted identifier is folded to upper case
        # here, and the model names the physical column exactly as declared.
        f'CREATE TABLE "{SEED_SCHEMA}"."{TABLE}" '
        f'("id" VARCHAR, "cost" FLOAT, "Labels" ARRAY, "Credits" ARRAY)',
        f"""INSERT INTO "{SEED_SCHEMA}"."{TABLE}"
            SELECT 'c1', 100,
                   ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('Key','team','Value','prod'),
                                   OBJECT_CONSTRUCT('Key','env','Value','prod')),
                   ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('Type','CUD','Amount',-5),
                                   OBJECT_CONSTRUCT('Type','CUD','Amount',-5))
            UNION ALL SELECT 'c2', 100,
                   ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('Key','team','Value','prod')),
                   ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('Type','SUD','Amount',-1))
            UNION ALL SELECT 'c3', 50, ARRAY_CONSTRUCT(), ARRAY_CONSTRUCT()""",
    ],
    "databricks": [
        f"DROP TABLE IF EXISTS `{SEED_SCHEMA}`.`{TABLE}`",
        f"""CREATE TABLE `{SEED_SCHEMA}`.`{TABLE}` AS
            SELECT 'c1' AS id, 100.0D AS cost,
                   array(named_struct('Key','team','Value','prod'),
                         named_struct('Key','env','Value','prod')) AS Labels,
                   array(named_struct('Type','CUD','Amount',-5.0D),
                         named_struct('Type','CUD','Amount',-5.0D)) AS Credits
            UNION ALL SELECT 'c2', 100.0D, array(named_struct('Key','team','Value','prod')),
                   array(named_struct('Type','SUD','Amount',-1.0D))
            UNION ALL SELECT 'c3', 50.0D,
                   cast(array() AS array<struct<Key:string,Value:string>>),
                   cast(array() AS array<struct<Type:string,Amount:double>>)""",
    ],
}

MODEL_YAML = """
version: "1.0"
name: nested_vendor
dataObjects:
  Charges:
    code: {table}
    schema: {schema}
    columns:
      Charge Id: {{code: id, abstractType: string, primaryKey: true}}
      Cost: {{code: cost, abstractType: float}}
  Charge Labels:
    nestedIn: {{dataObject: Charges, column: Labels}}
    columns:
      Label Value: {{code: Value, abstractType: string}}
  Charge Credits:
    nestedIn: {{dataObject: Charges, column: Credits}}
    columns:
      Credit Type: {{code: Type, abstractType: string}}
      Credit Amount: {{code: Amount, abstractType: float}}
dimensions:
  Label Value: {{dataObject: Charge Labels, column: Label Value}}
  Credit Type: {{dataObject: Charge Credits, column: Credit Type}}
measures:
  Total Cost:
    columns: [{{dataObject: Charges, column: Cost}}]
    resultType: float
    aggregation: sum
  Total Credit:
    columns: [{{dataObject: Charge Credits, column: Credit Amount}}]
    resultType: float
    aggregation: sum
"""


def _model(target: VendorTarget) -> SemanticModel:
    schema = SEED_SCHEMA if target.dialect in QUALIFIED else '""'
    raw, source_map = TrackedLoader().load_string(MODEL_YAML.format(table=TABLE, schema=schema))
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _prepare(target: VendorTarget) -> None:
    for statement in DDL[target.dialect]:
        # DDL returns no cursor description for the fixture to read.
        with contextlib.suppress(TypeError):
            target.execute(statement)


def _key(value: Any, dialect: str) -> Any:
    """The group a row belongs to, with ClickHouse's empty-string pad as NULL."""
    if dialect == "clickhouse" and value == "":
        return None
    return value


def _number(value: Any, dialect: str) -> float | None:
    """A measure value as a float, with ClickHouse's zero pad as NULL."""
    if value is None:
        return None
    number = float(value) if isinstance(value, Decimal) else float(value)
    if dialect == "clickhouse" and number == 0.0:
        return None
    return number


def _grouped(target: VendorTarget, dimensions: list[str], measures: list[str]) -> dict[Any, tuple]:
    """Run the compiled query and return ``{dimension value: (measures...)}``."""
    result = CompilationPipeline().compile(
        QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures)),
        _model(target),
        target.dialect,
    )
    rows = target.execute(result.sql)
    out: dict[Any, tuple] = {}
    for row in rows:
        # Result keys are the aliases the model declares, but engines differ on
        # case, so they are matched insensitively.
        lookup = {str(k).lower(): v for k, v in row.items()}
        key = _key(lookup[dimensions[0].lower()], target.dialect)
        out[key] = tuple(_number(lookup[m.lower()], target.dialect) for m in measures)
    return out


def _assert_parent_measure(target: VendorTarget) -> None:
    """Spend by label value: ``c1`` is one charge however many labels it has."""
    _prepare(target)
    grouped = _grouped(target, ["Label Value"], ["Total Cost"])
    assert grouped.get("prod") == (200.0,), f"{target.name}: {grouped}"
    # The untagged charge survives the outer unnest and keeps its cost, so the
    # groups still add up to the table's total.
    assert grouped.get(None) == (50.0,), f"{target.name}: {grouped}"
    assert sum(v[0] or 0 for v in grouped.values()) == 250.0, f"{target.name}: {grouped}"


def _assert_nested_measure(target: VendorTarget) -> None:
    """Credits by type: two identical credits are two credits."""
    _prepare(target)
    grouped = _grouped(target, ["Credit Type"], ["Total Credit"])
    assert grouped.get("CUD") == (-10.0,), f"{target.name}: {grouped}"
    assert grouped.get("SUD") == (-1.0,), f"{target.name}: {grouped}"


def _assert_mixed(target: VendorTarget) -> None:
    """Both at once: the two are computed over different row sets."""
    _prepare(target)
    grouped = _grouped(target, ["Credit Type"], ["Total Credit", "Total Cost"])
    assert grouped.get("CUD") == (-10.0, 100.0), f"{target.name}: {grouped}"
    assert grouped.get("SUD") == (-1.0, 100.0), f"{target.name}: {grouped}"


def _assert_all(target: VendorTarget) -> None:
    _assert_parent_measure(target)
    _assert_nested_measure(target)
    _assert_mixed(target)


def test_duckdb_nested_plan(vendor_duckdb: VendorTarget) -> None:
    _assert_all(vendor_duckdb)


def test_postgres_nested_plan(vendor_postgres: VendorTarget) -> None:
    _assert_all(vendor_postgres)


def test_mysql_nested_plan(vendor_mysql: VendorTarget) -> None:
    _assert_all(vendor_mysql)


def test_clickhouse_nested_plan(vendor_clickhouse: VendorTarget) -> None:
    _assert_all(vendor_clickhouse)


def test_bigquery_nested_plan(vendor_bigquery: VendorTarget) -> None:
    _assert_all(vendor_bigquery)


def test_snowflake_nested_plan(vendor_snowflake: VendorTarget) -> None:
    _assert_all(vendor_snowflake)


def test_databricks_nested_plan(vendor_databricks: VendorTarget) -> None:
    _assert_all(vendor_databricks)
