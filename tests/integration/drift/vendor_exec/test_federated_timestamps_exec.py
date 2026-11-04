"""A federated query reads timestamps without a time zone in the query's zone.

Each leg converts them at the column, from the zone its source's sessions run
in to the model's ``defaultTimezone``, before a grain, a filter or an
aggregate reads them. Both sources are the engine itself here, declared to
run in UTC, and the query's zone is Berlin's; the reference is the engine's
own answer over one source reading the same UTC timestamps in Berlin's time
(``queryTimezone``). An order placed at 23:30 UTC on 31 January is
February's in Berlin. The hour the clocks go back is left out: there two
instants are one wall-clock time, which ClickHouse alone keeps apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pyarrow as pa
import pytest

from orionbelt.compiler.federation import FederatedLeg
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.service import datasource_selection
from orionbelt.service.federated_executor import combine, injected_fact_leg, normalize_leg
from orionbelt.service.model_store import ModelStore

from .conftest import VendorTarget
from .test_federated_equality_exec import TYPES

pytestmark = pytest.mark.docker

MODEL = """\
version: 1.0
settings:
{settings}dataObjects:
  Facts:
    code: t
{facts}    columns:
      Key: {{code: k, abstractType: int}}
      Placed: {{code: p, abstractType: timestamp}}
    joins:
      - {{joinType: many-to-one, joinTo: Groups, columnsFrom: [Key], columnsTo: [Group Key]}}
  Groups:
    code: g
{groups}    columns:
      Group Key: {{code: k, abstractType: int}}
      Label: {{code: l, abstractType: string}}
dimensions:
  Label: {{dataObject: Groups, column: Label, resultType: string}}
  Placed: {{dataObject: Facts, column: Placed, resultType: timestamp}}
measures:
  Rows: {{columns: [{{dataObject: Facts, column: Key}}], aggregation: count}}
  First:
    {{columns: [{{dataObject: Facts, column: Placed}}], aggregation: min, resultType: timestamp}}
  Last:
    {{columns: [{{dataObject: Facts, column: Placed}}], aggregation: max, resultType: timestamp}}
"""

# UTC wall-clock times.
FACTS: list[tuple[int, str | None]] = [
    (1, "2024-01-05 10:00:00"),
    (2, "2024-01-31 23:30:00"),
    (3, "2024-03-30 23:10:00"),
    (4, "2024-03-31 00:30:00"),
    (5, "2024-10-27 00:30:00"),
    (6, "2024-10-27 02:30:00"),
    (7, None),
]
GROUPS = [(1, "x"), (2, "x"), (3, "y"), (4, "y"), (5, "z"), (6, "z"), (7, "z")]

# A timestamp without a time zone, as each engine spells one.
TIMESTAMP = {
    "postgres": "TIMESTAMP",
    "mysql": "DATETIME",
    "snowflake": "TIMESTAMP_NTZ",
    "bigquery": "DATETIME",
    "clickhouse": "Nullable(DateTime64(3))",
    "dremio": "TIMESTAMP",
    "databricks": "TIMESTAMP_NTZ",
    "duckdb": "TIMESTAMP",
}

QUERIES: dict[str, dict[str, Any]] = {
    "by_day": {"select": {"dimensions": ["Label", "Placed:day"], "measures": ["Rows"]}},
    "bounds": {"select": {"dimensions": ["Label"], "measures": ["First", "Last"]}},
    "as_stored": {"select": {"dimensions": ["Placed", "Label"], "measures": ["Rows"]}},
    "filtered": {
        "select": {"dimensions": ["Label"], "measures": ["Rows"]},
        "where": [{"field": "Placed", "op": ">=", "value": "2024-02-01"}],
    },
    # Filtered on the other source: the fact leg reads the keys it returned.
    "keys_given": {
        "select": {"dimensions": ["Placed:month"], "measures": ["Rows"]},
        "where": [{"field": "Label", "op": "=", "value": "x"}],
    },
}


def _tables(dialect: str) -> str:
    """``WITH t AS (...), g AS (...)``: the rows, as tables of this engine."""
    text, _, whole = TYPES[dialect]
    stamp = TIMESTAMP[dialect]
    q = DialectRegistry.get(dialect).quote_identifier

    def value(v: object, kind: str) -> str:
        return f"CAST({'NULL' if v is None else repr(v)} AS {kind})"

    facts = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(p, stamp)} AS {q('p')}" for k, p in FACTS
    )
    groups = " UNION ALL ".join(
        f"SELECT {value(k, whole)} AS {q('k')}, {value(label, text)} AS {q('l')}"
        for k, label in GROUPS
    )
    return f"WITH {q('t')} AS ({facts}), {q('g')} AS ({groups})\n"


def _wall(value: Any) -> Any:
    """A timestamp as its wall-clock time, in the zone it carries if any."""
    return value.replace(tzinfo=None) if isinstance(value, datetime) else value


def _rows(rows: list[dict]) -> list[tuple]:
    folded = [tuple(sorted((k.lower(), _wall(v)) for k, v in row.items())) for row in rows]
    return sorted(folded, key=repr)


def _assert_in_the_query_zone(target: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    # Both sources run their sessions in UTC. Not configured: two sources of
    # one engine with the same settings are one database to the planner.
    monkeypatch.setattr(datasource_selection, "source_timezone", lambda name, dialect: "UTC")
    store = ModelStore()
    single_model = MODEL.format(
        settings="  defaultTimezone: UTC\n  queryTimezone: Europe/Berlin\n", facts="", groups=""
    )
    federated_model = MODEL.format(
        settings="  defaultTimezone: Europe/Berlin\n",
        facts="    dataSource: facts\n",
        groups="    dataSource: groups\n",
    )
    single_id = store.load_model(single_model).model_id
    federated_id = store.load_model(federated_model).model_id

    def run(leg: FederatedLeg) -> pa.Table:
        rows = target.execute(_tables(target.dialect) + leg.sql)
        return normalize_leg(leg, pa.Table.from_pylist(rows))

    for name, query in QUERIES.items():
        query_object = QueryObject.model_validate(query)
        single = store.compile_query(single_id, query_object, target.dialect)
        plan = store.compile_query(federated_id, query_object, target.dialect).federation
        assert plan is not None and plan.timezone == "Europe/Berlin"
        tables = {leg.name: run(leg) for leg in plan.legs[1:]}
        fact = injected_fact_leg(plan, tables, 100) if plan.injections else plan.legs[0]
        tables["leg_0"] = run(fact)
        federated = combine(plan, tables, None).arrow_table.to_pylist()
        expected = target.execute(_tables(target.dialect) + single.sql)
        assert _rows(federated) == _rows(expected), f"{target.name}: {name} differs"


def test_duckdb_federated_timestamps(
    vendor_duckdb: VendorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_in_the_query_zone(vendor_duckdb, monkeypatch)


def test_postgres_federated_timestamps(
    vendor_postgres: VendorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_in_the_query_zone(vendor_postgres, monkeypatch)


def test_mysql_federated_timestamps(
    vendor_mysql: VendorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_in_the_query_zone(vendor_mysql, monkeypatch)


def test_clickhouse_federated_timestamps(
    vendor_clickhouse: VendorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_in_the_query_zone(vendor_clickhouse, monkeypatch)


def test_snowflake_federated_timestamps(
    vendor_snowflake: VendorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_in_the_query_zone(vendor_snowflake, monkeypatch)


def test_bigquery_federated_timestamps(
    vendor_bigquery: VendorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_in_the_query_zone(vendor_bigquery, monkeypatch)


def test_databricks_federated_timestamps(
    vendor_databricks: VendorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_in_the_query_zone(vendor_databricks, monkeypatch)


def test_dremio_federated_timestamps(
    vendor_dremio: VendorTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_in_the_query_zone(vendor_dremio, monkeypatch)
