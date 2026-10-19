"""``exists`` / ``nonexists`` over a nested data object, executed on DuckDB.

A nested object's rows are an array column on its parent: there is no table
to select from in a correlated subquery. All three shapes reaching one were
refused (``NESTED_OBJECT_IN_SUBQUERY``). The subquery body now unnests it:

* from a parent already in the body, when the path or an inner filter walks
  through it;
* from the subject's own row, re-read under an alias of its own and matched
  on its primary key, when the nested object is the subject's array.

===========  =======  ======  ==========================
charge       account  cost    labels
===========  =======  ======  ==========================
``c1``       ``a1``   100     ``team=prod``, ``env=prod``
``c2``       ``a2``   100     ``team=prod``
``c3``       ``a2``   50      *(empty)*
===========  =======  ======  ==========================
"""

from __future__ import annotations

import duckdb
import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.dialect.base import UnsupportedNestedAccessError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
name: nested_exists
dataObjects:
  Accounts:
    code: accounts
    columns:
      Account Id: {code: id, abstractType: string, primaryKey: true}
      Account Name: {code: name, abstractType: string}
  Charges:
    code: charges
    columns:
      Charge Id: {code: id, abstractType: string, primaryKey: true}
      Charge Account Id: {code: account_id, abstractType: string}
      Cost: {code: cost, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Accounts
        columnsFrom: [Charge Account Id]
        columnsTo: [Account Id]
  Charge Labels:
    nestedIn: {dataObject: Charges, column: Labels}
    columns:
      Label Key: {code: Key, abstractType: string}
      Label Value: {code: Value, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Owners
        columnsFrom: [Label Value]
        columnsTo: [Owner Key]
  Owners:
    code: owners
    columns:
      Owner Key: {code: owner_key, abstractType: string, primaryKey: true}
      Owner Name: {code: name, abstractType: string}
dimensions:
  Account Name: {dataObject: Accounts, column: Account Name, resultType: string}
measures:
  Total Cost:
    columns: [{dataObject: Charges, column: Cost}]
    resultType: float
    aggregation: sum
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE accounts (id VARCHAR, name VARCHAR)")
    con.execute("INSERT INTO accounts VALUES ('a1', 'Acme'), ('a2', 'Beta')")
    con.execute(
        "CREATE TABLE charges (id VARCHAR, account_id VARCHAR, cost DOUBLE,"
        ' Labels STRUCT("Key" VARCHAR, "Value" VARCHAR)[])'
    )
    con.execute(
        "INSERT INTO charges VALUES"
        " ('c1', 'a1', 100, [{'Key': 'team', 'Value': 'prod'}, {'Key': 'env', 'Value': 'prod'}]),"
        " ('c2', 'a2', 100, [{'Key': 'team', 'Value': 'prod'}]),"
        " ('c3', 'a2', 50, [])"
    )
    con.execute("CREATE TABLE owners (owner_key VARCHAR, name VARCHAR)")
    con.execute("INSERT INTO owners VALUES ('prod', 'Ann')")
    return con


def _model(yaml_text: str = MODEL_YAML) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _sql(query: dict, dialect: str = "duckdb", yaml_text: str = MODEL_YAML) -> str:
    return CompilationPipeline().compile(QueryObject(**query), _model(yaml_text), dialect).sql


def _exists(op: str, field: str, target: str, inner: list[dict] | None = None) -> dict:
    return {"field": field, "op": op, "subquery": {"dataObject": target, "filter": inner or []}}


ENV = {"field": "Charge Labels.Label Key", "op": "equals", "value": "env"}


@pytest.mark.parametrize(
    ("op", "inner", "total"),
    [
        ("exists", [], 200.0),
        ("nonexists", [], 50.0),
        ("exists", [{"field": "Label Key", "op": "equals", "value": "env"}], 100.0),
        ("nonexists", [{"field": "Label Key", "op": "equals", "value": "env"}], 150.0),
    ],
    ids=["any-label", "no-label", "an-env-label", "no-env-label"],
)
def test_the_subjects_own_array(
    con: duckdb.DuckDBPyConnection, op: str, inner: list[dict], total: float
) -> None:
    """Charges with a label: c1 and c2 (c3's array is empty). The body
    re-reads each charge by its key and unnests its labels."""
    query = {
        "select": {"measures": ["Total Cost"]},
        "where": [_exists(op, "Charges.Charge Id", "Charge Labels", inner)],
    }
    assert con.execute(_sql(query)).fetchall() == [(total,)]


def test_an_inner_filter_on_a_nested_object(con: duckdb.DuckDBPyConnection) -> None:
    """Accounts with a charge labelled env: Acme. The body selects from the
    charges and unnests their labels for the filter."""
    query = {
        "select": {"dimensions": ["Account Name"]},
        "where": [_exists("exists", "Accounts.Account Id", "Charges", [ENV])],
    }
    assert con.execute(_sql(query)).fetchall() == [("Acme",)]


@pytest.mark.parametrize(("op", "total"), [("exists", 200.0), ("nonexists", 50.0)])
def test_a_target_behind_a_nested_object(
    con: duckdb.DuckDBPyConnection, op: str, total: float
) -> None:
    """Owners are reached through the labels' values: c1 and c2 carry prod,
    owned by Ann; c3 has no label and so no owner."""
    query = {
        "select": {"measures": ["Total Cost"]},
        "where": [
            _exists(
                op,
                "Charges.Charge Id",
                "Owners",
                [{"field": "Owner Name", "op": "equals", "value": "Ann"}],
            )
        ],
    }
    assert con.execute(_sql(query)).fetchall() == [(total,)]


def test_the_subjects_array_needs_its_key() -> None:
    """The subject's row is matched on its primary key; without one the
    body cannot tell which row's array it is reading."""
    yaml_text = MODEL_YAML.replace(
        "      Charge Id: {code: id, abstractType: string, primaryKey: true}",
        "      Charge Id: {code: id, abstractType: string}",
    )
    query = {
        "select": {"measures": ["Total Cost"]},
        "where": [_exists("exists", "Charges.Charge Id", "Charge Labels")],
    }
    with pytest.raises(ResolutionError) as raised:
        _sql(query, yaml_text=yaml_text)
    assert [e.code for e in raised.value.errors] == ["NESTED_OBJECT_IN_SUBQUERY"]
    assert "primaryKey" in (raised.value.errors[0].hint or "")


def test_an_engine_without_an_unnest_says_so() -> None:
    """Dremio has no FROM-clause unnest: refused by name, not a failed query."""
    query = {
        "select": {"measures": ["Total Cost"]},
        "where": [_exists("exists", "Charges.Charge Id", "Charge Labels")],
    }
    with pytest.raises(UnsupportedNestedAccessError):
        _sql(query, dialect="dremio")


def test_from_a_nested_subject_up_to_its_parent_is_refused() -> None:
    """A label's charge is its containing row, not a keyed one: nothing in a
    label identifies it, so the body has nothing to correlate on."""
    query = {
        "select": {"measures": ["Total Cost"]},
        "where": [_exists("exists", "Charge Labels.Label Key", "Charges")],
    }
    with pytest.raises(ResolutionError) as raised:
        _sql(query)
    assert [e.code for e in raised.value.errors] == ["NESTED_OBJECT_IN_SUBQUERY"]
