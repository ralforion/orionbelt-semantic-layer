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

from orionbelt.ast.nodes import Expr, FunctionCall
from orionbelt.dialect.base import Dialect
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

    # 2. Structural inference: an integer SUM is an integer
    #
    # The numeric default carries 16 integer digits, and a 64-bit source needs
    # 19. So SUM over a BIGINT column overflowed the very cast that was meant
    # to describe it: the engine computed 2000000000000000003 quite happily and
    # then failed converting it, on the plain star path as much as under CFL.
    # ``bigint`` is the same inference COUNT already gets, and what every
    # engine does natively for a sum of integers - exact everywhere.
    #
    # AVG does not get an inferred type at all, and the reason is not that it
    # is safe. The loss for AVG happens *inside* the aggregate - measured, it
    # is exact on Postgres (numeric), MySQL (decimal) and Snowflake but
    # floating point on BigQuery, ClickHouse, Dremio and DuckDB, whatever the
    # input type (duckdb/duckdb#6829, closed as not planned). Widening the cast
    # would let an already-rounded value through instead of failing on it.
    #
    # So AVG is fixed by rewriting the *expression* rather than the type. That
    # is ``rewrite_exact_integer_avg`` below, which the planners call and which
    # supplies its own widened type when it fires. Here AVG simply falls
    # through to the default, which is what DuckDB keeps - it has no exact
    # division to rewrite to, so the default's overflow is the honest outcome
    # (#316).
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


def rewrite_exact_integer_avg(
    measure: Measure,
    settings: ModelSettings | None,
    dialect: Dialect | None,
    expr: Expr,
) -> tuple[Expr, OBMLType] | None:
    """An exact ``AVG`` and the type to cast it to, or ``None`` to leave both.

    ``AVG`` is a floating-point aggregate on BigQuery, ClickHouse, Dremio and
    DuckDB whatever the input type, so it drifts past a ``double`` mantissa -
    around fifteen significant digits - and no output cast can repair a value
    the aggregate has already rounded. Three of those four can be asked for
    exact arithmetic instead; :meth:`Dialect.exact_integer_avg` says how, and
    answers ``None`` where it cannot, which is DuckDB and every engine that was
    exact to begin with.

    The result type has to move with the expression. Left at the
    ``decimal(18, 2)`` default, the exact average would be computed correctly
    and then overflow the cast describing it, so an **inferred** type is
    widened to hold a 64-bit integer part. A **declared** ``dataType`` is not:
    the resolution order promises that an explicit declaration wins, and a
    declaration too narrow for its own data should fail as loudly here as
    anywhere else.

    Deliberately conservative about what it will rewrite. Only a bare
    ``AVG(x)`` qualifies - not a DISTINCT one, and not one already wrapped by a
    measure filter or default, where the argument is no longer the thing being
    averaged. Those keep today's behaviour rather than get a rewrite this
    function cannot verify.
    """
    if dialect is None:
        return None
    if measure.aggregation.upper() != "AVG" or measure.result_type != DataType.INT:
        return None
    if not isinstance(expr, FunctionCall) or expr.name != "AVG":
        return None
    if expr.distinct or len(expr.args) != 1:
        return None
    resolved = resolve_measure_data_type(measure, settings)
    if not isinstance(resolved, DecimalType):
        return None
    target = resolved if measure.data_type else _widen_to_integer_range(resolved)
    exact = dialect.exact_integer_avg(expr.args[0], target)
    if exact is None:
        return None
    return exact, target


def _widen_to_integer_range(default: DecimalType) -> DecimalType:
    """The default's scale, widened to hold any 64-bit integer part.

    Precision moves first, so a model that pinned ``defaultNumericDataType``
    keeps the decimal places it asked for. Where both cannot fit - a scale of
    20 leaves only 18 integer digits inside the 38 every engine accepts - the
    scale gives way, because dropping fractional digits from an average is a
    smaller lie than refusing values the source column holds legally.
    """
    needed = _INT64_DIGITS + default.scale
    if default.precision >= needed and default.precision - default.scale >= _INT64_DIGITS:
        return default
    if needed <= _MAX_DECIMAL_PRECISION:
        return DecimalType(precision=needed, scale=default.scale)
    return DecimalType(
        precision=_MAX_DECIMAL_PRECISION, scale=_MAX_DECIMAL_PRECISION - _INT64_DIGITS
    )
