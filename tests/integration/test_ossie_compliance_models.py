"""Apache Ossie's compliance models convert, load and answer on DuckDB.

The two models of Ossie's query compliance suites (``compliance/wide_table``
retail and ``compliance/query`` SCOTT) are written in spec 0.3.0.dev0. Before the
converter read that shape they converted to a model with no measures, and the
SCOTT model did not load at all (a self-relationship). Values are the suites'
own expected results, for the requests an OBML query states directly. These
models mark no field as a dimension; every field is one, so they group by any.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.model_store import ModelStore

osi_orionbelt = pytest.importorskip("osi_orionbelt")

_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "osi-orionbelt"
    / "tests"
    / "fixtures"
    / "ossie_compliance"
)


def _load(model_file: str) -> SemanticModel:
    doc = yaml.safe_load((_FIXTURES / model_file).read_text())
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    store = ModelStore()
    result = store.load_model(yaml.safe_dump(obml, sort_keys=False), dedup=False)
    return store.get_model(result.model_id)


def _run(model: SemanticModel, data_file: str, query: dict[str, Any]) -> list[tuple[Any, ...]]:
    sql = CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql
    con = duckdb.connect()
    con.execute((_FIXTURES / data_file).read_text())
    return con.execute(sql).fetchall()


@pytest.fixture(scope="module")
def retail() -> SemanticModel:
    return _load("retail_model.ossie.yaml")


@pytest.fixture(scope="module")
def scott() -> SemanticModel:
    return _load("scott_model.ossie.yaml")


def test_retail_model_metric_counts_customers_without_orders(retail: SemanticModel) -> None:
    # wide_table a08: CAST(SUM(orders.amount) AS DOUBLE) / COUNT(DISTINCT customers.customer_id)
    rows = _run(retail, "retail.sql", {"select": {"measures": ["revenue_per_customer"]}})
    assert rows == [(pytest.approx(162.5),)]


def test_retail_filter_on_a_joined_dataset(retail: SemanticModel) -> None:
    # wide_table a10: total_revenue where customers.region = 'EAST'
    query = {
        "select": {"measures": ["total_revenue"]},
        "where": [{"field": "customers.region", "op": "=", "value": "EAST"}],
    }
    assert _run(retail, "retail.sql", query) == [(350,)]


def test_retail_dataset_metrics_count_their_own_rows(retail: SemanticModel) -> None:
    query = {
        "select": {"measures": ["customer_count", "return_count", "visit_count", "ticket_count"]}
    }
    assert _run(retail, "retail.sql", query) == [(4, 2, 3, 0)]


def test_retail_rejected_metrics_are_not_queryable(retail: SemanticModel) -> None:
    names = set(retail.effective_measures) | set(retail.metrics)
    assert not {"amount_less_order", "running_revenue", "row_count"} & names


def test_retail_groups_come_from_the_metric_dataset(retail: SemanticModel) -> None:
    # wide_table a05: NORTH has no orders, so it is not a group.
    query = {"select": {"dimensions": ["region"], "measures": ["total_revenue"]}}
    assert sorted(_run(retail, "retail.sql", query)) == [("EAST", 350), ("WEST", 300)]


def test_retail_dimension_only(retail: SemanticModel) -> None:
    # wide_table a07: the distinct regions of customers.
    query = {"select": {"dimensions": ["region"]}}
    assert sorted(_run(retail, "retail.sql", query)) == [("EAST",), ("NORTH",), ("WEST",)]


def test_retail_two_metrics_share_a_dimension(retail: SemanticModel) -> None:
    # wide_table a01: a region missing from a branch takes SUM's empty value, NULL.
    query = {"select": {"dimensions": ["region"], "measures": ["total_revenue", "total_returns"]}}
    assert sorted(_run(retail, "retail.sql", query)) == [
        ("EAST", 350, 25),
        ("NORTH", None, 10),
        ("WEST", 300, None),
    ]


def test_scott_measure_per_group(scott: SemanticModel) -> None:
    # query a02: SELECT deptno, AGG(sum_sal), AGG(emp_count) ... GROUP BY deptno
    query = {
        "select": {"dimensions": ["deptno"], "measures": ["sum_sal", "emp_count"]},
        "orderBy": [{"field": "deptno"}],
    }
    assert _run(scott, "scott.sql", query) == [(10, 8750, 3), (20, 10875, 5), (30, 9400, 6)]


def test_a_field_named_like_a_metric_loads() -> None:
    # OBML refuses a model whose dimension and measure share a name.
    doc = copy.deepcopy(_COMPUTED_DOC)
    doc["datasets"][0]["fields"].append({"name": "paid", "expression": {"sql": "status"}})
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    store = ModelStore()
    model = store.get_model(store.load_model(yaml.safe_dump(obml), dedup=False).model_id)
    assert model.dimensions["orders paid"].column == "paid"
    assert "paid" in model.effective_measures


def test_scott_grand_total(scott: SemanticModel) -> None:
    # query a01: SELECT AGG(sum_sal), AGG(emp_count) FROM scott_model.emp
    rows = _run(scott, "scott.sql", {"select": {"measures": ["sum_sal", "emp_count"]}})
    assert rows == [(29025, 14)]


def test_scott_empty_input_set(scott: SemanticModel) -> None:
    # query a07: deptno 40 has no employees: COUNT 0, SUM and AVG NULL.
    query = {
        "select": {"measures": ["emp_count", "sum_sal", "avg_sal"]},
        "where": [{"field": "emp.deptno", "op": "=", "value": 40}],
    }
    assert _run(scott, "scott.sql", query) == [(0, None, None)]


def test_a_quoted_source_column_runs() -> None:
    # SUM(source."Order Total") reads a physical column no field computes; the
    # field added for it is a column name OBML quotes once.
    doc = {
        "version": "0.3.0.dev0",
        "name": "quoted",
        "datasets": [
            {
                "name": "orders",
                "source": "main.orders",
                "primary_key": ["order_id"],
                "fields": [{"name": "order_id", "expression": {"sql": "order_id"}}],
                "metrics": [{"name": "gross", "expression": {"sql": 'SUM(source."Order Total")'}}],
            }
        ],
    }
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    store = ModelStore()
    model = store.get_model(store.load_model(yaml.safe_dump(obml), dedup=False).model_id)
    sql = (
        CompilationPipeline()
        .compile(QueryObject(select={"measures": ["gross"]}), model, "duckdb")
        .sql
    )
    con = duckdb.connect()
    con.execute(
        'CREATE TABLE orders AS SELECT * FROM (VALUES (1, 10), (2, 15)) t(order_id, "Order Total")'
    )
    assert con.execute(sql).fetchall() == [(25,)]


_COMPUTED_DOC: dict[str, Any] = {
    "version": "0.3.0.dev0",
    "name": "computed",
    "datasets": [
        {
            "name": "orders",
            "source": "main.orders",
            "primary_key": ["order_id"],
            "fields": [
                {"name": "order_id", "expression": {"sql": "order_id"}},
                {
                    "name": "amount",
                    "expression": {
                        "sql": "CASE WHEN status = 'paid' THEN amount_cents / 100.0 ELSE 0 END"
                    },
                },
            ],
            "metrics": [{"name": "paid", "expression": {"sql": "SUM(amount)"}}],
        }
    ],
}
# Each quoted identifier in compiled SQL, read left to right.
_QUOTED_IDENTIFIER = re.compile(r'"([^"]*)"|`([^`]*)`')


def _compile_computed(dialect: str) -> str:
    obml = osi_orionbelt.OSItoOBML(copy.deepcopy(_COMPUTED_DOC)).convert()
    store = ModelStore()
    model = store.get_model(store.load_model(yaml.safe_dump(obml), dedup=False).model_id)
    query = QueryObject(select={"measures": ["paid"]})
    return CompilationPipeline().compile(query, model, dialect).sql


@pytest.mark.parametrize(
    "dialect",
    ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "mysql", "postgres", "snowflake"],
)
def test_a_field_computed_by_an_expression_compiles_as_an_expression(dialect: str) -> None:
    sql = _compile_computed(dialect)
    names = [a or b for a, b in _QUOTED_IDENTIFIER.findall(sql)]
    assert "amount_cents" in names
    # Compiled as one name, the expression was a quoted identifier holding "/".
    assert not [name for name in names if "/" in name or "=" in name], sql


def test_a_field_computed_by_an_expression_runs() -> None:
    # The field's SQL used to become the column's code, compiled as one name:
    # "orders"."amount_cents / 100.0". It is a computed column over amount_cents.
    sql = _compile_computed("duckdb")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 1050, 'paid'), (2, 990, 'open'),"
        " (3, 250, 'paid')) t(order_id, amount_cents, status)"
    )
    assert con.execute(sql).fetchall() == [(pytest.approx(13.0),)]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("amount_cents / 1e2", 30.0),
        ('"$amount" * 2', 60.0),
        ('"Amount [USD]" * 2', 60.0),
        ('"amount " * 2', 60.0),
    ],
    ids=["scientific notation", "name no placeholder holds", "bracket", "trailing space"],
)
def test_a_field_obml_writes_differently_still_runs(sql: str, expected: float) -> None:
    doc = copy.deepcopy(_COMPUTED_DOC)
    doc["datasets"][0]["fields"][1]["expression"] = {"sql": sql}
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    store = ModelStore()
    model = store.get_model(store.load_model(yaml.safe_dump(obml), dedup=False).model_id)
    query = QueryObject(select={"measures": ["paid"]})
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 1000, 10, 10, 10), (2, 2000, 20, 20, 20))"
        ' t(order_id, amount_cents, "$amount", "Amount [USD]", "amount ")'
    )
    rows = con.execute(CompilationPipeline().compile(query, model, "duckdb").sql).fetchall()
    assert rows == [(pytest.approx(expected),)]


def test_a_filtered_metric_keeps_every_group() -> None:
    # FILTER (WHERE ...) gates the aggregate's input, never the groups: a region
    # with no completed order still has its row, with no sum and a count of 0.
    doc = {
        "version": "0.3.0.dev0",
        "name": "filtered",
        "datasets": [
            {
                "name": "orders",
                "source": "main.orders",
                "primary_key": ["order_id"],
                "fields": [
                    {"name": n, "expression": {"sql": n}}
                    for n in ("order_id", "region", "status", "amount")
                ],
                "metrics": [
                    {
                        "name": "done_amount",
                        "expression": {"sql": "SUM(amount) FILTER (WHERE status = 'done')"},
                    },
                    {
                        "name": "done_orders",
                        "expression": {"sql": "COUNT(*) FILTER (WHERE status = 'done')"},
                    },
                ],
            }
        ],
    }
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    store = ModelStore()
    model = store.get_model(store.load_model(yaml.safe_dump(obml), dedup=False).model_id)
    query = QueryObject(
        select={"dimensions": ["region"], "measures": ["done_amount", "done_orders"]},
        order_by=[{"field": "region"}],
    )
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 'east', 'done', 10),"
        " (2, 'east', 'open', 5), (3, 'west', 'open', 7), (4, 'east', 'done', 20))"
        " t(order_id, region, status, amount)"
    )
    rows = con.execute(CompilationPipeline().compile(query, model, "duckdb").sql).fetchall()
    assert rows == [("east", 30, 2), ("west", None, 0)]


@pytest.mark.parametrize(
    "sql",
    [
        "SUM(amount) FILTER (WHERE status ILIKE 'done')",
        "SUM(amount) FILTER (WHERE status IS DISTINCT FROM region)",
        "SUM(CASE WHEN order_id % 2 = 0 THEN amount END)",
        "SUM(amount) FILTER (WHERE EXTRACT(DAYOFWEEK FROM order_id) = 1)",
        "SUM(amount) FILTER (WHERE region > CURRENT_TIMESTAMP(3))",
        "SUM(amount) FILTER (WHERE TRIM(BOTH ' ' FROM status) = 'done')",
    ],
    ids=["ilike", "is distinct from", "modulo", "extract dayofweek", "clock precision", "trim"],
)
def test_a_metric_obml_cannot_parse_leaves_the_model_loading(sql: str) -> None:
    # Imported as a measure, the expression stopped the whole model loading
    # (INVALID_MEASURE_EXPRESSION); it is preserved instead.
    doc = {
        "version": "0.3.0.dev0",
        "name": "unparsed",
        "datasets": [
            {
                "name": "orders",
                "source": "main.orders",
                "primary_key": ["order_id"],
                "fields": [
                    {"name": n, "expression": {"sql": n}}
                    for n in ("order_id", "region", "status", "amount")
                ],
                "metrics": [
                    {"name": "kept", "expression": {"sql": sql}},
                    {"name": "total", "expression": {"sql": "SUM(amount)"}},
                ],
            }
        ],
    }
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    store = ModelStore()
    model = store.get_model(store.load_model(yaml.safe_dump(obml), dedup=False).model_id)
    assert "total" in model.measures
    assert "kept" not in model.measures


def test_extract_and_current_date_run() -> None:
    # EXTRACT(YEAR FROM x) is OBML's extract('year', x), and CURRENT_DATE its
    # current_date(): a bare CURRENT_DATE is a string to OBML.
    doc = {
        "version": "0.3.0.dev0",
        "name": "dated",
        "datasets": [
            {
                "name": "orders",
                "source": "main.orders",
                "primary_key": ["order_id"],
                "fields": [
                    {"name": n, "expression": {"sql": n}} for n in ("order_id", "day", "amount")
                ],
                "metrics": [
                    {
                        "name": "this_year",
                        "expression": {
                            "sql": "SUM(amount) FILTER (WHERE EXTRACT(YEAR FROM day) = 2026"
                            " AND day <= CURRENT_DATE)"
                        },
                    }
                ],
            }
        ],
    }
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    store = ModelStore()
    model = store.get_model(store.load_model(yaml.safe_dump(obml), dedup=False).model_id)
    sql = (
        CompilationPipeline()
        .compile(QueryObject(select={"measures": ["this_year"]}), model, "duckdb")
        .sql
    )
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, DATE '2026-01-05', 10),"
        " (2, DATE '2025-06-01', 5), (3, DATE '2999-01-01', 7)) t(order_id, day, amount)"
    )
    assert con.execute(sql).fetchall() == [(10,)]


def _function_samples() -> list[str]:
    from osi_orionbelt._ossie_sql import _FUNCTIONS

    aggregates_and_windows = {
        "SUM", "COUNT", "AVG", "MIN", "MAX", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
        "VARIANCE", "VAR_POP", "VAR_SAMP", "MEDIAN", "PERCENTILE_CONT", "PERCENTILE_DISC",
        "APPROX_COUNT_DISTINCT", "APPROX_PERCENTILE", "ROW_NUMBER", "RANK", "DENSE_RANK",
        "NTILE", "PERCENT_RANK", "CUME_DIST", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE",
        "NTH_VALUE",
    }  # fmt: skip
    return [
        part.strip()
        for name, sample in sorted(_FUNCTIONS.items())
        if name not in aggregates_and_windows
        for part in re.split(r"\s\+\s|\s\|\|\s", sample)
    ]


@pytest.mark.parametrize("call", _function_samples())
def test_every_ossie_function_leaves_the_model_loading(call: str) -> None:
    # Each function of Ossie's expression language, inside a metric: imported as
    # a measure or kept aside, never one that stops the model loading.
    fields = ("id", "x", "y", "d", "s", "a", "b", "c")
    doc = {
        "version": "0.3.0.dev0",
        "name": "functions",
        "datasets": [
            {
                "name": "t",
                "source": "main.t",
                "primary_key": ["id"],
                "fields": [{"name": n, "expression": {"sql": n}} for n in fields],
                "metrics": [{"name": "m", "expression": {"sql": f"COUNT({call})"}}],
            }
        ],
    }
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    ModelStore().load_model(yaml.safe_dump(obml), dedup=False)


def test_the_clock_functions_run() -> None:
    # CURRENT_TIMESTAMP and CURRENT_TIME are OBML's current_timestamp() and
    # current_time(): the session's clock, as the source's, with no queryTimezone.
    doc = {
        "version": "0.3.0.dev0",
        "name": "clocked",
        "datasets": [
            {
                "name": "events",
                "source": "main.events",
                "primary_key": ["event_id"],
                "fields": [
                    {"name": n, "expression": {"sql": n}} for n in ("event_id", "happened", "opens")
                ],
                "metrics": [
                    {
                        "name": "past",
                        "expression": {
                            "sql": "COUNT(*) FILTER (WHERE happened <= CURRENT_TIMESTAMP)"
                        },
                    },
                    {
                        "name": "open_now",
                        "expression": {"sql": "COUNT(*) FILTER (WHERE opens <= CURRENT_TIME)"},
                    },
                ],
            }
        ],
    }
    obml = osi_orionbelt.OSItoOBML(doc).convert()
    store = ModelStore()
    model = store.get_model(store.load_model(yaml.safe_dump(obml), dedup=False).model_id)
    query = QueryObject(select={"measures": ["past", "open_now"]})
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE events AS SELECT * FROM (VALUES"
        " (1, TIMESTAMP '2001-01-01 00:00:00', TIME '00:00:00'),"
        " (2, TIMESTAMP '2999-01-01 00:00:00', TIME '23:59:59.999999'))"
        " t(event_id, happened, opens)"
    )
    past, open_now = con.execute(sql).fetchone()  # type: ignore[misc]
    assert past == 1
    assert open_now in (1, 2)  # 2 only in the last microsecond of the day
