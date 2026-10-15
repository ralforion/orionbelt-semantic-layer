"""Can a WHERE predicate hold for a row whose columns from some tables are all NULL?

A query whose measures sit on the *one* side of a join (customers) but whose
WHERE reads the *many* side (orders) is rooted at the many side, with the
one-side measures deduplicated. Every customer it counts is then a customer
with at least one order the predicate accepts. That is the right answer for a
predicate no customer without orders could satisfy (``status = 'ok'``), and the
wrong one for a predicate such a customer could: ``tier = 'gold' OR status =
'ok'``, ``order_id IS NULL``. Those customers have no order row to carry them,
so they are silently left out.

:func:`can_hold_without` tells the two apart. It evaluates the predicate in
SQL's three-valued logic with every column of the given tables NULL (the row a
LEFT JOIN would give a customer without orders) and every other column
unknown, and answers whether TRUE is a possible outcome. An expression it does
not model counts as possibly TRUE, so the answer errs towards refusing.
"""

from __future__ import annotations

from orionbelt.ast.nodes import (
    Between,
    BinaryOp,
    Cast,
    ColumnRef,
    Exists,
    Expr,
    FunctionCall,
    InList,
    IsNull,
    LikeMatch,
    Literal,
    RegexMatch,
    RelativeDateRange,
    Select,
    UnaryOp,
)
from orionbelt.compiler.expr_rewrite import collect_column_refs

_T, _F, _N = "T", "F", "N"
_ANY_TRUTH = frozenset({_T, _F, _N})

# Functions that return NULL whenever an argument is NULL do not need listing;
# these are the ones that can turn a NULL argument into a value.
_NULL_ABSORBING = frozenset({"COALESCE", "IFNULL", "NVL", "NVL2", "ISNULL", "IF", "IIF"})

_COMPARISONS = frozenset({"=", "<>", "!=", "<", ">", "<=", ">=", "LIKE", "ILIKE"})


def can_hold_without(expr: Expr, tables: set[str]) -> bool:
    """Whether *expr* can be TRUE when every column of *tables* is NULL."""
    return _T in _truth(expr, tables)


def _is_null(expr: Expr, tables: set[str]) -> bool:
    """Whether *expr* is certainly NULL with *tables*' columns NULL."""
    match expr:
        case ColumnRef(table=table):
            return table is not None and table in tables
        case Literal(value=value):
            return value is None
        case Cast(expr=inner):
            return _is_null(inner, tables)
        case BinaryOp(left=left, op=op, right=right) if op.upper() not in ("AND", "OR"):
            return _is_null(left, tables) or _is_null(right, tables)
        case FunctionCall(name=name, args=args) if name.upper() not in _NULL_ABSORBING:
            return any(_is_null(arg, tables) for arg in args)
        case _:
            return False


def _truth(expr: Expr, tables: set[str]) -> frozenset[str]:
    """The truth values *expr* can take."""
    match expr:
        case BinaryOp(left=left, op=op, right=right) if op.upper() == "AND":
            return frozenset(
                _and(a, b) for a in _truth(left, tables) for b in _truth(right, tables)
            )
        case BinaryOp(left=left, op=op, right=right) if op.upper() == "OR":
            return frozenset(_or(a, b) for a in _truth(left, tables) for b in _truth(right, tables))
        case UnaryOp(op=op, operand=operand) if op.upper() == "NOT":
            return frozenset(_not(v) for v in _truth(operand, tables))
        case BinaryOp(left=left, op=op, right=right) if op.upper() in _COMPARISONS:
            return _null_or_any(_is_null(left, tables) or _is_null(right, tables))
        case IsNull(expr=inner, negated=negated):
            if _is_null(inner, tables):
                return frozenset({_F if negated else _T})
            return _ANY_TRUTH
        case InList(expr=inner) | Between(expr=inner):
            return _null_or_any(_is_null(inner, tables))
        case LikeMatch(column=inner) | RegexMatch(column=inner) | RelativeDateRange(column=inner):
            return _null_or_any(_is_null(inner, tables))
        case Exists(subquery=subquery, negated=negated):
            # Correlated on a NULL key, the subquery finds no row.
            if _correlated_tables(subquery) & tables:
                return frozenset({_T if negated else _F})
            return _ANY_TRUTH
        case FunctionCall():
            return _null_or_any(_is_null(expr, tables))
        case _:
            # CASE and anything else: a value is possible.
            return _ANY_TRUTH


def _correlated_tables(select: Select) -> set[str]:
    """The outer tables an ``EXISTS`` body's WHERE names: the ones its own
    FROM and JOINs do not bind."""
    bound = {select.from_.alias} if select.from_ and select.from_.alias else set()
    bound.update(join.alias for join in select.joins if join.alias)
    refs: list[ColumnRef] = []
    if select.where is not None:
        collect_column_refs(select.where, refs)
    return {ref.table for ref in refs if ref.table is not None and ref.table not in bound}


def _null_or_any(is_null: bool) -> frozenset[str]:
    return frozenset({_N}) if is_null else _ANY_TRUTH


def _and(a: str, b: str) -> str:
    if _F in (a, b):
        return _F
    if _N in (a, b):
        return _N
    return _T


def _or(a: str, b: str) -> str:
    if _T in (a, b):
        return _T
    if _N in (a, b):
        return _N
    return _F


def _not(v: str) -> str:
    return {_T: _F, _F: _T, _N: _N}[v]
