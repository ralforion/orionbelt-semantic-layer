"""Resolve effective data_type for measures and metrics.

Resolution order (first match wins):
1. Explicit declaration on the measure/metric
2. Structural inference from expression shape and result type
3. Model-level default (settings.defaultNumericDataType)
4. Built-in default: decimal(18, 2)

Note that the built-in default holds only 16 integer digits, so anything that
can carry a 64-bit value has to be inferred rather than defaulted, or the cast
describing the result overflows on values the source held quite legally.

Pass-through (no CAST): MIN/MAX, LISTAGG, non-numeric aggregates.
"""

from __future__ import annotations

from orionbelt.models.semantic import (
    DataType,
    Measure,
    Metric,
    ModelSettings,
    PeriodOverPeriodComparison,
)
from orionbelt.models.types import (
    BUILTIN_DEFAULT,
    DIVISION_DEFAULT,
    DecimalType,
    OBMLType,
    SimpleType,
    parse_data_type,
)

_NUMERIC_AGGREGATIONS = frozenset({"SUM", "AVG"})
_COUNT_AGGREGATIONS = frozenset({"COUNT", "COUNT_DISTINCT"})
_PASSTHROUGH_AGGREGATIONS = frozenset({"MIN", "MAX", "ANY_VALUE", "MEDIAN", "MODE", "LISTAGG"})

# Digits in the largest 64-bit integer (9223372036854775807), and the widest
# precision every supported dialect accepts.
_INT64_DIGITS = 19
_MAX_DECIMAL_PRECISION = 38


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

    # 2. Structural inference: an integer measure is not a decimal(18, 2)
    #
    # The numeric default carries 16 integer digits, and a 64-bit source needs
    # 19. So SUM over a BIGINT column overflowed the very cast that was meant
    # to describe it: the engine computed 2000000000000000003 quite happily and
    # then failed converting it, on the plain star path as much as under CFL.
    #
    # SUM of integers is an integer, so it takes ``bigint`` - the same
    # inference COUNT already gets, and what every engine does natively. AVG is
    # not integral, so it stays a decimal and only gains the room it was
    # missing: the default's scale, at the width a 64-bit value needs.
    if measure.result_type == DataType.INT:
        if agg == "SUM":
            return SimpleType(name="bigint")
        if agg == "AVG":
            return _widen_to_integer_range(_get_default(settings))

    # 3. Numeric aggregation (SUM, AVG) → default
    if agg in _NUMERIC_AGGREGATIONS:
        return _get_default(settings)

    # 4. Unknown aggregation → pass-through
    return None


def _widen_to_integer_range(default: OBMLType) -> OBMLType:
    """The default's scale, widened to hold any 64-bit integer part.

    Only the precision moves, so a model that pinned ``defaultNumericDataType``
    keeps the number of decimal places it asked for; it just stops overflowing
    on values the source column holds legally. A default that is already wide
    enough, or is not a decimal at all, is left alone.
    """
    if not isinstance(default, DecimalType):
        return default
    needed = _INT64_DIGITS + default.scale
    if default.precision >= needed:
        return default
    return DecimalType(precision=min(needed, _MAX_DECIMAL_PRECISION), scale=default.scale)


# PoP comparisons whose result is a ratio rather than a value in the base
# measure's units, so they take the division default rather than inherit.
_POP_RATIO_COMPARISONS = frozenset(
    {
        PeriodOverPeriodComparison.PERCENT_CHANGE,
        PeriodOverPeriodComparison.RATIO,
    }
)


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

    # 2b. A period-over-period metric divides too, but the division is in the
    # comparison rather than the expression: its ``expression`` names the base
    # measure alone (``{[Revenue]}``). Without this it fell through to the
    # model default — ``decimal(18, 2)`` — which would round a growth ratio to
    # two places. ``difference`` and ``previousValue`` are not ratios: they
    # carry the base measure's own units, so they inherit its type.
    if metric.period_over_period is not None:
        if metric.period_over_period.comparison in _POP_RATIO_COMPARISONS:
            return DIVISION_DEFAULT
        return None

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
