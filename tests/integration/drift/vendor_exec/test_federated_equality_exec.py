"""A federated query groups and counts distinct values as its source engine does.

The combine step runs in DuckDB, over rows a leg returned from different
groups. Where the source engine calls two values equal and DuckDB does not -
MySQL's default collation ignores case and accents, ClickHouse tells ``-0.0``
from ``0.0`` - the leg rewrites the value (``Dialect.equality_key``) so the
combine step agrees, and both legs of a join rewrite its keys
(``Dialect.match_key``). A filter on one source's dimensions runs in its leg,
where the engine compares as it does over one source. A minimum or maximum
of strings is taken at the least or greatest rank its leg gave it, in the
engine's own order: MySQL's collation and PostgreSQL's ``en_US.utf8`` put
``'b'`` before ``'C'``, which DuckDB puts after it, and so is a sort by
strings. Each engine's answer
over one source is the reference: both of a query's sources are the engine
itself here, and the same rows are read either way.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pyarrow as pa
import pytest

from orionbelt.compiler.federation import FederatedLeg
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service.federated_executor import combine, injected_fact_leg, normalize_leg
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

MODEL = """\
version: 1.0
dataObjects:
  Facts:
    code: t
{facts}    columns:
      Key: {{code: k, abstractType: int}}
      Name: {{code: n, abstractType: string}}
      F: {{code: f, abstractType: float}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [Key], columnsTo: [Group Key]}}
      - {{joinType: many-to-one, joinTo: Kinds, columnsFrom: [Name], columnsTo: [Kind Name]}}
  Groups:
    code: g
{groups}    columns:
      Group Key: {{code: k, abstractType: int}}
      Label: {{code: l, abstractType: string}}
  Kinds:
    code: s
{groups}    columns:
      Kind Name: {{code: n, abstractType: string}}
      Kind: {{code: k, abstractType: string}}
dimensions:
  Label: {{dataObject: Groups, column: Label, resultType: string}}
  Kind: {{dataObject: Kinds, column: Kind, resultType: string}}
  Name: {{dataObject: Facts, column: Name, resultType: string}}
measures:
  Rows: {{columns: [{{dataObject: Facts, column: Key}}], aggregation: count}}
  Names: {{columns: [{{dataObject: Facts, column: Name}}], aggregation: count_distinct}}
  Floats: {{columns: [{{dataObject: Facts, column: F}}], aggregation: count_distinct}}
  Least Name: {{columns: [{{dataObject: Facts, column: Name}}], aggregation: min}}
  Greatest Name: {{columns: [{{dataObject: Facts, column: Name}}], aggregation: max}}
"""

# Values one engine or another calls equal, on different keys of one label,
# so a leg grouped by key returns them in separate rows.
FACTS: list[tuple[int, str | None, float | None]] = [
    (1, "a", 0.0),
    (2, "A", -0.0),
    (3, "e", 1.5),
    (4, "é", 1.5),
    (1, "ss", 2.5),
    (2, "ß", 2.5),
    (5, "b", 3.0),
    (6, "b ", 3.0),
    (7, "æ", -0.0),
    (8, "ae", 0.0),
    (9, None, None),
    # Ordered 'b' before 'C' by a collation, after it by bytes.
    (10, "b", 4.0),
    (11, "C", 4.5),
]
GROUPS = [
    *[(k, "x") for k in (1, 2, 3, 4)],
    *[(k, "y") for k in (5, 6, 7, 8, 9)],
    *[(k, "z") for k in (10, 11)],
]
# The names' kinds, keyed by a name: MySQL's collation joins 'a' and 'A' to
# 'A', 'ß' to 'SS', where bytes match 'A' alone. 'b ' is not 'B' there.
KINDS = [("A", "first"), ("É", "accent"), ("SS", "double"), ("B", "plain"), ("AE", "joined")]

# string, float, int
TYPES = {
    "postgres": ("VARCHAR(10)", "DOUBLE PRECISION", "INTEGER"),
    "mysql": ("CHAR", "DOUBLE", "SIGNED"),
    "snowflake": ("VARCHAR", "DOUBLE", "INTEGER"),
    "bigquery": ("STRING", "FLOAT64", "INT64"),
    "clickhouse": ("Nullable(String)", "Nullable(Float64)", "Int32"),
    "dremio": ("VARCHAR", "DOUBLE", "INTEGER"),
    "databricks": ("STRING", "DOUBLE", "INT"),
    "duckdb": ("VARCHAR", "DOUBLE", "INTEGER"),
}


def _tables(dialect: str) -> str:
    """``WITH t AS (...), g AS (...)``: the rows, as tables of this engine."""
    text, real, whole = TYPES[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        if v is None:
            return f"CAST(NULL AS {kind})"
        return f"CAST({v!r} AS {kind})"

    facts = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(n, text)} AS {q('n')}, "
        f"{value(f, real)} AS {q('f')}"
        for k, n, f in FACTS
    )
    groups = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(label, text)} AS {q('l')}"
        for k, label in GROUPS
    )
    kinds = " UNION ALL ".join(
        f"SELECT {value(n, text)} AS {q('n')}, {value(kind, text)} AS {q('k')}" for n, kind in KINDS
    )
    return f"WITH {q('t')} AS ({facts}), {q('g')} AS ({groups}), {q('s')} AS ({kinds})\n"


def _model(federated: bool) -> str:
    return MODEL.format(
        facts="    dataSource: facts\n" if federated else "",
        groups="    dataSource: groups\n" if federated else "",
    )


def _answers(target: VendorTarget, query: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """The query over one source, and federated across two, both on *target*."""
    store = ModelStore()
    query_object = QueryObject.model_validate(query)
    single = store.compile_query(
        store.load_model(_model(False)).model_id, query_object, target.dialect
    )
    federated = store.compile_query(
        store.load_model(_model(True)).model_id, query_object, target.dialect
    )
    plan = federated.federation
    assert plan is not None

    def run(leg: FederatedLeg) -> pa.Table:
        rows = target.execute(_tables(target.dialect) + leg.sql)
        return normalize_leg(leg, pa.Table.from_pylist(rows))

    tables = {leg.name: run(leg) for leg in plan.legs[1:]}
    # The fact leg reading only the keys its filtered dimension legs returned,
    # or joining maps of them, which the combine step reads in their place.
    fact, maps = injected_fact_leg(plan, tables, 100)
    tables.update(maps)
    tables["leg_0"] = run(fact)
    combined = combine(plan, tables, None).arrow_table.to_pylist()
    return target.execute(_tables(target.dialect) + single.sql), combined


def _fold(rows: list[dict]) -> list[dict]:
    return [{key.lower(): value for key, value in row.items()} for row in rows]


def _assert_equality(target: VendorTarget) -> None:
    single, federated = _answers(
        target, {"select": {"dimensions": ["Label"], "measures": ["Names", "Floats", "Rows"]}}
    )
    by_label = {r["label"]: (r["names"], r["floats"], r["rows"]) for r in _fold(single)}
    assert {r["label"]: (r["names"], r["floats"], r["rows"]) for r in _fold(federated)} == (
        by_label
    ), f"{target.name}: distinct counts differ from the engine's own"

    # Grouped by a string: the value shown is one member of each class, so
    # the groups are compared by how many there are and the rows in each.
    single, federated = _answers(
        target, {"select": {"dimensions": ["Label", "Name"], "measures": ["Rows"]}}
    )

    def groups(rows: list[dict]) -> Counter[tuple[Any, int]]:
        return Counter((r["label"], r["rows"]) for r in _fold(rows))

    assert groups(federated) == groups(single), f"{target.name}: groups differ"

    # Dimensions alone: each label with each of its names, a class of them once.
    single, federated = _answers(target, {"select": {"dimensions": ["Label", "Name"]}})

    def names(rows: list[dict]) -> Counter[Any]:
        return Counter(r["label"] for r in _fold(rows))

    assert names(federated) == names(single), f"{target.name}: dimensions alone differ"

    # Filtered on the other source: 'Y' is 'y' to MySQL's collation alone,
    # which DuckDB, comparing bytes, would not find.
    single, federated = _answers(
        target,
        {
            "select": {"measures": ["Rows", "Names"]},
            "where": [{"field": "Label", "op": "in", "value": ["x", "Y"]}],
        },
    )
    assert _fold(federated) == _fold(single), f"{target.name}: the filter differs"

    # Joined on a string key: MySQL matches 'a' to 'A' and 'ß' to 'SS'.
    single, federated = _answers(target, {"select": {"dimensions": ["Kind"], "measures": ["Rows"]}})
    assert sorted(_fold(federated), key=repr) == sorted(_fold(single), key=repr), (
        f"{target.name}: the string join differs"
    )

    # Filtered on the kinds, whose keys the fact leg then reads alone: as
    # MySQL compares them, 'A' reads 'a' too.
    single, federated = _answers(
        target,
        {
            "select": {"dimensions": ["Label"], "measures": ["Rows"]},
            "where": [{"field": "Kind", "op": "in", "value": ["first", "double"]}],
        },
    )
    assert sorted(_fold(federated), key=repr) == sorted(_fold(single), key=repr), (
        f"{target.name}: the injected keys differ"
    )

    # The least and greatest of the names of two keys, 'b' and 'C': in the
    # engine's order, which a collation can make 'b' first.
    single, federated = _answers(
        target,
        {
            "select": {"dimensions": ["Label"], "measures": ["Least Name", "Greatest Name"]},
            "where": [{"field": "Label", "op": "=", "value": "z"}],
        },
    )
    assert _fold(federated) == _fold(single), f"{target.name}: the least and greatest differ"

    # Sorted by the names 'b' and 'C' of the facts, in the engine's order:
    # ranked by the fact leg, where the combine step would sort the bytes.
    # The labels' filter reads the other source.
    for sort in ({"field": "Name"}, {"field": "Name", "direction": "desc"}, None):
        query: dict[str, Any] = {
            "select": {"dimensions": ["Name"], "measures": ["Rows"]},
            "where": [
                {"field": "Name", "op": "in", "value": ["b", "C"]},
                {"field": "Label", "op": "in", "value": ["x", "y", "z"]},
            ],
        }
        if sort is None:
            query["limit"] = 1
        else:
            query["orderBy"] = [sort]
        single, federated = _answers(target, query)
        assert _fold(federated) == _fold(single), f"{target.name}: sorted by {sort} differs"


def test_duckdb_federated_equality(vendor_duckdb: VendorTarget) -> None:
    _assert_equality(vendor_duckdb)


def test_postgres_federated_equality(vendor_postgres: VendorTarget) -> None:
    _assert_equality(vendor_postgres)


def test_mysql_federated_equality(vendor_mysql: VendorTarget) -> None:
    _assert_equality(vendor_mysql)


def test_clickhouse_federated_equality(vendor_clickhouse: VendorTarget) -> None:
    _assert_equality(vendor_clickhouse)


def test_snowflake_federated_equality(vendor_snowflake: VendorTarget) -> None:
    _assert_equality(vendor_snowflake)


def test_bigquery_federated_equality(vendor_bigquery: VendorTarget) -> None:
    _assert_equality(vendor_bigquery)


def test_databricks_federated_equality(vendor_databricks: VendorTarget) -> None:
    _assert_equality(vendor_databricks)


def test_dremio_federated_equality(vendor_dremio: VendorTarget) -> None:
    _assert_equality(vendor_dremio)
