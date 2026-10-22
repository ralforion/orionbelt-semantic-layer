"""A cumulative or window metric over another metric names it as a metric.

``measure`` there takes a measure or a reaggregate metric. Any other metric was
refused as ``UNKNOWN_MEASURE "unknown measure 'Double'"``, although ``Double``
exists; it is now ``CUMULATIVE_MEASURE_ONLY`` / ``WINDOW_MEASURE_ONLY``, like
``REAGGREGATE_MEASURE_ONLY``.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: "1.0"
dataObjects:
  Orders:
    code: orders
    columns:
      Id: {code: id, abstractType: string, primaryKey: true}
      Customer: {code: customer, abstractType: string}
      Day: {code: day, abstractType: date}
      Amount: {code: amount, abstractType: float}
dimensions:
  Day: {dataObject: Orders, column: Day, resultType: date}
  Customer: {dataObject: Orders, column: Customer, resultType: string}
measures:
  Revenue: {columns: [{dataObject: Orders, column: Amount}], aggregation: sum,
            resultType: float}
metrics:
  Double: {expression: "{[Revenue]} * 2"}
  Run: {type: cumulative, measure: Revenue, timeDimension: Day}
  Prior:
    type: period_over_period
    expression: "{[Revenue]}"
    periodOverPeriod: {timeDimension: Day, grain: month, offsetGrain: month,
                       comparison: difference}
  Ranked: {type: window, windowFunction: rank, measure: Revenue}
  Per Customer: {type: reaggregate, measure: Revenue, per: [Customer], aggregation: avg}
"""

METRIC = {
    "Window": "  X: {{type: window, windowFunction: rank, measure: {ref}}}\n",
    "Cumulative": "  X: {{type: cumulative, measure: {ref}, timeDimension: Day}}\n",
}


def _load(extra: str = "") -> tuple[SemanticModel, list[tuple[str, str]]]:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML + extra)
    model, result = ReferenceResolver().resolve(raw, source_map)
    return model, [(e.code, e.message) for e in result.errors]


@pytest.mark.parametrize("kind", ["Window", "Cumulative"])
@pytest.mark.parametrize("ref", ["Double", "Run", "Prior", "Ranked"])
def test_a_metric_input_is_named_as_a_metric(kind: str, ref: str) -> None:
    _, errors = _load(METRIC[kind].format(ref=ref))
    assert errors == [
        (
            f"{kind.upper()}_MEASURE_ONLY",
            f"{kind} metric 'X' references metric '{ref}'; 'measure' must name a "
            f"measure or a reaggregate metric.",
        )
    ]


@pytest.mark.parametrize("kind", ["Window", "Cumulative"])
def test_an_unknown_name_is_still_unknown_measure(kind: str) -> None:
    _, errors = _load(METRIC[kind].format(ref="Nope"))
    assert errors == [("UNKNOWN_MEASURE", f"{kind} metric 'X' references unknown measure 'Nope'")]


@pytest.mark.parametrize("kind", ["Window", "Cumulative"])
@pytest.mark.parametrize("ref", ["Revenue", "Per Customer"])
def test_a_measure_or_reaggregate_metric_still_loads(kind: str, ref: str) -> None:
    assert _load(METRIC[kind].format(ref=ref))[1] == []


@pytest.mark.parametrize("kind", ["Window", "Cumulative"])
def test_the_compiler_names_it_for_a_model_built_in_python(kind: str) -> None:
    """A model assembled without the loader reaches the compiler's own check."""
    model, errors = _load(METRIC[kind].format(ref="Revenue"))
    assert errors == []
    metrics = dict(model.metrics)
    metrics["X"] = metrics["X"].model_copy(update={"measure": "Double"})
    model = model.model_copy(update={"metrics": metrics})
    query = QueryObject.model_validate({"select": {"dimensions": ["Day"], "measures": ["X"]}})
    with pytest.raises(ResolutionError) as excinfo:
        CompilationPipeline().compile(query, model, "duckdb")
    assert [e.code for e in excinfo.value.errors] == [f"{kind.upper()}_MEASURE_ONLY"]
