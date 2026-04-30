"""Fluent builder API for constructing SQL AST nodes."""

from __future__ import annotations

from typing import Self

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    Except,
    Expr,
    From,
    FunctionCall,
    Join,
    JoinType,
    Literal,
    OrderByItem,
    RawSQL,
    Select,
    UnionAll,
)


class QueryBuilder:
    """Fluent builder for ergonomic AST construction."""

    def __init__(self) -> None:
        self._columns: list[Expr] = []
        self._from: From | None = None
        self._joins: list[Join] = []
        self._where: Expr | None = None
        self._group_by: list[Expr] = []
        self._having: Expr | None = None
        self._order_by: list[OrderByItem] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._ctes: list[CTE] = []
        self._distinct: bool = False

    def select(self, *columns: Expr) -> Self:
        self._columns.extend(columns)
        return self

    def select_aliased(self, expr: Expr, alias: str) -> Self:
        self._columns.append(AliasedExpr(expr=expr, alias=alias))
        return self

    def from_(self, table: str, alias: str | None = None) -> Self:
        self._from = From(source=table, alias=alias)
        return self

    def from_subquery(self, subquery: Select, alias: str) -> Self:
        self._from = From(source=subquery, alias=alias)
        return self

    def join(
        self,
        table: str,
        on: Expr,
        join_type: JoinType = JoinType.LEFT,
        alias: str | None = None,
    ) -> Self:
        self._joins.append(Join(join_type=join_type, source=table, alias=alias, on=on))
        return self

    def where(self, condition: Expr) -> Self:
        if self._where is None:
            self._where = condition
        else:
            self._where = BinaryOp(left=self._where, op="AND", right=condition)
        return self

    def group_by(self, *exprs: Expr) -> Self:
        self._group_by.extend(exprs)
        return self

    def having(self, condition: Expr) -> Self:
        if self._having is None:
            self._having = condition
        else:
            self._having = BinaryOp(left=self._having, op="AND", right=condition)
        return self

    def order_by(self, expr: Expr, desc: bool = False) -> Self:
        self._order_by.append(OrderByItem(expr=expr, desc=desc))
        return self

    def limit(self, n: int) -> Self:
        self._limit = n
        return self

    def offset(self, n: int) -> Self:
        self._offset = n
        return self

    def with_cte(self, name: str, query: Select | UnionAll | Except | RawSQL) -> Self:
        self._ctes.append(CTE(name=name, query=query))
        return self

    def distinct(self, value: bool = True) -> Self:
        self._distinct = value
        return self

    def build(self) -> Select:
        return Select(
            columns=self._columns,
            from_=self._from,
            joins=self._joins,
            where=self._where,
            group_by=self._group_by,
            having=self._having,
            order_by=self._order_by,
            limit=self._limit,
            offset=self._offset,
            ctes=self._ctes,
            distinct=self._distinct,
        )


# Convenience constructors for common expressions.


def col(name: str, table: str | None = None) -> ColumnRef:
    """Create a column reference."""
    return ColumnRef(name=name, table=table)


def func(name: str, *args: Expr, distinct: bool = False) -> FunctionCall:
    """Create a function call."""
    return FunctionCall(name=name, args=list(args), distinct=distinct)


def lit(value: str | int | float | bool | None) -> Literal:
    """Create a literal value."""
    return Literal(value=value)


def alias(expr: Expr, name: str) -> AliasedExpr:
    """Create an aliased expression."""
    return AliasedExpr(expr=expr, alias=name)


def eq(left: Expr, right: Expr) -> BinaryOp:
    """Create an equality comparison."""
    return BinaryOp(left=left, op="=", right=right)


def and_(*conditions: Expr) -> Expr:
    """Chain conditions with AND."""
    result: Expr | None = None
    for cond in conditions:
        result = cond if result is None else BinaryOp(left=result, op="AND", right=cond)
    if result is None:
        return Literal(value=True)
    return result


def or_(*conditions: Expr) -> Expr:
    """Chain conditions with OR."""
    result: Expr | None = None
    for cond in conditions:
        result = cond if result is None else BinaryOp(left=result, op="OR", right=cond)
    if result is None:
        return Literal(value=True)
    return result
