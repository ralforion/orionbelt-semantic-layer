"""Does the outermost query only name tables its own FROM provides?

The defect this catches produces *runnable-looking* SQL: an expression built
against the planner's FROM, left behind in a query that wraps it, where the
table it names is out of scope. Nothing upstream notices - the model is valid,
the AST is well-formed, and sqlglot parses the statement happily, because
syntactically it is fine. The database is the first thing to object, and it
objects by naming a data object from the model, which reads as a join problem
rather than a projection-scope one (#358).

So this asks the one question that separates the two: every table qualifier the
outer query names, against the sources that query actually has. It reads the
outermost ``Select`` only. Its CTEs and subqueries carry their own FROM and
answer for themselves.
"""

from __future__ import annotations

from orionbelt.ast.nodes import Expr, Join, Select, Unnest
from orionbelt.compiler.expr_rewrite import collect_referenced_tables


def out_of_scope_tables(select: Select) -> set[str]:
    """Table qualifiers *select* names that its own FROM, joins and CTEs do not.

    Empty for every query the compiler is supposed to emit. A non-empty result
    is a compiler defect rather than a modelling one: no query can put a table
    into an expression here that the user could take out again.
    """
    referenced: set[str] = set()
    for expr in _outer_expressions(select):
        collect_referenced_tables(expr, referenced)
    return referenced - _in_scope(select)


def _in_scope(select: Select) -> set[str]:
    """The names this query's own FROM binds, and only those.

    One name per source: its alias if it has one, else the source itself. An
    alias *replaces* the name, it does not add to it - ``FROM base AS b`` binds
    ``b``, and every engine rejects ``base.x`` after it. A subquery source has
    no name of its own to fall back on, so it is in scope only under its alias.

    Declaring a CTE does not put it in scope either. ``WITH other AS (...)``
    that this query never selects from is a table it cannot name, which is the
    same defect as naming the table of an enclosing CTE - the case this module
    exists for. An unnest is the one source that binds two names: the element
    alias, and the parent it is correlated to.
    """
    scope: set[str] = set()
    sources: list[tuple[str | Select, str | None]] = []
    if select.from_ is not None:
        sources.append((select.from_.source, select.from_.alias))
    for joined in select.joins:
        if isinstance(joined, Unnest):
            scope.update({joined.alias, joined.parent_alias})
        else:
            sources.append((joined.source, joined.alias))
    for source, alias in sources:
        if alias:
            scope.add(alias)
        elif isinstance(source, str):
            scope.add(source)
    return scope


def _outer_expressions(select: Select) -> list[Expr]:
    """The expressions that bind in the outer query's own scope.

    A join's ON is here too: it names the joined source, which is in scope by
    definition, and anything else it names has to be.
    """
    exprs: list[Expr] = [*select.columns, *select.group_by]
    exprs.extend(item.expr for item in select.order_by)
    exprs.extend(j.on for j in select.joins if isinstance(j, Join) and j.on is not None)
    if select.where is not None:
        exprs.append(select.where)
    if select.having is not None:
        exprs.append(select.having)
    return exprs


__all__ = ["out_of_scope_tables"]
