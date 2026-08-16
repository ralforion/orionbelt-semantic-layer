"""Visitor pattern for AST traversal and transformation."""

from __future__ import annotations

from typing import Any

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
    InTimeZone,
    IsNull,
    Join,
    Literal,
    OrderByItem,
    RawSQL,
    Select,
    Star,
    SubqueryExpr,
    UnaryOp,
    WindowFunction,
)


class ASTVisitor:
    """Base visitor for SQL AST traversal.

    Override specific visit_* methods to customize behavior.
    The default implementations recursively visit child nodes.
    """

    def visit(self, node: Any) -> Any:
        """Dispatch to the appropriate visit_* method."""
        method_name = f"visit_{type(node).__name__.lower()}"
        method = getattr(self, method_name, self.generic_visit)
        return method(node)

    def generic_visit(self, node: Any) -> Any:
        return node

    def visit_select(self, node: Select) -> Any:
        ctes = [self.visit(cte) for cte in node.ctes]
        columns = [self.visit(col) for col in node.columns]
        from_ = self.visit(node.from_) if node.from_ else None
        joins = [self.visit(j) for j in node.joins]
        where = self.visit(node.where) if node.where else None
        group_by = [self.visit(g) for g in node.group_by]
        having = self.visit(node.having) if node.having else None
        order_by = [self.visit(o) for o in node.order_by]
        return Select(
            columns=columns,
            from_=from_,
            joins=joins,
            where=where,
            group_by=group_by,
            having=having,
            order_by=order_by,
            limit=node.limit,
            offset=node.offset,
            ctes=ctes,
        )

    def visit_from(self, node: From) -> Any:
        if isinstance(node.source, Select):
            return From(source=self.visit(node.source), alias=node.alias)
        return node

    def visit_join(self, node: Join) -> Any:
        source = self.visit(node.source) if isinstance(node.source, Select) else node.source
        on = self.visit(node.on) if node.on else None
        return Join(join_type=node.join_type, source=source, alias=node.alias, on=on)

    def visit_cte(self, node: CTE) -> Any:
        return CTE(name=node.name, query=self.visit(node.query))

    def visit_orderbyitem(self, node: OrderByItem) -> Any:
        return OrderByItem(expr=self.visit(node.expr), desc=node.desc, nulls_last=node.nulls_last)

    def visit_literal(self, node: Literal) -> Any:
        return node

    def visit_star(self, node: Star) -> Any:
        return node

    def visit_columnref(self, node: ColumnRef) -> Any:
        return node

    def visit_aliasedexpr(self, node: AliasedExpr) -> Any:
        return AliasedExpr(expr=self.visit(node.expr), alias=node.alias)

    def visit_functioncall(self, node: FunctionCall) -> Any:
        args = [self.visit(a) for a in node.args]
        return FunctionCall(name=node.name, args=args, distinct=node.distinct)

    def visit_intimezone(self, node: InTimeZone) -> Any:
        return InTimeZone(expr=self.visit(node.expr), zone=node.zone, from_zone=node.from_zone)

    def visit_binaryop(self, node: BinaryOp) -> Any:
        return BinaryOp(left=self.visit(node.left), op=node.op, right=self.visit(node.right))

    def visit_unaryop(self, node: UnaryOp) -> Any:
        return UnaryOp(op=node.op, operand=self.visit(node.operand))

    def visit_isnull(self, node: IsNull) -> Any:
        return IsNull(expr=self.visit(node.expr), negated=node.negated)

    def visit_inlist(self, node: InList) -> Any:
        return InList(
            expr=self.visit(node.expr),
            values=[self.visit(v) for v in node.values],
            negated=node.negated,
        )

    def visit_caseexpr(self, node: CaseExpr) -> Any:
        whens = [(self.visit(w), self.visit(t)) for w, t in node.when_clauses]
        else_ = self.visit(node.else_clause) if node.else_clause else None
        return CaseExpr(when_clauses=whens, else_clause=else_)

    def visit_cast(self, node: Cast) -> Any:
        return Cast(expr=self.visit(node.expr), type_name=node.type_name)

    def visit_subqueryexpr(self, node: SubqueryExpr) -> Any:
        return SubqueryExpr(query=self.visit(node.query))

    def visit_rawsql(self, node: RawSQL) -> Any:
        return node

    def visit_between(self, node: Between) -> Any:
        return Between(
            expr=self.visit(node.expr),
            low=self.visit(node.low),
            high=self.visit(node.high),
            negated=node.negated,
        )

    def visit_windowfunction(self, node: WindowFunction) -> Any:
        args = [self.visit(a) for a in node.args]
        partition_by = [self.visit(p) for p in node.partition_by]
        order_by = [self.visit(o) for o in node.order_by]
        return WindowFunction(
            func_name=node.func_name,
            args=args,
            partition_by=partition_by,
            order_by=order_by,
            distinct=node.distinct,
        )

    def visit_expr(self, node: Expr) -> Any:
        """Visit any expression node by dispatching to the correct method."""
        return self.visit(node)
