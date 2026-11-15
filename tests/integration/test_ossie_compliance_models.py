"""Apache Ossie's compliance models convert, load and answer on DuckDB.

The two models of Ossie's query compliance suites (``compliance/wide_table``
retail and ``compliance/query`` SCOTT) are written in spec 0.3.0.dev0. Before the
converter read that shape they converted to a model with no measures, and the
SCOTT model did not load at all (a self-relationship). Values are the suites'
own expected results, for the requests an OBML query states directly: these
models mark no field as a dimension, so the grouped cases are not reachable.
"""

from __future__ import annotations

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
