"""Tests for the case-insensitive ``ilike`` / ``notilike`` filter operators.

The per-dialect forms follow an executed probe (``scripts/probe_functions.py``,
group ``ilike``): Dremio has only the ``ILIKE`` function, BigQuery and MySQL
have no ``ILIKE`` at all, and ClickHouse's ``LOWER`` folds ASCII only, so its
native ``ILIKE`` is the one that matches ``Ä``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import orionbelt.dialect  # noqa: F401 - registers all 8 dialects
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import FilterOperator, QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML

_MEASURE_FILTER_YAML = SAMPLE_MODEL_YAML.replace(
    "metrics:\n",
    """  US Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
    filters:
      - column: {dataObject: Customers, column: Country}
        operator: ilike
        values: [{dataType: string, valueString: "u%"}]

rules:
  US Customer:
    type: classification
    condition: {field: Customer Country, op: notilike, value: "u%"}

metrics:
""",
    1,
)


def _model(yaml: str = SAMPLE_MODEL_YAML) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, [e.message for e in result.errors]
    return model


def _compile(op: FilterOperator, value: object, dialect: str = "duckdb") -> str:
    query = QueryObject(
        select=QuerySelect(dimensions=["Customer Country"]),
        where=[QueryFilter(field="Customer Country", op=op, value=value)],
    )
    return CompilationPipeline().compile(query, _model(), dialect_name=dialect).sql


@pytest.mark.parametrize(
    ("dialect", "ilike", "notilike"),
    [
        ("postgres", '"Customers"."COUNTRY" ILIKE \'u%\'', None),
        ("snowflake", "\"Customers\".\"COUNTRY\" ILIKE 'u%' ESCAPE '\\\\'", None),
        ("duckdb", "\"Customers\".\"COUNTRY\" ILIKE 'u%' ESCAPE '\\'", None),
        ("clickhouse", '"Customers"."COUNTRY" ILIKE \'u%\'', None),
        ("databricks", "`Customers`.`COUNTRY` ILIKE 'u%'", None),
        (
            "dremio",
            "ILIKE(\"Customers\".\"COUNTRY\", 'u%', '\\')",
            "NOT ILIKE(\"Customers\".\"COUNTRY\", 'u%', '\\')",
        ),
        (
            "bigquery",
            "LOWER(`Customers`.`COUNTRY`) LIKE LOWER('u%')",
            "LOWER(`Customers`.`COUNTRY`) NOT LIKE LOWER('u%')",
        ),
        (
            "mysql",
            "LOWER(`Customers`.`COUNTRY`) LIKE LOWER('u%')",
            "LOWER(`Customers`.`COUNTRY`) NOT LIKE LOWER('u%')",
        ),
    ],
)
def test_rendering_per_dialect(dialect: str, ilike: str, notilike: str | None) -> None:
    assert f"WHERE {ilike}\n" in _compile(FilterOperator.ILIKE, "u%", dialect)
    expected_not = notilike or ilike.replace(" ILIKE ", " NOT ILIKE ")
    assert f"WHERE {expected_not}\n" in _compile(FilterOperator.NOT_ILIKE, "u%", dialect)


class TestExecutedOnDuckDB:
    @pytest.fixture
    def customers(self) -> Iterator[Any]:
        duckdb = pytest.importorskip("duckdb")
        connection = duckdb.connect()
        connection.execute("CREATE SCHEMA PUBLIC")
        connection.execute(
            "CREATE TABLE PUBLIC.CUSTOMERS AS SELECT * FROM (VALUES "
            "('1', 'USA'), ('2', 'uk'), ('3', 'Germany'), ('4', 'ÄGYPTEN')"
            ") AS t(CUSTOMER_ID, COUNTRY)"
        )
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _countries(connection: Any, op: FilterOperator, pattern: str) -> list[str]:
        rows = connection.execute(_compile(op, pattern)).fetchall()
        return sorted(row[0] for row in rows)

    def test_ilike_ignores_case(self, customers: Any) -> None:
        assert self._countries(customers, FilterOperator.ILIKE, "u%") == ["USA", "uk"]

    def test_notilike_is_the_complement(self, customers: Any) -> None:
        assert self._countries(customers, FilterOperator.NOT_ILIKE, "u%") == [
            "Germany",
            "ÄGYPTEN",
        ]

    def test_non_ascii_folds(self, customers: Any) -> None:
        assert self._countries(customers, FilterOperator.ILIKE, "äg%") == ["ÄGYPTEN"]


class TestBackslashEscapeOnDuckDB:
    """A backslash escapes ``%`` and ``_`` in every pattern operator.

    DuckDB reads the backslash literally unless told ``ESCAPE '\\'``: before,
    ``a\\_b`` matched neither ``a_b`` nor ``axb``, and ``contains: a_b`` (which
    escapes the underscore itself) matched nothing.
    """

    @pytest.fixture
    def customers(self) -> Iterator[Any]:
        duckdb = pytest.importorskip("duckdb")
        connection = duckdb.connect()
        connection.execute("CREATE SCHEMA PUBLIC")
        connection.execute(
            "CREATE TABLE PUBLIC.CUSTOMERS AS SELECT * FROM (VALUES "
            "('1', 'a_b'), ('2', 'axb'), ('3', 'A_B')"
            ") AS t(CUSTOMER_ID, COUNTRY)"
        )
        try:
            yield connection
        finally:
            connection.close()

    @pytest.mark.parametrize(
        ("op", "value", "expected"),
        [
            (FilterOperator.LIKE, "a\\_b", ["a_b"]),
            (FilterOperator.NOT_LIKE, "a\\_b", ["A_B", "axb"]),
            (FilterOperator.ILIKE, "a\\_b", ["A_B", "a_b"]),
            (FilterOperator.NOT_ILIKE, "a\\_b", ["axb"]),
            (FilterOperator.CONTAINS, "a_b", ["a_b"]),
            (FilterOperator.STARTS_WITH, "a_", ["a_b"]),
        ],
    )
    def test_escaped_wildcard(
        self, customers: Any, op: FilterOperator, value: str, expected: list[str]
    ) -> None:
        rows = customers.execute(_compile(op, value)).fetchall()
        assert sorted(row[0] for row in rows) == expected


def test_measure_filter_ilike() -> None:
    query = QueryObject(select=QuerySelect(dimensions=[], measures=["US Revenue"]))
    sql = CompilationPipeline().compile(query, _model(_MEASURE_FILTER_YAML), "bigquery").sql
    assert "CASE WHEN LOWER(`Customers`.`COUNTRY`) LIKE LOWER('u%')" in sql


def test_rule_condition_accepts_notilike() -> None:
    model = _model(_MEASURE_FILTER_YAML)
    assert model.rules["US Customer"].condition.op == "notilike"
