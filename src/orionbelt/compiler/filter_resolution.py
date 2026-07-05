"""Filter and order-by resolution extracted from ``QueryResolver``.

Covers static model filters, query WHERE/HAVING filters (leaf + group),
auto-join extension, and ORDER BY field resolution. Functions take the
owning ``QueryResolver`` as their first argument (``resolver``);
``QueryResolver`` keeps one-line delegators so its public surface is
unchanged. Pure code movement — no behaviour change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    BinaryOp,
    ColumnRef,
    Expr,
    Literal,
    UnaryOp,
)
from orionbelt.compiler.filters import (
    build_exists_filter_expr,
    build_filter_expr,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
)
from orionbelt.models.semantic import ModelFilter

if TYPE_CHECKING:
    from orionbelt.compiler.resolution import (
        QueryResolver,
        ResolvedFilter,
        _ResolutionContext,
    )


def resolve_static_filter(
    resolver: QueryResolver, ctx: _ResolutionContext, mf: ModelFilter
) -> ResolvedFilter | None:
    """Resolve a static model filter to a physical WHERE expression.

    Silently skips filters on data objects that are unreachable from the
    query's join graph — they are simply irrelevant to the current query.
    """
    from orionbelt.compiler.resolution import ResolvedFilter, make_column_expr

    obj = ctx.model.data_objects.get(mf.data_object)
    if obj is None:
        return None

    col = obj.columns.get(mf.column)
    if col is None:
        return None

    if not resolver._resolve_filter_object(ctx, mf.data_object, "filters", mf.column):
        return None

    # Route through ``make_column_expr`` so a ``MeasureFilter`` on a
    # computed column inlines the expression — without this a filter
    # on a boolean ``expression:`` column emitted ``(1 = FALSE)``
    # because the empty ``code:`` was collapsed by the CAST.
    col_expr: Expr = make_column_expr(ctx.model, mf.data_object, mf.column)
    qf = QueryFilter(field=mf.column, op=mf.operator, value=mf.value or mf.values or None)
    filter_expr = build_filter_expr(col_expr, qf, ctx.errors)
    if filter_expr is None:
        return None
    return ResolvedFilter(
        expression=filter_expr,
        is_aggregate=False,
        referenced_fields=frozenset({mf.column}),
    )


def resolve_filter_object(
    resolver: QueryResolver,
    ctx: _ResolutionContext,
    obj_name: str,
    filter_path: str,
    _field_label: str,
) -> bool:
    """Ensure *obj_name* is joined; auto-extend if reachable.

    Silently skips filters on unreachable data objects — they are
    irrelevant to the current query.
    """
    if obj_name in ctx.joined_objects:
        return True
    if ctx.graph is None:
        return False
    reachable = any(obj_name in ctx.graph.descendants(j) for j in list(ctx.joined_objects))
    if not reachable:
        return False
    new_steps = ctx.graph.find_join_path(ctx.joined_objects, {obj_name})
    for step in new_steps:
        if step.to_object not in ctx.joined_objects:
            ctx.result.join_steps.append(step)
            ctx.joined_objects.add(step.to_object)
            ctx.result.required_objects.add(step.to_object)
    return True


def resolve_filter_item(
    resolver: QueryResolver,
    ctx: _ResolutionContext,
    item: QueryFilterItem,
    *,
    is_having: bool,
) -> ResolvedFilter | None:
    """Resolve a filter item (leaf or group) to a physical expression."""
    if isinstance(item, QueryFilter):
        return resolver._resolve_filter(ctx, item, is_having=is_having)
    return resolver._resolve_filter_group(ctx, item, is_having=is_having)


def resolve_filter_group(
    resolver: QueryResolver,
    ctx: _ResolutionContext,
    group: QueryFilterGroup,
    *,
    is_having: bool,
) -> ResolvedFilter | None:
    """Resolve a filter group recursively, combining with AND/OR."""
    from orionbelt.compiler.resolution import ResolvedFilter

    child_exprs: list[Expr] = []
    all_fields: set[str] = set()
    for child in group.filters:
        resolved = resolver._resolve_filter_item(ctx, child, is_having=is_having)
        if resolved:
            child_exprs.append(resolved.expression)
            all_fields.update(resolved.referenced_fields)

    if not child_exprs:
        return None

    # Combine children with the group's logic
    op = "AND" if group.logic == "and" else "OR"
    combined: Expr = child_exprs[0]
    for expr in child_exprs[1:]:
        combined = BinaryOp(left=combined, op=op, right=expr)

    # Optionally negate
    if group.negated:
        combined = UnaryOp(op="NOT", operand=combined)

    return ResolvedFilter(
        expression=combined,
        is_aggregate=is_having,
        referenced_fields=frozenset(all_fields),
    )


def resolve_filter(
    resolver: QueryResolver,
    ctx: _ResolutionContext,
    qf: QueryFilter,
    *,
    is_having: bool,
) -> ResolvedFilter | None:
    """Resolve a query filter to a physical expression.

    Filter fields can reference:
    1. A dimension name (e.g. ``"Order Priority"``)
    2. A qualified column ``"DataObject.Column"`` (e.g. ``"Orders.Order Priority"``)
    3. For HAVING filters, a measure name (e.g. ``"Revenue"``)

    If the referenced data object is reachable but not yet joined, the
    join path is auto-extended.
    """
    from orionbelt.compiler.resolution import ResolvedFilter, make_column_expr

    filter_path = "having" if is_having else "where"

    # 1. Try dimension name
    col_expr: Expr
    subject_object: str | None = None
    dim = ctx.model.dimensions.get(qf.field)
    if dim:
        obj_name = dim.view
        if not resolver._resolve_filter_object(ctx, obj_name, filter_path, qf.field):
            return None
        col_name = dim.column
        col_expr = make_column_expr(ctx.model, obj_name, col_name)
        subject_object = obj_name

    # 2. HAVING: try measure or metric name
    elif is_having and (qf.field in ctx.model.effective_measures or qf.field in ctx.model.metrics):
        col_expr = ColumnRef(name=qf.field)

    # 3. Try qualified column: "DataObject.Column"
    elif "." in qf.field:
        parts = qf.field.split(".", 1)
        obj_name, col_name = parts[0].strip(), parts[1].strip()
        obj = ctx.model.data_objects.get(obj_name)
        if obj is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_FILTER_FIELD",
                    message=(f"Unknown data object '{obj_name}' in filter field '{qf.field}'"),
                    path=filter_path,
                )
            )
            return None
        if col_name not in obj.columns:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_FILTER_FIELD",
                    message=(
                        f"Unknown column '{col_name}' in data object "
                        f"'{obj_name}' for filter field '{qf.field}'"
                    ),
                    path=filter_path,
                )
            )
            return None
        if not resolver._resolve_filter_object(ctx, obj_name, filter_path, qf.field):
            return None
        col_expr = make_column_expr(ctx.model, obj_name, col_name)
        subject_object = obj_name

    else:
        ctx.errors.append(
            SemanticError(
                code="UNKNOWN_FILTER_FIELD",
                message=f"Unknown filter field '{qf.field}'",
                path=filter_path,
            )
        )
        return None

    # exists/nonexists need model + subject object + qualify_table to
    # build the correlated subquery. HAVING is rejected entirely in v2.7:
    # the correlation predicate references row-level columns of the
    # subject data object, but HAVING is evaluated after GROUP BY — the
    # raw subject column is no longer in scope, producing invalid SQL on
    # every dialect. Measure-level EXISTS (the proper HAVING-equivalent)
    # is deferred to ``MeasureFilter.subquery`` in a future release.
    if qf.op in (FilterOperator.EXISTS, FilterOperator.NONEXISTS):
        if is_having:
            ctx.errors.append(
                SemanticError(
                    code="INVALID_FILTER_OPERATOR",
                    message=(
                        f"'{qf.op}' is only valid in 'where' — HAVING is "
                        "evaluated after GROUP BY, where the row-level "
                        "correlation predicate is out of scope. Move the "
                        "filter to 'where', or use a precomputed boolean "
                        "column on the data object."
                    ),
                    path=filter_path,
                )
            )
            return None
        if subject_object is None:
            ctx.errors.append(
                SemanticError(
                    code="INVALID_FILTER_OPERATOR",
                    message=(
                        f"'{qf.op}' requires a dimension or qualified column "
                        "as the subject — measure/metric references are not "
                        "valid subjects for a correlated subquery."
                    ),
                    path=filter_path,
                )
            )
            return None
        qt = ctx.qualify_table or (lambda obj: obj.qualified_code)
        filter_expr = build_exists_filter_expr(qf, ctx.model, subject_object, qt, ctx.errors)
    else:
        filter_expr = build_filter_expr(col_expr, qf, ctx.errors)

    if filter_expr is None:
        return None
    return ResolvedFilter(
        expression=filter_expr,
        is_aggregate=is_having,
        referenced_fields=frozenset({qf.field}),
    )


def resolve_order_by_field(
    resolver: QueryResolver,
    ctx: _ResolutionContext,
    field_name: str,
    select_count: int,
) -> Expr | None:
    """Resolve an order-by field to its expression."""
    from orionbelt.compiler.resolution import make_column_expr

    # Coalesce alias: outer SELECT exposes it as a bare alias column,
    # so a table-less ColumnRef is the right form for both star and CFL.
    if field_name in ctx.result.coalesce_aliases:
        return ColumnRef(name=field_name)

    for dim in ctx.result.dimensions:
        if dim.name == field_name:
            # Use make_column_expr so computed columns (which have empty
            # ``code``) inline their expression instead of producing an
            # empty column ref like ``"Orders"."" ``.
            return make_column_expr(ctx.model, dim.object_name, dim.column_name)

    for meas in ctx.result.measures:
        if meas.name == field_name:
            # Window / cumulative / period-over-period metrics are
            # exposed by the outer SELECT as a bare alias after their
            # wrapper CTE runs — ordering by ``meas.expression`` here
            # would point ORDER BY at the *base measure's* inner
            # aggregate (the lag-input, the cumulative-input), not at
            # the windowed output the user asked for. Same pattern as
            # coalesce_aliases above: emit a table-less ColumnRef so
            # both star and CFL outer SELECTs bind it correctly.
            if meas.is_window or meas.is_cumulative or meas.is_pop:
                return ColumnRef(name=meas.name)
            return meas.expression

    # Raw mode: order by the field's "DataObject.Column" alias.
    for f in ctx.result.fields:
        if f.alias == field_name:
            return make_column_expr(ctx.model, f.object_name, f.column_name)

    if field_name.isdigit():
        pos = int(field_name)
        if 1 <= pos <= select_count:
            return Literal.number(pos)
        ctx.errors.append(
            SemanticError(
                code="INVALID_ORDER_BY_POSITION",
                message=(
                    f"ORDER BY position {pos} is out of range (SELECT has {select_count} columns)"
                ),
                path="orderBy",
            )
        )
        return None

    ctx.errors.append(
        SemanticError(
            code="UNKNOWN_ORDER_BY_FIELD",
            message=(
                f"ORDER BY field '{field_name}' is not a dimension or measure in the query's SELECT"
            ),
            path="orderBy",
        )
    )
    return None
