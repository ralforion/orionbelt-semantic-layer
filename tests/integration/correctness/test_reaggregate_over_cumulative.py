"""A reaggregate metric over a cumulative metric (DuckDB, commerce).

Stage 1 is the cumulative metric planned as a query of its own at the query's
dimensions plus ``per``, which names its time dimension: one running value per
period. Stage 2 aggregates those values again: the average monthly
year-to-date, the year-end year-to-date. The reference is worked out in Python
from the cumulative metric's own values.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

#: ``YTD Sales`` is the commerce model's: Total Sales, grain-to-date year, by
#: Sales Month.
_METRICS: dict[str, dict[str, Any]] = {
    "Avg Monthly YTD": {
        "type": "reaggregate",
        "measure": "YTD Sales",
        "per": ["Sales Month"],
        "aggregation": "avg",
    },
    "Year End YTD": {
        "type": "reaggregate",
        "measure": "YTD Sales",
        "per": ["Sales Month"],
        "aggregation": "last",
    },
}


def _resolve(extra: dict[str, Any]) -> tuple[SemanticModel, list[tuple[str, str]]]:
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["metrics"].update(extra)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    return resolved, [(e.code, e.path or "") for e in result.errors]


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    resolved, errors = _resolve(_METRICS)
    assert not errors, errors
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


def _ytd_by(run: Callable, dims: list[str], **query: Any) -> dict[Any, list[tuple[Any, Any]]]:
    """Each group's (month, YTD) pairs, by month."""
    groups: dict[Any, list[tuple[Any, Any]]] = defaultdict(list)
    for r in run([*dims, "Sales Month"], ["YTD Sales"], **query):
        if r["YTD Sales"] is not None:
            groups[tuple(r[d] for d in dims)].append((r["Sales Month"], r["YTD Sales"]))
    return {g: sorted(pairs) for g, pairs in groups.items()}


def _close(got: Any, want: Decimal) -> bool:
    return abs(Decimal(str(got)) - want) <= Decimal("0.01")


def test_by_year(run: Callable) -> None:
    ytd = _ytd_by(run, ["Sales Year"])
    rows = run(["Sales Year"], ["Avg Monthly YTD", "Year End YTD"])
    assert rows
    for r in rows:
        values = [v for _, v in ytd[(r["Sales Year"],)]]
        assert _close(r["Avg Monthly YTD"], sum(values) / len(values)), r
        assert r["Year End YTD"] == values[-1], r


def test_by_country_and_year(run: Callable) -> None:
    dims = ["Country Name", "Sales Year"]
    ytd = _ytd_by(run, dims)
    for r in run(dims, ["Avg Monthly YTD"]):
        values = [v for _, v in ytd[(r["Country Name"], r["Sales Year"])]]
        assert _close(r["Avg Monthly YTD"], sum(values) / len(values)), r


def test_time_filter_shows_months_but_ytd_reads_past_it(run: Callable) -> None:
    """From June the running values still start in January: the cumulative in
    stage 1 reads past the filter, the months averaged are June onwards."""
    june = [QueryFilter(field="Sales Month", op=">=", value="2021-06-01")]
    ytd = _ytd_by(run, ["Sales Year"], where=june)
    full = _ytd_by(run, ["Sales Year"])
    rows = run(["Sales Year"], ["Avg Monthly YTD"], where=june)
    for r in rows:
        values = [v for _, v in ytd[(r["Sales Year"],)]]
        assert _close(r["Avg Monthly YTD"], sum(values) / len(values)), r
    # The first month shown carries the running value the unfiltered query
    # shows for it, January to June, not June alone.
    year = (rows[0]["Sales Year"],)
    first_shown = ytd[year][0]
    assert first_shown in full[year]


@pytest.mark.parametrize(
    ("metrics", "code"),
    [
        (
            {
                "Bad": {
                    "type": "reaggregate",
                    "measure": "YTD Sales",
                    "per": ["Country Name"],
                    "aggregation": "avg",
                }
            },
            "REAGGREGATE_CUMULATIVE_TIME_DIMENSION",
        ),
        (
            {
                "Running Avg": {
                    "type": "cumulative",
                    "measure": "Total Sales",
                    "timeDimension": "Sales Month",
                    "cumulativeType": "avg",
                },
                "Bad": {
                    "type": "reaggregate",
                    "measure": "Running Avg",
                    "per": ["Sales Month"],
                    "aggregation": "avg",
                },
            },
            "REAGGREGATE_AVG_OF_AVG",
        ),
        (
            {
                "Loop Cumulative": {
                    "type": "cumulative",
                    "measure": "Bad",
                    "timeDimension": "Sales Month",
                },
                "Bad": {
                    "type": "reaggregate",
                    "measure": "Loop Cumulative",
                    "per": ["Sales Month"],
                    "aggregation": "max",
                },
            },
            "REAGGREGATE_CYCLE",
        ),
    ],
)
def test_refused(metrics: dict[str, Any], code: str) -> None:
    _require_seed()
    _model, errors = _resolve(metrics)
    assert code in {c for c, _ in errors}, errors


def test_avg_over_a_running_sum_allowed() -> None:
    """A running sum is no average, so averaging its values is allowed."""
    _require_seed()
    _model, errors = _resolve(
        {
            "Ok": {
                "type": "reaggregate",
                "measure": "YTD Sales",
                "per": ["Sales Month"],
                "aggregation": "avg",
            }
        }
    )
    assert errors == []
