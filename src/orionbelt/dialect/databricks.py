"""Databricks SQL dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem
from orionbelt.dialect.base import Dialect, DialectCapabilities
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain


@DialectRegistry.register
class DatabricksDialect(Dialect):
    """Databricks SQL dialect — Spark SQL semantics, backtick identifiers."""

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "BIGINT",
        "integer": "INT",
        "double": "DOUBLE",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "time": "STRING",
        "string": "STRING",
        "boolean": "BOOLEAN",
    }

    @property
    def name(self) -> str:
        return "databricks"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=False,
            supports_arrays=True,
            supports_window_filters=False,
            supports_ilike=False,
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace("`", "``")
        return f"`{escaped}`"

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="date_trunc", args=[Literal.string(grain.value), column])

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        from orionbelt.ast.nodes import BinaryOp

        return BinaryOp(
            left=FunctionCall(name="lower", args=[column]),
            op="LIKE",
            right=BinaryOp(
                left=BinaryOp(
                    left=Literal.string("%"),
                    op="||",
                    right=FunctionCall(name="lower", args=[pattern]),
                ),
                op="||",
                right=Literal.string("%"),
            ),
        )

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """Databricks: ARRAY_JOIN(COLLECT_LIST/COLLECT_SET(col), sep).

        Databricks does not support ORDER BY inside COLLECT_LIST/COLLECT_SET.
        Only self-ordering (sorting the aggregated column) is supported via SORT_ARRAY.
        Cross-column ordering raises an error.
        """
        sep = separator if separator is not None else ","
        col_sql = self.compile_expr(args[0]) if args else "''"
        escaped_sep = sep.replace("'", "''")
        collect_fn = "COLLECT_SET" if distinct else "COLLECT_LIST"
        inner = f"{collect_fn}({col_sql})"
        if order_by:
            ob_expr = order_by[0]
            ob_sql = self.compile_expr(ob_expr.expr)
            if ob_sql != col_sql:
                raise ValueError(
                    f"Databricks LISTAGG does not support ORDER BY on a different column "
                    f"(aggregated: {col_sql}, order by: {ob_sql})"
                )
            inner = f"SORT_ARRAY({inner}, false)" if ob_expr.desc else f"SORT_ARRAY({inner})"
        return f"ARRAY_JOIN({inner}, '{escaped_sep}')"

    def current_date_sql(self) -> str:
        return "current_date()"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        if unit == "day":
            return f"date_add({date_sql}, {count})"
        if unit == "week":
            return f"date_add({date_sql}, {count * 7})"
        if unit == "month":
            return f"add_months({date_sql}, {count})"
        if unit == "year":
            return f"add_months({date_sql}, {count * 12})"
        raise ValueError(f"Unsupported unit '{unit}' for Databricks")

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"date_trunc('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev = self.date_add_sql("d", offset_grain, offset)
        return (
            f"SELECT d AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (SELECT EXPLODE(SEQUENCE("
            f"{min_date}, {max_date}, INTERVAL 1 {grain.upper()})) AS d)"
        )

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """Databricks uses ``RLIKE`` / ``NOT RLIKE``."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        op = "NOT RLIKE" if negated else "RLIKE"
        return f"({col_sql} {op} {pat_sql})"
