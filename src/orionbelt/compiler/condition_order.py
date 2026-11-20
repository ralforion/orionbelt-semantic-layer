"""A fixed order for a query's AND-ed conditions, for its result-cache key.

Two queries that differ only in the order of their filters, ``Client = 'A' AND
Product = 'B'`` against ``Product = 'B' AND Client = 'A'``, return the same
rows but compile to different SQL. The result cache keys on the compiled SQL,
so the second missed the entry the first wrote: in the UI, clicking the same
two values as filters in the other order ran the query on the database again.

:func:`key_sql` renders the compiled query a second time with the operands of
every chain of ANDs sorted by their rendered text, through the same dialect,
and the cache keys on that. AND is commutative wherever it stands in SQL, and
nothing else moves, so two queries share a key only when the SQL that runs
differs in the order of AND-ed conditions alone: the same joins, the same
rows. The SQL that runs is unchanged.

It reorders the compiled query, not the query object: the order of the
filters can pick the join path a dimension is reached by, so the same filters
in another order may well be another query. The conditions the compiler
writes as text, in the CTEs of a period-over-period metric, it writes in this
order already (:func:`sorted_conjuncts`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import fields, is_dataclass, replace
from typing import Any, TypeGuard

from orionbelt.ast.nodes import BinaryOp, Expr, Select
from orionbelt.dialect.base import Dialect


def _is_and(value: object) -> TypeGuard[BinaryOp]:
    return isinstance(value, BinaryOp) and value.op.upper() == "AND"


def _conjuncts(expr: Expr) -> list[Expr]:
    """The operands of a chain of ANDs, however it is nested; else ``[expr]``."""
    parts: list[Expr] = []
    pending = [expr]
    while pending:
        node = pending.pop()
        if _is_and(node):
            pending.extend((node.right, node.left))
        else:
            parts.append(node)
    return parts


def _balanced_and(parts: Sequence[Expr]) -> Expr:
    """*parts* joined by AND, as a tree as deep as the log of their count.

    A chain is as deep as the count: four filter groups of 128 conditions each,
    flattened into one chain of 512, are past what the renderer recurses into.
    """
    if len(parts) == 1:
        return parts[0]
    middle = len(parts) // 2
    return BinaryOp(
        left=_balanced_and(parts[:middle]), op="AND", right=_balanced_and(parts[middle:])
    )


def sorted_conjuncts(conditions: Iterable[Expr], dialect: Dialect) -> list[Expr]:
    """The conditions AND-ed in *conditions*, each in its fixed order, sorted."""
    parts = [
        _canonical(part, dialect) for condition in conditions for part in _conjuncts(condition)
    ]
    return sorted(parts, key=dialect.compile_expr)


def _canonical(value: Any, dialect: Dialect) -> Any:
    """*value* with every chain of ANDs inside it in a fixed order.

    Walks every field of every node rather than a hand-kept list of them, so no
    part of the query (a DISTINCT, a GROUPING SETS, a window frame) can be left
    out of the key: dropping one would give two different queries one entry.
    """
    if isinstance(value, list):
        return [_canonical(item, dialect) for item in value]
    if isinstance(value, tuple):
        return tuple(_canonical(item, dialect) for item in value)
    if _is_and(value):
        return _balanced_and(sorted_conjuncts([value], dialect))
    if not is_dataclass(value) or isinstance(value, type):
        return value
    return replace(
        value,
        **{f.name: _canonical(getattr(value, f.name), dialect) for f in fields(value) if f.init},
    )


def key_sql(ast: Select, dialect: Dialect) -> str:
    """The SQL of *ast* with its AND-ed conditions in a fixed order.

    Inner conditions are ordered first (a subquery inside a condition), so the
    text a condition sorts by is already canonical.
    """
    canonical: Select = _canonical(ast, dialect)
    return dialect.compile(canonical)
