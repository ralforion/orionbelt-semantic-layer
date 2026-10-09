"""Reaggregate metrics beside the other wrappers, checked against themselves.

A reaggregate metric selected beside a total, a grain override, a filterContext,
a cumulative, period-over-period or window metric has to return what each
returns in a query of its own: the combined rows are compared with the two
separate queries' rows, merged on the dimensions. Every wrapper reshapes the
plan its own way, and the reaggregate pass, which runs after all of them, has
to leave each of their columns as it found them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.models.query import QueryFilter, QueryObject, QueryOrderBy, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

_SALES_AMOUNT = [{"dataObject": "Sales", "column": "Sales Amount"}]

_MEASURES: dict[str, dict[str, Any]] = {
    "Sales Grand Total": {"aggregation": "sum", "columns": _SALES_AMOUNT, "total": True},
    # A string-valued measure: a reaggregate ``count`` over it is a number, its
    # inner aggregate is not.
    "Last Client ID": {
        "aggregation": "max",
        "columns": [{"dataObject": "Sales", "column": "Sales Client"}],
    },
    "All Country Sales": {
        "aggregation": "sum",
        "columns": _SALES_AMOUNT,
        "filterContext": {"mode": "RELATIVE", "exclude": ["Country Name"]},
    },
}

_METRICS: dict[str, dict[str, Any]] = {
    "Avg Sales per Client": {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "avg",
    },
    "Avg Daily Sales": {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Date:day"],
        "aggregation": "avg",
    },
    "Avg Client Share": {"expression": "{[Avg Sales per Client]} / {[Total Sales]}"},
    "Named Clients": {
        "type": "reaggregate",
        "measure": "Last Client ID",
        "per": ["Sales Client Name"],
        "aggregation": "count",
    },
    "Named Clients Doubled": {"expression": "{[Named Clients]} * 2"},
    "Orders MoM Change": {
        "type": "period_over_period",
        "expression": "{[Sales Count]}",
        "periodOverPeriod": {
            "timeDimension": "Sales Month",
            "grain": "month",
            "offset": -1,
            "offsetGrain": "month",
            "comparison": "difference",
        },
    },
    "Sales Rank": {
        "type": "window",
        "measure": "Total Sales",
        "windowFunction": "rank",
        "orderDirection": "desc",
    },
    "Prev Month Sales": {
        "type": "window",
        "measure": "Total Sales",
        "windowFunction": "lag",
        "offset": 1,
        "timeDimension": "Sales Month",
    },
    "Share of Grand Total": {
        "expression": "{[Avg Sales per Client]} / {[Sales Grand Total]}",
    },
}

_GERMANY = [QueryFilter(field="Country Name", op="=", value="Germany")]
_NOWHERE = [QueryFilter(field="Country Name", op="=", value="Atlantis")]

#: (case, dimensions, the other measures, where)
_CASES: list[tuple[str, list[str], list[str], list[QueryFilter]]] = [
    ("total", ["Country Name"], ["Sales Grand Total"], []),
    ("total, no dimensions", [], ["Sales Grand Total"], []),
    ("grain override", ["Country Name", "Sales Year"], ["Sales by Country"], []),
    ("grain override and total", ["Country Name"], ["Sales by Country", "Sales Grand Total"], []),
    ("filterContext excluding the filter", ["Country Name"], ["All Country Sales"], _GERMANY),
    ("filterContext fixed", ["Country Name"], ["Unfiltered Sales"], []),
    ("filterContext, no dimensions", [], ["All Country Sales"], _GERMANY),
    ("filterContext fixed, no dimensions", [], ["Unfiltered Sales"], []),
    ("filterContext, no dimensions, no rows", [], ["All Country Sales"], _NOWHERE),
    ("total, no dimensions, no rows", [], ["Sales Grand Total"], _NOWHERE),
    ("window rank, no dimensions", [], ["Sales Rank"], []),
    ("cumulative, no dimensions", [], ["Cumulative Sales"], []),
    ("cumulative", ["Sales Month"], ["Cumulative Sales"], []),
    ("cumulative with where", ["Sales Month"], ["Cumulative Sales"], _GERMANY),
    ("year to date", ["Sales Month"], ["YTD Sales"], []),
    ("rolling window", ["Sales Date"], ["Rolling 30 Day Sales"], []),
    ("cumulative beside another fact", ["Sales Month"], ["Cumulative Sales", "Total Returns"], []),
    ("period over period", ["Sales Month"], ["Sales MoM Change"], []),
    ("period over period, percent", ["Sales Month"], ["Sales YoY Growth"], []),
    (
        "period over period and window",
        ["Sales Month"],
        ["Sales MoM Change", "Prev Month Sales"],
        [],
    ),
    ("period over period on another measure", ["Sales Month"], ["Orders MoM Change"], []),
    ("window rank", ["Country Name"], ["Sales Rank"], []),
    ("window lag", ["Sales Month"], ["Prev Month Sales"], []),
    ("grain dedup", ["Product Category"], ["Grand Total Units In Stock"], []),
]

#: The reaggregate side: by customer, by a time bucket, inside a formula (alone,
#: and beside the metric it is built on), and a count over a string measure
#: inside a formula.
_REAGGREGATES = [
    ["Avg Sales per Client"],
    ["Avg Daily Sales"],
    ["Avg Client Share"],
    ["Avg Sales per Client", "Avg Client Share"],
    ["Named Clients Doubled"],
]


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["measures"].update(_MEASURES)
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


def _keyed(rows: list[dict[str, Any]], dimensions: list[str]) -> dict[tuple[Any, ...], dict]:
    """Rows by their dimension values, which have to be unique: a key seen twice
    is a duplicated row that a dict would otherwise silently fold."""
    keyed = {tuple(row[d] for d in dimensions): row for row in rows}
    assert len(keyed) == len(rows), f"{len(rows)} rows for {len(keyed)} keys"
    return keyed


@pytest.mark.parametrize("reaggregates_first", [False, True], ids=["after", "before"])
@pytest.mark.parametrize("reaggregates", _REAGGREGATES, ids=" + ".join)
@pytest.mark.parametrize(
    ("dimensions", "others", "where"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_same_as_separate_queries(
    run: Callable,
    reaggregates: list[str],
    reaggregates_first: bool,
    dimensions: list[str],
    others: list[str],
    where: list[QueryFilter],
) -> None:
    """Also in both selection orders: a wrapper that rebuilds its projection
    from the resolution lists the measures in the order they were asked for."""
    selected = [*reaggregates, *others] if reaggregates_first else [*others, *reaggregates]
    combined = _keyed(run(dimensions, selected, where=where), dimensions)
    expected = _keyed(run(dimensions, others, where=where), dimensions)
    for key, row in _keyed(run(dimensions, reaggregates, where=where), dimensions).items():
        expected.setdefault(key, {}).update(row)
    assert combined
    assert combined == expected


@pytest.fixture(scope="module")
def by_month(run: Callable) -> list[dict[str, Any]]:
    return run(["Sales Month"], ["Cumulative Sales", "Avg Sales per Client"])


def test_having_on_the_metric_leaves_the_running_total_alone(
    run: Callable, by_month: list[dict[str, Any]]
) -> None:
    """HAVING filters the finished rows: the months it drops still count
    toward the running total of the months it keeps."""
    rows = run(
        ["Sales Month"],
        ["Cumulative Sales", "Avg Sales per Client"],
        having=[QueryFilter(field="Avg Sales per Client", op=">", value=10000)],
    )
    expected = [r for r in by_month if r["Avg Sales per Client"] > 10000]
    assert 0 < len(rows) < len(by_month)
    assert _keyed(rows, ["Sales Month"]) == _keyed(expected, ["Sales Month"])


def test_having_on_the_other_metric(run: Callable, by_month: list[dict[str, Any]]) -> None:
    rows = run(
        ["Sales Month"],
        ["Cumulative Sales", "Avg Sales per Client"],
        having=[QueryFilter(field="Cumulative Sales", op=">", value=20000000)],
    )
    expected = [r for r in by_month if r["Cumulative Sales"] > 20000000]
    assert 0 < len(rows) < len(by_month)
    assert _keyed(rows, ["Sales Month"]) == _keyed(expected, ["Sales Month"])


def test_having_on_a_window_metric(run: Callable) -> None:
    every = run(["Country Name"], ["Sales Rank", "Avg Sales per Client"])
    rows = run(
        ["Country Name"],
        ["Sales Rank", "Avg Sales per Client"],
        having=[QueryFilter(field="Sales Rank", op="<=", value=3)],
    )
    expected = [r for r in every if r["Sales Rank"] <= 3]
    assert len(rows) == 3
    assert _keyed(rows, ["Country Name"]) == _keyed(expected, ["Country Name"])


def test_order_by_the_metric_with_limit(run: Callable, by_month: list[dict[str, Any]]) -> None:
    rows = run(
        ["Sales Month"],
        ["Cumulative Sales", "Avg Sales per Client"],
        order_by=[QueryOrderBy(field="Avg Sales per Client", direction="desc")],
        limit=3,
    )
    assert rows == sorted(by_month, key=lambda r: r["Avg Sales per Client"], reverse=True)[:3]


def test_formula_over_a_wrapped_component_refused(model: SemanticModel) -> None:
    """A total read inside the formula would be read before the totals wrapper."""
    query = QueryObject(
        select=QuerySelect(dimensions=["Country Name"], measures=["Share of Grand Total"])
    )
    with pytest.raises(ResolutionError) as exc:
        CompilationPipeline().compile(query, model, "duckdb")
    assert exc.value.errors[0].code == "REAGGREGATE_COMBINATION_NOT_SUPPORTED"
