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

    # 2. Structural inference: an integer SUM is an integer
    #
    # The numeric default carries 16 integer digits, and a 64-bit source needs
    # 19. So SUM over a BIGINT column overflowed the very cast that was meant
    # to describe it: the engine computed 2000000000000000003 quite happily and
    # then failed converting it, on the plain star path as much as under CFL.
    # ``bigint`` is the same inference COUNT already gets, and what every
    # engine does natively for a sum of integers - exact everywhere.
    #
    # AVG deliberately does **not** get the same treatment. Widening its cast
    # would clear the overflow without making the average right, because the
    # loss happens in the aggregate, before any cast: measured, AVG over BIGINT
    # returns numeric on Postgres and decimal on MySQL (exact) but Float64 on
    # ClickHouse and DOUBLE on DuckDB, where 1000000000000000002 and
    # ...004 average to 1000000000000000000. Nor can the loss be arranged away:
    # on DuckDB every route through ``/`` is float division, so casting the
    # input, casting the operands and rewriting as SUM/COUNT were each measured
    # to come back DOUBLE.
    #
    # Both engines average in floating point whatever the input type, so the
    # boundary is magnitude rather than declared type and a wide decimal drifts
    # too - duckdb/duckdb#6829, closed as not planned. Recovering exactness
    # needs a per-dialect rewrite; tracked in #316.
    #
    # So widening AVG would trade a loud overflow for a quietly wrong number,
    # on exactly the engines where the number is wrong. It keeps the default.
    #
    # Declaring ``dataType`` is *not* an escape hatch here either - it widens
    # this same cast, so on DuckDB it turns the error back into
    # 1000000000000000000.00 for a true average of ...003.00. Nothing chosen at
    # this layer can help; only a different aggregate expression can, which is
    # what #316 is for.
    if measure.result_type == DataType.INT and agg == "SUM":
        return SimpleType(name="bigint")

    # 3. Numeric aggregation (SUM, AVG) → default
    if agg in _NUMERIC_AGGREGATIONS:
        return _get_default(settings)

    # 4. Unknown aggregation → pass-through
    return None


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
