"""Reaggregate metrics over a measure some wrapper computes, checked against stage 1.

The first stage is the measure at the query's dimensions plus ``per``, exactly
as a query of its own at that grain answers it: a total, a grain override, a
filterContext or a deduplicated measure included. So each result here is
compared with that query, run on its own and aggregated again in Python by the
query's dimensions. The outer query never computes the measure, so none of
those wrappers may touch it there.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

_SALES_AMOUNT = [{"dataObject": "Sales", "column": "Sales Amount"}]

_MEASURES: dict[str, dict[str, Any]] = {
    "Grand Total Sales": {"aggregation": "sum", "columns": _SALES_AMOUNT, "total": True},
    "All Country Sales": {
        "aggregation": "sum",
        "columns": _SALES_AMOUNT,
        "filterContext": {"mode": "RELATIVE", "exclude": ["Country Name"]},
    },
    "Sales Ignoring Client": {
        "aggregation": "sum",
        "columns": _SALES_AMOUNT,
        "grain": {"mode": "RELATIVE", "exclude": ["Sales Client Name"]},
    },
}

#: Inner measures, each computed by a wrapper of its own: a fixed grain, a
#: fixed filterContext, a total, a relative filterContext, a relative grain, a
#: deduplicated measure and a deduplicated total.
_INNER = [
    "Sales by Country",
    "Unfiltered Sales",
    "Grand Total Sales",
    "All Country Sales",
    "Sales Ignoring Client",
    "Total Units In Stock",
    "Grand Total Units In Stock",
]
_AGGREGATIONS = ["avg", "sum", "min", "max", "count", "median"]
_PERS = [["Sales Client Name"], ["Sales Date:day"], ["Country Name"]]

_METRICS: dict[str, dict[str, Any]] = {
    f"{agg} of {inner} per {per[0]}": {
        "type": "reaggregate",
        "measure": inner,
        "per": per,
        "aggregation": agg,
    }
    for inner in _INNER
    for agg in _AGGREGATIONS
    for per in _PERS
}
_METRICS["Peak Share of Grand Total"] = {
    "expression": "{[max of Grand Total Sales per Sales Client Name]} / {[Total Sales]}"
}
# The measure itself, read by a formula: a component with its total.
_METRICS["Grand Total Doubled"] = {"expression": "{[Grand Total Sales]} * 2"}
_METRICS["Sales Rank"] = {
    "type": "window",
    "measure": "Total Sales",
    "windowFunction": "rank",
    "orderDirection": "desc",
}
# Reaggregates over other facts, each in a leg of its own.
_METRICS["Avg Purchases per Supplier"] = {
    "type": "reaggregate",
    "measure": "Total Purchases",
    "per": ["Purchase Supplier Name"],
    "aggregation": "avg",
}
_METRICS["Peak Monthly Returns"] = {
    "type": "reaggregate",
    "measure": "Total Returns",
    "per": ["Return Year Month"],
    "aggregation": "max",
}
# A formula over one, reached before the metric itself is.
_METRICS["Avg Purchases per Supplier Doubled"] = {
    "expression": "{[Avg Purchases per Supplier]} * 2"
}

_GERMANY = [QueryFilter(field="Country Name", op="=", value="Germany")]
_DIMENSIONS = [[], ["Country Name"], ["Sales Year"]]


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


def _aggregate(aggregation: str, values: list[Any]) -> Any:
    present = [v for v in values if v is not None]
    if aggregation == "count":
        return len(present)
    if not present:
        return None
    if aggregation == "avg":
        return Decimal(sum(present)) / len(present)
    if aggregation == "median":
        return statistics.median(Decimal(str(v)) for v in present)
    return {"sum": sum, "min": min, "max": max}[aggregation](present)


def _close(got: Any, want: Any) -> bool:
    """Equal, but for the average's cast to the default decimal(18, 2)."""
    if got is None or want is None:
        return got is None and want is None
    return abs(Decimal(str(got)) - Decimal(str(want))) <= Decimal("0.005")


def _stage_one_refused(name: str, dimensions: list[str]) -> bool:
    """A fixed grain on Country needs Country in the first stage's grouping."""
    metric = _METRICS[name]
    return metric["measure"] == "Sales by Country" and "Country Name" not in [
        *dimensions,
        *metric["per"],
    ]


_CASES = [
    (name, dims, where)
    for name, metric in _METRICS.items()
    if metric.get("type") == "reaggregate" and metric["measure"] in _INNER
    for dims in _DIMENSIONS
    for where in ([], _GERMANY)
    if not _stage_one_refused(name, dims)
]


@pytest.mark.parametrize(
    ("name", "dimensions", "where"),
    _CASES,
    ids=[f"{n} by {d or 'nothing'}{', Germany' if w else ''}" for n, d, w in _CASES],
)
def test_same_as_stage_one_aggregated_again(
    run: Callable, name: str, dimensions: list[str], where: list[QueryFilter]
) -> None:
    metric = _METRICS[name]
    rows = run(dimensions, [name], where=where)
    got = {tuple(r[d] for d in dimensions): r[name] for r in rows}
    assert len(got) == len(rows), f"{len(rows)} rows for {len(got)} keys"

    groups: dict[tuple[Any, ...], list[Any]] = {}
    for row in run([*dimensions, *metric["per"]], [metric["measure"]], where=where):
        groups.setdefault(tuple(row[d] for d in dimensions), []).append(row[metric["measure"]])
    want = {key: _aggregate(metric["aggregation"], values) for key, values in groups.items()}

    assert got.keys() == want.keys()
    assert all(_close(got[key], want[key]) for key in want), (got, want)


def test_formula_over_it_beside_the_measure(run: Callable) -> None:
    """The inner total belongs to stage 1, so a formula over the metric is not
    refused as one combining a reaggregate with a total."""
    peak = "max of Grand Total Sales per Sales Client Name"
    rows = run(["Country Name"], ["Peak Share of Grand Total", peak, "Total Sales"])
    assert rows
    for row in rows:
        assert _close(row["Peak Share of Grand Total"], row[peak] / row["Total Sales"])


def test_stage_one_refusal_names_the_metric(model: SemanticModel) -> None:
    """A fixed grain the first stage does not group by is refused there, and
    the error says the dimensions it lists are that stage's."""
    query = QueryObject(
        select=QuerySelect(measures=["avg of Sales by Country per Sales Client Name"])
    )
    with pytest.raises(ResolutionError) as exc:
        CompilationPipeline().compile(query, model, "duckdb")
    error = exc.value.errors[0]
    assert error.code == "GRAIN_NOT_SUBSET"
    assert error.message.startswith(
        "In the first stage of reaggregating 'Sales by Country' per ['Sales Client Name']: "
    )


def _same_as_separate(run: Callable, dimensions: list[str], measures: list[str]) -> None:
    combined = {tuple(r[d] for d in dimensions): r for r in run(dimensions, measures)}
    expected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for measure in measures:
        for row in run(dimensions, [measure]):
            expected.setdefault(tuple(row[d] for d in dimensions), {}).update(row)
    assert combined.keys() == expected.keys()
    for key, row in expected.items():
        # A group one fact lacks has no row in that fact's own query.
        assert {m: combined[key][m] for m in measures} == {m: row.get(m) for m in measures}


@pytest.mark.parametrize("window", [False, True], ids=["", "and a window metric"])
@pytest.mark.parametrize("first", [True, False], ids=["reaggregate first", "formula first"])
def test_beside_a_formula_over_the_same_measure(run: Callable, first: bool, window: bool) -> None:
    """The formula reads the measure with its total; the reaggregate metric's
    first stage reads it too. Neither takes the other's form of it, and the
    outer wrappers do not take the reaggregate metric for one over a total."""
    pair = ["avg of Grand Total Sales per Sales Client Name", "Grand Total Doubled"]
    measures = pair if first else pair[::-1]
    _same_as_separate(run, ["Country Name"], [*measures, *(["Sales Rank"] if window else [])])


_MULTI_FACT = [
    ["avg of Sales by Country per Country Name", "Avg Purchases per Supplier"],
    ["Avg Purchases per Supplier", "Peak Monthly Returns"],
    [
        "Avg Purchases per Supplier",
        "avg of Grand Total Sales per Sales Client Name",
        "Total Returns",
        "Total Sales",
    ],
    ["Avg Purchases per Supplier Doubled", "Avg Purchases per Supplier", "Total Sales"],
    ["Avg Purchases per Supplier Doubled", "Peak Monthly Returns"],
]


@pytest.mark.parametrize("measures", _MULTI_FACT, ids=" + ".join)
@pytest.mark.parametrize(
    "dimensions", [["Channel Name"], ["Year Month"], ["Currency"], ["Employee Name"]], ids=str
)
def test_multi_fact(run: Callable, dimensions: list[str], measures: list[str]) -> None:
    """Each reaggregate metric stays on its own fact's leg, reached directly or
    through a formula over it: a group only one fact has is kept, and no leg
    reads a table it does not join."""
    _same_as_separate(run, dimensions, measures)
