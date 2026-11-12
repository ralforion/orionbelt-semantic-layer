"""Online validation of computed columns and nested fields, run on each engine.

The datasource probe builds its statements from the planners' own pieces, so
the SQL it sends a dialect is the SQL that dialect's queries send. That is only
worth something if each engine accepts the statements for a model that matches
it, and rejects them where the model is wrong, so this runs the probe itself
through the connection each fixture opened.

The tables are ``test_nested_plan_exec``'s: a charges table with two arrays of
structs. Two passes. The first runs every statement through the fixture's own
connection, on all seven engines, for what each engine accepts and rejects. The
second runs the probe through OBSL's own driver (``db_executor.execute_sql``), so
the type comparison reads the result metadata a deployment reads: every engine
but DuckDB, whose fixture database is in-memory and which the real-DuckDB tests
in ``tests/unit/test_named_datasource_selection.py`` cover.

What the engines can tell differs, and the probe can only report what the
engine says:

*   A field the array's elements lack fails on the engines that read a typed
    struct, and reads as NULL on Snowflake (a path into a semi-structured value)
    and MySQL (``JSON_TABLE``), where the probe cannot see it.
*   An array column the parent lacks fails everywhere.
*   A boolean comes back as an integer on MySQL, which has no boolean type, and
    from a ClickHouse comparison (``UInt8``), and is accepted there; a float or
    a decimal behind a boolean is reported everywhere.
*   A nested field's type is the declared one on Snowflake and MySQL, which cast
    to it, so a wrong declaration cannot be seen there either.
*   An expression no engine can evaluate (it reads a column that does not
    exist) fails everywhere. One that only fails on the values it reads, such as
    a string times a number, is the engine's call: some reject it while planning
    and some only once rows are read, which ``LIMIT 0`` never does.

Dremio is absent for the same reason as in ``test_nested_plan_exec``: it has no
FROM-clause unnest and reads the ``code`` table, an ordinary table probe.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.service.datasource_probe import probe_datasource
from orionbelt.service.db_executor import ExecutionError

from ._seed import SCHEMA as SEED_SCHEMA
from .conftest import VendorTarget
from .test_nested_plan_exec import DDL, QUALIFIED, TABLE

pytestmark = pytest.mark.docker

MODEL_YAML = """
version: "1.0"
name: probe_vendor
dataObjects:
  Charges:
    code: {table}
    schema: {schema}
    columns:
      Charge Id: {{code: id, abstractType: string, primaryKey: true}}
      Cost: {{code: cost, abstractType: float}}
      Net Cost: {{expression: "{{Cost}} * 0.8", abstractType: float}}
  Charge Labels:
    nestedIn: {{dataObject: Charges, column: Labels}}
    columns:
      Label Key: {{code: Key, abstractType: string}}
      Label Value: {{code: Value, abstractType: string}}
      Is Team:
        expression: "CASE WHEN {{Label Key}} = 'team' THEN 1 ELSE 0 END"
        abstractType: int
  Charge Credits:
    nestedIn: {{dataObject: Charges, column: Credits}}
    columns:
      Credit Type: {{code: Type, abstractType: string}}
      Credit Amount: {{code: Amount, abstractType: float}}
dimensions:
  Label Value: {{dataObject: Charge Labels, column: Label Value}}
"""

#: The engines whose elements are typed structs, so a field they lack fails.
READS_STRUCT_FIELDS = {"duckdb", "postgres", "clickhouse", "bigquery", "databricks"}


class _NoRows:
    """What the probe reads of a result: no Arrow schema, so no type comparison."""

    arrow_schema = None
    columns: list[Any] = []


def _model(target: VendorTarget, yaml: str) -> SemanticModel:
    schema = SEED_SCHEMA if target.dialect in QUALIFIED else '""'
    raw, source_map = TrackedLoader().load_string(yaml.format(table=TABLE, schema=schema))
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _findings(
    target: VendorTarget, monkeypatch: pytest.MonkeyPatch, yaml: str
) -> list[tuple[str, str | None]]:
    """The probe's findings for *yaml*, every statement run on *target*."""

    def execute_sql(sql: str, *, dialect: str, source: Any = None) -> _NoRows:
        assert dialect == target.dialect
        try:
            target.execute(sql)
        except Exception as exc:  # noqa: BLE001 - each driver raises its own type
            raise ExecutionError(str(exc)) from exc
        return _NoRows()

    with monkeypatch.context() as patched:
        patched.setattr("orionbelt.service.db_executor.execute_sql", execute_sql)
        findings = probe_datasource(_model(target, yaml), dialect=target.dialect)
    return [(f.code, f.path) for f in findings]


TYPED_MODEL_YAML = """
version: "1.0"
name: probe_types
dataObjects:
  Charges:
    code: {table}
    schema: {schema}
    columns:
      Charge Id: {{code: id, abstractType: string}}
      Cost: {{code: cost, abstractType: float}}
      Net Cost: {{expression: "{{Cost}} * 0.8", abstractType: float}}
      Is Costly: {{expression: "{{Cost}} > 75", abstractType: boolean}}
      Always: {{expression: "TRUE", abstractType: boolean}}
  Charge Labels:
    nestedIn: {{dataObject: Charges, column: Labels}}
    columns:
      Label Key: {{code: Key, abstractType: string}}
      Is Team: {{expression: "{{Label Key}} = 'team'", abstractType: boolean}}
  Charge Credits:
    nestedIn: {{dataObject: Charges, column: Credits}}
    columns:
      Credit Amount: {{code: Amount, abstractType: float}}
dimensions:
  Label Key: {{dataObject: Charge Labels, column: Label Key}}
"""

#: The engines whose nested fields come back in their stored type rather than
#: cast to the declared one.
TYPES_NESTED_FIELDS = {"postgres", "clickhouse", "bigquery", "databricks"}


def _driver_findings(
    target: VendorTarget, monkeypatch: pytest.MonkeyPatch, yaml: str
) -> list[tuple[str, str | None]]:
    """The probe's findings through OBSL's own driver, with its result metadata."""
    with monkeypatch.context() as env:
        env.delenv("OBSL_DATABASES", raising=False)
        for name, value in target.connection_env.items():
            env.setenv(name, value)
        findings = probe_datasource(_model(target, yaml), dialect=target.dialect)
    return [(f.code, f.path) for f in findings]


def _assert_types(target: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared types against the engine's own, booleans included."""
    assert _driver_findings(target, monkeypatch, TYPED_MODEL_YAML) == [], target.name

    net_cost = "dataObjects.Charges.columns.Net Cost.abstractType"
    wrong = TYPED_MODEL_YAML.replace('0.8", abstractType: float', '0.8", abstractType: string')
    assert _driver_findings(target, monkeypatch, wrong) == [
        ("DATASOURCE_TYPE_MISMATCH", net_cost)
    ], target.name

    # A boolean is an integer on MySQL and in a ClickHouse comparison; a
    # fraction is not one anywhere.
    always = "dataObjects.Charges.columns.Always.abstractType"
    fraction = TYPED_MODEL_YAML.replace('expression: "TRUE"', 'expression: "0.5"')
    assert _driver_findings(target, monkeypatch, fraction) == [
        ("DATASOURCE_TYPE_MISMATCH", always)
    ], target.name

    amount = "dataObjects.Charge Credits.columns.Credit Amount.abstractType"
    wrong_field = TYPED_MODEL_YAML.replace(
        "code: Amount, abstractType: float", "code: Amount, abstractType: date"
    )
    assert _driver_findings(target, monkeypatch, wrong_field) == (
        [("DATASOURCE_TYPE_MISMATCH", amount)] if target.dialect in TYPES_NESTED_FIELDS else []
    ), target.name


def _assert_probe(target: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    for statement in DDL[target.dialect]:
        # DDL returns no cursor description for the fixture to read.
        with contextlib.suppress(TypeError):
            target.execute(statement)

    assert _findings(target, monkeypatch, MODEL_YAML) == [], target.name

    missing_field = [
        ("DATASOURCE_COLUMN_MISSING", "dataObjects.Charge Labels.columns.Label Value.code")
    ]
    lacking_field = MODEL_YAML.replace("code: Value,", "code: Valu,")
    assert _findings(target, monkeypatch, lacking_field) == (
        missing_field if target.dialect in READS_STRUCT_FIELDS else []
    ), target.name

    lacking_array = MODEL_YAML.replace("column: Credits}", "column: Credit}")
    assert _findings(target, monkeypatch, lacking_array) == [
        ("DATASOURCE_UNNEST_FAILED", "dataObjects.Charge Credits.nestedIn.column")
    ], target.name

    # The computed column reads a field the elements lack. On the engines that
    # cannot see the field, the expression reads NULL and passes too.
    reading_lacking_field = MODEL_YAML.replace("{{Label Key}} = 'team'", "{{Label Value}} = 'x'")
    reading_lacking_field = reading_lacking_field.replace("code: Value,", "code: Valu,")
    expected = (
        [
            *missing_field,
            (
                "DATASOURCE_EXPRESSION_FAILED",
                "dataObjects.Charge Labels.columns.Is Team.expression",
            ),
        ]
        if target.dialect in READS_STRUCT_FIELDS
        else []
    )
    assert _findings(target, monkeypatch, reading_lacking_field) == expected, target.name


def test_duckdb_probe(vendor_duckdb: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_probe(vendor_duckdb, monkeypatch)


def test_postgres_probe(vendor_postgres: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_probe(vendor_postgres, monkeypatch)
    _assert_types(vendor_postgres, monkeypatch)


def test_mysql_probe(vendor_mysql: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_probe(vendor_mysql, monkeypatch)
    _assert_types(vendor_mysql, monkeypatch)


def test_clickhouse_probe(vendor_clickhouse: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_probe(vendor_clickhouse, monkeypatch)
    _assert_types(vendor_clickhouse, monkeypatch)


def test_bigquery_probe(vendor_bigquery: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_probe(vendor_bigquery, monkeypatch)
    _assert_types(vendor_bigquery, monkeypatch)


def test_snowflake_probe(vendor_snowflake: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_probe(vendor_snowflake, monkeypatch)
    _assert_types(vendor_snowflake, monkeypatch)


def test_databricks_probe(vendor_databricks: VendorTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_probe(vendor_databricks, monkeypatch)
    _assert_types(vendor_databricks, monkeypatch)
