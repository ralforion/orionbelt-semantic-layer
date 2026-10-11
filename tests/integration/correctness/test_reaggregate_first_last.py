"""``first`` / ``last`` as a reaggregate metric's second stage (DuckDB, commerce).

Stage 1 is the measure per month; stage 2 takes the value of the earliest /
latest month in each group of the query, ordered by the one time dimension in
``per``. The reference is worked out in Python from the monthly totals.
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

_METRICS = {
    f"{aggregation.title()} Month Sales": {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Date:month"],
        "aggregation": aggregation,
    }
    for aggregation in ("first", "last")
}
# Over another reaggregate metric: each year's last month of the average client.
_METRICS["Avg Client Sales"] = {
    "type": "reaggregate",
    "measure": "Total Sales",
    "per": ["Sales Client Name"],
    "aggregation": "avg",
}
_METRICS["Year-End Avg Client Sales"] = {
    "type": "reaggregate",
    "measure": "Avg Client Sales",
    "per": ["Sales Date:month"],
    "aggregation": "last",
}
# The bare name of a month-grained dimension is a time entry too.
_METRICS["Closing Month Sales"] = {
    "type": "reaggregate",
    "measure": "Total Sales",
    "per": ["Sales Month"],
    "aggregation": "last",
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


def _monthly(run: Callable, dimensions: list[str], **query: Any) -> dict[Any, list[Any]]:
    """Each group's (month, total) pairs, sorted by month."""
    months: dict[Any, list[Any]] = defaultdict(list)
    for row in run([*dimensions, "Sales Month"], ["Total Sales"], **query):
        if row["Total Sales"] is not None:
            group = tuple(row[d] for d in dimensions)
            months[group].append((row["Sales Month"], Decimal(str(row["Total Sales"]))))
    return {group: sorted(pairs) for group, pairs in months.items()}


def _check(rows: list[dict[str, Any]], monthly: dict[Any, list[Any]], dims: list[str]) -> None:
    assert rows
    for row in rows:
        pairs = monthly[tuple(row[d] for d in dims)]
        want = {"First Month Sales": pairs[0][1], "Last Month Sales": pairs[-1][1]}
        got = {n: Decimal(str(row[n])) for n in want}
        assert got == want, row


def test_by_year(run: Callable) -> None:
    rows = run(["Sales Year"], ["First Month Sales", "Last Month Sales"])
    _check(rows, _monthly(run, ["Sales Year"]), ["Sales Year"])


def test_by_country_and_year(run: Callable) -> None:
    dims = ["Country Name", "Sales Year"]
    rows = run(dims, ["First Month Sales", "Last Month Sales"])
    _check(rows, _monthly(run, dims), dims)


def test_by_the_same_date_at_a_coarser_grain(run: Callable) -> None:
    """The query's ``Sales Date:year`` beside the per's ``Sales Date:month``: the
    month bucket is renamed in stage 1, and still the key."""
    rows = run(["Sales Date:year"], ["Last Month Sales"])
    years = _monthly(run, ["Sales Year"])
    got = {str(r["Sales Date"])[:4]: Decimal(str(r["Last Month Sales"])) for r in rows}
    assert got == {str(y[0])[:4]: pairs[-1][1] for y, pairs in years.items()}


def test_declared_month_dimension(run: Callable) -> None:
    rows = run(["Sales Year"], ["Last Month Sales", "Closing Month Sales"])
    assert all(r["Last Month Sales"] == r["Closing Month Sales"] for r in rows)


def test_over_another_reaggregate_metric(run: Callable) -> None:
    by_month: dict[Any, list[Any]] = defaultdict(list)
    for row in run(["Sales Year", "Sales Month"], ["Avg Client Sales"]):
        by_month[row["Sales Year"]].append((row["Sales Month"], row["Avg Client Sales"]))
    rows = run(["Sales Year"], ["Year-End Avg Client Sales"])
    got = {r["Sales Year"]: r["Year-End Avg Client Sales"] for r in rows}
    assert got == {year: max(pairs)[1] for year, pairs in by_month.items()}


def test_by_month_is_the_month(run: Callable) -> None:
    """The query already groups by month: one value per group, the measure's."""
    rows = run(["Sales Month"], ["Last Month Sales", "Total Sales"])
    assert all(r["Last Month Sales"] == r["Total Sales"] for r in rows)


def test_under_a_filter(run: Callable) -> None:
    germany = [QueryFilter(field="Country Name", op="=", value="Germany")]
    rows = run(["Sales Year"], ["First Month Sales", "Last Month Sales"], where=germany)
    _check(rows, _monthly(run, ["Sales Year"], where=germany), ["Sales Year"])


def test_the_two_differ(run: Callable) -> None:
    rows = run(["Sales Year"], ["First Month Sales", "Last Month Sales"])
    assert any(r["First Month Sales"] != r["Last Month Sales"] for r in rows)


@pytest.mark.parametrize(
    ("per", "found"),
    [(["Country Name"], "found none"), (["Sales Date:month", "Sales Date:day"], "found ")],
)
def test_order_key_required(per: list[str], found: str) -> None:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["metrics"]["Probe"] = {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": per,
        "aggregation": "last",
    }
    _model, result = ReferenceResolver().resolve(raw, source_map)
    errors = [e for e in result.errors if e.code == "REAGGREGATE_ORDER_KEY"]
    assert errors and found in errors[0].message, result.errors
