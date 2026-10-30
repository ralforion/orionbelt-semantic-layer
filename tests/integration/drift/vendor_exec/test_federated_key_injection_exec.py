"""A federated fact leg reading only the keys a filtered dimension leg returned.

The fact leg is compiled again with ``key IN (...)``, the keys written as the
engine's literals: integers, and strings holding a quote, a backslash and
text outside Latin-1. Each engine runs it, here whatever its own limit
(``Dialect.key_injection_max_keys``), and the combined answer must be the
engine's own over one source. Both sources are the engine itself, as in
``test_federated_equality_exec``.
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
      Text Key: {{code: ks, abstractType: string}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [{fk}], columnsTo: [{pk}]}}
  Groups:
    code: g
{groups}    columns:
      Group Key: {{code: k, abstractType: int}}
      Group Text Key: {{code: ks, abstractType: string}}
      Label: {{code: l, abstractType: string}}
dimensions:
  Label: {{dataObject: Groups, column: Label, resultType: string}}
measures:
  Rows: {{columns: [{{dataObject: Facts, column: Key}}], aggregation: count}}
"""

KEYS = {"int": ("Key", "Group Key"), "string": ("Text Key", "Group Text Key")}

GROUPS = [(1, "a", "x"), (2, "it's", "x"), (3, "İstanbul", "x"), (4, "a\\b", "y"), (5, "b", "y")]
# Key 6 has no group.
FACTS = [(1, "a"), (1, "a"), (2, "it's"), (3, "İstanbul"), (4, "a\\b"), (5, "b"), (6, "zz")]

QUERY = {
    "select": {"dimensions": ["Label"], "measures": ["Rows"]},
    "where": [{"field": "Label", "op": "in", "value": ["x", "nowhere"]}],
}


def _tables(dialect: str) -> str:
    """``WITH t AS (...), g AS (...)``: the rows, as tables of this engine."""
    text, _, whole = TYPES[dialect]
    engine = DialectRegistry.get(dialect)
    q = engine.quote_identifier

    def value(v: int | str, kind: str) -> str:
        literal = engine.quote_string_literal(v) if isinstance(v, str) else str(v)
        return f"CAST({literal} AS {kind})"

    facts = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(ks, text)} AS {q('ks')}" for k, ks in FACTS
    )
    groups = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(ks, text)} AS {q('ks')}, "
        f"{value(label, text)} AS {q('l')}"
        for k, ks, label in GROUPS
    )
    return f"WITH {q('t')} AS ({facts}), {q('g')} AS ({groups})\n"


def _model(federated: bool, key: str) -> str:
    fk, pk = KEYS[key]
    return MODEL.format(
        facts="    dataSource: facts\n" if federated else "",
        groups="    dataSource: groups\n" if federated else "",
        fk=fk,
        pk=pk,
    )


def _assert_injected(target: VendorTarget) -> None:
    for key in KEYS:
        store = ModelStore()
        query = QueryObject.model_validate(QUERY)
        single = store.compile_query(
            store.load_model(_model(False, key)).model_id, query, target.dialect
        )
        plan = store.compile_query(
            store.load_model(_model(True, key)).model_id, query, target.dialect
        ).federation
        assert plan is not None and plan.injections, f"{target.name}: no key injected"

        def run(leg: Any) -> Any:
            rows = target.execute(_tables(target.dialect) + leg.sql)
            return normalize_leg(leg, pa.Table.from_pylist(rows))

        tables = {leg.name: run(leg) for leg in plan.legs[1:]}
        fact = injected_fact_leg(plan, tables, 10_000)
        assert " IN (" in fact.sql, f"{target.name}: the {key} keys were not listed"
        tables[fact.name] = run(fact)
        combined = combine(plan, tables, None).arrow_table.to_pylist()

        expected = [
            {k.lower(): v for k, v in row.items()}
            for row in target.execute(_tables(target.dialect) + single.sql)
        ]
        assert [{k.lower(): v for k, v in row.items()} for row in combined] == expected, (
            f"{target.name}: {key} keys"
        )
        assert expected == [{"label": "x", "rows": 4}]


def test_duckdb_key_injection(vendor_duckdb: VendorTarget) -> None:
    _assert_injected(vendor_duckdb)


def test_postgres_key_injection(vendor_postgres: VendorTarget) -> None:
    _assert_injected(vendor_postgres)


def test_mysql_key_injection(vendor_mysql: VendorTarget) -> None:
    _assert_injected(vendor_mysql)


def test_clickhouse_key_injection(vendor_clickhouse: VendorTarget) -> None:
    _assert_injected(vendor_clickhouse)


def test_snowflake_key_injection(vendor_snowflake: VendorTarget) -> None:
    _assert_injected(vendor_snowflake)


def test_bigquery_key_injection(vendor_bigquery: VendorTarget) -> None:
    _assert_injected(vendor_bigquery)


def test_databricks_key_injection(vendor_databricks: VendorTarget) -> None:
    _assert_injected(vendor_databricks)


def test_dremio_key_injection(vendor_dremio: VendorTarget) -> None:
    _assert_injected(vendor_dremio)
