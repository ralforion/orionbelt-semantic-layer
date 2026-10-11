"""Window metrics over a reaggregate metric (DuckDB, commerce).

The window runs over the query's result rows after the reaggregate pass has
finished them, so it reads the same values the reaggregate metric shows. The
reference is worked out in Python from those values.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

_AVG = "Avg Client Sales"
_METRICS: dict[str, dict[str, Any]] = {
    _AVG: {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "avg",
    },
    "Prior Avg Client": {
        "type": "window",
        "measure": _AVG,
        "windowFunction": "lag",
        "timeDimension": "Sales Month",
        "offset": 1,
    },
    "Prior Avg Client By Country": {
        "type": "window",
        "measure": _AVG,
        "windowFunction": "lag",
        "timeDimension": "Sales Month",
        "offset": 1,
        "partitionBy": ["Country Name"],
    },
    "Avg Client Rank": {"type": "window", "measure": _AVG, "windowFunction": "rank"},
    "Avg Client Change": {"expression": "{[Avg Client Sales]} - {[Prior Avg Client]}"},
    # A formula over the change: nested derived metrics expand in place.
    "Avg Client Change Doubled": {"expression": "{[Avg Client Change]} * 2"},
    # A plain window beside: still the window pass's.
    "Sales Rank": {"type": "window", "measure": "Total Sales", "windowFunction": "rank"},
    "Mixed": {"expression": "{[Prior Avg Client]} - {[Sales Rank]}"},
}


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["metrics"].update(_METRICS)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture(scope="module")
def run(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection
) -> Callable[..., list[dict[str, Any]]]:
    pipeline = CompilationPipeline()

    def _run(dimensions: list[str], measures: list[str], **query: Any) -> list[dict[str, Any]]:
        q = QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures), **query)
        return _rows_as_dicts(commerce_db, pipeline.compile(q, model, "duckdb").sql)

    return _run


def _by_month(run: Callable, **query: Any) -> list[tuple[Any, Any]]:
    rows = run(["Sales Month"], [_AVG], **query)
    return sorted((r["Sales Month"], r[_AVG]) for r in rows)


def test_lag_by_month(run: Callable) -> None:
    months = _by_month(run)
    rows = run(["Sales Month"], ["Prior Avg Client"])
    assert list(rows[0]) == ["Sales Month", "Prior Avg Client"]
    got = {r["Sales Month"]: r["Prior Avg Client"] for r in rows}
    want = {month: (months[i - 1][1] if i else None) for i, (month, _) in enumerate(months)}
    assert got == want


def test_formula_over_the_window(run: Callable) -> None:
    months = dict(_by_month(run))
    prior = {
        r["Sales Month"]: r["Prior Avg Client"] for r in run(["Sales Month"], ["Prior Avg Client"])
    }
    rows = run(["Sales Month"], ["Avg Client Change", "Avg Client Change Doubled"])
    for r in rows:
        month = r["Sales Month"]
        want = None if prior[month] is None else months[month] - prior[month]
        assert r["Avg Client Change"] == want, month
        assert r["Avg Client Change Doubled"] == (None if want is None else want * 2), month


def test_rank_by_country(run: Callable) -> None:
    values = {r["Country Name"]: r[_AVG] for r in run(["Country Name"], [_AVG])}
    rows = run(["Country Name"], ["Avg Client Rank"])
    for r in rows:
        want = 1 + sum(1 for v in values.values() if v > values[r["Country Name"]])
        assert r["Avg Client Rank"] == want, r


def test_lag_partitioned_by_country(run: Callable) -> None:
    rows = run(["Country Name", "Sales Month"], [_AVG, "Prior Avg Client By Country"])
    series: dict[Any, list[Any]] = defaultdict(list)
    for r in rows:
        series[r["Country Name"]].append(r)
    for country_rows in series.values():
        ordered = sorted(country_rows, key=lambda r: r["Sales Month"])
        for i, r in enumerate(ordered):
            want = ordered[i - 1][_AVG] if i else None
            assert r["Prior Avg Client By Country"] == want, r


def test_limit_applies_after_the_window(run: Callable) -> None:
    """The window reads every month; LIMIT cuts the result after it."""
    months = _by_month(run)
    rows = run(
        ["Sales Month"],
        ["Prior Avg Client"],
        order_by=[{"field": "Sales Month", "direction": "desc"}],
        limit=1,
    )
    assert [(r["Sales Month"], r["Prior Avg Client"]) for r in rows] == [
        (months[-1][0], months[-2][1])
    ]


def test_under_a_filter(run: Callable) -> None:
    germany = [QueryFilter(field="Country Name", op="=", value="Germany")]
    months = _by_month(run, where=germany)
    rows = run(["Sales Month"], ["Prior Avg Client"], where=germany)
    got = {r["Sales Month"]: r["Prior Avg Client"] for r in rows}
    assert got == {m: (months[i - 1][1] if i else None) for i, (m, _) in enumerate(months)}


def test_beside_other_measures(run: Callable) -> None:
    rows = run(["Sales Month"], ["Total Sales", _AVG, "Prior Avg Client", "Sales Rank"])
    assert list(rows[0]) == ["Sales Month", "Total Sales", _AVG, "Prior Avg Client", "Sales Rank"]
    sales = sorted(r["Total Sales"] for r in rows)
    assert {r["Sales Rank"] for r in rows if r["Total Sales"] == sales[-1]} == {1}


def test_formula_mixing_a_plain_window(run: Callable) -> None:
    """Read from finished columns, a plain window's included the same way."""
    finished = {
        r["Sales Month"]: (r["Prior Avg Client"], r["Sales Rank"])
        for r in run(["Sales Month"], ["Prior Avg Client", "Sales Rank"])
    }
    rows = run(["Sales Month"], ["Mixed"])
    assert list(rows[0]) == ["Sales Month", "Mixed"]
    for r in rows:
        prior, rank = finished[r["Sales Month"]]
        assert r["Mixed"] == (None if prior is None else prior - rank), r
