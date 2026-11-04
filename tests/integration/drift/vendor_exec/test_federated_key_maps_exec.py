"""A federated fact leg joining a map of a small dimension leg's keys.

The fact leg is compiled again joining the keys and their ordinals as rows of
literals (``Dialect.key_map``) and grouping by the ordinal; the combine step
reads the map's rows for the dimension leg. Each engine runs it, and the
combined answer must be the engine's own over one source: a group of two
keys (1 and 3), a group of one, a key with no label (4), a fact row whose key
has no group (6), and one without a key. Both sources are the engine itself,
as in ``test_federated_equality_exec``.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.federated_executor import combine, injected_fact_leg, normalize_leg
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget
from .test_federated_equality_exec import TYPES

pytestmark = pytest.mark.docker

MODEL = """\
version: 1.0
dataObjects:
  Facts:
    code: t
{facts}    columns:
      Key: {{code: k, abstractType: int}}
      Day: {{code: d, abstractType: int}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [Key], columnsTo: [Group Key]}}
  Groups:
    code: g
{groups}    columns:
      Group Key: {{code: k, abstractType: int}}
      Label: {{code: l, abstractType: string}}
dimensions:
  Label: {{dataObject: Groups, column: Label, resultType: string}}
  Day: {{dataObject: Facts, column: Day, resultType: int}}
measures:
  Rows: {{columns: [{{dataObject: Facts, column: Day}}], aggregation: count}}
  Days: {{columns: [{{dataObject: Facts, column: Day}}], aggregation: sum}}
"""

GROUPS = [(1, "x"), (2, "y"), (3, "x"), (4, None)]
FACTS = [(1, 1), (1, 1), (3, 1), (2, 1), (2, 2), (4, 2), (6, 2), (None, 1)]

QUERIES = {
    "all_rows": {
        "select": {"dimensions": ["Label", "Day"], "measures": ["Rows", "Days"]},
        "orderBy": [{"field": "Label"}, {"field": "Day"}],
    },
    "filtered": {
        "select": {"dimensions": ["Label", "Day"], "measures": ["Rows", "Days"]},
        "where": [{"field": "Label", "op": "in", "value": ["x", "nowhere"]}],
        "orderBy": [{"field": "Day"}],
    },
}


def _tables(dialect: str) -> str:
    """``WITH t AS (...), g AS (...)``: the rows, as tables of this engine."""
    text, _, whole = TYPES[dialect]
    engine = DialectRegistry.get(dialect)
    q = engine.quote_identifier

    def value(v: int | str | None, kind: str) -> str:
        if v is None:
            return f"CAST(NULL AS {kind})"
        literal = engine.quote_string_literal(v) if isinstance(v, str) else str(v)
        return f"CAST({literal} AS {kind})"

    # ClickHouse's Int32 holds no NULL key.
    nullable = f"Nullable({whole})" if dialect == "clickhouse" else whole
    facts = " UNION ALL ".join(
        f"SELECT {value(k, nullable)} AS {q('k')}, {value(d, whole)} AS {q('d')}" for k, d in FACTS
    )
    groups = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(label, text)} AS {q('l')}"
        for k, label in GROUPS
    )
    return f"WITH {q('t')} AS ({facts}), {q('g')} AS ({groups})\n"


def _model(federated: bool) -> str:
    return MODEL.format(
        facts="    dataSource: facts\n" if federated else "",
        groups="    dataSource: groups\n" if federated else "",
    )


def _lowered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*rows* with lower-case names, sorted: where NULL sorts is its engine's."""
    return sorted(({k.lower(): v for k, v in row.items()} for row in rows), key=repr)


def _assert_mapped(target: VendorTarget) -> None:
    for name, raw in QUERIES.items():
        store = ModelStore()
        query = QueryObject.model_validate(raw)
        single = store.compile_query(
            store.load_model(_model(False)).model_id, query, target.dialect
        )
        plan = store.compile_query(
            store.load_model(_model(True)).model_id, query, target.dialect
        ).federation
        assert plan is not None and plan.key_maps, f"{target.name}: no key map"

        def run(leg: Any) -> Any:
            rows = target.execute(_tables(target.dialect) + leg.sql)
            return normalize_leg(leg, pa.Table.from_pylist(rows))

        tables = {leg.name: run(leg) for leg in plan.legs[1:]}
        fact, maps = injected_fact_leg(plan, tables, 10_000)
        assert list(maps) == ["leg_1"], f"{target.name}: {name}: the map was not joined"
        tables.update(maps)
        tables[fact.name] = run(fact)
        combined = combine(plan, tables, None).arrow_table.to_pylist()

        expected = _lowered(target.execute(_tables(target.dialect) + single.sql))
        assert _lowered(combined) == expected, f"{target.name}: {name}"
        assert expected


def test_duckdb_key_maps(vendor_duckdb: VendorTarget) -> None:
    _assert_mapped(vendor_duckdb)


def test_postgres_key_maps(vendor_postgres: VendorTarget) -> None:
    _assert_mapped(vendor_postgres)


def test_mysql_key_maps(vendor_mysql: VendorTarget) -> None:
    _assert_mapped(vendor_mysql)


def test_clickhouse_key_maps(vendor_clickhouse: VendorTarget) -> None:
    _assert_mapped(vendor_clickhouse)


def test_snowflake_key_maps(vendor_snowflake: VendorTarget) -> None:
    _assert_mapped(vendor_snowflake)


def test_bigquery_key_maps(vendor_bigquery: VendorTarget) -> None:
    _assert_mapped(vendor_bigquery)


def test_databricks_key_maps(vendor_databricks: VendorTarget) -> None:
    _assert_mapped(vendor_databricks)


def test_dremio_key_maps(vendor_dremio: VendorTarget) -> None:
    _assert_mapped(vendor_dremio)
