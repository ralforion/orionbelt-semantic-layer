"""``first`` / ``last`` measures through the planner on the commerce model (DuckDB).

The dialect renderings are asserted on all eight engines in
``drift/vendor_exec/test_first_last_exec.py``. This checks what the planner
does around them: by country alone, beside another fact (the ``UNION ALL`` legs
carry the key beside the value, and the outer query orders by it), and under a
query filter. The reference is worked out in Python from each sale's date and
amount: the amount of the latest sale, a tie going to the greatest amount.
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

_MEASURES = {
    f"{aggregation.title()} Sale": {
        "columns": [{"dataObject": "Sales", "column": "Sales Amount"}],
        "aggregation": aggregation,
        "withinGroup": {"column": {"dataObject": "Sales", "column": "Sales Date"}},
    }
    for aggregation in ("first", "last")
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


@pytest.fixture(scope="module")
def reference(run: Callable[..., list[dict[str, Any]]]) -> dict[Any, dict[str, Decimal]]:
    """Each country's first and last sale amount, from each sale's date."""
    sales: dict[Any, list[tuple[Any, Decimal]]] = defaultdict(list)
    for row in run(["Country Name", "Sale ID", "Sales Date"], ["Total Sales"]):
        if row["Total Sales"] is not None and row["Sales Date"] is not None:
            sales[row["Country Name"]].append((row["Sales Date"], Decimal(str(row["Total Sales"]))))
    return {
        country: {"First Sale": min(pairs)[1], "Last Sale": max(pairs)[1]}
        for country, pairs in sales.items()
    }


def _check(rows: list[dict[str, Any]], reference: dict[Any, dict[str, Decimal]]) -> None:
    assert rows
    for row in rows:
        want = reference[row["Country Name"]]
        got = {n: Decimal(str(row[n])) for n in _MEASURES}
        assert got == want, row["Country Name"]


def test_by_country(run: Callable, reference: dict) -> None:
    rows = run(["Country Name"], list(_MEASURES))
    assert {r["Country Name"] for r in rows} == set(reference)
    _check(rows, reference)


def test_beside_another_fact(run: Callable, reference: dict) -> None:
    """The ``UNION ALL`` legs carry the sale date; the outer query orders by it."""
    rows = run(["Country Name"], [*_MEASURES, "Total Purchases"])
    _check([r for r in rows if r["Country Name"] in reference], reference)


def test_under_a_query_filter(run: Callable, reference: dict) -> None:
    germany = [QueryFilter(field="Country Name", op="=", value="Germany")]
    rows = run(["Country Name"], list(_MEASURES), where=germany)
    assert [r["Country Name"] for r in rows] == ["Germany"]
    _check(rows, reference)


def test_the_two_differ(reference: dict) -> None:
    """Not a vacuous comparison: somewhere the first sale is not the last."""
    assert any(v["First Sale"] != v["Last Sale"] for v in reference.values())
