"""Shared measure/metric execution-sweep helpers.

The sweep runs *one query per measure and per metric* of the commerce model and
asserts it executes on a given engine. Its job is breadth — catching dialect
SQL that compiles cleanly but the engine rejects at runtime — so it asserts
execution only; row-level correctness stays the ``COMMERCE_CASES`` battery's
job (:mod:`tests.integration._commerce`).

The item list is derived from the resolved model so it stays in sync as the
model grows, and uses ``effective_measures`` so *synthesised* row-count
measures (``Sales Count`` etc.) are swept too, not just declared measures.

Vendor sweep tests parametrise over :data:`SWEEP_ITEMS` and compile each item
with the vendor-specific model fixture, so the same battery covers every
dialect from one definition.
"""

from __future__ import annotations

from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import Metric, SemanticModel
from tests.integration._commerce import load_commerce_model


def _metric_time_dimension(metric: Metric) -> str | None:
    """The dimension a cumulative / period-over-period metric requires in SELECT."""
    if metric.time_dimension:
        return metric.time_dimension
    pop = metric.period_over_period
    return pop.time_dimension if pop is not None else None


def _measure_required_dimensions(measure: object) -> list[str]:
    """Dimensions a measure cannot be queried without.

    A ``grain: {mode: FIXED, include: [...]}`` measure is aggregated at exactly
    those dimensions, so resolution requires them to be a subset of the query's
    dimensions - a grand-total sweep of one raises rather than executing. That
    is the feature working, not a defect, so the sweep groups such a measure by
    the grain it declares instead of skipping it.
    """
    grain = getattr(measure, "grain", None)
    return list(getattr(grain, "include", None) or []) if grain is not None else []


def build_sweep_items(model: SemanticModel) -> list[tuple[str, str, list[str]]]:
    """Return ``(kind, name, dimensions)`` for every measure and metric.

    Measures are swept as grand totals (exercises the aggregation) except where
    a fixed grain requires dimensions, in which case they are swept at that
    grain. Cumulative / period-over-period metrics carry their required time
    dimension; derived metrics are grouped by a plain dimension.
    """
    items: list[tuple[str, str, list[str]]] = [
        ("measure", name, _measure_required_dimensions(measure))
        for name, measure in model.effective_measures.items()
    ]
    for name, metric in model.metrics.items():
        time_dim = _metric_time_dimension(metric)
        items.append(("metric", name, [time_dim] if time_dim else ["Product Category"]))
    return items


def sweep_query(name: str, dims: list[str]) -> QueryObject:
    """A single-item query: the measure/metric grouped by ``dims``."""
    return QueryObject.model_validate({"select": {"dimensions": dims, "measures": [name]}})


# Item names/dimensions are independent of the physical database/schema, so a
# throwaway model load is enough to enumerate them once for parametrisation.
SWEEP_ITEMS: list[tuple[str, str, list[str]]] = build_sweep_items(
    load_commerce_model(database="orionbelt")
)
SWEEP_IDS: list[str] = [f"{kind}:{name}" for kind, name, _ in SWEEP_ITEMS]
