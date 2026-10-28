"""Resolve effective data_type for measures and metrics.

Resolution order (first match wins):
1. Explicit declaration on the measure/metric
2. Structural inference from expression shape and result type, including
   ``double`` for a ``resultType: float`` SUM/AVG unless a model-level default
   is set or its float columns are fixed-point
3. Model-level default (settings.defaultNumericDataType)
4. Built-in default: decimal(18, 2)

Note that the built-in default holds only 16 integer digits, so anything that
can carry a 64-bit value has to be inferred rather than defaulted, or the cast
describing the result overflows on values the source held quite legally.

Pass-through (no CAST): MIN/MAX, LISTAGG, non-numeric aggregates.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from orionbelt.ast.nodes import BinaryOp, Expr, FunctionCall
from orionbelt.dialect.base import PORTABLE_DECIMAL_PRECISION, Dialect
from orionbelt.models.expressions import QUALIFIED_COLUMN_REF, find_qualified_refs
from orionbelt.models.semantic import (
    CumulativeAggType,
    DataObjectColumn,
    DataType,
    Measure,
    Metric,
    MetricType,
    ModelSettings,
    PeriodOverPeriodComparison,
    ReaggregateAggType,
    SemanticModel,
)
from orionbelt.models.types import (
    BUILTIN_DEFAULT,
    DIVISION_DEFAULT,
    FLOAT_DEFAULT,
    DecimalType,
    OBMLType,
    SimpleType,
    decimal_width,
    default_as_decimal,
    parse_data_type,
)

_NUMERIC_AGGREGATIONS = frozenset({"SUM", "AVG"})
_COUNT_AGGREGATIONS = frozenset({"COUNT", "COUNT_DISTINCT"})
_PASSTHROUGH_AGGREGATIONS = frozenset(
    {
        "MIN",
        "MAX",
        "ANY_VALUE",
        "MEDIAN",
        "PERCENTILE_CONT",
        "PERCENTILE_DISC",
        "FIRST",
        "LAST",
        "MODE",
        "LISTAGG",
    }
)

# What an expression may hold besides column references and still be an integer.
_INTEGER_ARITHMETIC = re.compile(r"[\s\d+\-*()]*")

# The result types a measure is advertised at its decimal cast's width for.
_FRACTIONAL_TYPES = frozenset({DataType.FLOAT, DataType.DECIMAL})

# Digits in the largest 64-bit integer (9223372036854775807), and the widest
# precision every supported dialect accepts.
_INT64_DIGITS = 19
_MAX_DECIMAL_PRECISION = 38


def resolve_measure_data_type(
    measure: Measure,
    settings: ModelSettings | None,
    model: SemanticModel | None = None,
) -> OBMLType | None:
    """Resolve the effective data_type for a measure.

    Returns None for pass-through (no CAST should be emitted). *model* lets a
    ``float`` measure over fixed-point columns keep the exact default, see
    :func:`_reads_fixed_point_floats`.
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

    # A ``resultType: float`` measure asked for a float, and the decimal
    # defaults below rounded it: an average of 1, 1, 1 and 1.25 came back as
    # 1.06. The model-level default is a declaration too and still wins, and
    # so does a fixed-point source. ``decimal`` keeps the exact defaults.
    if (
        measure.result_type == DataType.FLOAT
        and agg in _NUMERIC_AGGREGATIONS
        and not (settings and settings.default_numeric_data_type)
        and not _reads_fixed_point_floats(measure, model)
    ):
        return FLOAT_DEFAULT

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
    # through to the default, which is what the engines whose own AVG is
    # already exact keep. Every engine whose AVG drifts now has a rewrite,
    # DuckDB included, which assembles the average from integer arithmetic
    # since it has no exact division to rewrite to (#316).
    if measure.result_type == DataType.INT and agg == "SUM":
        return SimpleType(name="bigint")

    # 3. Numeric aggregation (SUM, AVG) → default
    if agg in _NUMERIC_AGGREGATIONS:
        return _get_default(settings)

    # 4. Unknown aggregation → pass-through
    return None


def _reads_fixed_point_floats(measure: Measure, model: SemanticModel | None) -> bool:
    """Whether every ``float`` column *measure* reads declares its width.

    Before OBML had ``decimal``, a fixed-point warehouse column was declared
    ``float`` with ``sqlPrecision`` and ``sqlScale``, and its measures
    ``resultType: float``. Those measures were exact and stay so: the width
    says what the column holds. It takes at least one such column, so a
    ``float`` measure over ``int`` columns alone is the float it declares.
    """
    if model is None:
        return False
    refs = [(ref.view, ref.column) for ref in measure.columns if ref.view and ref.column]
    if measure.expression:
        refs.extend(find_qualified_refs(measure.expression))
    floats = []
    for obj_name, col_name in refs:
        obj = model.data_objects.get(obj_name)
        column = obj.columns.get(col_name) if obj else None
        if column is None:
            return False
        if column.abstract_type is DataType.FLOAT:
            floats.append(column)
    return bool(floats) and all(
        column.sql_precision is not None and column.sql_scale is not None for column in floats
    )


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

    # 2a. A reaggregate metric's second stage: an average is a new numeric value
    # and takes the default, as an ``avg`` measure does; a count is an integer;
    # sum, min and max carry the base measure's units and inherit, and a
    # median or percentile passes through as the measure aggregation does: a
    # default decimal would round an interpolated value.
    if metric.type is MetricType.REAGGREGATE:
        if metric.aggregation is ReaggregateAggType.AVG:
            return _get_default(settings)
        if metric.aggregation is ReaggregateAggType.COUNT:
            return SimpleType(name="bigint")
        return None

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


def governed_measure_data_type(
    measure: Measure, settings: ModelSettings | None, model: SemanticModel | None
) -> str | None:
    """The ``dataType`` a measure's result is advertised with, or ``None``.

    Its own ``dataType``, or ``settings.defaultNumericDataType`` when the
    compiler casts the result to that default. A count, an integer sum or a
    ``min`` of dates is not cast to it, so it is advertised by its
    ``resultType`` instead: naming the default there described a ``BIGINT`` or
    a ``DATE`` as the decimal it never became. A ``decimal`` measure, and a
    ``float`` one the compiler casts to a decimal anyway - one over fixed-point
    columns, see :func:`_reads_fixed_point_floats` - is advertised as the
    decimal it is cast to, at the width the cast has: the generic
    ``decimal(38, 10)`` of its ``resultType`` cannot hold the 34 integer digits
    of a sum widened to ``decimal(36, 2)``.
    """
    if measure.data_type:
        return measure.data_type
    resolved = resolve_measure_data_type(measure, settings, model)
    default = settings.default_numeric_data_type if settings else None
    if default is not None and resolved == parse_data_type(default):
        return default
    if measure.result_type in _FRACTIONAL_TYPES and isinstance(resolved, DecimalType):
        cast = resolve_measure_cast_type(measure, settings, None, model)
        if isinstance(cast, DecimalType):
            return f"decimal({cast.precision}, {cast.scale})"
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
    model: SemanticModel | None = None,
) -> tuple[Expr, OBMLType] | None:
    """An exact ``AVG`` and the type to cast it to, or ``None`` to leave both.

    ``AVG`` is a floating-point aggregate on BigQuery, ClickHouse, Dremio and
    DuckDB whatever the input type, so it drifts past a ``double`` mantissa -
    around fifteen significant digits - and no output cast can repair a value
    the aggregate has already rounded. All four can be asked for exact
    arithmetic instead, though only three of them by dividing: DuckDB has no
    exact division at all and assembles its average from integer arithmetic
    (#316). :meth:`Dialect.exact_integer_avg` says how, and answers ``None``
    for the engines that were exact to begin with.

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
    aggregate, rebuild = unwrap_default(expr)
    if not isinstance(aggregate, FunctionCall) or aggregate.name != "AVG":
        return None
    if aggregate.distinct or len(aggregate.args) != 1:
        return None
    resolved = resolve_measure_data_type(measure, settings, model)
    if not isinstance(resolved, DecimalType):
        return None
    target = resolved if measure.data_type else _integer_avg_target(measure, resolved, model)
    exact = dialect.exact_integer_avg(aggregate.args[0], target)
    if exact is not None:
        return rebuild(exact), target
    if dialect.avg_over_integers_is_exact:
        # No rewrite needed, but the result type still is. The aggregate is
        # already exact here; it is the declared decimal(18, 2) that cannot
        # hold what it produces - measured, MySQL saturates a true
        # 1000000000000000003 to 9999999999999999.99 with no warning, and
        # Postgres raises. Widening lets an exact average through intact.
        return expr, target
    # Neither exact nor rewritable. No dialect is in this position today -
    # DuckDB was the last, until its average was assembled from integer
    # arithmetic (#316) - and the branch stays because a new dialect starts
    # here until someone measures it.
    return None


def exact_reaggregate_avg(
    metric: Metric,
    integer_values: bool,
    settings: ModelSettings | None,
    dialect: Dialect,
    arg: Expr,
) -> tuple[Expr, OBMLType] | None:
    """An exact second-stage ``AVG`` of integer values, and the type to cast it to.

    A reaggregate metric averages its measure's per-group values. When those are
    integers - a count, an integer sum - the engines with a floating-point
    ``AVG`` round them exactly as they would an ``avg`` measure's integer column,
    so the same route applies: :meth:`Dialect.exact_integer_avg` where the engine
    needs a rewrite, the plain aggregate where it is already exact, and in both
    cases a type wide enough for a 64-bit integer part unless the metric declares
    one. ``None`` leaves the plain ``AVG`` and the metric's own type.

    *integer_values* is :func:`measure_yields_integers` for the base measure.
    """
    if not integer_values:
        return None
    declared = parse_data_type(metric.data_type) if metric.data_type else None
    if declared is not None and not isinstance(declared, DecimalType):
        return None
    if declared is not None:
        target: OBMLType = declared
    else:
        default = _get_default(settings)
        if not isinstance(default, DecimalType):
            return None
        target = _widen_to_integer_range(default)
    exact = dialect.exact_integer_avg(arg, target)
    if exact is not None:
        return exact, target
    if dialect.avg_over_integers_is_exact:
        return FunctionCall(name="AVG", args=[arg]), target
    return None


_INTEGER_NAMES = frozenset({"bigint", "integer"})

#: Second-stage aggregations that interpolate between the values they read.
_INTERPOLATING_REAGGREGATIONS = frozenset(
    {ReaggregateAggType.MEDIAN, ReaggregateAggType.PERCENTILE_CONT}
)

#: Aggregates that answer one of their input values, so an integer column gives
#: an integer. ``MEDIAN`` and ``PERCENTILE_CONT`` are absent: they interpolate
#: between two values.
_VALUE_PRESERVING_AGGREGATIONS = frozenset(
    {"MIN", "MAX", "ANY_VALUE", "MODE", "PERCENTILE_DISC", "FIRST", "LAST"}
)


def passthrough_source_column(measure: Measure, model: SemanticModel) -> DataObjectColumn | None:
    """The column whose values *measure* returns as they are, or ``None``.

    A ``min``, ``max``, ``first`` or other aggregate that answers one of its
    input values, over one column and not cast, returns a value of that column,
    at its declared width. A ``median`` or ``percentile_cont`` interpolates:
    the midpoint of 1 and 2 is 1.5, which a column of scale 0 cannot hold.
    """
    if measure.data_type or measure.expression or len(measure.columns) != 1:
        return None
    if measure.aggregation.upper() not in _VALUE_PRESERVING_AGGREGATIONS:
        return None
    ref = measure.columns[0]
    data_object = model.data_objects.get(ref.view or "")
    return data_object.columns.get(ref.column) if data_object and ref.column else None


def passthrough_decimal_width(measure: Measure, model: SemanticModel) -> tuple[int, int] | None:
    """The ``(precision, scale)`` of the values *measure* returns as they are.

    The declared width of its :func:`passthrough_source_column`, normalized by
    :func:`~orionbelt.models.types.decimal_width`, and widened to hold its
    ``defaultValue``: over an empty table ``COALESCE(MIN(x), 0.5)`` returns the
    0.5, which a column of scale 0 cannot. A default that is not a number gives
    no width at all, since no decimal holds it. ``None`` when the column
    declares no width.
    """
    column = passthrough_source_column(measure, model)
    if column is None or column.sql_precision is None or column.sql_scale is None:
        return None
    precision, scale = decimal_width(column.sql_precision, column.sql_scale)
    if measure.default_value is None:
        return precision, scale
    default = default_as_decimal(measure.default_value)
    if default is None:
        return None
    digits, exponent = default.as_tuple()[1:]
    if not isinstance(exponent, int):
        return None
    default_scale = max(-exponent, 0)
    default_integer_digits = max(len(digits) + exponent, 0)
    integer_digits = max(precision - scale, default_integer_digits)
    scale = max(scale, default_scale)
    return max(integer_digits + scale, 1), scale


def measure_yields_integers(
    measure: Measure, settings: ModelSettings | None, model: SemanticModel | None
) -> bool:
    """Whether every value of *measure* is an integer.

    Its resolved type says so for a count or an integer sum. A value-preserving
    aggregate - ``MIN``, ``MAX`` - resolves to no type at all, meaning "no
    cast", and keeps its input's: an integer when every column it reads is
    declared ``int``, or when an ``expression`` measure declares ``resultType:
    int``. A ``defaultValue`` that is not a whole number written as one makes the
    value something else, as in :func:`measure_source_is_exact`.
    """
    resolved = resolve_measure_data_type(measure, settings, model)
    if resolved is not None:
        return isinstance(resolved, SimpleType) and resolved.name in _INTEGER_NAMES
    if measure.aggregation.upper() not in _VALUE_PRESERVING_AGGREGATIONS:
        return False
    default = measure.default_value
    if default is not None and (isinstance(default, bool) or not isinstance(default, int)):
        return False
    if measure.expression or not measure.columns:
        return measure.result_type is DataType.INT
    if model is None:
        return False
    for ref in measure.columns:
        obj = model.data_objects.get(ref.view or "")
        column = obj.columns.get(ref.column) if obj and ref.column else None
        if column is None or column.abstract_type is not DataType.INT:
            return False
    return True


def reaggregated_data_type(model: SemanticModel, name: str) -> OBMLType | None:
    """The resolved type of the values a reaggregate metric's second stage reads.

    Those are *name*'s: a measure, or a reaggregate metric nested under it,
    whose ``sum``, ``min`` and ``max`` carry its own measure's type down the
    chain.
    """
    metric = model.metrics.get(name)
    if metric is None:
        measure = model.effective_measures.get(name)
        return resolve_measure_data_type(measure, model.settings, model) if measure else None
    declared = resolve_metric_data_type(metric, model.settings)
    if declared is None and metric.measure:
        return reaggregated_data_type(model, metric.measure)
    return declared


def reaggregated_values_are_integers(model: SemanticModel, name: str) -> bool:
    """Whether every value of measure or reaggregate metric *name* is an integer.

    A ``sum``, ``min``, ``max`` or ``percentile_disc`` of integers is one; a
    ``median`` or ``percentile_cont`` of them is not, unless declared so: the
    midpoint of two integers is a half (1.5 for 1 and 2).
    """
    metric = model.metrics.get(name)
    if metric is None:
        measure = model.effective_measures.get(name)
        return measure is not None and measure_yields_integers(measure, model.settings, model)
    declared = resolve_metric_data_type(metric, model.settings)
    if declared is not None:
        return isinstance(declared, SimpleType) and declared.name in _INTEGER_NAMES
    if metric.aggregation in _INTERPOLATING_REAGGREGATIONS:
        return False
    # A running average of integers is not one; the other cumulative types of
    # integers are.
    if metric.type is MetricType.CUMULATIVE and metric.cumulative_type is CumulativeAggType.AVG:
        return False
    return metric.measure is not None and reaggregated_values_are_integers(model, metric.measure)


def apply_exact_integer_sum(
    expr: Expr,
    measure: Measure,
    dialect: Dialect | None,
) -> Expr:
    """The integer-``SUM`` rewrite alone, without the cast.

    The ``AVG`` counterpart exists because that aggregate *drifts*; this one
    exists because on two engines it *wraps*. Measured on two rows of
    9000000000000000000, ClickHouse and Dremio both return -446744073709551616
    - a negative total from two positive rows - where DuckDB, Postgres,
    BigQuery and Databricks raise and Snowflake answers exactly. The
    accumulator is 64-bit and has already overflowed by the time anything casts
    it, so :meth:`Dialect.exact_integer_sum` widens the **argument** instead,
    and answers ``None`` on the six engines that need nothing.

    Rewrite-only, and used on every path rather than only the wrapper ones. The
    cast cannot be applied here - a wrapper metric's placeholder holds the base
    measure's aggregate and casting it would truncate the window's input - and
    the type is not this function's business anyway: it comes from
    :func:`resolve_measure_cast_type`, which decides without looking at an
    expression and therefore still decides correctly once wrappers compose.

    Deliberately conservative: only a bare ``SUM(x)`` qualifies. A windowed sum
    arrives as a ``WindowFunction`` rather than a call, and the measure it
    aggregates is already rewritten in the CTE beneath it, which is where the
    accumulation over rows happens. A DISTINCT one is declined rather than
    rewritten - OBML has no aggregation that produces one today, so the branch
    would be untestable, and dropping the keyword silently is the one way this
    could change an answer rather than repair it.
    """
    if dialect is None or not _is_integer_sum(measure):
        return expr
    aggregate, rebuild = unwrap_default(expr)
    if not isinstance(aggregate, FunctionCall) or aggregate.name != "SUM":
        return expr
    if aggregate.distinct or len(aggregate.args) != 1:
        return expr
    rewritten = dialect.exact_integer_sum(aggregate.args[0])
    return expr if rewritten is None else rebuild(rewritten)


def _is_integer_sum(measure: Measure) -> bool:
    return measure.aggregation.upper() == "SUM" and measure.result_type == DataType.INT


def _is_integer_avg(measure: Measure) -> bool:
    return measure.aggregation.upper() == "AVG" and measure.result_type == DataType.INT


def _integer_avg_target(
    measure: Measure,
    resolved: DecimalType,
    model: SemanticModel | None,
) -> DecimalType:
    """The type an exact integer average is cast to.

    Both widenings apply and they are not the same one. A 64-bit source needs
    19 integer digits whatever the model says, and a source the model declares
    wider needs whatever it declared - a ``decimal(38, 0)`` column averaged to
    ``decimal(21, 2)`` still overflows, since 21 minus 2 is nineteen digits and
    the column holds thirty-eight.

    Only ever reached for a dialect whose average is exact. Widening an
    inexact one is the trade #315 refused: it turns a loud overflow into a
    quietly rounded answer, which is how DuckDB briefly came to return
    1000000000000000000 for a true 1000000000000000002.
    """
    return _widen_for_declared_source(measure, _widen_to_integer_range(resolved), model)


def _widen_for_declared_source(
    measure: Measure,
    resolved: DecimalType,
    model: SemanticModel | None,
) -> DecimalType:
    """Widen an inferred type that the source column is declared to outgrow.

    The default carries 16 integer digits. A model that says its column is
    ``decimal(38, 15)`` has said, in the only vocabulary OBML has for it, that
    the column holds values with up to 23 - so casting its total to the default
    cannot work. Measured, that overflows on DuckDB, Postgres and ClickHouse
    and **saturates silently on MySQL**: a true 100000000000000001.10 comes
    back as 9999999999999999.99, no warning.

    Only the precision moves; the scale is the rounding the model asked for and
    is left alone. Both halves of the declared width must be present, per #313 -
    ``sqlPrecision`` alone says nothing about scale, and assuming zero there
    reintroduced a rounding bug.

    An **undeclared** column is not covered and cannot be: nothing in the model
    says how large its values are. Those still overflow, and still saturate on
    MySQL, which is tracked separately as a cross-engine consistency defect
    rather than a typing one.
    """
    if model is None:
        return resolved
    declared_integer_digits = _widest_declared_source(measure, model)
    if declared_integer_digits <= resolved.precision - resolved.scale:
        return resolved
    needed = declared_integer_digits + resolved.scale
    if needed <= _MAX_DECIMAL_PRECISION:
        return DecimalType(precision=needed, scale=resolved.scale)
    # Past what any engine accepts, so the scale gives way rather than the
    # integer part. A source declared decimal(38, 0) holds 38 integer digits;
    # keeping the default's two decimals would leave 36 and overflow on a value
    # the column holds quite legally. Dropping fractional places the source
    # never had is the smaller loss.
    return DecimalType(
        precision=_MAX_DECIMAL_PRECISION,
        scale=max(0, _MAX_DECIMAL_PRECISION - declared_integer_digits),
    )


def _widest_declared_source(measure: Measure, model: SemanticModel) -> int:
    """Integer digits of the widest column this measure reads, or 0 if unknown.

    Reads ``columns`` **and** an ``expression``. Keying on ``len(columns) == 1``
    skipped every expression measure, which is the third time that exact cut
    has hidden a bug - the CFL leg alignment made it twice (#305, #311). An
    expression measure aggregates a formula over the same physical columns and
    has the same reason to outgrow the default.

    Only a fully declared width counts, per #313: ``sqlPrecision`` alone says
    nothing about scale.
    """
    refs: list[tuple[str, str]] = [
        (ref.view, ref.column) for ref in measure.columns if ref.view and ref.column
    ]
    if measure.expression:
        refs.extend(find_qualified_refs(measure.expression))
    widest = 0
    for obj_name, col_name in refs:
        obj = model.data_objects.get(obj_name)
        column = obj.columns.get(col_name) if obj else None
        if column is None or column.sql_precision is None or column.sql_scale is None:
            continue
        widest = max(widest, column.sql_precision - column.sql_scale)
    return widest


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


#: Aggregates that answer their operand's own type, so an exact operand makes an
#: exact result. ``AVG`` is deliberately absent: measured on ClickHouse, it
#: answers Float64 even over a Decimal column, which is the case an engine's
#: exactness rewrite exists for. So are the statistical ones, which are floating
#: by definition, and ``MEDIAN``, which interpolates.
_TYPE_PRESERVING_AGGREGATIONS = frozenset({"SUM", "MIN", "MAX", "ANY_VALUE", "FIRST", "LAST"})


def measure_source_is_exact(measure: Measure, model: SemanticModel | None) -> bool:
    """Whether no floating value can reach this measure's cast.

    A dialect renders from the AST, where a column carries its abstract type
    and nothing else - and OBML's abstract types have no ``decimal``, so a
    decimal column is declared ``float`` and the width lives in ``sqlPrecision``
    and ``sqlScale`` on the model. Only a caller holding the model can answer
    this, so it is answered here and carried on the node (see
    :class:`~orionbelt.ast.nodes.Cast`).

    True needs all of:

    * an aggregate that carries its operand's type through, per
      :data:`_TYPE_PRESERVING_AGGREGATIONS`;
    * the ``columns:`` form, which names its source outright. An ``expression``
      may divide, or name a column this cannot see;
    * every named column declared with a width - ``sqlPrecision`` **and**
      ``sqlScale``, the pair ``cfl_projection`` already trusts - or declared
      ``int``, which has no fraction to lose. A computed column is excluded:
      its body is an expression again;
    * a ``defaultValue`` that is absent or a whole number *written as one*.
      The field takes a string too, and it is emitted as the literal it was
      written as: ``defaultValue: "0"`` becomes ``COALESCE(SUM(x), '0')``,
      whose type is the engine's to decide. Measured, ``coalesce(SUM(dec), 0)``
      is a Decimal, ``coalesce(SUM(dec), 0.5)`` is a Variant carrying a Float64,
      and a quoted one leans on an implicit conversion that is not this rule's
      to promise - ``coalesce(SUM(dec), '0.5')`` answers 0. So only an integer
      qualifies, and a bool is not one however Python counts it.

    It trusts the model to describe its warehouse, the way the union-alignment
    width already does. A column declared exact that is really a float loses the
    place its engine would have rounded, which is what the declaration promises
    it is not.
    """
    if model is None or measure.expression or not measure.columns:
        return False
    if measure.aggregation.upper() not in _TYPE_PRESERVING_AGGREGATIONS:
        return False
    default = measure.default_value
    if default is not None and (isinstance(default, bool) or not isinstance(default, int)):
        return False
    for ref in measure.columns:
        obj = model.data_objects.get(ref.view or "")
        column = obj.columns.get(ref.column) if obj and ref.column else None
        if column is None or column.expression:
            return False
        exact_width = column.sql_precision is not None and column.sql_scale is not None
        if not exact_width and column.abstract_type is not DataType.INT:
            return False
    return True


def cast_measure_to_resolved_type(
    expr: Expr,
    measure: Measure,
    settings: ModelSettings | None,
    dialect: Dialect | None,
    model: SemanticModel | None = None,
) -> Expr:
    """Cast a built measure expression to the type it resolves to.

    The single place that decides how a measure aggregate is typed, so the
    exact-AVG handling cannot be applied on some paths and forgotten on others.
    It was: ``star`` and ``cfl`` rewrote an integer AVG and widened its type,
    while the cumulative, period-over-period and window wrappers called
    :func:`resolve_measure_data_type` alone. A direct query therefore emitted
    ``CAST(AVG(x) AS DECIMAL(21, 2))`` and the same measure inside a PoP metric
    emitted ``DECIMAL(18, 2)``, which is the type that saturates on MySQL and
    overflows on Postgres (#330).

    Returns *expr* unchanged when the measure is pass-through, which is what a
    ``None`` resolved type means.
    """
    if dialect is None:
        return expr
    exact = rewrite_exact_integer_avg(measure, settings, dialect, expr, model)
    if exact is not None:
        rewritten, target = exact
        return dialect.cast_to_obml_type(rewritten, target)
    expr = apply_float_input(expr, measure, settings, dialect, model)
    expr = apply_exact_integer_sum(expr, measure, dialect)
    resolved = resolve_measure_cast_type(measure, settings, dialect, model)
    if resolved is None:
        return expr
    return dialect.cast_to_obml_type(
        expr, resolved, source_exact=measure_source_is_exact(measure, model)
    )


def unwrap_default(expr: Expr) -> tuple[Expr, Callable[[Expr], Expr]]:
    """See past a ``defaultValue`` wrapper to the aggregate inside it.

    ``defaultValue`` emits ``COALESCE(AVG(x), 0)``, so what reaches the rewrite
    is not a bare ``AVG`` and the rewrite declined - leaving a raw floating
    average with a widened cast around it, which is the silent drift this is
    meant to remove. Measure *filters* need no equivalent: they change the
    aggregate's argument, so it stays a bare ``AVG``.
    """
    if isinstance(expr, FunctionCall) and expr.name == "COALESCE" and len(expr.args) == 2:
        inner, fallback = expr.args
        return inner, lambda done: FunctionCall(name="COALESCE", args=[done, fallback])
    return expr, lambda done: done


def apply_exact_integer_avg(
    expr: Expr,
    measure: Measure,
    settings: ModelSettings | None,
    dialect: Dialect | None,
    model: SemanticModel | None = None,
) -> Expr:
    """The rewrite alone, without the cast.

    For the wrapper metrics, whose base aggregate must **not** be cast to the
    metric's declared type at this point - a rank declaring ``dataType:
    integer`` would truncate ``SUM(amount)`` before the window ever saw it.
    Making the average exact and casting it are separate concerns, and only the
    second is unsafe there; skipping both left those paths on a raw floating
    average.
    """
    if dialect is None:
        return expr
    exact = rewrite_exact_integer_avg(measure, settings, dialect, expr, model)
    if exact is not None:
        return exact[0]
    return apply_float_input(expr, measure, settings, dialect, model)


def apply_float_input(
    expr: Expr,
    measure: Measure,
    settings: ModelSettings | None,
    dialect: Dialect,
    model: SemanticModel | None,
) -> Expr:
    """Aggregate a ``double`` measure's argument as a double where it matters.

    The ``double`` a ``float`` measure is cast to comes too late in two cases,
    both measured:

    * an engine whose integer accumulator wraps
      (:meth:`~orionbelt.dialect.base.Dialect.integer_sum_is_widened`):
      ClickHouse and Dremio sum two rows of 9000000000000000000 to
      -446744073709551616 and average them to -2.2e17, and the cast carries
      that along. ``SUM(CAST(x AS DOUBLE))`` is 1.8e19;
    * an engine whose ``AVG`` of an exact input keeps a fixed number of places
      (:attr:`~orionbelt.dialect.base.Dialect.avg_rounds_exact_input`):
      Snowflake averages 1, 1 and 2 to 1.333333. ``AVG(CAST(x AS FLOAT))`` is
      1.3333333333333333.

    A DISTINCT aggregate cannot take the cast inside: two integers past 2**53
    become the same double and are deduplicated as one, so 0, 2**53 and
    2**53 + 1 averaged two values. It sums exactly instead - through the
    dialect's integer widening (:meth:`~orionbelt.dialect.base.Dialect.
    exact_integer_sum`) where the accumulator wraps and every column read is an
    integer, which no two distinct values share - and an average divides that
    sum by ``COUNT(DISTINCT x)``. A float or decimal column needs no widening:
    its accumulator does not wrap.
    """
    agg = measure.aggregation.upper()
    if agg not in ("SUM", "AVG"):
        return expr
    if resolve_measure_data_type(measure, settings, model) != FLOAT_DEFAULT:
        return expr
    wraps = dialect.integer_sum_is_widened()
    rounds = agg == "AVG" and dialect.avg_rounds_exact_input
    if not (wraps or rounds):
        return expr
    aggregate, rebuild = unwrap_default(expr)
    if not isinstance(aggregate, FunctionCall) or aggregate.name != agg:
        return expr
    if len(aggregate.args) != 1:
        return expr
    if not aggregate.distinct:
        arg = dialect.cast_to_obml_type(aggregate.args[0], FLOAT_DEFAULT)
        return rebuild(replace(aggregate, args=[arg]))
    widened = dialect.exact_integer_sum(aggregate.args[0]) if wraps else None
    if isinstance(widened, FunctionCall) and _reads_integer_columns(measure, model):
        total = replace(widened, distinct=True)
    elif rounds:
        total = FunctionCall(name="SUM", args=aggregate.args, distinct=True)
    else:
        return expr
    if agg == "SUM":
        return rebuild(total)
    count = FunctionCall(name="COUNT", args=aggregate.args, distinct=True)
    mean = BinaryOp(left=dialect.cast_to_obml_type(total, FLOAT_DEFAULT), op="/", right=count)
    return rebuild(mean)


def _reads_integer_columns(measure: Measure, model: SemanticModel | None) -> bool:
    """Whether every row *measure* aggregates is provably an integer.

    Its ``columns`` are all ``int``, or its ``expression`` reads only ``int``
    columns and combines them with nothing but integer literals, ``+``, ``-``,
    ``*`` and parentheses: ``{[T].[Qty]}`` or ``{[T].[Qty]} * 2 - 1``. A
    division, a function, a string or a fractional literal is not proven, and
    neither is an undeclared column.
    """
    if model is None:
        return False
    if measure.expression:
        refs = find_qualified_refs(measure.expression)
        residue = QUALIFIED_COLUMN_REF.sub("", measure.expression)
        if not refs or not _INTEGER_ARITHMETIC.fullmatch(residue):
            return False
        pairs = refs
    elif measure.columns:
        pairs = [(ref.view or "", ref.column or "") for ref in measure.columns]
    else:
        return False
    for view, name in pairs:
        obj = model.data_objects.get(view)
        column = obj.columns.get(name) if obj and name else None
        if column is None or column.abstract_type is not DataType.INT:
            return False
    return True


# Set while a federated query's fact leg compiles: see ``partial_sums``.
_partial_sums: ContextVar[bool] = ContextVar("partial_sums", default=False)


@contextmanager
def partial_sums() -> Iterator[None]:
    """Leave every ``SUM`` measure in the engine's own type while the block runs.

    A federated query's fact leg returns partial sums that the combine step
    adds up and then casts to the measure's type, once, as one database casts
    its single sum. Cast in the leg, each partial would be rounded or bounded
    before they are added: a ``dataType: integer`` measure over 0.60 in two
    groups gave 2 rather than 1, and no fixed decimal width keeps both the
    range and the scale of every source. The engine's own ``SUM`` keeps both:
    the input's scale, and the range the engine would compute the single sum
    in. The exact integer-sum widening still applies, since it rewrites the
    expression rather than casting it.
    """
    token = _partial_sums.set(True)
    try:
        yield
    finally:
        _partial_sums.reset(token)


_combined_averages: ContextVar[tuple[Dialect, dict[str, tuple[str, int | None]]] | None] = (
    ContextVar("combined_averages", default=None)
)


@contextmanager
def combined_averages(dialect: Dialect, parts: dict[str, tuple[str, int | None]]) -> Iterator[None]:
    """Build each named measure as a quotient while the block runs: a combined ``avg``.

    A federated query's combine step re-aggregates an ``avg`` from its fact
    leg's partial sums and counts. Its combine model declares the measure as
    the ``SUM`` of the partial sums; *parts* maps its name to the leg column
    holding the counts and the scale the sums are read at (``None`` for a
    ``float`` measure, whose sums are doubles). The measure's expression is
    then the one sum over the other (:func:`combined_average`), wherever the
    query reads it: its column, HAVING, ORDER BY and the metrics over it.
    """
    token = _combined_averages.set((dialect, parts))
    try:
        yield
    finally:
        _combined_averages.reset(token)


def combined_average(measure: Measure, total: Expr, count: Expr) -> Expr | None:
    """``total / count`` for a measure :func:`combined_averages` names, else ``None``.

    Exact for a decimal result (:meth:`Dialect.exact_quotient`), and left
    unrounded at more places than the measure's declared type keeps: HAVING,
    ORDER BY and metrics read the average as they read a raw ``AVG``, and the
    measure's own cast rounds its column once. Floating point for a float
    result, as every engine's ``AVG`` over doubles is; a zero count is
    guarded by the division itself (``NULLIF``).
    """
    active = _combined_averages.get()
    if active is None or measure.name not in active[1]:
        return None
    dialect, parts = active
    _, scale = parts[measure.name]
    target = parse_data_type(measure.data_type) if measure.data_type else None
    if scale is not None and target is not None:
        exact = dialect.exact_quotient(total, count, scale, target)
        if exact is not None:
            return exact
    return BinaryOp(left=total, op="/", right=count)


def combined_average_counts(measure: Measure) -> str | None:
    """The leg column of counts a :func:`combined_averages` measure divides by."""
    active = _combined_averages.get()
    if active is None or measure.name not in active[1]:
        return None
    return active[1][measure.name][0]


def resolve_measure_cast_type(
    measure: Measure,
    settings: ModelSettings | None,
    dialect: Dialect | None,
    model: SemanticModel | None = None,
) -> OBMLType | None:
    """The type a measure is cast to, decided without looking at its expression.

    :func:`rewrite_exact_integer_avg` can only fire on a bare ``AVG(x)``, since
    it needs the argument to rewrite. That is the right test for *rewriting*
    and the wrong one for *typing*: once wrappers compose - a window over a
    period-over-period, a cumulative over one - the later wrapper holds a CTE
    alias, so the rewrite declines and the cast fell back to the narrow
    default. The value in that CTE had already been computed exactly, so the
    narrow type saturated it on MySQL and overflowed it on Postgres for no
    reason at all.

    The type therefore follows the **measure and the dialect**, which are known
    everywhere, rather than the shape the expression happens to have at this
    point in the plan.

    ``None`` for a ``SUM`` inside :func:`partial_sums`.
    """
    if _partial_sums.get() and measure.aggregation.upper() == "SUM":
        return None
    resolved = resolve_measure_data_type(measure, settings, model)
    if measure.data_type:
        return resolved
    if _is_integer_sum(measure) and dialect is not None and dialect.integer_sum_is_widened():
        # ClickHouse and Dremio widen the accumulator, so ``bigint`` - which an
        # integer SUM otherwise infers (#315) - would cast the exact 128-bit
        # total straight back into the 64 bits the rewrite escaped. Decided
        # here rather than alongside the rewrite because the two see different
        # things: once a cumulative composes over a period-over-period, the
        # expression is a CTE alias and no rewrite fires, but the value inside
        # that CTE is already 128-bit and still needs a type that holds it.
        return DecimalType(precision=PORTABLE_DECIMAL_PRECISION, scale=0)
    if not isinstance(resolved, DecimalType):
        return resolved
    if not _is_integer_avg(measure):
        # Every other aggregate is exact on every engine, so a source declared
        # wider than the default simply gets a type that fits.
        return _widen_for_declared_source(measure, resolved, model)
    if dialect is None or not dialect.integer_avg_is_exact():
        # An engine whose average is neither exact nor rewritten: **no**
        # widening applies, neither the 64-bit room nor the declared source
        # width, since a wider type would only let a rounded value through
        # instead of failing on it. Empty today, and the reason it is empty is
        # #316; a new dialect lands here until it is measured.
        return resolved
    return _integer_avg_target(measure, resolved, model)
