"""Resolve effective data_type for measures and metrics.

Resolution order (first match wins):
1. Explicit declaration on the measure/metric
2. Structural inference from expression shape
3. Model-level default (settings.defaultNumericDataType)
4. Built-in default: decimal(18, 2)

Pass-through (no CAST): MIN/MAX, LISTAGG, non-numeric aggregates.
"""

from __future__ import annotations

from orionbelt.models.semantic import Measure, Metric, ModelSettings
from orionbelt.models.types import (
    BUILTIN_DEFAULT,
    DIVISION_DEFAULT,
    OBMLType,
    SimpleType,
    parse_data_type,
)

_NUMERIC_AGGREGATIONS = frozenset({"SUM", "AVG"})
_COUNT_AGGREGATIONS = frozenset({"COUNT", "COUNT_DISTINCT"})
_PASSTHROUGH_AGGREGATIONS = frozenset({"MIN", "MAX", "ANY_VALUE", "MEDIAN", "MODE", "LISTAGG"})


def resolve_measure_data_type(
    measure: Measure,
    settings: ModelSettings | None,
) -> OBMLType | None:
    """Resolve the effective data_type for a measure.

    Returns None for pass-through (no CAST should be emitted).
    """
    # 1. Explicit declaration
    if measure.data_type:
        return parse_data_type(measure.data_type)

    agg = measure.aggregation.upper()

    # Pass-through for non-numeric aggregations
    if agg in _PASSTHROUGH_AGGREGATIONS:
        return None

    # 2. Structural inference: COUNT → bigint
    if agg in _COUNT_AGGREGATIONS:
        return SimpleType(name="bigint")

    # 2. Structural inference: division in expression → decimal(18, 6)
    if measure.expression and "/" in measure.expression:
        return DIVISION_DEFAULT

    # 3. Numeric aggregation (SUM, AVG) → default
    if agg in _NUMERIC_AGGREGATIONS:
        return _get_default(settings)

    # 4. Unknown aggregation → pass-through
    return None


def resolve_metric_data_type(
    metric: Metric,
    settings: ModelSettings | None,
) -> OBMLType | None:
    """Resolve the effective data_type for a metric.

    Returns None for pass-through (no CAST should be emitted).
    """
    # 1. Explicit declaration
    if metric.data_type:
        return parse_data_type(metric.data_type)

    # 2. Structural inference: division in expression → decimal(18, 6)
    if metric.expression and "/" in metric.expression:
        return DIVISION_DEFAULT

    # 3. Metrics are always numeric expressions → default
    if metric.expression:
        return _get_default(settings)

    # Cumulative/PoP metrics inherit from their underlying measure
    return None


def _get_default(settings: ModelSettings | None) -> OBMLType:
    """Return the model-level or built-in default numeric type."""
    if settings and settings.default_numeric_data_type:
        return parse_data_type(settings.default_numeric_data_type)
    return BUILTIN_DEFAULT
