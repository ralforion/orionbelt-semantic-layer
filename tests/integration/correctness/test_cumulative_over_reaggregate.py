"""Cumulative metrics over a reaggregate metric (DuckDB, commerce).

Running, grain-to-date and rolling windows over the reaggregate metric's
finished values per month. A filter on the time dimension limits the months
shown, not the months read: the running value from March still adds January
and February, as for every cumulative metric. The reference is worked out in
Python from the reaggregate metric's own values.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.models.query import (  # noqa: E402
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryObject,
    QuerySelect,
    Subquery,
)
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
    "Running Avg Client": {
        "type": "cumulative",
        "measure": _AVG,
        "timeDimension": "Sales Month",
        "cumulativeType": "sum",
    },
    "YTD Avg Client": {
        "type": "cumulative",
        "measure": _AVG,
        "timeDimension": "Sales Month",
        "cumulativeType": "sum",
        "grainToDate": "year",
    },
    "Rolling 3 Avg Client": {
        "type": "cumulative",
        "measure": _AVG,
        "timeDimension": "Sales Month",
        "cumulativeType": "avg",
        "window": 3,
    },
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


def _series(run: Callable, dims: list[str], **query: Any) -> dict[Any, list[tuple[Any, Any]]]:
    """Each group's (month, value) pairs of the reaggregate metric, by month."""
    groups: dict[Any, list[tuple[Any, Any]]] = defaultdict(list)
    for r in run([*dims, "Sales Month"], [_AVG], **query):
        groups[tuple(r[d] for d in dims)].append((r["Sales Month"], r[_AVG]))
    return {g: sorted(pairs) for g, pairs in groups.items()}


def _running(pairs: list[tuple[Any, Any]]) -> dict[Any, Decimal]:
    out: dict[Any, Decimal] = {}
    total = Decimal(0)
    for month, value in pairs:
        total += value or 0
        out[month] = total
    return out


def test_running_by_month(run: Callable) -> None:
    want = _running(_series(run, [])[()])
    rows = run(["Sales Month"], ["Running Avg Client"])
    assert {r["Sales Month"]: r["Running Avg Client"] for r in rows} == want


def test_running_per_country(run: Callable) -> None:
    series = _series(run, ["Country Name"])
    rows = run(["Country Name", "Sales Month"], ["Running Avg Client"])
    for r in rows:
        want = _running(series[(r["Country Name"],)])[r["Sales Month"]]
        assert r["Running Avg Client"] == want, r


def test_year_to_date_restarts_each_year(run: Callable) -> None:
    pairs = _series(run, [])[()]
    want: dict[Any, Decimal] = {}
    for year in {m.year for m, _ in pairs}:
        want.update(_running([(m, v) for m, v in pairs if m.year == year]))
    rows = run(["Sales Month"], ["YTD Avg Client"])
    assert {r["Sales Month"]: r["YTD Avg Client"] for r in rows} == want


def test_rolling_three_months(run: Callable) -> None:
    pairs = _series(run, [])[()]
    rows = run(["Sales Month"], ["Rolling 3 Avg Client"])
    got = {r["Sales Month"]: r["Rolling 3 Avg Client"] for r in rows}
    for i, (month, _) in enumerate(pairs):
        window = [v for _, v in pairs[max(0, i - 2) : i + 1]]
        want = sum(window) / len(window)
        assert abs(Decimal(str(got[month])) - want) < Decimal("1e-6"), month


def test_reads_past_a_time_filter(run: Callable) -> None:
    """Shown from March, the running value still adds January and February."""
    want = _running(_series(run, [])[()])
    march = [QueryFilter(field="Sales Month", op=">=", value="2021-03-01")]
    rows = run(["Sales Month"], ["Running Avg Client", _AVG], where=march)
    assert rows and min(r["Sales Month"] for r in rows).month == 3
    for r in rows:
        assert r["Running Avg Client"] == want[r["Sales Month"]], r
    # The reaggregate metric itself is the filtered query's, as always.
    assert all(r[_AVG] is not None for r in rows)


def test_other_filters_stay(run: Callable) -> None:
    """A filter on anything but the time axis limits the data read."""
    germany = [QueryFilter(field="Country Name", op="=", value="Germany")]
    want = _running(_series(run, [], where=germany)[()])
    rows = run(["Sales Month"], ["Running Avg Client"], where=germany)
    assert {r["Sales Month"]: r["Running Avg Client"] for r in rows} == want


def test_without_its_time_dimension_refused(model: SemanticModel) -> None:
    query = QueryObject(select=QuerySelect(measures=["Running Avg Client"]))
    with pytest.raises(
        ResolutionError, match="CUMULATIVE_TIME_DIMENSION_NOT_IN_SELECT|timeDimension"
    ):
        CompilationPipeline().compile(query, model, "duckdb")


def test_having_kept_when_reading_past_the_time_filter(run: Callable) -> None:
    """HAVING picks the periods there are: a time bound that removes nothing
    must not bring back a month it excluded."""
    having = [QueryFilter(field="Total Sales", op=">", value=300000)]
    plain = run(["Sales Month"], ["Running Avg Client"], having=having)
    bound = [QueryFilter(field="Sales Month", op=">=", value="2000-01-01")]
    bounded = run(["Sales Month"], ["Running Avg Client"], having=having, where=bound)
    assert {r["Sales Month"]: r["Running Avg Client"] for r in bounded} == {
        r["Sales Month"]: r["Running Avg Client"] for r in plain
    }
    assert len(plain) < len(_series(run, [])[()])


def test_negated_mixed_group_kept(run: Callable) -> None:
    """``NOT (before March AND Germany)`` is no time filter: split, it read as
    ``NOT Germany`` and dropped valid rows."""
    group = QueryFilterGroup(
        negated=True,
        filters=[
            QueryFilter(field="Sales Month", op="<", value="2021-03-01"),
            QueryFilter(field="Country Name", op="=", value="Germany"),
        ],
    )
    want = _running(_series(run, [], where=[group])[()])
    rows = run(["Sales Month"], ["Running Avg Client"], where=[group])
    assert {r["Sales Month"]: r["Running Avg Client"] for r in rows} == want


def test_time_filter_classification(model: SemanticModel) -> None:
    from orionbelt.compiler.late_window import _without_axis
    from orionbelt.compiler.resolution import QueryResolver

    query = QueryObject(select=QuerySelect(dimensions=["Sales Month"], measures=["Total Sales"]))
    resolved = QueryResolver().resolve(query, model)
    on_time = QueryFilter(field="Sales Month", op=">=", value="2021-03-01")
    other_grain = QueryFilter(field="Sales Year", op="=", value=2021)
    other = QueryFilter(field="Country Name", op="=", value="Germany")
    time_group = QueryFilterGroup(negated=True, filters=[on_time, other_grain])
    mixed = QueryFilterGroup(filters=[on_time, other])
    exists = QueryFilter(
        field="Sales Date",
        op=FilterOperator.EXISTS,
        subquery=Subquery(data_object="Clients"),
    )
    kept = _without_axis(
        [on_time, other_grain, other, time_group, mixed, exists], "Sales Month", resolved, model
    )
    assert kept == [other, mixed, exists]


@pytest.mark.parametrize(
    "condition",
    [
        # Evaluated after the reaggregate pass, on the shown rows.
        QueryFilter(field=_AVG, op=">", value=8000),
        # Evaluated after the cumulative pass, on the shown rows.
        QueryFilter(field="Cumulative Sales", op=">", value=1000000),
    ],
)
def test_deferred_having_stays_out_of_the_look_back(run: Callable, condition: QueryFilter) -> None:
    """A HAVING on a value a wrapper finishes filters the rows shown, not the
    periods read: a time bound that removes nothing changes nothing."""
    plain = run(["Sales Month"], ["Running Avg Client"], having=[condition])
    bound = [QueryFilter(field="Sales Month", op=">=", value="2000-01-01")]
    bounded = run(["Sales Month"], ["Running Avg Client"], having=[condition], where=bound)
    assert {r["Sales Month"]: r["Running Avg Client"] for r in bounded} == {
        r["Sales Month"]: r["Running Avg Client"] for r in plain
    }
    assert 0 < len(plain) < len(_series(run, [])[()])
