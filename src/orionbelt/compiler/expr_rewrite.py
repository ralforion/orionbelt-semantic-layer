"""Structure-preserving rewrites over the SQL expression AST.

Several passes need to walk an arbitrary ``Expr`` and rebuild it with some
leaves replaced: ``grain_dedup`` repoints a measure's column references at a
subquery projection, ``metric_expansion`` swaps a metric's placeholders for the
components they name. Doing that with a partial ``isinstance`` chain is how
references inside a ``CASE`` or a function's ``ORDER BY`` get silently left
behind, so the recursion lives here once, covering every composite node in the
``Expr`` union.
"""

from __future__ import annotations

from collections.abc import Callable

from orionbelt.ast.nodes import (
    AliasedExpr,
    Between,
    BinaryOp,
    CaseExpr,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    InList,
    IsNull,
    OrderByItem,
    RegexMatch,
    RelativeDateRange,
    UnaryOp,
    WindowFunction,
)


def map_column_refs(expr: Expr, fn: Callable[[ColumnRef], Expr]) -> Expr:
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
            return AliasedExpr(expr=map_column_refs(inner, fn), alias=alias)
        case FunctionCall(name=name, args=args):
            return FunctionCall(
                name=name,
                args=[map_column_refs(a, fn) for a in args],
                distinct=expr.distinct,
                order_by=[
                    OrderByItem(
                        expr=map_column_refs(o.expr, fn),
                        desc=o.desc,
                        nulls_last=o.nulls_last,
                    )
                    for o in expr.order_by
                ],
                separator=expr.separator,
            )
        case BinaryOp(left=left, op=op, right=right):
            return BinaryOp(
                left=map_column_refs(left, fn),
                op=op,
                right=map_column_refs(right, fn),
            )
        case UnaryOp(op=op, operand=operand):
            return UnaryOp(op=op, operand=map_column_refs(operand, fn))
        case IsNull(expr=inner, negated=negated):
            return IsNull(expr=map_column_refs(inner, fn), negated=negated)
        case InList(expr=inner, values=values, negated=negated):
            return InList(
                expr=map_column_refs(inner, fn),
                values=[map_column_refs(v, fn) for v in values],
                negated=negated,
            )
        case CaseExpr(when_clauses=whens, else_clause=else_clause):
            return CaseExpr(
                when_clauses=[(map_column_refs(w, fn), map_column_refs(t, fn)) for w, t in whens],
                else_clause=(None if else_clause is None else map_column_refs(else_clause, fn)),
            )
        case Cast(expr=inner, type_name=type_name):
            return Cast(expr=map_column_refs(inner, fn), type_name=type_name)
        case Between(expr=inner, low=low, high=high, negated=negated):
            return Between(
                expr=map_column_refs(inner, fn),
                low=map_column_refs(low, fn),
                high=map_column_refs(high, fn),
                negated=negated,
            )
        case RegexMatch(column=column, pattern=pattern, negated=negated):
            return RegexMatch(
                column=map_column_refs(column, fn),
                pattern=pattern,
                negated=negated,
            )
        case RelativeDateRange(column=column):
            return RelativeDateRange(
                column=map_column_refs(column, fn),
                unit=expr.unit,
                count=expr.count,
                direction=expr.direction,
                include_current=expr.include_current,
            )
        case WindowFunction(func_name=func_name, args=args):
            return WindowFunction(
                func_name=func_name,
                args=[map_column_refs(a, fn) for a in args],
                partition_by=[map_column_refs(p, fn) for p in expr.partition_by],
                order_by=[
                    OrderByItem(
                        expr=map_column_refs(o.expr, fn),
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


def rewrite_column_refs(expr: Expr, mapping: dict[tuple[str, str | None], ColumnRef]) -> Expr:
    """Rebuild *expr* with every mapped ``ColumnRef`` replaced."""
    return map_column_refs(expr, lambda ref: mapping.get((ref.name, ref.table), ref))


def collect_column_refs(expr: Expr, found: list[ColumnRef]) -> None:
    """Append every distinct ``ColumnRef`` in *expr* to *found*, in first-seen order."""
    seen = {(ref.name, ref.table) for ref in found}

    def visit(ref: ColumnRef) -> Expr:
        key = (ref.name, ref.table)
        if key not in seen:
            seen.add(key)
            found.append(ref)
        return ref

    map_column_refs(expr, visit)
