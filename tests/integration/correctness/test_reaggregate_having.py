"""Reaggregate metrics with ``having``, checked against their first stage.

A reaggregate metric's ``having`` keeps the first-stage groups that meet its
conditions. So each result here is compared with the first stage run as a
query of its own, at the query's dimensions plus ``per`` with the condition
measures selected, filtered in Python and aggregated again by the query's
dimensions. A query group with no group left is NULL, or 0 for ``count``.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")

from orionbelt.compiler.composability import resolve_composables_for_anchors  # noqa: E402
from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "between": lambda v, bounds: bounds[0] <= v <= bounds[1],
}

#: Conditions on Sales' first stage: on the measure itself, on a count of the
#: same fact, two at once, and a range.
_CONDITIONS: dict[str, list[dict[str, Any]]] = {
    "big": [{"field": "Total Sales", "op": ">", "value": 150000}],
    "repeat": [{"field": "Sales Count", "op": ">=", "value": 4}],
    "repeat small": [
        {"field": "Sales Count", "op": ">=", "value": 3},
        {"field": "Total Sales", "op": "<", "value": 100000},
    ],
    "mid": [{"field": "Total Sales", "op": "between", "value": [20000, 120000]}],
}
_AGGREGATIONS = ["avg", "sum", "max", "count"]

_METRICS: dict[str, dict[str, Any]] = {
    f"{agg} client sales, {cond}": {
        "type": "reaggregate",
        "measure": "Total Sales",
        "per": ["Sales Client Name"],
        "aggregation": agg,
        "having": having,
    }
    for cond, having in _CONDITIONS.items()
    for agg in _AGGREGATIONS
}
# A condition on another fact, at a grain both reach.
_METRICS["avg month sales, big purchases"] = {
    "type": "reaggregate",
    "measure": "Total Sales",
    "per": ["Year Month"],
    "aggregation": "avg",
    "having": [{"field": "Total Purchases", "op": ">", "value": 5500000}],
}
# Nested: a filtered metric as the inner stage, and a filter on an outer stage
# over an inner metric.
_METRICS["max month of avg client sales, repeat"] = {
    "type": "reaggregate",
    "measure": "avg client sales, repeat",
    "per": ["Sales Date:month"],
    "aggregation": "max",
}
_METRICS["avg month of avg client sales, busy months"] = {
    "type": "reaggregate",
    "measure": "avg client sales, repeat",
    "per": ["Sales Date:month"],
    "aggregation": "avg",
    "having": [{"field": "Sales Count", "op": ">=", "value": 25}],
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
    return {"sum": sum, "min": min, "max": max}[aggregation](present)


def _close(got: Any, want: Any) -> bool:
    """Equal, but for an average's cast to the default decimal(18, 2) at each stage."""
    if got is None or want is None:
        return got is None and want is None
    return abs(Decimal(str(got)) - Decimal(str(want))) <= Decimal("0.01")


def _kept(row: dict[str, Any], having: list[dict[str, Any]]) -> bool:
    return all(
        row[c["field"]] is not None and _OPS[c["op"]](row[c["field"]], c["value"]) for c in having
    )


def _first_stage(
    run: Callable, metric: dict[str, Any], dimensions: list[str], where: list[QueryFilter]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The first-stage rows, and those of them the metric's ``having`` keeps."""
    having = metric.get("having", [])
    measures = list(dict.fromkeys([metric["measure"], *(c["field"] for c in having)]))
    rows = run([*dimensions, *metric["per"]], measures, where=where)
    return rows, [r for r in rows if _kept(r, having)]


_CASES = [
    (name, dims, where)
    for name in _METRICS
    for dims in _DIMENSIONS
    for where in ([], _GERMANY)
    # Purchases reach no Country or Sales Year.
    if not (name == "avg month sales, big purchases" and (dims or where))
]


@pytest.mark.parametrize(
    ("name", "dimensions", "where"),
    _CASES,
    ids=[f"{n} by {d or 'nothing'}{', Germany' if w else ''}" for n, d, w in _CASES],
)
def test_same_as_the_kept_first_stage_aggregated_again(
    run: Callable, name: str, dimensions: list[str], where: list[QueryFilter]
) -> None:
    metric = _METRICS[name]
    rows = run(dimensions, [name], where=where)
    got = {tuple(r[d] for d in dimensions): r[name] for r in rows}
    assert len(got) == len(rows), f"{len(rows)} rows for {len(got)} keys"

    stage_one, kept = _first_stage(run, metric, dimensions, where)
    groups: dict[tuple[Any, ...], list[Any]] = {
        tuple(r[d] for d in dimensions): [] for r in stage_one
    }
    for row in kept:
        groups[tuple(row[d] for d in dimensions)].append(row[metric["measure"]])
    want = {key: _aggregate(metric["aggregation"], values) for key, values in groups.items()}

    assert got.keys() == want.keys()
    assert all(_close(got[key], want[key]) for key in want), (got, want)


@pytest.mark.parametrize(
    "name",
    [n for n in _METRICS if "having" in _METRICS[n]],
)
def test_each_condition_removes_some_groups_and_keeps_some(run: Callable, name: str) -> None:
    """Otherwise the comparison above could not tell ``having`` from its absence."""
    metric = _METRICS[name]
    dimensions = [] if name == "avg month sales, big purchases" else ["Country Name"]
    stage_one, kept = _first_stage(run, metric, dimensions, [])
    assert 0 < len(kept) < len(stage_one)


def test_count_is_zero_where_no_group_is_left(run: Callable) -> None:
    """A country none of whose clients meets the condition."""
    name = "count client sales, big"
    rows = {r["Country Name"]: r[name] for r in run(["Country Name"], [name])}
    assert 0 in rows.values()
    assert None not in rows.values()
    name = "avg client sales, big"
    averages = {r["Country Name"]: r[name] for r in run(["Country Name"], [name])}
    assert {c for c, v in rows.items() if v == 0} == {c for c, v in averages.items() if v is None}


@pytest.mark.parametrize(
    "anchors",
    [[], ["Year Month"], ["Total Sales"], ["Total Sales", "Year Month"]],
    ids=" + ".join,
)
def test_composability_offers_a_condition_on_another_fact(
    model: SemanticModel, run: Callable, anchors: list[str]
) -> None:
    """Each fact is a leg of its own and reaches ``Year Month`` by itself; no
    root has to reach both, nor the fact of a measure already selected."""
    name = "avg month sales, big purchases"
    result = resolve_composables_for_anchors(model, anchors)
    assert name in set(result.metrics) | set(result.cfl_metrics)
    dimensions = [a for a in anchors if a in model.dimensions]
    measures = [a for a in anchors if a not in model.dimensions]
    assert len(run(dimensions, [*measures, name])) >= 1
