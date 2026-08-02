"""Conform an independent fact to a measure's anchor grain.

A measure whose expression reads two facts no join path reaches together has no
value until something says *which fact's rows it runs over*. ``anchor:`` says
that. This module turns the declaration into a plan.

``SUM({[Sales].[Qty]})`` cannot simply be joined onto ``Returns``: the two are
independent, so joining them on their shared calendar key produces every
(sale, return) pair for a date, and the expression would be evaluated over pairs
that do not exist in either fact. Instead each foreign fact is aggregated to the
key it shares with the anchor *first*, which makes it one row per key, and that
is joined many-to-one:

    SELECT ..., AVG("Returns"."qty" / "__ob_conf_0"."__ob_av0") AS "Return Rate"
    FROM "returns" AS "Returns"
    LEFT JOIN (
      SELECT "Sales"."datekey" AS "__ob_ak0", SUM("Sales"."qty") AS "__ob_av0"
      FROM "sales" AS "Sales" GROUP BY "Sales"."datekey"
    ) AS "__ob_conf_0" ON "Returns"."datekey" = "__ob_conf_0"."__ob_ak0"
    LEFT JOIN "calendar" AS "Calendar" ON ...

One row per key on the joined side means no fanout, so the anchor keeps its own
grain and the expression is evaluated exactly once per anchor row. That is what
makes the result well defined, and it is why the anchor cannot be inferred: the
same expression anchored on the shared key instead of on ``Returns`` averages
over a different population and returns a different number.

The foreign column is conformed with ``SUM``. It is the aggregate that makes the
conformed value independent of how many rows the foreign fact happens to have
per key, which is what "the sales quantity for that date" means; any other
choice would make the measure's value depend on the foreign fact's row count in
a way the expression never asked about.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import (
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Expr,
    FunctionCall,
    JoinType,
    Select,
)
from orionbelt.compiler.expr_rewrite import collect_column_refs, map_column_refs
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.graph import path_overrides
from orionbelt.compiler.resolution import (
    ResolvedQuery,
    anchored_conformed_objects,
    make_column_expr,
)
from orionbelt.models.semantic import DataObject, SemanticModel

_CONFORM_ALIAS = "__ob_conf_"
_KEY_ALIAS = "__ob_ak"
_VALUE_ALIAS = "__ob_av"


class AnchorNotConformableError(FanoutError):
    """An anchored measure's foreign fact shares no key with its anchor.

    Conforming needs a column both facts carry, so the aggregated foreign side
    can be joined back on many-to-one. Without one there is nothing to group by
    and nothing to join on, and the measure has no more meaning than it had
    before the anchor was declared.

    Subclasses :class:`~orionbelt.compiler.fanout.FanoutError` so the callers
    that already surface fanout as a 422 surface this the same way.
    """

    def __init__(self, measure_name: str, anchor: str, foreign: str) -> None:
        super().__init__(
            f"Measure '{measure_name}' is anchored on '{anchor}' and reads "
            f"'{foreign}', but the two share no join key: no data object is "
            f"joined directly from both on the same columns. Conforming "
            f"'{foreign}' to '{anchor}' needs one, since the aggregated result "
            f"has to be joined back onto '{anchor}'. Join both to a common data "
            f"object on the same columns, or anchor the measure on a data "
            f"object that reaches '{foreign}'."
        )


@dataclass(frozen=True)
class ConformedFact:
    """One foreign fact, aggregated to the shared key and ready to join in."""

    alias: str
    select: Select
    on: Expr
    """Join condition against the anchor's own copy of the shared key."""

    column_aliases: dict[str, str]
    """Physical column of the foreign fact, mapped to its subquery alias."""

    object_name: str
    measure_name: str
    """The anchored measure this conformed fact was built for.

    The star planner joins every conformed fact into one FROM, but a CFL leg
    joins only the ones belonging to the measures it owns, so the association
    has to survive planning."""


def _shared_key(
    model: SemanticModel,
    anchor: str,
    foreign: str,
    overrides: dict[tuple[str, str], str],
) -> tuple[list[str], list[str]] | None:
    """Columns of (anchor, foreign) that address the same rows.

    Two shapes qualify, matching the two things an anchor can be.

    The anchor is the object *foreign* joins to (the default, shared-key case):
    the key is that join's own two sides, ``columns_to`` on the anchor and
    ``columns_from`` on the foreign fact.

    The anchor is a peer fact (a declared ``anchor:``): both must join directly
    to one object on the same target columns. That is what makes their two key
    columns comparable, since each holds values drawn from the same column of
    the same table.

    Returns their respective columns, or ``None`` when neither shape holds.
    """
    for foreign_join in model.effective_joins(foreign, overrides):
        if foreign_join.join_to == anchor:
            return foreign_join.columns_to, foreign_join.columns_from
    for anchor_join in model.effective_joins(anchor, overrides):
        for foreign_join in model.effective_joins(foreign, overrides):
            if (
                anchor_join.join_to == foreign_join.join_to
                and anchor_join.columns_to == foreign_join.columns_to
            ):
                return anchor_join.columns_from, foreign_join.columns_from
    return None


def _conform_one(
    model: SemanticModel,
    measure_name: str,
    anchor: str,
    foreign: str,
    expression: Expr,
    alias: str,
    qualify: Callable[[DataObject], str] | None,
    overrides: dict[tuple[str, str], str],
) -> ConformedFact:
    """Aggregate *foreign* to the key it shares with *anchor*."""
    shared = _shared_key(model, anchor, foreign, overrides)
    if shared is None:
        raise AnchorNotConformableError(measure_name, anchor, foreign)
    anchor_columns, foreign_columns = shared

    foreign_obj = model.data_objects.get(foreign)
    if foreign_obj is None:  # pragma: no cover - resolution rejects this earlier
        raise AnchorNotConformableError(measure_name, anchor, foreign)

    # Every column of the foreign fact the expression reads, in a stable order
    # so the generated aliases do not depend on set iteration.
    refs: list[ColumnRef] = []
    collect_column_refs(expression, refs)
    read_columns = sorted({ref.name for ref in refs if ref.table == foreign})

    builder = QueryBuilder()
    key_aliases: list[str] = []
    for i, label in enumerate(foreign_columns):
        key_alias = f"{_KEY_ALIAS}{i}"
        key_aliases.append(key_alias)
        key_expr = make_column_expr(model, foreign, label)
        builder.select(AliasedExpr(expr=key_expr, alias=key_alias))
        builder.group_by(key_expr)

    column_aliases: dict[str, str] = {}
    for i, column in enumerate(read_columns):
        value_alias = f"{_VALUE_ALIAS}{i}"
        column_aliases[column] = value_alias
        builder.select(
            AliasedExpr(
                expr=FunctionCall(name="SUM", args=[ColumnRef(name=column, table=foreign)]),
                alias=value_alias,
            )
        )

    qualified = qualify(foreign_obj) if qualify else foreign_obj.qualified_code
    builder.from_(qualified, alias=foreign)

    on: Expr | None = None
    for label, key_alias in zip(anchor_columns, key_aliases, strict=True):
        predicate: Expr = BinaryOp(
            left=make_column_expr(model, anchor, label),
            op="=",
            right=ColumnRef(name=key_alias, table=alias),
        )
        on = predicate if on is None else BinaryOp(left=on, op="AND", right=predicate)
    assert on is not None  # a join always declares at least one column pair

    return ConformedFact(
        alias=alias,
        select=builder.build(),
        on=on,
        column_aliases=column_aliases,
        object_name=foreign,
        measure_name=measure_name,
    )


def plan_conformed_facts(
    resolved: ResolvedQuery,
    model: SemanticModel,
    qualify: Callable[[DataObject], str] | None = None,
) -> tuple[list[ConformedFact], dict[str, Expr]]:
    """Conform every anchored measure's foreign facts.

    Returns the subqueries to join in, and the measure expressions rewritten to
    read the conformed columns instead of the foreign fact's own. The rewrite is
    what keeps the anchor honest: leave it out and the expression still names a
    table the star plan never joined.
    """
    conformed: list[ConformedFact] = []
    rewritten: dict[str, Expr] = {}
    if not resolved.anchored_measures:
        return conformed, rewritten

    # The conform key has to come from the join the query is actually using: a
    # secondary path selected by usePathNames replaces its pair's primary, and
    # grouping by the primary's column instead answers at a different date.
    overrides = path_overrides(resolved.use_path_names)

    # Metric components count. A metric is planned by inlining its components'
    # expressions, so a metric over an anchored measure reaches the projection
    # through ``metric_components`` and never through ``measures``. Walking only
    # the latter left ``{[Cross]} + 1`` projecting the raw
    # ``SUM("Sales"."qty" * "Returns"."qty")`` with no conformed subqueries
    # under it, over a FROM that has neither fact.
    seen: set[str] = set()
    for resolved_measure in (*resolved.measures, *resolved.metric_components.values()):
        if resolved_measure.name in seen:
            continue
        seen.add(resolved_measure.name)
        anchor = resolved.anchored_measures.get(resolved_measure.name)
        if not anchor:
            continue
        model_measure = model.effective_measures.get(resolved_measure.name)
        if model_measure is None:  # pragma: no cover - resolution guarantees this
            continue
        foreign_objects = anchored_conformed_objects(model, model_measure, resolved.use_path_names)
        expression = resolved_measure.expression
        for foreign in sorted(foreign_objects):
            fact = _conform_one(
                model,
                resolved_measure.name,
                anchor,
                foreign,
                expression,
                f"{_CONFORM_ALIAS}{len(conformed)}",
                qualify,
                overrides,
            )
            conformed.append(fact)
            expression = _point_at_conformed(expression, fact)
        rewritten[resolved_measure.name] = expression

    return conformed, rewritten


def _point_at_conformed(expression: Expr, fact: ConformedFact) -> Expr:
    """Repoint the foreign fact's column references at its conformed subquery."""

    def rewrite(ref: ColumnRef) -> ColumnRef:
        if ref.table != fact.object_name:
            return ref
        conformed_name = fact.column_aliases.get(ref.name)
        if conformed_name is None:  # pragma: no cover - every read column is conformed
            return ref
        return ColumnRef(name=conformed_name, table=fact.alias)

    return map_column_refs(expression, rewrite)


def conformed_join_type() -> JoinType:
    """Conformed facts join LEFT so an anchor row survives a key with no match.

    An inner join would drop the anchor row entirely, silently shrinking the
    population the aggregate runs over. LEFT keeps the row and lets the
    expression see NULL, which is the dialect's own arithmetic and matches what
    the same expression does on a single-fact plan.
    """
    return JoinType.LEFT
