"""A total or grain override no window combines is computed again, in a CTE of its own.

A window over the grouped rows sums, counts, minimises and maximises per-group
values, which is the total only for those aggregations and an average. A
standard deviation of the whole is not the sum of the regions' standard
deviations, a distinct count counted a value once per region it appeared in,
and under ``rollup`` the window added the subtotal rows as well. Each value
here is checked against the same aggregate DuckDB computes over the rows
directly.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.service.model_store import ModelStore

MODEL = """\
version: 1.0
dataObjects:
  Sales:
    code: sales
    columns:
      ID: {code: id, abstractType: int}
      Region: {code: region, abstractType: string}
      City: {code: city, abstractType: string}
      Amount: {code: amount, abstractType: int}
dimensions:
  Region: {dataObject: Sales, column: Region, resultType: string}
  City: {dataObject: Sales, column: City, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum}
  Revenue Total: {columns: [{dataObject: Sales, column: Amount}], aggregation: sum, total: true}
  SD Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: stddev
    resultType: float
    total: true
  Variance Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: var_pop
    resultType: float
    total: true
  Amounts Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: count_distinct
    total: true
  Distinct Amounts Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    distinct: true
    total: true
  Median Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: median
    resultType: float
    total: true
  Big Sales SD Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: stddev
    resultType: float
    total: true
    filters:
      - column: {dataObject: Sales, column: Amount}
        operator: gt
        values: [{dataType: int, valueInt: 15}]
  Empty SD Total:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: stddev
    resultType: float
    total: true
    defaultValue: 0
    filters:
      - column: {dataObject: Sales, column: Amount}
        operator: gt
        values: [{dataType: int, valueInt: 1000}]
  SD Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: stddev
    resultType: float
    grain: {mode: FIXED, keepOnly: [Region]}
  Amounts Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: count_distinct
    grain: {mode: FIXED, keepOnly: [Region]}
  Revenue Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    grain: {mode: FIXED, keepOnly: [Region]}
  Revenue Any Region:
    columns: [{dataObject: Sales, column: Amount}]
    aggregation: sum
    filterContext:
      mode: RELATIVE
      exclude: [Region]
metrics:
  SD per Revenue: {expression: "{[SD Total]} / {[Revenue]}"}
"""

# id, region, city, amount: region n spans two cities, a NULL region is a
# member of its own, and the amount 10 recurs in another group, which a sum of
# per-group distinct counts or distinct sums counted twice.
ROWS = [
    (1, "n", "a", 10),
    (2, "n", "b", 20),
    (3, "s", "c", 40),
    (4, "n", "b", 60),
    (5, None, "d", 5),
    (6, None, "d", 7),
    (7, "s", "c", 40),
    (8, "s", "c", 10),
]


@pytest.fixture(scope="module")
def con() -> Any:
    con = duckdb.connect()
    con.execute("CREATE TABLE sales (id INTEGER, region VARCHAR, city VARCHAR, amount INTEGER)")
    con.executemany("INSERT INTO sales VALUES (?, ?, ?, ?)", ROWS)
    return con


def _sql(query: dict[str, Any]) -> str:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    return store.compile_query(model_id, QueryObject.model_validate(query), "duckdb").sql


def _run(con: Any, query: dict[str, Any]) -> list[tuple[Any, ...]]:
    return con.sql(_sql(query)).fetchall()


def _one(con: Any, sql: str) -> Any:
    return con.sql(sql).fetchall()[0][0]


WHOLE = {
    "SD Total": "SELECT STDDEV(amount) FROM sales",
    "Variance Total": "SELECT VAR_POP(amount) FROM sales",
    "Amounts Total": "SELECT COUNT(DISTINCT amount) FROM sales",
    "Distinct Amounts Total": "SELECT SUM(DISTINCT amount) FROM sales",
    "Median Total": "SELECT MEDIAN(amount) FROM sales",
    "Big Sales SD Total": "SELECT STDDEV(amount) FROM sales WHERE amount > 15",
    "Revenue Total": "SELECT SUM(amount) FROM sales",
}


class TestTotals:
    @pytest.mark.parametrize("measure", list(WHOLE))
    def test_every_row_reads_the_aggregate_of_the_whole(self, con: Any, measure: str) -> None:
        rows = _run(con, {"select": {"dimensions": ["Region", "City"], "measures": [measure]}})

        assert {r[2] for r in rows} == {_one(con, WHOLE[measure])}
        assert len(rows) == 4

    def test_the_groups_values_are_not_summed(self, con: Any) -> None:
        """Per region and city the deviations are 1.4, NULL, 28.3 and 17.3;
        the window summed them to 47.0 for the whole's 20.2."""
        rows = _run(con, {"select": {"dimensions": ["Region", "City"], "measures": ["SD Total"]}})

        assert rows[0][2] == pytest.approx(20.2343, abs=1e-4)

    def test_a_default_applies_to_a_total_over_no_rows(self, con: Any) -> None:
        rows = _run(con, {"select": {"dimensions": ["Region"], "measures": ["Empty SD Total"]}})

        assert {r[1] for r in rows} == {0}

    def test_a_query_filter_narrows_the_total(self, con: Any) -> None:
        query = {
            "select": {"dimensions": ["Region"], "measures": ["SD Total"]},
            "where": [{"field": "City", "op": "!=", "value": "d"}],
        }

        rows = _run(con, query)

        assert {r[1] for r in rows} == {
            _one(con, "SELECT STDDEV(amount) FROM sales WHERE city <> 'd'")
        }

    def test_a_having_keeps_the_total_of_every_group(self, con: Any) -> None:
        """As a windowed total does: HAVING filters the rows returned, not the
        rows the total covers."""
        query = {
            "select": {"dimensions": ["Region"], "measures": ["Revenue", "SD Total"]},
            "having": [{"field": "Revenue", "op": ">", "value": 50}],
        }

        rows = _run(con, query)

        assert sorted(r[0] for r in rows) == ["n", "s"]
        assert {r[2] for r in rows} == {_one(con, WHOLE["SD Total"])}

    def test_without_dimensions_the_total_is_the_aggregate(self, con: Any) -> None:
        rows = _run(con, {"select": {"measures": ["SD Total", "Median Total"]}})

        assert rows == [(_one(con, WHOLE["SD Total"]), _one(con, WHOLE["Median Total"]))]

    def test_a_metric_reads_the_total(self, con: Any) -> None:
        rows = _run(con, {"select": {"dimensions": ["Region"], "measures": ["SD per Revenue"]}})

        whole = _one(con, WHOLE["SD Total"])
        revenue = dict(con.sql("SELECT region, SUM(amount) FROM sales GROUP BY 1").fetchall())
        assert {r[0]: r[1] for r in rows} == pytest.approx(
            {region: whole / value for region, value in revenue.items()}
        )

    @pytest.mark.parametrize("grouping", ["rollup", "cube"])
    def test_subtotal_rows_do_not_add_to_the_total(self, con: Any, grouping: str) -> None:
        """The window summed the detail, region and grand-total rows: 3 x 192."""
        query = {
            "select": {
                "dimensions": ["Region", "City"],
                "measures": ["Revenue Total", "SD Total"],
            },
            "grouping": grouping,
        }

        rows = _run(con, query)

        assert {r[2] for r in rows} == {192}
        assert {r[3] for r in rows} == {_one(con, WHOLE["SD Total"])}


REGIONS = {
    "SD Region": "STDDEV(amount)",
    "Amounts Region": "COUNT(DISTINCT amount)",
    "Revenue Region": "SUM(amount)",
}


class TestGrainOverrides:
    @pytest.mark.parametrize("measure", list(REGIONS))
    def test_each_city_reads_its_regions_aggregate(self, con: Any, measure: str) -> None:
        """The NULL region is a group of its own, joined to its own rows."""
        rows = _run(con, {"select": {"dimensions": ["Region", "City"], "measures": [measure]}})

        by_region = dict(
            con.sql(f"SELECT region, {REGIONS[measure]} FROM sales GROUP BY 1").fetchall()
        )
        assert {(r[0], r[1]): r[2] for r in rows} == {(r[0], r[1]): by_region[r[0]] for r in rows}

    def test_under_rollup_a_grain_override_is_refused(self) -> None:
        """A subtotal row's empty region may be rolled up or the NULL member:
        the window counted region n twice, 180 for 90."""
        query = {
            "select": {"dimensions": ["Region", "City"], "measures": ["Revenue Region"]},
            "grouping": "rollup",
        }

        with pytest.raises(ResolutionError) as caught:
            _sql(query)

        assert [e.code for e in caught.value.errors] == ["INCOMPATIBLE_COMBINATION"]


class TestRewrittenRows:
    def test_a_total_beside_a_filter_context_is_refused(self) -> None:
        """The filterContext runs first and leaves a CTE of finished values,
        which the total cannot be computed again from."""
        query = {
            "select": {"dimensions": ["Region"], "measures": ["SD Total", "Revenue Any Region"]}
        }

        with pytest.raises(ResolutionError) as caught:
            _sql(query)

        assert [e.code for e in caught.value.errors] == ["INCOMPATIBLE_COMBINATION"]
        assert "'SD Total'" in caught.value.errors[0].message

    def test_a_windowed_total_beside_a_filter_context_still_compiles(self, con: Any) -> None:
        query = {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Revenue Total", "Revenue Any Region"],
            }
        }

        rows = _run(con, query)

        assert {r[1] for r in rows} == {192}
