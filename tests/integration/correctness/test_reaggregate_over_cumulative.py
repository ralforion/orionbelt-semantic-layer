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
    "YTD Months": {
        "type": "reaggregate",
        "measure": "YTD Sales",
        "per": ["Sales Month"],
        "aggregation": "count",
    },
    "Max Monthly YTD": {
        "type": "reaggregate",
        "measure": "Avg Monthly YTD",
        "per": ["Sales Month"],
        "aggregation": "max",
    },
    **{
        f"{agg.title()} By {'Year First' if year_first else 'Month First'}": {
            "type": "reaggregate",
            "measure": "YTD Sales",
            "per": ["Sales Month:year", "Sales Month"]
            if year_first
            else ["Sales Month", "Sales Month:year"],
            "aggregation": agg,
        }
        for agg in ("avg", "count")
        for year_first in (True, False)
    },
    "Avg Daily YTD": {
        "type": "reaggregate",
        "measure": "YTD Sales",
        "per": ["Sales Month:day", "Sales Month"],
        "aggregation": "avg",
    },
    "Avg Daily YTD Reversed": {
        "type": "reaggregate",
        "measure": "YTD Sales",
        "per": ["Sales Month", "Sales Month:day"],
        "aggregation": "avg",
    },
    "Avg Weekly YTD": {
        "type": "reaggregate",
        "measure": "YTD Sales",
        "per": ["Sales Month:week", "Sales Month"],
        "aggregation": "avg",
    },
    "Avg Weekly YTD Reversed": {
        "type": "reaggregate",
        "measure": "YTD Sales",
        "per": ["Sales Month", "Sales Month:week"],
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


@pytest.mark.parametrize("from_february", [False, True])
def test_query_by_the_same_dimension_at_a_coarser_grain(run: Callable, from_february: bool) -> None:
    """``Sales Month:year`` beside ``per: [Sales Month]``: two grains of one
    dimension in stage 1, kept apart before the cumulative is planned, answer
    as the separate ``Sales Year`` dimension does. Named alike, the look-back
    joined the months by year and multiplied them (132 months for 11)."""
    where = [QueryFilter(field="Sales Month", op=">=", value="2021-02-01")] if from_february else []
    measures = ["Avg Monthly YTD", "YTD Months"]
    by_year = sorted(
        (r["Sales Year"], r["Avg Monthly YTD"], r["YTD Months"])
        for r in run(["Sales Year"], measures, where=where)
    )
    same = sorted(
        (r["Sales Month"], r["Avg Monthly YTD"], r["YTD Months"])
        for r in run(["Sales Month:year"], measures, where=where)
    )
    assert [row[1:] for row in same] == [row[1:] for row in by_year]
    assert all(row[2] <= 12 for row in same)


_FEBRUARY = [QueryFilter(field="Sales Month", op=">=", value="2021-02-01")]


def test_nested_over_the_same_dimension_at_a_coarser_grain(run: Callable) -> None:
    """The axes are kept apart down the chain: an outer reaggregate metric
    passes ``Sales Month:year`` and its own ``per`` to the inner one."""
    by_year = sorted(
        (r["Sales Year"], r["Max Monthly YTD"])
        for r in run(["Sales Year"], ["Max Monthly YTD"], where=_FEBRUARY)
    )
    same = sorted(
        (r["Sales Month"], r["Max Monthly YTD"])
        for r in run(["Sales Month:year"], ["Max Monthly YTD"], where=_FEBRUARY)
    )
    assert [v for _, v in same] == [v for _, v in by_year]


def test_two_grains_of_one_dimension_in_per(run: Callable) -> None:
    """``per: [Sales Month:year, Sales Month]`` answers as the reversed order:
    the cumulative's own grain keeps the name, whichever comes first."""
    year_first = ["Avg By Year First", "Count By Year First"]
    month_first = ["Avg By Month First", "Count By Month First"]
    a = run(["Country Name"], year_first, where=_FEBRUARY)
    b = {r["Country Name"]: r for r in run(["Country Name"], month_first, where=_FEBRUARY)}
    assert a
    for r in a:
        other = b[r["Country Name"]]
        assert (r[year_first[0]], r[year_first[1]]) == (
            other[month_first[0]],
            other[month_first[1]],
        )
        assert r[year_first[1]] <= 60


def test_finer_grain_in_per_orders_the_running_values(run: Callable) -> None:
    """``per: [Sales Month:day, Sales Month]``: stage 1 has a row per day, so
    the running window orders by day; ordered by month, the days of a month
    tied and the answer depended on the order rows arrived in."""
    daily: dict[Any, list[tuple[Any, Decimal]]] = defaultdict(list)
    for r in run(["Country Name", "Sales Date"], ["Total Sales"]):
        if r["Total Sales"] is not None and r["Sales Date"] is not None:
            daily[r["Country Name"]].append((r["Sales Date"], r["Total Sales"]))
    want: dict[Any, Decimal] = {}
    for country, pairs in daily.items():
        running: list[Decimal] = []
        total, year = Decimal(0), None
        for day, value in sorted(pairs):
            if day.year != year:
                total, year = Decimal(0), day.year
            total += value
            running.append(total)
        want[country] = sum(running) / len(running)
    rows = run(["Country Name"], ["Avg Daily YTD", "Avg Daily YTD Reversed"])
    assert rows
    for r in rows:
        assert r["Avg Daily YTD"] == r["Avg Daily YTD Reversed"], r
        assert _close(r["Avg Daily YTD"], want[r["Country Name"]]), r


def _daily_running_ytd(run: Callable) -> dict[tuple[Any, Any], Decimal]:
    """Each country's and day's year-to-date sales, accumulated by day."""
    daily: dict[Any, list[tuple[Any, Decimal]]] = defaultdict(list)
    for r in run(["Country Name", "Sales Date"], ["Total Sales"]):
        if r["Total Sales"] is not None and r["Sales Date"] is not None:
            daily[r["Country Name"]].append((r["Sales Date"], r["Total Sales"]))
    out: dict[tuple[Any, Any], Decimal] = {}
    for country, pairs in daily.items():
        total, year = Decimal(0), None
        for day, value in sorted(pairs):
            if day.year != year:
                total, year = Decimal(0), day.year
            total += value
            out[(country, day)] = total
    return out


def test_separately_named_dimension_on_the_same_axis(run: Callable) -> None:
    """The query's daily ``Sales Date`` beside ``per: [Sales Month]``: another
    dimension over the same date column orders the running window too, so a
    day's value is the year-to-date at that day, not a tie within its month."""
    want = _daily_running_ytd(run)
    rows = run(["Country Name", "Sales Date"], ["Avg Monthly YTD"])
    assert rows
    for r in rows:
        assert r["Avg Monthly YTD"] == want[(r["Country Name"], r["Sales Date"])], r


def test_plain_cumulative_breaks_ties_on_the_same_axis(run: Callable) -> None:
    """The same in an ordinary cumulative query: ``YTD Sales`` by month beside
    the daily dimension accumulates by day, deterministically."""
    want = _daily_running_ytd(run)
    rows = run(["Country Name", "Sales Month", "Sales Date"], ["YTD Sales"])
    assert rows
    for r in rows:
        assert r["YTD Sales"] == want[(r["Country Name"], r["Sales Date"])], r


def test_overlapping_week_buckets(run: Callable) -> None:
    """``per: [Sales Month:week, Sales Month]``: a week split across two months
    is two stage-1 rows, ordered by week and then month, in either order."""
    rows = run(["Country Name"], ["Avg Weekly YTD", "Avg Weekly YTD Reversed"])
    assert rows
    for r in rows:
        assert r["Avg Weekly YTD"] == r["Avg Weekly YTD Reversed"], r
