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

from collections.abc import Callable

from orionbelt.ast.nodes import Expr, FunctionCall
from orionbelt.dialect.base import Dialect
from orionbelt.models.expressions import find_qualified_refs
from orionbelt.models.semantic import (
    DataType,
    Measure,
    Metric,
    ModelSettings,
    PeriodOverPeriodComparison,
    SemanticModel,
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
    aggregate, rebuild = _unwrap_default(expr)
    if not isinstance(aggregate, FunctionCall) or aggregate.name != "AVG":
        return None
    if aggregate.distinct or len(aggregate.args) != 1:
        return None
    resolved = resolve_measure_data_type(measure, settings)
    if not isinstance(resolved, DecimalType):
        return None
    target = resolved if measure.data_type else _widen_to_integer_range(resolved)
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
    # Neither exact nor rewritable: DuckDB alone. It keeps the default so the
    # overflow stays loud, rather than trading it for a quiet wrong number
    # (#316).
    return None


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
    exact = rewrite_exact_integer_avg(measure, settings, dialect, expr)
    if exact is not None:
        rewritten, target = exact
        return dialect.cast_to_obml_type(rewritten, target)
    resolved = resolve_measure_cast_type(measure, settings, dialect, model)
    if resolved is None:
        return expr
    return dialect.cast_to_obml_type(expr, resolved)


def _unwrap_default(expr: Expr) -> tuple[Expr, Callable[[Expr], Expr]]:
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
    exact = rewrite_exact_integer_avg(measure, settings, dialect, expr)
    return expr if exact is None else exact[0]


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
    """
    resolved = resolve_measure_data_type(measure, settings)
    if measure.data_type or not isinstance(resolved, DecimalType):
        return resolved
    resolved = _widen_for_declared_source(measure, resolved, model)
    if dialect is None:
        return resolved
    if measure.aggregation.upper() != "AVG" or measure.result_type != DataType.INT:
        return resolved
    if not dialect.integer_avg_is_exact():
        # DuckDB alone: the average is not exact, so a wider type would let a
        # rounded value through instead of failing on it (#316).
        return resolved
    return _widen_to_integer_range(resolved)
