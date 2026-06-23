"""Metric resolution extracted from ``QueryResolver``.

Covers derived, window, cumulative, and period-over-period metrics plus the
shared ``partitionBy`` validation. Functions take the owning
``QueryResolver`` as their first argument (``resolver``); ``QueryResolver``
keeps one-line delegators so its public surface is unchanged. Pure code
movement — no behaviour change.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import ColumnRef
from orionbelt.compiler.expr_parser import (
    parse_expression,
    tokenize_metric_formula,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import (
    Metric,
    MetricType,
    WindowFunctionKind,
)

if TYPE_CHECKING:
    from orionbelt.compiler.resolution import (
        QueryResolver,
        ResolvedMeasure,
        _ResolutionContext,
    )


def resolve_metric(
    resolver: QueryResolver, ctx: _ResolutionContext, name: str, metric: Metric
) -> ResolvedMeasure | None:
    """Resolve a metric to its combined expression."""
    if metric.type == MetricType.CUMULATIVE:
        return resolver._resolve_cumulative_metric(ctx, name, metric)
    if metric.type == MetricType.PERIOD_OVER_PERIOD:
        return resolver._resolve_pop_metric(ctx, name, metric)
    if metric.type == MetricType.WINDOW:
        return resolver._resolve_window_metric(ctx, name, metric)
    return resolver._resolve_derived_metric(ctx, name, metric)


def validate_partition_dimensions(
    resolver: QueryResolver,
    ctx: _ResolutionContext,
    metric_name: str,
    partition_by: list[str],
    path_template: str,
) -> bool:
    """Validate every partitionBy entry references a model dimension
    present in the query's SELECT. Returns False (and accumulates errors)
    on any failure. Reachability to the measure source is enforced
    transitively by ``required_objects`` reachability later in resolution.
    """
    if not partition_by:
        return True
    dim_names = {d.name for d in ctx.result.dimensions}
    for dim_name in partition_by:
        if dim_name not in ctx.model.dimensions:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_PARTITION_DIMENSION",
                    message=(
                        f"Metric '{metric_name}' references unknown partition "
                        f"dimension '{dim_name}'"
                    ),
                    path=path_template.format(metric_name),
                )
            )
            return False
        if dim_name not in dim_names:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_PARTITION_DIMENSION",
                    message=(
                        f"Metric '{metric_name}' requires partitionBy dimension "
                        f"'{dim_name}' to be in the query's selected dimensions"
                    ),
                    path=path_template.format(metric_name),
                )
            )
            return False
    return True


def resolve_window_metric(
    resolver: QueryResolver, ctx: _ResolutionContext, name: str, metric: Metric
) -> ResolvedMeasure | None:
    """Resolve a window metric (rank/lag/lead/ntile/first_value/last_value)."""
    from orionbelt.compiler.resolution import ResolvedMeasure

    if metric.window_function is None:
        ctx.errors.append(
            SemanticError(
                code="INVALID_METRIC",
                message=f"Window metric '{name}' missing required 'windowFunction'",
                path=f"metrics.{name}",
            )
        )
        return None

    wf = metric.window_function
    base_measure_name = metric.measure
    base_aggregation = ""

    # Validate referenced measure (if any) exists
    if base_measure_name is not None:
        base_measure = ctx.model.measures.get(base_measure_name)
        if base_measure is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_MEASURE",
                    message=(
                        f"Window metric '{name}' references unknown measure '{base_measure_name}'"
                    ),
                    path=f"metrics.{name}.measure",
                )
            )
            return None
        base_aggregation = base_measure.aggregation
        if base_measure_name not in ctx.result.metric_components:
            comp = resolver._resolve_measure(ctx, base_measure_name)
            if comp:
                ctx.result.metric_components[base_measure_name] = comp

    # timeDimension is required for LAG/LEAD, optional otherwise (RANK uses measure value)
    if metric.time_dimension is not None:
        dim = ctx.model.dimensions.get(metric.time_dimension)
        if dim is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_DIMENSION",
                    message=(
                        f"Window metric '{name}' references unknown "
                        f"timeDimension '{metric.time_dimension}'"
                    ),
                    path=f"metrics.{name}.timeDimension",
                )
            )
            return None
        dim_names = {d.name for d in ctx.result.dimensions}
        if metric.time_dimension not in dim_names:
            ctx.errors.append(
                SemanticError(
                    code="WINDOW_TIME_DIMENSION_NOT_IN_SELECT",
                    message=(
                        f"Window metric '{name}' requires timeDimension "
                        f"'{metric.time_dimension}' to be in the query's selected dimensions"
                    ),
                    path=f"metrics.{name}.timeDimension",
                )
            )
            return None
    elif wf in {WindowFunctionKind.LAG, WindowFunctionKind.LEAD}:
        ctx.errors.append(
            SemanticError(
                code="INVALID_LAG_LEAD",
                message=(
                    f"Window metric '{name}' with function '{wf.value}' requires 'timeDimension'"
                ),
                path=f"metrics.{name}",
            )
        )
        return None

    if wf == WindowFunctionKind.NTILE and (metric.buckets is None or metric.buckets < 2):
        ctx.errors.append(
            SemanticError(
                code="INVALID_NTILE_BUCKETS",
                message=(f"Window metric '{name}' with function 'ntile' requires 'buckets' >= 2"),
                path=f"metrics.{name}.buckets",
            )
        )
        return None

    if wf in {WindowFunctionKind.LAG, WindowFunctionKind.LEAD} and (
        metric.offset is None or metric.offset < 1
    ):
        ctx.errors.append(
            SemanticError(
                code="INVALID_LAG_LEAD",
                message=(
                    f"Window metric '{name}' with function '{wf.value}' requires positive 'offset'"
                ),
                path=f"metrics.{name}.offset",
            )
        )
        return None

    if not resolver._validate_partition_dimensions(
        ctx, name, metric.partition_by, "metrics.{}.partitionBy"
    ):
        return None

    return ResolvedMeasure(
        name=name,
        aggregation=base_aggregation,
        expression=ColumnRef(name=base_measure_name or name),
        is_expression=True,
        component_measures=[base_measure_name] if base_measure_name else [],
        is_window=True,
        window_function=wf,
        window_base_measure=base_measure_name,
        window_time_dimension=metric.time_dimension,
        window_partition_by=list(metric.partition_by),
        window_offset=metric.offset,
        window_buckets=metric.buckets,
        window_order_direction=metric.order_direction.lower(),
        window_default_value=metric.default_value,
    )


def resolve_derived_metric(
    resolver: QueryResolver, ctx: _ResolutionContext, name: str, metric: Metric
) -> ResolvedMeasure | None:
    """Resolve a derived metric to its combined expression."""
    from orionbelt.compiler.resolution import ResolvedMeasure

    formula = metric.expression

    # Extract and resolve each component measure
    component_names = re.findall(r"\{\[([^\]]+)\]\}", formula or "")
    for comp_name in component_names:
        if comp_name not in ctx.result.metric_components:
            comp = resolver._resolve_measure(ctx, comp_name)
            if comp:
                ctx.result.metric_components[comp_name] = comp

    # Parse the formula into an AST tree
    try:
        tokens = tokenize_metric_formula(formula or "")
        parsed_expr = parse_expression(tokens)
    except Exception as exc:
        ctx.errors.append(
            SemanticError(
                code="INVALID_METRIC_EXPRESSION",
                message=f"Metric '{name}' has invalid expression: {exc}",
                path=f"metrics.{name}.expression",
            )
        )
        return None

    return ResolvedMeasure(
        name=name,
        aggregation="",
        expression=parsed_expr,
        component_measures=component_names,
        is_expression=True,
    )


def resolve_cumulative_metric(
    resolver: QueryResolver, ctx: _ResolutionContext, name: str, metric: Metric
) -> ResolvedMeasure | None:
    """Resolve a cumulative metric referencing an existing measure."""
    from orionbelt.compiler.resolution import ResolvedMeasure

    if metric.measure is None:
        ctx.errors.append(
            SemanticError(
                code="INVALID_METRIC",
                message=f"Cumulative metric '{name}' missing required 'measure' field",
                path=f"metrics.{name}",
            )
        )
        return None
    if metric.time_dimension is None:
        ctx.errors.append(
            SemanticError(
                code="INVALID_METRIC",
                message=f"Cumulative metric '{name}' missing required 'timeDimension' field",
                path=f"metrics.{name}",
            )
        )
        return None

    # Validate referenced measure exists
    base_measure = ctx.model.measures.get(metric.measure)
    if base_measure is None:
        ctx.errors.append(
            SemanticError(
                code="UNKNOWN_MEASURE",
                message=(
                    f"Cumulative metric '{name}' references unknown measure '{metric.measure}'"
                ),
                path=f"metrics.{name}.measure",
            )
        )
        return None

    # Validate timeDimension is a known dimension
    dim = ctx.model.dimensions.get(metric.time_dimension)
    if dim is None:
        ctx.errors.append(
            SemanticError(
                code="UNKNOWN_DIMENSION",
                message=(
                    f"Cumulative metric '{name}' references unknown "
                    f"timeDimension '{metric.time_dimension}'"
                ),
                path=f"metrics.{name}.timeDimension",
            )
        )
        return None

    # Validate timeDimension is in the query's selected dimensions
    dim_names = {d.name for d in ctx.result.dimensions}
    if metric.time_dimension not in dim_names:
        ctx.errors.append(
            SemanticError(
                code="CUMULATIVE_TIME_DIMENSION_NOT_IN_SELECT",
                message=(
                    f"Cumulative metric '{name}' requires timeDimension "
                    f"'{metric.time_dimension}' to be in the query's selected dimensions"
                ),
                path=f"metrics.{name}.timeDimension",
            )
        )
        return None

    # Validate partitionBy dimensions
    if not resolver._validate_partition_dimensions(
        ctx, name, metric.partition_by, "metrics.{}.partitionBy"
    ):
        return None

    # Resolve the base measure as a component (reuse existing resolution)
    if metric.measure not in ctx.result.metric_components:
        comp = resolver._resolve_measure(ctx, metric.measure)
        if comp:
            ctx.result.metric_components[metric.measure] = comp

    # The cumulative metric's expression is a placeholder ColumnRef to the base measure
    # The actual window function is built during the cumulative_wrap phase
    return ResolvedMeasure(
        name=name,
        aggregation=base_measure.aggregation,
        expression=ColumnRef(name=metric.measure),
        is_expression=True,
        component_measures=[metric.measure],
        is_cumulative=True,
        cumulative_measure=metric.measure,
        cumulative_time_dimension=metric.time_dimension,
        cumulative_type=metric.cumulative_type,
        cumulative_window=metric.window,
        cumulative_grain_to_date=metric.grain_to_date,
        cumulative_partition_by=list(metric.partition_by),
    )


def resolve_pop_metric(
    resolver: QueryResolver, ctx: _ResolutionContext, name: str, metric: Metric
) -> ResolvedMeasure | None:
    """Resolve a period-over-period metric."""
    from orionbelt.compiler.resolution import ResolvedMeasure

    if metric.period_over_period is None:
        ctx.errors.append(
            SemanticError(
                code="INVALID_METRIC",
                message=f"PoP metric '{name}' missing required 'periodOverPeriod' field",
                path=f"metrics.{name}",
            )
        )
        return None
    if metric.expression is None:
        ctx.errors.append(
            SemanticError(
                code="INVALID_METRIC",
                message=f"PoP metric '{name}' missing required 'expression' field",
                path=f"metrics.{name}",
            )
        )
        return None

    pop = metric.period_over_period

    # Validate timeDimension is a known dimension
    dim = ctx.model.dimensions.get(pop.time_dimension)
    if dim is None:
        ctx.errors.append(
            SemanticError(
                code="POP_UNKNOWN_TIME_DIMENSION",
                message=(
                    f"Period-over-period metric '{name}' references unknown "
                    f"time dimension '{pop.time_dimension}'"
                ),
                path=f"metrics.{name}.periodOverPeriod.timeDimension",
            )
        )
        return None

    # Validate timeDimension is in the query's selected dimensions
    dim_names = {d.name for d in ctx.result.dimensions}
    if pop.time_dimension not in dim_names:
        ctx.errors.append(
            SemanticError(
                code="POP_TIME_DIMENSION_NOT_IN_SELECT",
                message=(
                    f"Period-over-period metric '{name}' requires time dimension "
                    f"'{pop.time_dimension}' to be in the query's selected dimensions"
                ),
                path=f"metrics.{name}.periodOverPeriod.timeDimension",
            )
        )
        return None

    # Validate offset is non-zero
    if pop.offset == 0:
        ctx.errors.append(
            SemanticError(
                code="POP_INVALID_OFFSET",
                message=(
                    f"Period-over-period metric '{name}' has offset=0 "
                    f"(must be non-zero, e.g. -1 for previous period)"
                ),
                path=f"metrics.{name}.periodOverPeriod.offset",
            )
        )
        return None

    # Resolve the expression (same as derived — parse {[Measure Name]} refs)
    component_names = re.findall(r"\{\[([^\]]+)\]\}", metric.expression)

    # PoP comparison logic only supports single-measure expressions
    if len(component_names) > 1:
        ctx.errors.append(
            SemanticError(
                code="POP_MULTI_MEASURE_NOT_SUPPORTED",
                message=(
                    f"Period-over-period metric '{name}' references multiple measures "
                    f"({', '.join(component_names)}). PoP comparison currently supports "
                    f"only single-measure expressions."
                ),
                path=f"metrics.{name}.expression",
            )
        )
        return None

    for comp_name in component_names:
        if comp_name not in ctx.result.metric_components:
            comp = resolver._resolve_measure(ctx, comp_name)
            if comp:
                ctx.result.metric_components[comp_name] = comp

    try:
        tokens = tokenize_metric_formula(metric.expression)
        parsed_expr = parse_expression(tokens)
    except Exception as exc:
        ctx.errors.append(
            SemanticError(
                code="INVALID_METRIC_EXPRESSION",
                message=f"Metric '{name}' has invalid expression: {exc}",
                path=f"metrics.{name}.expression",
            )
        )
        return None

    # Use the first component measure as the base (for single-measure PoP)
    pop_base = component_names[0] if component_names else None

    return ResolvedMeasure(
        name=name,
        aggregation="",
        expression=parsed_expr,
        component_measures=component_names,
        is_expression=True,
        is_pop=True,
        pop_base_measure=pop_base,
        pop_time_dimension=pop.time_dimension,
        pop_grain=pop.grain,
        pop_offset=pop.offset,
        pop_offset_grain=pop.offset_grain,
        pop_comparison=pop.comparison,
    )
