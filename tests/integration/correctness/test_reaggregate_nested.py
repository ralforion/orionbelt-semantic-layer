"""Reaggregate metrics over reaggregate metrics, checked against the inner one.

A reaggregate metric whose ``measure`` names another reaggregate metric has
that metric as its first stage: the inner metric at the query's dimensions plus
the outer ``per``, exactly as a query of its own at that grain answers it. So
each result here is compared with that query, run on its own and aggregated
again in Python by the query's dimensions.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

_AGGREGATIONS = [
    "avg",
    "sum",
    "min",
    "max",
    "count",
    "median",
    "percentile_cont",
    "percentile_disc",
]
#: The percentiles' fraction, at which a double finds the wrong position.
_FRACTION = "0.3"

#: Inner reaggregate metrics, each over a measure of the Sales fact.
_INNER: dict[str, dict[str, Any]] = {
    "Avg Client Sales": {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "avg",
    },
    "Peak Daily Sales": {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Date:day"],
        "aggregation": "max",
    },
    # A median of integers is not one: 1.5 for 1 and 2.
    "Median Client Orders": {
        "type": "reaggregate",
        "measure": "Sales Count",
        "per": ["Sales Client Name"],
        "aggregation": "median",
    },
    "Clients With Sales": {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": "count",
    },
}
_PERS = [["Sales Date:month"], ["Country Name"]]

_METRICS: dict[str, dict[str, Any]] = {
    **_INNER,
    **{
        f"{agg} of {inner} per {per[0]}": {
            "type": "reaggregate",
            "measure": inner,
            "per": per,
            "aggregation": agg,
            **({"percentile": float(_FRACTION)} if agg.startswith("percentile") else {}),
        }
        for inner in _INNER
        for agg in _AGGREGATIONS
        for per in _PERS
        # An average of averages is refused (REAGGREGATE_AVG_OF_AVG).
        if not (agg == "avg" and _INNER[inner]["aggregation"] == "avg")
    },
}
# Three stages: each country's best month for the average client, the lowest.
_METRICS["Lowest Country Peak Month Client"] = {
    "type": "reaggregate",
    "measure": "max of Avg Client Sales per Sales Date:month",
    "per": ["Country Name"],
    "aggregation": "min",
}
_METRICS["Peak Month Client Doubled"] = {
    "expression": "{[max of Avg Client Sales per Sales Date:month]} * 2"
}
# Over another fact, which no Country or Sales Year reaches: beside the Sales
# metrics only.
_METRICS["Avg Supplier Purchases"] = {
    "type": "reaggregate",
    "measure": "Total Purchases",
    "per": ["Purchase Supplier Name"],
    "aggregation": "avg",
}
_METRICS["Peak Month Supplier Purchases"] = {
    "type": "reaggregate",
    "measure": "Avg Supplier Purchases",
    "per": ["Year Month"],
    "aggregation": "max",
}

_GERMANY = [QueryFilter(field="Country Name", op="=", value="Germany")]
_DIMENSIONS = [[], ["Country Name"], ["Sales Year"]]


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
    if aggregation.startswith("percentile"):
        ordered = sorted(Decimal(str(v)) for v in present)
        fraction = Decimal(_FRACTION)
        if aggregation == "percentile_disc":
            return ordered[max(math.ceil(fraction * len(ordered)), 1) - 1]
        position = fraction * (len(ordered) - 1)
        lower, upper = ordered[math.floor(position)], ordered[math.ceil(position)]
        return lower + (position - math.floor(position)) * (upper - lower)
    return {"sum": sum, "min": min, "max": max}[aggregation](present)


def _close(got: Any, want: Any) -> bool:
    """Equal, but for an average's cast to the default decimal(18, 2) at each stage."""
    if got is None or want is None:
        return got is None and want is None
    return abs(Decimal(str(got)) - Decimal(str(want))) <= Decimal("0.01")


_CASES = [
    (name, dims, where)
    for name, metric in _METRICS.items()
    if metric.get("type") == "reaggregate"
    and metric["measure"] in _METRICS
    and name != "Peak Month Supplier Purchases"
    for dims in _DIMENSIONS
    for where in ([], _GERMANY)
]


@pytest.mark.parametrize(
    ("name", "dimensions", "where"),
    _CASES,
    ids=[f"{n} by {d or 'nothing'}{', Germany' if w else ''}" for n, d, w in _CASES],
)
def test_same_as_the_inner_metric_aggregated_again(
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


def _same_as_separate(run: Callable, dimensions: list[str], measures: list[str]) -> None:
    combined = {tuple(r[d] for d in dimensions): r for r in run(dimensions, measures)}
    expected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for measure in measures:
        for row in run(dimensions, [measure]):
            expected.setdefault(tuple(row[d] for d in dimensions), {}).update(row)
    assert combined.keys() == expected.keys()
    for key, row in expected.items():
        assert {m: combined[key][m] for m in measures} == {m: row.get(m) for m in measures}


@pytest.mark.parametrize(
    "measures",
    [
        ["max of Avg Client Sales per Sales Date:month", "Avg Client Sales", "Total Sales"],
        ["Peak Month Client Doubled", "max of Avg Client Sales per Sales Date:month"],
        ["Peak Month Supplier Purchases", "max of Avg Client Sales per Sales Date:month"],
        ["Peak Month Supplier Purchases", "Total Sales", "Avg Supplier Purchases"],
    ],
    ids=" + ".join,
)
@pytest.mark.parametrize("dimensions", [["Channel Name"], ["Employee Name"]], ids=str)
def test_beside_other_metrics(run: Callable, dimensions: list[str], measures: list[str]) -> None:
    """Beside its own inner metric, a formula over it and a nested metric over
    another fact, each value is the one it has on its own."""
    _same_as_separate(run, dimensions, measures)
