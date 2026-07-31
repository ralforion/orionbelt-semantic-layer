"""Grain deduplication for measures sourced from the *one* side of a join.

``compiler/fanout.py`` catches row multiplication that happens when a join is
traversed *backwards* (a reversed many-to-one, or a many-to-many). It treats a
*forward* many-to-one as safe, which is true for measures sourced from the many
side — the side that drives the query grain.

It is not true for a measure sourced from the **one** side. ``Sales`` joined to
``Products`` repeats each product row once per sale, so
``SUM(Products.stock_on_hand)`` grouped by ``Sales.region`` counts every product
once per sale it appeared in.

The query is refused outright when the one-side measure is the *only* measure —
resolution anchors the base object on the measure's source and the many side
becomes unreachable (``UNREACHABLE_REQUIRED_OBJECT``). But when a many-side
measure rides along, the base flips to the many side, the join is forward, and
the inflated ``SUM`` compiles silently. That is the case this module fixes.

The rewrite aggregates the affected measures in their own CTE, over rows
deduplicated on the source object's identity, then joins the result back onto
the main query's grain:

    WITH "main" AS (            -- many-side measures, at base grain
      SELECT region, SUM(s.amount) ... GROUP BY region
    ),
    "dedup_0" AS (              -- one-side measures, one row per (grain, product)
      SELECT "Region", SUM("__ob_c0") AS "Total Stock On Hand"
      FROM (
        SELECT DISTINCT s.region AS "Region",
               p.id AS "__ob_k0", p.stock_on_hand AS "__ob_c0"
        FROM sales s LEFT JOIN products p ON ...
      ) AS "dedup_src_0"
      GROUP BY "Region"
    )
    SELECT ... FROM "main" LEFT JOIN "dedup_0" USING (grain)

Note the arithmetic this produces: per-group values are correct, but a product
sold in two regions is counted in both, so the groups overlap and the column
does not sum to the product catalogue's grand total. That is inherent to the
question, not to the rewrite — it is the same caveat
:data:`~orionbelt.models.warnings.WarningCode.FAN_TRAP_RISK` already carries for
the junction-table case, and a warning says so.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    Between,
    BinaryOp,
    CaseExpr,
    Cast,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    InList,
    IsNull,
    Join,
    JoinType,
    Literal,
    OrderByItem,
    RegexMatch,
    RelativeDateRange,
    Select,
    UnaryOp,
    WindowFunction,
)
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.filters import collect_measure_filter_objects
from orionbelt.compiler.resolution import ResolvedQuery, make_column_expr
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import Cardinality, Measure, SemanticModel
from orionbelt.models.warnings import WarningCode, warning

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect

_MEASURE_COLUMN_REF = re.compile(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}")

# Aggregations whose result is unchanged when every input row is duplicated.
# Everything else — SUM, COUNT, AVG, the distribution/regression family,
# LISTAGG — reads row multiplicity and is wrong over replicated rows.
MULTIPLICITY_SAFE_AGGREGATIONS = frozenset({"min", "max", "count_distinct", "any_value"})

# Vendor-delegated aggregation: the engine resolves it through its own metric
# machinery, so we cannot reason about its multiplicity behaviour. Left alone.
_DELEGATED_AGGREGATION = "measure"

# Everything this pass introduces into the query namespace carries an ``__ob_``
# prefix. A CTE named ``main`` — which is what this pass used to emit — is a
# plausible schema or table name in a user's warehouse (it is DuckDB's default
# schema), so the generated SQL read as ``FROM "main"."sales"`` on one line and
# ``FROM "main"`` meaning the CTE on another. It resolved correctly, since a
# schema-qualified reference cannot be confused with a single-identifier CTE,
# but nothing about it was obvious to a reader debugging the output.
_KEY_PREFIX = "__ob_k"
_COL_PREFIX = "__ob_c"
_MAIN_CTE = "__ob_main"
_DEDUP_CTE = "__ob_dedup"
_DEDUP_SRC = "__ob_dedup_src"
_DEDUP_TOTAL_CTE = "__ob_dedup_total"


class GrainDedupUnsupportedError(FanoutError):
    """A measure needs grain dedup but the query combines it with something the
    rewrite cannot express.

    Raised instead of emitting an inflated result. Subclasses
    :class:`~orionbelt.compiler.fanout.FanoutError` because it reports the same
    class of problem — row multiplication that would corrupt an aggregate — so
    every caller that already handles fanout handles this too.
    """


def measure_source_objects(measure: Measure) -> set[str]:
    """Data objects whose columns a measure reads.

    Covers both declaration forms: the structured ``columns:`` list and
    ``{[Object].[Column]}`` references inside ``expression:``.
    """
    objects = {cref.view for cref in measure.columns if cref.view}
    if measure.expression:
        objects.update(obj for obj, _col in _MEASURE_COLUMN_REF.findall(measure.expression))
    return objects


def replicated_objects(resolved: ResolvedQuery) -> set[str]:
    """Objects whose rows the query's join path replicates.

    ``join_steps`` form a tree rooted at ``base_object``. Walking outward, an
    object's rows are replicated once the path reaching it crosses a *forward*
    many-to-one or many-to-many step: the many side sets the grain, so each row
    on the one side repeats per matching base row. Replication is inherited —
    anything joined beyond an already-replicated object is replicated too.

    Reversed steps are skipped: those are the classic fanout that
    :func:`~orionbelt.compiler.fanout.detect_fanout` already rejects, and
    claiming them here would report one problem twice.
    """
    replicated: set[str] = set()
    for step in resolved.join_steps:
        if step.reversed:
            continue
        multiplies = step.cardinality in (Cardinality.MANY_TO_ONE, Cardinality.MANY_TO_MANY)
        if multiplies or step.from_object in replicated:
            replicated.add(step.to_object)
    return replicated


def detect_dedup_measures(resolved: ResolvedQuery, model: SemanticModel) -> dict[str, str]:
    """Map each measure needing dedup to the replicated object it is sourced from.

    A measure qualifies when **every** column it reads comes from a single
    replicated object. A measure that mixes a replicated column with a
    base-grain one (``{[Sales].[Quantity]} * {[Products].[List Price]}``) is evaluated
    per base row and is already correct, so it is left alone.

    ``allowFanOut: true`` opts out — the modeller has declared the duplication
    intentional.
    """
    if not resolved.join_steps:
        return {}

    replicated = replicated_objects(resolved)
    if not replicated:
        return {}

    effective = model.effective_measures
    dedup: dict[str, str] = {}

    for resolved_measure in resolved.measures:
        # Metrics inline their components' aggregates into one expression;
        # dedup would have to split that across CTEs. Reject instead of
        # silently returning the inflated number.
        if resolved_measure.component_measures:
            for component in resolved_measure.component_measures:
                target = _dedup_target(component, effective, replicated)
                if target is not None:
                    msg = (
                        f"Metric '{resolved_measure.name}' references measure "
                        f"'{component}', which is sourced from '{target}' — an object "
                        f"whose rows this query's joins replicate. Deduplicating a "
                        f"metric component is not supported yet, and the metric would "
                        f"otherwise be computed from an inflated value. Query "
                        f"'{component}' on its own, or set allowFanOut: true on it if "
                        f"the duplication is intended."
                    )
                    raise GrainDedupUnsupportedError(msg)
            continue

        target = _dedup_target(resolved_measure.name, effective, replicated)
        if target is not None:
            dedup[resolved_measure.name] = target

    return dedup


def _dedup_target(
    measure_name: str,
    effective: dict[str, Measure],
    replicated: set[str],
) -> str | None:
    """The replicated object a measure must be deduplicated on, if any."""
    measure = effective.get(measure_name)
    if measure is None or measure.allow_fan_out:
        return None

    # ``distinct: true`` renders ``AGG(DISTINCT x)``. Replicating rows does not
    # change the *set* of distinct values, so any such measure already reads
    # correctly over a replicating join — most commonly a
    # ``count`` + ``distinct`` over the parent key, counted from the child fact.
    if measure.distinct:
        return None

    aggregation = measure.aggregation.lower()
    if aggregation in MULTIPLICITY_SAFE_AGGREGATIONS or aggregation == _DELEGATED_AGGREGATION:
        return None

    sources = measure_source_objects(measure)
    if not sources or not sources <= replicated:
        return None

    if len(sources) > 1:
        joined = ", ".join(f"'{s}'" for s in sorted(sources))
        msg = (
            f"Measure '{measure_name}' reads columns from {joined} — several objects "
            f"whose rows this query's joins replicate. OrionBelt cannot infer which "
            f"grain to deduplicate on, and the result would be overcounted. Split the "
            f"measure so it reads from one object, or set allowFanOut: true if the "
            f"duplication is intended."
        )
        raise GrainDedupUnsupportedError(msg)

    target = next(iter(sources))

    # Everything the aggregate reads beyond its own value columns has to be
    # projected into the inner SELECT so the rendered aggregate can reference it
    # — a ``filters:`` predicate becomes CASE WHEN inside the aggregate, a
    # ``withinGroup:`` column becomes its ORDER BY. Whatever is projected joins
    # the DISTINCT, so a reference to the dedup object itself is harmless (its
    # columns are functionally determined by the key being deduplicated on)
    # while a reference to any other object splits one source row into one row
    # per distinct value of it.
    for clause, referenced in _auxiliary_references(measure).items():
        outside = referenced - {target}
        if not outside:
            continue
        joined = ", ".join(f"'{s}'" for s in sorted(outside))
        msg = (
            f"Measure '{measure_name}' is sourced from '{target}', whose rows this "
            f"query's joins replicate, but its {clause} clause references {joined}. "
            f"Deduplicating on '{target}' would keep one row per distinct value of "
            f"that clause rather than one row per '{target}' row, so the result "
            f"would be overcounted. Reference '{target}' there instead, query the "
            f"measure at its own grain, or set allowFanOut: true if the duplication "
            f"is intended."
        )
        raise GrainDedupUnsupportedError(msg)

    return target


def _auxiliary_references(measure: Measure) -> dict[str, set[str]]:
    """Objects a measure reads outside its own value columns, keyed by clause.

    These are the references that get projected into the deduplicating inner
    SELECT alongside the value columns, and so end up inside its DISTINCT.
    """
    references: dict[str, set[str]] = {}

    filter_objects: set[str] = set()
    for item in measure.filters:
        collect_measure_filter_objects(item, filter_objects)
    if filter_objects:
        references["filters"] = filter_objects

    if measure.within_group is not None and measure.within_group.column.view:
        references["withinGroup"] = {measure.within_group.column.view}

    return references


def _identity_columns(
    object_name: str,
    resolved: ResolvedQuery,
    model: SemanticModel,
) -> list[str]:
    """Column names identifying one row of *object_name*.

    Prefers the object's declared ``primaryKey`` columns. Falls back to the
    ``columnsTo`` of the join that reaches it — for a many-to-one those are a
    unique key of the target by definition of the declared cardinality.
    """
    obj = model.data_objects.get(object_name)
    if obj is not None:
        pk = [name for name, col in obj.columns.items() if col.primary_key]
        if pk:
            return pk

    for step in resolved.join_steps:
        if not step.reversed and step.to_object == object_name:
            return list(step.to_columns)

    return []


def _map_column_refs(expr: Expr, fn: Callable[[ColumnRef], Expr]) -> Expr:
    """Rebuild *expr*, passing every ``ColumnRef`` through *fn*.

    Recurses through every composite node in the ``Expr`` union so a measure
    body of any shape (``CASE``, ``CAST``, arithmetic, nested calls, window
    frames) is visited completely. Both the rewrite and the collection pass are
    built on this, so the two can never disagree about where column references
    live.
    """
    match expr:
        case ColumnRef():
            return fn(expr)
        case AliasedExpr(expr=inner, alias=alias):
            return AliasedExpr(expr=_map_column_refs(inner, fn), alias=alias)
        case FunctionCall(name=name, args=args):
            return FunctionCall(
                name=name,
                args=[_map_column_refs(a, fn) for a in args],
                distinct=expr.distinct,
                order_by=[
                    OrderByItem(
                        expr=_map_column_refs(o.expr, fn),
                        desc=o.desc,
                        nulls_last=o.nulls_last,
                    )
                    for o in expr.order_by
                ],
                separator=expr.separator,
            )
        case BinaryOp(left=left, op=op, right=right):
            return BinaryOp(
                left=_map_column_refs(left, fn),
                op=op,
                right=_map_column_refs(right, fn),
            )
        case UnaryOp(op=op, operand=operand):
            return UnaryOp(op=op, operand=_map_column_refs(operand, fn))
        case IsNull(expr=inner, negated=negated):
            return IsNull(expr=_map_column_refs(inner, fn), negated=negated)
        case InList(expr=inner, values=values, negated=negated):
            return InList(
                expr=_map_column_refs(inner, fn),
                values=[_map_column_refs(v, fn) for v in values],
                negated=negated,
            )
        case CaseExpr(when_clauses=whens, else_clause=else_clause):
            return CaseExpr(
                when_clauses=[(_map_column_refs(w, fn), _map_column_refs(t, fn)) for w, t in whens],
                else_clause=(None if else_clause is None else _map_column_refs(else_clause, fn)),
            )
        case Cast(expr=inner, type_name=type_name):
            return Cast(expr=_map_column_refs(inner, fn), type_name=type_name)
        case Between(expr=inner, low=low, high=high, negated=negated):
            return Between(
                expr=_map_column_refs(inner, fn),
                low=_map_column_refs(low, fn),
                high=_map_column_refs(high, fn),
                negated=negated,
            )
        case RegexMatch(column=column, pattern=pattern, negated=negated):
            return RegexMatch(
                column=_map_column_refs(column, fn),
                pattern=pattern,
                negated=negated,
            )
        case RelativeDateRange(column=column):
            return RelativeDateRange(
                column=_map_column_refs(column, fn),
                unit=expr.unit,
                count=expr.count,
                direction=expr.direction,
                include_current=expr.include_current,
            )
        case WindowFunction(func_name=func_name, args=args):
            return WindowFunction(
                func_name=func_name,
                args=[_map_column_refs(a, fn) for a in args],
                partition_by=[_map_column_refs(p, fn) for p in expr.partition_by],
                order_by=[
                    OrderByItem(
                        expr=_map_column_refs(o.expr, fn),
                        desc=o.desc,
                        nulls_last=o.nulls_last,
                    )
                    for o in expr.order_by
                ],
                frame=expr.frame,
                distinct=expr.distinct,
            )
        case _:
            # Literal, Star, RawSQL, SubqueryExpr, Exists — no column refs to
            # rewrite at this level.
            return expr


def _rewrite_column_refs(expr: Expr, mapping: dict[tuple[str, str | None], ColumnRef]) -> Expr:
    """Rebuild *expr* with every mapped ``ColumnRef`` replaced."""
    return _map_column_refs(expr, lambda ref: mapping.get((ref.name, ref.table), ref))


def _collect_column_refs(expr: Expr, found: list[ColumnRef]) -> None:
    """Append every distinct ``ColumnRef`` in *expr* to *found*, in first-seen order."""
    seen = {(ref.name, ref.table) for ref in found}

    def visit(ref: ColumnRef) -> Expr:
        key = (ref.name, ref.table)
        if key not in seen:
            seen.add(key)
            found.append(ref)
        return ref

    _map_column_refs(expr, visit)


def _measure_alias(column: Expr) -> str | None:
    return column.alias if isinstance(column, AliasedExpr) else None


def _counts_rows(measure: Measure | None) -> bool:
    """Whether the measure counts rows, so an empty input must read 0 rather than NULL."""
    return measure is not None and measure.aggregation.lower() in ("count", "count_distinct")


def wrap_with_grain_dedup(
    ast: Select,
    resolved: ResolvedQuery,
    model: SemanticModel,
    dialect: Dialect,
) -> Select:
    """Wrap the planner AST so one-side measures aggregate over deduplicated rows.

    Returns *ast* unchanged when no measure needs dedup.
    """
    dedup_measures = resolved.dedup_measures
    if not dedup_measures:
        return ast

    dim_names = [d.name for d in resolved.dimensions]

    measure_columns: dict[str, Expr] = {}
    for col in ast.columns:
        alias = _measure_alias(col)
        if alias is not None and alias in dedup_measures:
            measure_columns[alias] = col

    # --- main CTE: the query as planned, minus the measures being deduplicated ---
    main_columns = [c for c in ast.columns if _measure_alias(c) not in dedup_measures]

    # With no dimensions and every measure deduplicated, ``main`` would project
    # nothing and degenerate to one row per base row — multiplying the single
    # scalar result. Drop it and let the dedup CTEs stand alone; each already
    # yields exactly one row at this grain.
    keep_main = bool(main_columns)

    all_ctes = list(ast.ctes)
    if keep_main:
        all_ctes.append(
            CTE(
                name=_MAIN_CTE,
                query=Select(
                    columns=main_columns,
                    from_=ast.from_,
                    joins=ast.joins,
                    where=ast.where,
                    group_by=ast.group_by,
                    having=ast.having,
                    grouping=ast.grouping,
                ),
            )
        )

    # --- one dedup CTE per (source object, grain) ---
    # A ``total: true`` measure is deduplicated at *no* grain: one row per source
    # object row across the whole query, not per group. Summing the per-group
    # values instead would double count anything belonging to several groups —
    # a product sold in two regions is legitimately in both — which is exactly
    # what a ``SUM(...) OVER ()`` over this pass's output would do.
    effective_measures = model.effective_measures
    totals = {
        name
        for name in dedup_measures
        if (m := effective_measures.get(name)) is not None and m.total
    }

    by_group: dict[tuple[str, bool], list[str]] = {}
    for measure_name, object_name in dedup_measures.items():
        by_group.setdefault((object_name, measure_name in totals), []).append(measure_name)

    cte_for_measure: dict[str, str] = {}
    grand_total_ctes: set[str] = set()
    for idx, group_key in enumerate(sorted(by_group)):
        object_name, is_total = group_key
        cte_name = f"{_DEDUP_TOTAL_CTE}_{idx}" if is_total else f"{_DEDUP_CTE}_{idx}"
        src_alias = f"{_DEDUP_SRC}_{idx}"
        measure_names = by_group[group_key]
        for measure_name in measure_names:
            cte_for_measure[measure_name] = cte_name
        if is_total:
            grand_total_ctes.add(cte_name)
        # A grand total collapses the grain entirely.
        group_dims = [] if is_total else resolved.dimensions
        group_dim_names = [] if is_total else dim_names
        # Inner projection: the query's grain, the source object's identity, and
        # every column the measures read. DISTINCT over those collapses the
        # replication to exactly one row per (grain, source-object row).
        inner_columns: list[Expr] = []
        for dim in group_dims:
            dim_expr: Expr = make_column_expr(model, dim.object_name, dim.column_name)
            if dim.grain:
                dim_expr = dialect.render_time_grain(dim_expr, dim.grain)
            inner_columns.append(AliasedExpr(expr=dim_expr, alias=dim.name))

        # Without an identity the DISTINCT would also collapse two genuinely
        # different rows that happen to share a value, turning an overcount into
        # an undercount. Refuse rather than guess.
        identity = _identity_columns(object_name, resolved, model)
        if not identity:
            listed = ", ".join(f"'{m}'" for m in sorted(measure_names))
            msg = (
                f"Measure(s) {listed} are sourced from '{object_name}', whose rows "
                f"this query's joins replicate, but '{object_name}' declares no "
                f"primaryKey and the join reaching it names no target columns. "
                f"Without a key OrionBelt cannot deduplicate the rows, and the "
                f"result would be overcounted. Declare primaryKey on "
                f"'{object_name}', or set allowFanOut: true if the duplication is "
                f"intended."
            )
            raise GrainDedupUnsupportedError(msg)

        for key_idx, key_column in enumerate(identity):
            inner_columns.append(
                AliasedExpr(
                    expr=make_column_expr(model, object_name, key_column),
                    alias=f"{_KEY_PREFIX}{key_idx}",
                )
            )

        refs: list[ColumnRef] = []
        for measure_name in measure_names:
            _collect_column_refs(measure_columns[measure_name], refs)

        ref_map: dict[tuple[str, str | None], ColumnRef] = {}
        for col_idx, ref in enumerate(refs):
            projected = f"{_COL_PREFIX}{col_idx}"
            inner_columns.append(AliasedExpr(expr=ref, alias=projected))
            ref_map[(ref.name, ref.table)] = ColumnRef(name=projected, table=src_alias)

        deduplicated = Select(
            columns=inner_columns,
            from_=ast.from_,
            joins=ast.joins,
            where=ast.where,
            distinct=True,
        )

        # Outer: re-aggregate the measures against the deduplicated rows. The
        # measure expressions keep their casts and shape; only their leaf column
        # refs are repointed at the subquery projection.
        cte_columns: list[Expr] = [
            AliasedExpr(expr=ColumnRef(name=name, table=src_alias), alias=name)
            for name in group_dim_names
        ]
        cte_columns.extend(
            _rewrite_column_refs(measure_columns[name], ref_map) for name in measure_names
        )

        # A base row with no match on the joined object still yields a row, with
        # the whole object NULL. It is not one of the object's rows, so drop it:
        # otherwise a grain-anchored COUNT would count a phantom. SUM and the
        # rest already ignore NULL inputs, so this changes nothing for them.
        key_present: Expr | None = None
        for key_idx in range(len(identity)):
            not_null = IsNull(
                expr=ColumnRef(name=f"{_KEY_PREFIX}{key_idx}", table=src_alias), negated=True
            )
            key_present = (
                not_null if key_present is None else BinaryOp(key_present, "AND", not_null)
            )

        all_ctes.append(
            CTE(
                name=cte_name,
                query=Select(
                    columns=cte_columns,
                    from_=From(source=deduplicated, alias=src_alias),
                    where=key_present,
                    group_by=[ColumnRef(name=name, table=src_alias) for name in group_dim_names],
                ),
            )
        )

    # --- outer SELECT: main plus each dedup CTE, joined on the query grain ---
    outer_columns: list[Expr] = []
    for col in ast.columns:
        alias = _measure_alias(col)
        if alias is None:
            outer_columns.append(col)
            continue
        if alias not in dedup_measures:
            outer_columns.append(
                AliasedExpr(expr=ColumnRef(name=alias, table=_MAIN_CTE), alias=alias)
            )
            continue
        source = cte_for_measure[alias]
        measure_col: Expr = ColumnRef(name=alias, table=source)
        # A group whose rows all miss the joined object contributes no row to the
        # dedup CTE, so the LEFT JOIN yields NULL. For a count that must read
        # zero, not "unknown" — COUNT over no rows is 0. Other aggregations keep
        # NULL, which is what SQL returns for an empty input.
        if _counts_rows(effective_measures.get(alias)):
            measure_col = FunctionCall(name="COALESCE", args=[measure_col, Literal.number(0)])
        outer_columns.append(AliasedExpr(expr=measure_col, alias=alias))

    # Without ``main`` the first dedup CTE becomes the FROM and the rest join to
    # it; every one yields a single row at this grain, so a CROSS JOIN is exact.
    ordered_ctes: list[str] = []
    for group_key in sorted(by_group):
        name = cte_for_measure[by_group[group_key][0]]
        if name not in ordered_ctes:
            ordered_ctes.append(name)
    outer_from = _MAIN_CTE if keep_main else ordered_ctes[0]
    joined_ctes = ordered_ctes if keep_main else ordered_ctes[1:]

    outer_joins: list[Join] = []
    for cte_name in joined_ctes:
        # A grand-total CTE is a single row with no grain to match on.
        if not dim_names or cte_name in grand_total_ctes:
            # Scalar grain: one row each side.
            outer_joins.append(Join(join_type=JoinType.CROSS, source=cte_name, alias=cte_name))
            continue
        on_expr: Expr | None = None
        for name in dim_names:
            # NULL grain values must match, or a dimension that is legitimately
            # NULL would drop its deduplicated measure.
            condition: Expr = _null_safe_eq(
                ColumnRef(name=name, table=outer_from),
                ColumnRef(name=name, table=cte_name),
            )
            on_expr = condition if on_expr is None else BinaryOp(on_expr, "AND", condition)
        outer_joins.append(
            Join(join_type=JoinType.LEFT, source=cte_name, alias=cte_name, on=on_expr)
        )

    # ORDER BY still holds planner-level expressions: a dimension's source
    # expression (a plain column, a time-grain call, or a whole arithmetic tree
    # for a computed column) or a measure's full aggregate. None of those survive
    # into the outer query, whose FROM is just the CTEs, so every one is matched
    # structurally against what the planner projected and repointed at its alias.
    projected_exprs: list[tuple[Expr, str]] = []
    for dim in resolved.dimensions:
        order_expr: Expr = make_column_expr(model, dim.object_name, dim.column_name)
        if dim.grain:
            order_expr = dialect.render_time_grain(order_expr, dim.grain)
        projected_exprs.append((order_expr, dim.name))
        # The star planner rewrites a time-grained dimension to its bare alias
        # before emitting ORDER BY, so match that shape too.
        if dim.grain:
            projected_exprs.append((ColumnRef(name=dim.name), dim.name))
    projected_exprs.extend((m.expression, m.name) for m in resolved.measures)

    outer_order_by = [
        OrderByItem(
            expr=_order_expr_to_outer(item.expr, projected_exprs, cte_for_measure),
            desc=item.desc,
            nulls_last=item.nulls_last,
        )
        for item in ast.order_by
    ]

    return Select(
        columns=outer_columns,
        from_=From(source=outer_from, alias=outer_from),
        joins=outer_joins,
        order_by=outer_order_by,
        limit=ast.limit,
        offset=ast.offset,
        ctes=all_ctes,
    )


def _null_safe_eq(left: Expr, right: Expr) -> Expr:
    """``left = right`` that also matches when both sides are NULL.

    Rendered as ``a = b OR (a IS NULL AND b IS NULL)`` rather than a dialect
    ``IS NOT DISTINCT FROM`` / ``<=>`` so it stays portable across all eight
    dialects with no codegen change.
    """
    return BinaryOp(
        left=BinaryOp(left, "=", right),
        op="OR",
        right=BinaryOp(IsNull(expr=left), "AND", IsNull(expr=right)),
    )


def _order_expr_to_outer(
    expr: Expr,
    projected: list[tuple[Expr, str]],
    cte_for_measure: dict[str, str],
) -> Expr:
    """Repoint one ORDER BY expression at the wrapper's CTE aliases.

    *projected* pairs each expression the planner emitted with the alias it was
    emitted under. Matching is structural (AST nodes are frozen dataclasses, so
    ``==`` compares by value), which covers a computed dimension's whole
    arithmetic tree as readily as a plain column reference.

    A deduplicated measure sorts against the CTE that computes it; everything
    else against ``main``, which carries the full grain.
    """

    def source_of(name: str) -> str:
        return cte_for_measure.get(name, _MAIN_CTE)

    for candidate, name in projected:
        if expr == candidate:
            return ColumnRef(name=name, table=source_of(name))

    # Resolution rejects an ORDER BY that is not in the SELECT, so anything left
    # is already an alias reference.
    if isinstance(expr, ColumnRef) and expr.table is None:
        return ColumnRef(name=expr.name, table=source_of(expr.name))

    return expr


def dedup_warning(dedup_measures: dict[str, str]) -> SemanticError:
    """Warn that deduplicated groups overlap, so the column will not sum to a total."""
    listed = ", ".join(f"'{m}'" for m in sorted(dedup_measures))
    return warning(
        code=WarningCode.FAN_TRAP_RISK,
        message=(
            f"Measure(s) {listed} are sourced from an object whose rows this query's "
            f"joins replicate. Each was aggregated over rows deduplicated on that "
            f"object's key, so per-group values are correct - but one row can belong "
            f"to several groups, so the values do not add up to that object's grand "
            f"total."
        ),
        hint=(
            "Query the measure at its own grain for a total that adds up, or set "
            "allowFanOut: true to aggregate the duplicated rows as-is."
        ),
        context={
            "measures": sorted(dedup_measures),
            "dedupOn": sorted(set(dedup_measures.values())),
        },
    )
