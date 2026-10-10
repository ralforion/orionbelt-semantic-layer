"""Percentile measures through the planner on the commerce model (DuckDB).

The dialect renderings are asserted on all eight engines in
``drift/vendor_exec/test_percentile_exec.py``. This checks what the planner
does around them: by country alone, beside another fact (the legs of a
``UNION ALL`` carry the value and the outer query re-aggregates it, which must
keep the fraction), and under a query filter. The reference is worked out in
Python from each sale's amount and country.
"""

from __future__ import annotations

import math
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

_FRACTION = "0.3"
_MEASURES = {
    f"Sale {aggregation}": {
        "columns": [{"dataObject": "Sales", "column": "Sales Amount"}],
        "aggregation": aggregation,
        "percentile": float(_FRACTION),
    }
    for aggregation in ("percentile_cont", "percentile_disc")
}


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["measures"].update(_MEASURES)
    raw["dimensions"]["Sale ID"] = {
        "dataObject": "Sales",
        "column": "Sales ID",
        "resultType": "string",
    }
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


def _percentile(aggregation: str, values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    fraction = Decimal(_FRACTION)
    if aggregation == "percentile_disc":
        return ordered[max(math.ceil(fraction * len(ordered)), 1) - 1]
    position = fraction * (len(ordered) - 1)
    lower, upper = ordered[math.floor(position)], ordered[math.ceil(position)]
    return lower + (position - math.floor(position)) * (upper - lower)


@pytest.fixture(scope="module")
def reference(run: Callable[..., list[dict[str, Any]]]) -> dict[Any, dict[str, Decimal | None]]:
    """Each country's percentiles, from each sale's amount."""
    amounts: dict[Any, list[Decimal]] = defaultdict(list)
    for row in run(["Country Name", "Sale ID"], ["Total Sales"]):
        if row["Total Sales"] is not None:
            amounts[row["Country Name"]].append(Decimal(str(row["Total Sales"])))
    return {
        country: {
            name: _percentile(spec["aggregation"], values) for name, spec in _MEASURES.items()
        }
        for country, values in amounts.items()
    }


def _close(got: Any, want: Decimal | None) -> bool:
    if got is None or want is None:
        return got is None and want is None
    return abs(Decimal(str(got)) - want) <= abs(want) * Decimal("1e-12")


def _check(rows: list[dict[str, Any]], reference: dict[Any, dict[str, Decimal | None]]) -> None:
    assert rows
    for row in rows:
        want = reference[row["Country Name"]]
        diff = {n: (row[n], want[n]) for n in _MEASURES if not _close(row[n], want[n])}
        assert not diff, (row["Country Name"], diff)


def test_by_country(run: Callable, reference: dict) -> None:
    rows = run(["Country Name"], list(_MEASURES))
    assert {r["Country Name"] for r in rows} == set(reference)
    _check(rows, reference)


def test_beside_another_fact(run: Callable, reference: dict) -> None:
    """The ``UNION ALL`` legs carry the amount; the outer query keeps the fraction."""
    rows = run(["Country Name"], [*_MEASURES, "Total Purchases"])
    _check([r for r in rows if r["Country Name"] in reference], reference)


def test_under_a_query_filter(run: Callable, reference: dict) -> None:
    germany = [QueryFilter(field="Country Name", op="=", value="Germany")]
    rows = run(["Country Name"], list(_MEASURES), where=germany)
    assert [r["Country Name"] for r in rows] == ["Germany"]
    _check(rows, reference)


def test_the_two_differ(reference: dict) -> None:
    """Not a vacuous comparison: somewhere the interpolated value is not a sale's."""
    assert any(
        values["Sale percentile_cont"] != values["Sale percentile_disc"]
        for values in reference.values()
    )
