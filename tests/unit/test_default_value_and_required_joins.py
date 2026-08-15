"""Two things the model can now state that the engine used to decide.

``Measure.defaultValue`` says what an aggregate over nothing reads as, and
``DataObjectJoin.required`` says whether an unmatched row survives the join.
Both exist because the alternative is per-dialect behaviour a model author
cannot see: an aggregate over an empty row set is NULL in standard SQL and 0
on ClickHouse, and the ``IS NOT NULL`` filter that stands in for an inner join
keeps every row on ClickHouse rather than dropping the unmatched ones.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

PIPELINE = CompilationPipeline()

ALL_DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]

MODEL_YAML = """\
version: 1.0

dataObjects:
  Store:
    code: STORE
    database: WH
    schema: PUBLIC
    columns:
      Store Key: {code: S_KEY, abstractType: int, primaryKey: true}
      Store Name: {code: S_NAME, abstractType: string}

  Sales:
    code: SALES
    database: WH
    schema: PUBLIC
    columns:
      Store Key: {code: SS_STORE_SK, abstractType: int}
      Status: {code: STATUS, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Store
        columnsFrom: [Store Key]
        columnsTo: [Store Key]

dimensions:
  Store Name: {dataObject: Store, column: Store Name, resultType: string}

measures:
  Plain Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Zero Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    defaultValue: 0
    filters:
      - column: {dataObject: Sales, column: Status}
        operator: equals
        values: [{dataType: string, valueString: open}]
  Labelled Count:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: int
    aggregation: count
    defaultValue: none
"""

REQUIRED_JOIN = MODEL_YAML.replace(
    """        columnsFrom: [Store Key]
        columnsTo: [Store Key]""",
    """        columnsFrom: [Store Key]
        columnsTo: [Store Key]
        required: true""",
)


def _load(yaml_str: str = MODEL_YAML) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_str)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    assert SemanticValidator().validate(model) == []
    return model


def _sql(yaml_str: str, measures: list[str], dialect: str = "postgres") -> str:
    return PIPELINE.compile(
        QueryObject(select=QuerySelect(dimensions=["Store Name"], measures=measures)),
        _load(yaml_str),
        dialect,
    ).sql


class TestDefaultValue:
    @pytest.mark.parametrize("dialect", ALL_DIALECTS)
    def test_wraps_the_aggregate_on_every_dialect(self, dialect: str) -> None:
        """The point of declaring it is that the answer stops depending on the
        engine, so every dialect has to emit the same normalisation."""
        sql = _sql(MODEL_YAML, ["Zero Amount"], dialect)
        assert "COALESCE(" in sql.upper(), dialect

    def test_wraps_outside_not_inside(self) -> None:
        """``COALESCE(SUM(x), 0)`` answers 0 when the aggregate saw nothing;
        ``SUM(COALESCE(x, 0))`` answers 0 for a row whose value is missing.
        Different claims, and only the first is what the field means."""
        sql = _sql(MODEL_YAML, ["Zero Amount"])
        assert "COALESCE(SUM(" in sql
        assert "SUM(COALESCE(" not in sql

    def test_unset_keeps_the_standard_null(self) -> None:
        sql = _sql(MODEL_YAML, ["Plain Amount"])
        assert "COALESCE" not in sql

    def test_applies_to_an_unfiltered_measure_too(self) -> None:
        """Nothing ties it to filters: an unfiltered aggregate over an empty
        row set is equally NULL, and equally 0 on ClickHouse."""
        yaml_str = MODEL_YAML.replace(
            """  Plain Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum""",
            """  Plain Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    defaultValue: 0""",
        )
        assert "COALESCE(SUM(" in _sql(yaml_str, ["Plain Amount"])

    def test_a_non_numeric_default_is_quoted_as_a_literal(self) -> None:
        sql = _sql(MODEL_YAML, ["Labelled Count"])
        assert "'none'" in sql


class TestRequiredJoin:
    @pytest.mark.parametrize("dialect", ALL_DIALECTS)
    def test_emits_inner_join_on_every_dialect(self, dialect: str) -> None:
        sql = _sql(REQUIRED_JOIN, ["Plain Amount"], dialect)
        assert "INNER JOIN" in sql, dialect
        assert "LEFT JOIN" not in sql, dialect

    def test_default_is_still_left(self) -> None:
        sql = _sql(MODEL_YAML, ["Plain Amount"])
        assert "LEFT JOIN" in sql
        assert "INNER JOIN" not in sql

    def test_join_condition_is_unchanged(self) -> None:
        """Only the join *type* moves — the keys it matches on are the same."""
        on = 'ON "Sales"."SS_STORE_SK" = "Store"."S_KEY"'
        assert on in _sql(MODEL_YAML, ["Plain Amount"])
        assert on in _sql(REQUIRED_JOIN, ["Plain Amount"])


class TestExecuted:
    """What the SQL actually does, run on DuckDB."""

    @staticmethod
    def _run(sql: str) -> list[tuple]:
        import duckdb

        con = duckdb.connect(":memory:")
        con.execute('CREATE SCHEMA "PUBLIC"')
        con.execute('CREATE TABLE "PUBLIC"."STORE" (S_KEY INTEGER, S_NAME VARCHAR)')
        con.execute(
            'CREATE TABLE "PUBLIC"."SALES" (SS_STORE_SK INTEGER, STATUS VARCHAR, AMOUNT DOUBLE)'
        )
        con.execute('INSERT INTO "PUBLIC"."STORE" VALUES (1, \'Main\')')
        # One matched row whose status excludes it from the filtered measure,
        # and one row whose store key matches nothing.
        con.execute(
            "INSERT INTO \"PUBLIC\".\"SALES\" VALUES (1, 'closed', 10.0), (99, 'closed', 5.0)"
        )
        return con.execute(sql).fetchall()

    def test_default_value_replaces_the_null(self) -> None:
        rows = dict(
            (name, value)
            for name, value in [
                (r[0], r[1]) for r in self._run(_sql(MODEL_YAML, ["Zero Amount"], "duckdb"))
            ]
        )
        assert rows["Main"] == 0, rows  # no 'open' rows — 0, not NULL

    def test_without_a_default_the_group_reads_null(self) -> None:
        yaml_str = MODEL_YAML.replace("    defaultValue: 0\n", "")
        rows = self._run(_sql(yaml_str, ["Zero Amount"], "duckdb"))
        assert [r for r in rows if r[0] == "Main"][0][1] is None

    def test_required_join_drops_the_unmatched_row(self) -> None:
        left = self._run(_sql(MODEL_YAML, ["Plain Amount"], "duckdb"))
        inner = self._run(_sql(REQUIRED_JOIN, ["Plain Amount"], "duckdb"))
        # The unmatched sale groups under a NULL store name with LEFT, and is
        # gone entirely with the join declared required.
        assert None in [r[0] for r in left]
        assert None not in [r[0] for r in inner]
        assert sum(r[1] for r in inner) == 10.0
