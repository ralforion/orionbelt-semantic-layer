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
    static_filter_predicate,
    static_filter_unreachable_error,
)
from orionbelt.compiler.period_literals import column_is_date_bearing, dimension_is_date_bearing
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
)
from orionbelt.models.semantic import FilterEnforcement, ModelFilter
from orionbelt.models.warnings import WarningCode, warning

if TYPE_CHECKING:
    from orionbelt.compiler.resolution import (
        QueryResolver,
        ResolvedFilter,
        _ResolutionContext,
    )


def _join_expression_objects(
    resolver: QueryResolver,
    ctx: _ResolutionContext,
    object_name: str,
    column_name: str,
    filter_path: str,
    field_label: str,
    *,
    static: bool = False,
) -> bool:
    """Join whatever a computed column reads besides its own data object.

    ``make_column_expr`` inlines the expression, so the predicate names those
    aliases directly and is only usable once each of them is in the FROM chain.
    Unreachable means here what it means for the filter's own object: refused
    for a query filter, skipped for a static one, never emitted as SQL that
    binds to nothing.
    """
    return all(
        resolver._resolve_filter_object(ctx, dep, filter_path, field_label, static=static)
        for dep in sorted(ctx.model.column_reference_objects(object_name, column_name))
    )


def resolve_static_filter(
    resolver: QueryResolver, ctx: _ResolutionContext, mf: ModelFilter
) -> ResolvedFilter | None:
    """Resolve a static model filter to a physical WHERE expression.

    A filter on a data object the query cannot reach is never dropped
    silently: an ``enforce: always`` filter refuses the query
    (``STATIC_FILTER_UNREACHABLE``), an ``enforce: relevant`` one is skipped
    with a ``STATIC_FILTER_NOT_APPLIED`` warning.
    """
    from orionbelt.compiler.resolution import ResolvedFilter

    obj = ctx.model.data_objects.get(mf.data_object)
    if obj is None or mf.column not in obj.columns:
        # Only a model narrowed for one scan lacks the object: the member list
        # of a ``population: dimension`` metric is planned over the members'
        # data objects alone. Fact filters do not limit who counts there, but
        # an enforced filter cannot be left behind with the object it reads.
        if mf.enforce == FilterEnforcement.ALWAYS:
            report_unapplied_static_filter(ctx.errors, ctx.result.warnings, mf, mf.data_object)
        return None

    errors_before = len(ctx.errors)
    if not (
        resolver._resolve_filter_object(ctx, mf.data_object, "filters", mf.column, static=True)
        and _join_expression_objects(
            resolver, ctx, mf.data_object, mf.column, "filters", mf.column, static=True
        )
    ):
        # An ambiguous route has already been reported as an error of its own.
        if len(ctx.errors) == errors_before:
            report_unapplied_static_filter(ctx.errors, ctx.result.warnings, mf, mf.data_object)
        return None

    filter_expr = static_filter_predicate(ctx.model, mf, ctx.errors)
    if filter_expr is None:
        return None
    return ResolvedFilter(
        expression=filter_expr,
        is_aggregate=False,
        referenced_fields=frozenset({mf.column}),
        static=True,
        enforced=mf.enforce == FilterEnforcement.ALWAYS,
    )


def report_unapplied_static_filter(
    errors: list[SemanticError],
    warnings: list[SemanticError],
    mf: ModelFilter | None,
    unreachable: str,
    measures: str | None = None,
) -> None:
    """Record a static filter a query, or one leg of it, cannot apply.

    *mf* is ``None`` when only the resolved filter is at hand (a union leg);
    then the filter is described by the object it reads. ``enforced`` comes
    from *mf* when given.
    """
    label = (
        f"static filter on '{mf.data_object}.{mf.column}'"
        if mf is not None
        else f"static filter on '{unreachable}'"
    )
    where = f"the fact behind {measures}" if measures else "this query"
    if mf is not None and mf.enforce == FilterEnforcement.ALWAYS:
        errors.append(static_filter_unreachable_error(label, unreachable, where))
        return
    warnings.append(
        warning(
            code=WarningCode.STATIC_FILTER_NOT_APPLIED,
            message=(
                f"The {label} was not applied: {where} cannot reach '{unreachable}'. "
                f"Its rows are returned without that restriction."
            ),
            path="filters",
            hint=(
                "Declare the filter 'enforce: always' to refuse such queries instead, "
                "or add a join that reaches the filtered data object."
            ),
        )
    )


def conjuncts(expression: Expr) -> list[Expr]:
    """The conditions *expression* ANDs together, an AND group's flattened.

    Each restricts rows on its own, so a union leg applies the ones it can
    reach: skipping the whole group for one condition it cannot left the
    leg without the others too. A negated group is one condition.
    """
    if isinstance(expression, BinaryOp) and expression.op.upper() == "AND":
        return [*conjuncts(expression.left), *conjuncts(expression.right)]
    return [expression]


def report_unapplied_query_filter(
    warnings: list[SemanticError], unreachable: str, measures: str
) -> None:
    """Record a query filter one union leg cannot reach.

    The leg's values are computed without it rather than refused or blanked:
    they are what that fact holds, and the warning says which of the query's
    columns the filter does not restrict (decided for multi-fact queries).
    """
    warnings.append(
        warning(
            code=WarningCode.FILTER_NOT_APPLIED,
            message=(
                f"The filter on '{unreachable}' was not applied to {measures}: "
                f"their fact cannot reach '{unreachable}', so they are computed "
                f"without that restriction. The query's other columns are filtered."
            ),
            path="where",
            hint=(
                "Query those columns separately, or filter on a data object every "
                "selected fact reaches."
            ),
        )
    )


def resolve_filter_object(
    resolver: QueryResolver,
    ctx: _ResolutionContext,
    obj_name: str,
    filter_path: str,
    _field_label: str,
    *,
    static: bool = False,
) -> bool:
    """Ensure *obj_name* is joined; auto-extend if reachable.

    An object the query cannot reach is handled by who wrote the filter:

    * A **query** filter is refused (``UNREACHABLE_FILTER_OBJECT``). The caller
      asked for those rows only; dropping the predicate answers a different
      question with nothing to say so.
    * A **static** model filter is skipped. It is declared once for every
      query, and one naming an object this plan never reads is irrelevant to it.
    * In a **multi-fact** query the predicate is kept when some measure's fact
      reaches the object: each union leg joins what it needs, and the CFL
      planner decides per leg (``cfl.CFLPlanner``), refusing a leg it cannot
      restrict rather than leaving that leg unfiltered.

    Refuses, though, when the object is reachable as more than one *role*: a
    predicate on ``Date`` in a query joining two facts that each have their own
    date means one of them, and picking is not the compiler's call. Unlike an
    unreachable object — absent from the query, so dropping its filter changes
    nothing — an ambiguous one silently answers a different question.
    """
    if obj_name in ctx.joined_objects:
        return True
    # Read only through a fact an anchored measure conforms: the filter is
    # applied inside that fact's subquery, which joins what it reads
    # (``QueryResolver._push_filters_into_conformed_facts``).
    if any(obj_name in objs for facts in ctx.conformed_reach.values() for objs in facts.values()):
        return True
    if ctx.graph is None:
        return False
    reachable = any(obj_name in ctx.graph.descendants(j) for j in list(ctx.joined_objects))
    if not reachable:
        # The union's legs are rooted at the measures' facts, at a
        # dimension-only query's roots, or in raw mode at the selected fields'
        # objects.
        roots = (
            ctx.result.measure_source_objects
            | set(ctx.result.dimension_roots)
            | {f.object_name for f in ctx.result.fields}
        )
        reaching = sorted(
            root
            for root in roots
            if obj_name in ctx.graph.descendants_without_unnest(root) | {root}
        )
        if ctx.result.requires_cfl and reaching:
            # The legs on these roots join the object to filter, so it and the
            # tables on the way are read, and the result cache is invalidated
            # by the tables in required_objects.
            for root in reaching:
                ctx.result.required_objects.add(root)
                for step in ctx.graph.find_join_path({root}, {root, obj_name}):
                    ctx.result.required_objects.add(step.from_object)
                    ctx.result.required_objects.add(step.to_object)
            return True
        if static:
            return False
        ctx.errors.append(
            SemanticError(
                code="UNREACHABLE_FILTER_OBJECT",
                message=(
                    f"Filter field '{_field_label}' is on data object '{obj_name}', which "
                    f"this query cannot reach from its base '{ctx.result.base_object}' via "
                    f"directed joins. Applying the query without it would return rows the "
                    f"filter excludes."
                ),
                path=filter_path,
                hint=(
                    f"Select a measure or dimension whose data object reaches '{obj_name}', "
                    f"declare a join towards it, or remove the filter."
                ),
            )
        )
        return False
    roles = ctx.graph.role_candidates(
        ctx.joined_objects, obj_name, prefer_from=ctx.result.base_object
    )
    if len(roles) > 1:
        routes = ", ".join(f"via '{path[-2]}'" for path in roles)
        ctx.errors.append(
            SemanticError(
                code="AMBIGUOUS_JOIN_PATH",
                message=(
                    f"Filter field '{_field_label}' is on '{obj_name}', which this "
                    f"query reaches equally well by more than one route ({routes}). "
                    f"Those are different roles of the same data object and they "
                    f"select different rows."
                ),
                path=filter_path,
                hint=(
                    "Say which one is meant: declare a data object per role over "
                    "the same table and filter on that, or give the dimension a "
                    "'via:' waypoint naming the object the join must traverse."
                ),
            )
        )
        return False
    new_steps = ctx.graph.find_join_path(
        ctx.joined_objects, {obj_name}, prefer_from=ctx.result.base_object
    )
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
    subjects: set[tuple[str, str] | None] = set()
    for child in group.filters:
        resolved = resolver._resolve_filter_item(ctx, child, is_having=is_having)
        if resolved:
            child_exprs.append(resolved.expression)
            all_fields.update(resolved.referenced_fields)
            subjects.add(resolved.subject)

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

    # A group over one column tests that column, whatever its logic: a date
    # range written as a group is as much a time filter as two leaf filters.
    # A group mixing columns has no single subject.
    return ResolvedFilter(
        expression=combined,
        is_aggregate=is_having,
        referenced_fields=frozenset(all_fields),
        subject=next(iter(subjects)) if len(subjects) == 1 else None,
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
    from orionbelt.compiler.resolution import (
        ResolvedFilter,
        make_column_expr,
        make_filter_subject_expr,
    )

    filter_path = "having" if is_having else "where"

    # 1. Try dimension name
    col_expr: Expr
    subject_object: str | None = None
    subject_column = ""
    # A date-bearing field reads a period literal ('2025-Q1') as its bounds.
    date_bearing = False
    dim = ctx.model.dimensions.get(qf.field)
    if dim:
        obj_name = dim.view
        if not resolver._resolve_filter_object(ctx, obj_name, filter_path, qf.field):
            return None
        col_name = dim.column
        if not _join_expression_objects(resolver, ctx, obj_name, col_name, filter_path, qf.field):
            return None
        # A grained dimension is filtered by its period, at its own grain
        # whatever grain the query shows it at: the filter means the same in
        # every query.
        col_expr = make_filter_subject_expr(ctx.model, dim)
        subject_object, subject_column = obj_name, col_name
        date_bearing = dimension_is_date_bearing(dim)

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
        if not _join_expression_objects(resolver, ctx, obj_name, col_name, filter_path, qf.field):
            return None
        col_expr = make_column_expr(ctx.model, obj_name, col_name)
        subject_object, subject_column = obj_name, col_name
        date_bearing = column_is_date_bearing(ctx.model, obj_name, col_name)

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
        filter_expr = build_exists_filter_expr(
            qf,
            ctx.model,
            subject_object,
            qt,
            ctx.errors,
            touched_objects=ctx.result.subquery_objects,
        )
    else:
        filter_expr = build_filter_expr(col_expr, qf, ctx.errors, date_bearing=date_bearing)

    if filter_expr is None:
        return None
    is_row_filter = not is_having and qf.op not in (FilterOperator.EXISTS, FilterOperator.NONEXISTS)
    return ResolvedFilter(
        expression=filter_expr,
        is_aggregate=is_having,
        referenced_fields=frozenset({qf.field}),
        subject=(
            (subject_object, subject_column)
            if is_row_filter and subject_object is not None
            else None
        ),
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
        if dim.name == field_name and dim.name != dim.dimension:
            # A second grain of a dimension the query selects twice: its own
            # column, which the date column would name the first grain of.
            return ColumnRef(name=dim.name)
        if dim.name == field_name:
            # Use make_column_expr so computed columns (which have empty
            # ``code``) inline their expression instead of producing an
            # empty column ref like ``"Orders"."" ``.
            return make_column_expr(ctx.model, dim.object_name, dim.column_name)

    for meas in ctx.result.measures:
        if meas.name == field_name:
            # Window / cumulative / period-over-period / reaggregate metrics are
            # exposed by the outer SELECT as a bare alias after their
            # wrapper CTE runs — ordering by ``meas.expression`` here
            # would point ORDER BY at the *base measure's* inner
            # aggregate (the lag-input, the cumulative-input), not at
            # the windowed output the user asked for. Same pattern as
            # coalesce_aliases above: emit a table-less ColumnRef so
            # both star and CFL outer SELECTs bind it correctly.
            #
            # A derived metric likewise: its expression is a formula over its
            # components' aliases (``"revenue" / NULLIF("order_count", 0)``),
            # which name nothing unless every component is selected too, and
            # then bind only where an engine reads output aliases inside an
            # ORDER BY expression. Its own alias is in every SELECT.
            if (
                meas.is_window
                or meas.is_cumulative
                or meas.is_pop
                or meas.is_reaggregate
                or meas.component_measures
            ):
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
                f"ORDER BY field '{field_name}' is not a dimension in the query's SELECT, "
                "or a measure or metric of the model"
            ),
            path="orderBy",
        )
    )
    return None
