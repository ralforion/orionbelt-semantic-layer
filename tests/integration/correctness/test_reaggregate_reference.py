"""Reaggregate metrics against hand-written two-stage SQL on the commerce seed.

Each metric is a measure aggregated per group, then aggregated again. The
reference SQL spells both stages out by hand over the physical tables; the
compiled query has to return the same rows. The stage-1 value is the measure as
declared (``Total Sales`` is ``decimal(18, 2)``), and an ``avg`` second stage is
cast to the model default, so the reference applies the same two casts and the
comparison is exact.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryFilter, QueryObject, QueryOrderBy, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

_METRICS: dict[str, dict[str, Any]] = {
    "Avg Sales per Client": {
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "avg",
    },
    "Max Sales per Client": {
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "max",
    },
    "Min Sales per Client": {
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "min",
    },
    "Sum Sales per Client": {
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "sum",
    },
    "Buying Clients": {
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "count",
    },
    "Avg Orders per Client": {
        "measure": "Sales Count",
        "per": ["Sales Client Name"],
        "aggregation": "avg",
    },
    "Avg Monthly Sales": {"measure": "Total Sales", "per": ["Sales Month"], "aggregation": "avg"},
}

# Stage 1 per client, by country: the joins the model declares, by hand.
_PER_CLIENT_BY_COUNTRY = """
    SELECT co.countryname AS country, c.clientname AS client,
           CAST(SUM(s.salesamount) AS DECIMAL(18, 2)) AS sales,
           COUNT(*) AS orders
    FROM orionbelt_1.sales s
    LEFT JOIN orionbelt_1.clients   c  ON s.salesclient     = c.clientid
    LEFT JOIN orionbelt_1.countries co ON c.clientcountryid = co.countryid
    {where}
    GROUP BY co.countryname, c.clientname
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    for name, body in _METRICS.items():
        raw["metrics"][name] = {"type": "reaggregate", **body}
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture(scope="module")
def run(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection
) -> Callable[[QueryObject], list[dict[str, Any]]]:
    pipeline = CompilationPipeline()

    def _run(query: QueryObject) -> list[dict[str, Any]]:
        return _rows_as_dicts(commerce_db, pipeline.compile(query, model, "duckdb").sql)

    return _run


@pytest.fixture(scope="module")
def ref(commerce_db: duckdb.DuckDBPyConnection) -> Callable[[str], list[dict[str, Any]]]:
    return lambda sql: _rows_as_dicts(commerce_db, sql)


def _by(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row[key]: row for row in rows}


def test_per_client_by_country(run: Callable, ref: Callable) -> None:
    rows = run(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"],
                measures=[
                    "Total Sales",
                    "Avg Sales per Client",
                    "Max Sales per Client",
                    "Min Sales per Client",
                    "Buying Clients",
                    "Avg Orders per Client",
                ],
            )
        )
    )
    expected = ref(
        f"""
        SELECT country,
               SUM(sales) AS total,
               CAST(AVG(sales) AS DECIMAL(18, 2)) AS avg_sales,
               MAX(sales) AS max_sales,
               MIN(sales) AS min_sales,
               COUNT(sales) AS clients,
               CAST(AVG(orders) AS DECIMAL(18, 2)) AS avg_orders
        FROM ({_PER_CLIENT_BY_COUNTRY.format(where="")})
        GROUP BY country
        """
    )
    got = _by(rows, "Sales Country Name")
    assert set(got) == {row["country"] for row in expected}
    for row in expected:
        actual = got[row["country"]]
        assert actual["Total Sales"] == row["total"]
        assert actual["Avg Sales per Client"] == row["avg_sales"]
        assert actual["Max Sales per Client"] == row["max_sales"]
        assert actual["Min Sales per Client"] == row["min_sales"]
        assert actual["Buying Clients"] == row["clients"]
        assert actual["Avg Orders per Client"] == row["avg_orders"]


def test_unweighted_average_is_total_over_clients(run: Callable) -> None:
    """AVG of per-client totals is the total over the number of buying clients."""
    rows = run(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"],
                measures=["Total Sales", "Avg Sales per Client", "Buying Clients"],
            )
        )
    )
    for row in rows:
        expected = (row["Total Sales"] / row["Buying Clients"]).quantize(Decimal("0.01"))
        assert row["Avg Sales per Client"] == expected


def test_sum_reaggregate_equals_the_measure(run: Callable) -> None:
    rows = run(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"],
                measures=["Total Sales", "Sum Sales per Client"],
            )
        )
    )
    assert rows
    for row in rows:
        assert row["Sum Sales per Client"] == row["Total Sales"]


def test_ungrouped(run: Callable, ref: Callable) -> None:
    rows = run(QueryObject(select=QuerySelect(measures=["Avg Sales per Client"])))
    expected = ref(
        """
        SELECT CAST(AVG(sales) AS DECIMAL(18, 2)) AS v FROM (
            SELECT c.clientname, CAST(SUM(s.salesamount) AS DECIMAL(18, 2)) AS sales
            FROM orionbelt_1.sales s
            LEFT JOIN orionbelt_1.clients c ON s.salesclient = c.clientid
            GROUP BY c.clientname
        )
        """
    )
    assert rows == [{"Avg Sales per Client": expected[0]["v"]}]


def test_monthly_average_by_year(run: Callable, ref: Callable) -> None:
    rows = run(
        QueryObject(select=QuerySelect(dimensions=["Sales Year"], measures=["Avg Monthly Sales"]))
    )
    expected = ref(
        """
        SELECT y, CAST(AVG(sales) AS DECIMAL(18, 2)) AS v FROM (
            SELECT CAST(DATE_TRUNC('year', salesdate) AS DATE) AS y,
                   CAST(DATE_TRUNC('month', salesdate) AS DATE) AS m,
                   CAST(SUM(salesamount) AS DECIMAL(18, 2)) AS sales
            FROM orionbelt_1.sales
            GROUP BY 1, 2
        )
        GROUP BY y
        """
    )
    assert {r["Sales Year"]: r["Avg Monthly Sales"] for r in rows} == {
        r["y"]: r["v"] for r in expected
    }


def test_where_applies_to_the_first_stage(run: Callable, ref: Callable) -> None:
    """A row filter shrinks each client's total, not just which clients count."""
    rows = run(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"], measures=["Avg Sales per Client"]
            ),
            where=[QueryFilter(field="Product Category", op="=", value="Electronics")],
        )
    )
    where = (
        "JOIN orionbelt_1.products p ON s.product = p.productid WHERE p.productcat = 'Electronics'"
    )
    expected = ref(
        f"""
        SELECT country, CAST(AVG(sales) AS DECIMAL(18, 2)) AS v
        FROM ({_PER_CLIENT_BY_COUNTRY.format(where=where)})
        GROUP BY country
        """
    )
    assert rows
    assert {r["Sales Country Name"]: r["Avg Sales per Client"] for r in rows} == {
        r["country"]: r["v"] for r in expected
    }


def test_order_by_and_limit(run: Callable) -> None:
    full = run(
        QueryObject(
            select=QuerySelect(dimensions=["Sales Country Name"], measures=["Avg Sales per Client"])
        )
    )
    top = run(
        QueryObject(
            select=QuerySelect(
                dimensions=["Sales Country Name"], measures=["Avg Sales per Client"]
            ),
            order_by=[QueryOrderBy(field="Avg Sales per Client", direction="desc")],
            limit=3,
        )
    )
    ranked = sorted(full, key=lambda r: r["Avg Sales per Client"], reverse=True)
    assert [r["Avg Sales per Client"] for r in top] == [
        r["Avg Sales per Client"] for r in ranked[:3]
    ]
