"""HAVING on a value produced by a window wrapper must run after the window.

``total_wrap``, ``window_wrap`` and ``cumulative_wrap`` all compute a measure's
final value with a window function in the outer query. A HAVING predicate on
such a measure used to stay in the CTE, where only the pre-window aggregate
exists, so it silently filtered the wrong value. For a rank it was worse: the
window function is not in the CTE at all, so the comparison bound to the
underlying measure instead.

Each test asserts on the *emitted SQL* rather than the AST, because the defect
was only visible in the relationship between the CTE and the outer query.
"""

from __future__ import annotations

import re

import pytest

import orionbelt.dialect  # noqa: F401 - registers dialects
from orionbelt.ast.nodes import AliasedExpr, BinaryOp, ColumnRef, From, Literal, Select
from orionbelt.compiler.having_hoist import HavingHoistError, apply_having_hoist
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """\
version: 1.0
dataObjects:
  Sales:
    code: sales
    database: db
    schema: public
    columns:
      Id:
        code: id
        abstractType: int
        primaryKey: true
      Amount:
        code: amount
        abstractType: float
      Cls:
        code: cls
        abstractType: string
      Cat:
        code: cat
        abstractType: string
      St:
        code: st
        abstractType: string
      D:
        code: d
        abstractType: date

dimensions:
  Class: {dataObject: Sales, column: Cls}
  Category: {dataObject: Sales, column: Cat}
  State: {dataObject: Sales, column: St}
  Day: {dataObject: Sales, column: D}

measures:
  Sales Amount:
    aggregation: sum
    columns: [{dataObject: Sales, column: Amount}]
  Class Revenue:
    aggregation: sum
    columns: [{dataObject: Sales, column: Amount}]
    grain: {mode: FIXED, keepOnly: [Class]}
  Grand Total:
    aggregation: sum
    columns: [{dataObject: Sales, column: Amount}]
    total: true

metrics:
  State Rank:
    type: window
    measure: Sales Amount
    windowFunction: rank
    partitionBy: [State]
  Running Total:
    type: cumulative
    measure: Sales Amount
    timeDimension: Day
  Deviation:
    expression: "ABS({[Sales Amount]} - {[Class Revenue]}) / {[Class Revenue]}"
    dataType: "decimal(18, 6)"
"""

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


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, [e.message for e in result.errors]
    return resolved


def _compile(model: SemanticModel, query: dict, dialect: str = "duckdb") -> str:
    return CompilationPipeline().compile(QueryObject.model_validate(query), model, dialect).sql


def _cte_body(sql: str, name: str) -> str:
    """The text of one CTE, so a test can assert what is and is not inside it."""
    start = sql.index(f'"{name}" AS (')
    depth, i = 0, start + sql[start:].index("(")
    for end in range(i, len(sql)):
        if sql[end] == "(":
            depth += 1
        elif sql[end] == ")":
            depth -= 1
            if depth == 0:
                return sql[i : end + 1]
    raise AssertionError(f"unterminated CTE {name!r}")


def _tail_after_ctes(sql: str) -> str:
    """The final SELECT, i.e. everything after the last top-level CTE closes."""
    depth = 0
    for i, ch in enumerate(sql):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and sql[i:].lstrip(") \n").startswith("SELECT"):
                return sql[i:]
    return sql


class TestGrainOverrideHaving:
    """A grain-override measure is ``SUM(x) OVER (PARTITION BY ...)`` outside the CTE."""

    QUERY = {
        "select": {
            "dimensions": ["Class", "Category"],
            "measures": ["Sales Amount", "Class Revenue"],
        },
        "having": [{"field": "Class Revenue", "op": "gt", "value": 1000000}],
    }

    def test_predicate_is_not_left_in_the_cte(self, model: SemanticModel) -> None:
        sql = _compile(model, self.QUERY)
        assert "HAVING" not in _cte_body(sql, "base")

    def test_predicate_filters_the_windowed_value(self, model: SemanticModel) -> None:
        sql = _compile(model, self.QUERY)
        tail = _tail_after_ctes(sql)
        assert "WHERE" in tail
        assert '"Class Revenue" > 1000000' in tail
        # The partitioned window is what the predicate now sits above.
        assert 'SUM("Class Revenue") OVER (PARTITION BY "Class")' in sql


class TestTotalHaving:
    QUERY = {
        "select": {"dimensions": ["Class"], "measures": ["Sales Amount", "Grand Total"]},
        "having": [{"field": "Grand Total", "op": "gt", "value": 5}],
    }

    def test_predicate_moves_past_the_grand_total(self, model: SemanticModel) -> None:
        sql = _compile(model, self.QUERY)
        assert "HAVING" not in _cte_body(sql, "base")
        assert 'SUM("Grand Total") OVER ()' in sql
        assert '"Grand Total" > 5' in _tail_after_ctes(sql)


class TestWindowMetricHaving:
    """The regression that was worst: the predicate lost the window entirely."""

    QUERY = {
        "select": {
            "dimensions": ["State", "Class"],
            "measures": ["Sales Amount", "State Rank"],
        },
        "having": [{"field": "State Rank", "op": "lte", "value": 1}],
    }

    def test_predicate_constrains_the_rank_not_the_base_measure(self, model: SemanticModel) -> None:
        sql = _compile(model, self.QUERY)
        base = _cte_body(sql, "window_base")
        assert "HAVING" not in base
        # The old output compared the *measure* to the rank threshold.
        assert "<= 1" not in base
        assert '"State Rank" <= 1' in _tail_after_ctes(sql)

    def test_rank_is_computed_before_it_is_filtered(self, model: SemanticModel) -> None:
        sql = _compile(model, self.QUERY)
        rank_at = sql.index("RANK() OVER")
        filter_at = sql.index('"State Rank" <= 1')
        assert rank_at < filter_at


class TestCumulativeHaving:
    QUERY = {
        "select": {"dimensions": ["Day"], "measures": ["Sales Amount", "Running Total"]},
        "having": [{"field": "Running Total", "op": "gt", "value": 100}],
    }

    def test_predicate_filters_the_running_total(self, model: SemanticModel) -> None:
        sql = _compile(model, self.QUERY)
        assert "HAVING" not in _cte_body(sql, "cumulative_base")
        assert '"Running Total" > 100' in _tail_after_ctes(sql)


class TestDerivedMetricOverWindow:
    """A derived metric whose component is windowed is itself windowed.

    This is the shape TPC-DS Q53 and Q63 need: ``abs(sum - avg) / avg > 0.1``
    where ``avg`` is a partitioned window over the per-group sum. The metric's
    value does not exist until the window has run, so a predicate on it has to
    move out with the rest.
    """

    QUERY = {
        "select": {
            "dimensions": ["Class", "Category"],
            "measures": ["Sales Amount", "Class Revenue", "Deviation"],
        },
        "having": [{"field": "Deviation", "op": "gt", "value": 0.1}],
    }

    def test_metric_filter_runs_after_the_window(self, model: SemanticModel) -> None:
        sql = _compile(model, self.QUERY)
        assert "HAVING" not in _cte_body(sql, "base")
        # The metric is assembled from the windowed component, not the raw sum.
        assert 'SUM("Class Revenue") OVER (PARTITION BY "Class")' in _cte_body(sql, "totals")
        assert '"Deviation" > 0.1' in _tail_after_ctes(sql)


class TestUnaffectedQueries:
    """Nothing changes for a predicate the window does not touch."""

    def test_having_on_a_plain_measure_stays_in_the_group_by(self, model: SemanticModel) -> None:
        sql = _compile(
            model,
            {
                "select": {"dimensions": ["Class"], "measures": ["Sales Amount"]},
                "having": [{"field": "Sales Amount", "op": "gt", "value": 10}],
            },
        )
        assert "HAVING" in sql
        # No wrapper ran at all, so there is no extra nesting.
        assert "WITH" not in sql

    def test_plain_predicate_stays_in_the_cte_beside_a_windowed_one(
        self, model: SemanticModel
    ) -> None:
        """A mixed query splits: one predicate each side of the window."""
        sql = _compile(
            model,
            {
                "select": {
                    "dimensions": ["Class"],
                    "measures": ["Sales Amount", "Grand Total"],
                },
                "having": [
                    {"field": "Sales Amount", "op": "gt", "value": 10},
                    {"field": "Grand Total", "op": "gt", "value": 5},
                ],
            },
        )
        base = _cte_body(sql, "base")
        assert "HAVING" in base
        assert "> 10" in base
        assert "> 5" not in base
        assert '"Grand Total" > 5' in _tail_after_ctes(sql)

    def test_windowed_measure_without_having_is_unwrapped(self, model: SemanticModel) -> None:
        """No hoisted predicate means no extra CTE, so existing SQL is unchanged."""
        sql = _compile(
            model,
            {"select": {"dimensions": ["Class"], "measures": ["Grand Total"]}},
        )
        assert '"totals" AS (' not in sql


class TestCompoundPredicate:
    def test_predicate_mixing_windowed_and_plain_measures_hoists_whole(
        self, model: SemanticModel
    ) -> None:
        """One predicate cannot be split, so all of it moves past the window.

        The planner projects every measure a HAVING references, so the plain
        half is a column of the windowed query and the combined predicate
        binds there.
        """
        sql = _compile(
            model,
            {
                "select": {"dimensions": ["Class"], "measures": ["Grand Total"]},
                "having": [
                    {
                        "logic": "and",
                        "filters": [
                            {"field": "Grand Total", "op": "gt", "value": 5},
                            {"field": "Sales Amount", "op": "gt", "value": 1},
                        ],
                    }
                ],
            },
        )
        assert "HAVING" not in _cte_body(sql, "base")
        # "Sales Amount" is carried through the window CTE so the predicate resolves.
        assert '"Sales Amount"' in _cte_body(sql, "totals")
        tail = _tail_after_ctes(sql)
        assert '"Grand Total" > 5 AND "Sales Amount" > 1' in tail


class TestRejection:
    """The guard for a predicate that cannot bind in the wrapping query.

    Exercised directly: the planner projects every measure a HAVING mentions,
    so the pipeline does not currently produce this shape. The guard exists so
    that a future wrapper which drops a column fails with a message about the
    query rather than a database error about an unknown identifier.
    """

    def test_unprojected_reference_raises(self) -> None:
        windowed = Select(
            columns=[AliasedExpr(expr=ColumnRef(name="Total", table="base"), alias="Total")],
            from_=From(source="base", alias="base"),
        )
        predicate = BinaryOp(ColumnRef(name="Absent"), ">", Literal.number(1))
        with pytest.raises(HavingHoistError) as excinfo:
            apply_having_hoist(windowed, [predicate], cte_name="w")
        assert "Absent" in str(excinfo.value)

    def test_projected_reference_is_accepted(self) -> None:
        windowed = Select(
            columns=[AliasedExpr(expr=ColumnRef(name="Total", table="base"), alias="Total")],
            from_=From(source="base", alias="base"),
        )
        predicate = BinaryOp(ColumnRef(name="Total"), ">", Literal.number(1))
        result = apply_having_hoist(windowed, [predicate], cte_name="w")
        assert result.where is predicate
        assert [cte.name for cte in result.ctes] == ["w"]

    def test_no_predicates_returns_the_query_untouched(self) -> None:
        windowed = Select(
            columns=[AliasedExpr(expr=ColumnRef(name="Total", table="base"), alias="Total")],
            from_=From(source="base", alias="base"),
        )
        assert apply_having_hoist(windowed, [], cte_name="w") is windowed


class TestAllDialects:
    """Every dialect gets the same nesting: no QUALIFY, so no dialect is special."""

    QUERY = TestWindowMetricHaving.QUERY

    @pytest.mark.parametrize("dialect", ALL_DIALECTS)
    def test_rank_filter_lands_outside_the_window_query(
        self, model: SemanticModel, dialect: str
    ) -> None:
        sql = _compile(model, self.QUERY, dialect)
        assert "QUALIFY" not in sql.upper()
        assert re.search(r"State Rank.{0,4} <= 1", sql), sql
        # The filter is a WHERE over the windowed rows, never a HAVING.
        tail = _tail_after_ctes(sql)
        assert "WHERE" in tail
        assert "HAVING" not in tail
