"""A fixed order for a query's AND-ed conditions, for its result-cache key.

Two queries that differ only in the order of their filters, ``Client = 'A' AND
Product = 'B'`` against ``Product = 'B' AND Client = 'A'``, return the same
rows but compile to different SQL. The result cache keys on the compiled SQL,
so the second missed the entry the first wrote: in the UI, clicking the same
two values as filters in the other order ran the query on the database again.

:func:`key_sql` renders the query a second time with the top-level AND
conditions of every WHERE and HAVING sorted by their rendered text, through
the same dialect as the SQL that runs, and the cache keys on that. Only
conjunctions are reordered. AND is commutative in SQL, where the engine
chooses the evaluation order anyway, so two queries share a key only when they
hold the same set of conditions. OR, join conditions and everything else keep
their order, and the SQL that runs is unchanged.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from typing import Any

from orionbelt.ast.nodes import BinaryOp, Expr, Select
from orionbelt.dialect.base import Dialect


def _conjuncts(expr: Expr) -> list[Expr]:
    """The operands of a chain of ANDs, however it is nested; else ``[expr]``."""
    if isinstance(expr, BinaryOp) and expr.op.upper() == "AND":
        return _conjuncts(expr.left) + _conjuncts(expr.right)
    return [expr]


def _sorted_conjunction(expr: Expr | None, dialect: Dialect) -> Expr | None:
    if expr is None:
        return None
    parts = _conjuncts(expr)
    if len(parts) < 2:
        return expr
    parts.sort(key=dialect.compile_expr)
    result = parts[0]
    for part in parts[1:]:
        result = BinaryOp(left=result, op="AND", right=part)
    return result


def _canonical(value: Any, dialect: Dialect) -> Any:
    """*value* with the conditions of every SELECT inside it in a fixed order.

    Walks every field of every node rather than a hand-kept list of them, so no
    part of the query (a DISTINCT, a GROUPING SETS, a window frame) can be left
    out of the key: dropping one would give two different queries one entry.
    """
    if isinstance(value, list):
        return [_canonical(item, dialect) for item in value]
    if isinstance(value, tuple):
        return tuple(_canonical(item, dialect) for item in value)
    if not is_dataclass(value) or isinstance(value, type):
        return value
    node = replace(
        value,
        **{f.name: _canonical(getattr(value, f.name), dialect) for f in fields(value) if f.init},
    )
    if isinstance(node, Select):
        node = replace(
            node,
            where=_sorted_conjunction(node.where, dialect),
            having=_sorted_conjunction(node.having, dialect),
        )
    return node


def key_sql(ast: Select, dialect: Dialect) -> str:
    """The SQL of *ast* with its AND-ed conditions in a fixed order.

    Inner conditions are ordered first (a subquery inside a condition), so the
    text a condition sorts by is already canonical.
    """
    canonical: Select = _canonical(ast, dialect)
    return dialect.compile(canonical)
