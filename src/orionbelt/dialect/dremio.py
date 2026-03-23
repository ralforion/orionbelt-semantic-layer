"""Dremio dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal
from orionbelt.dialect.base import Dialect, DialectCapabilities, UnsupportedAggregationError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain


@DialectRegistry.register
class DremioDialect(Dialect):
    """Dremio dialect — reduced function surface, quoting differences."""

    @property
    def name(self) -> str:
        return "dremio"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=False,
            supports_arrays=False,
            supports_window_filters=False,
            supports_ilike=False,
            unsupported_aggregations=["mode"],
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="DATE_TRUNC", args=[Literal.string(grain.value), column])

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        from orionbelt.ast.nodes import BinaryOp

        return BinaryOp(
            left=FunctionCall(name="LOWER", args=[column]),
            op="LIKE",
            right=BinaryOp(
                left=BinaryOp(
                    left=Literal.string("%"),
                    op="||",
                    right=FunctionCall(name="LOWER", args=[pattern]),
                ),
                op="||",
                right=Literal.string("%"),
            ),
        )

    def _compile_mode(self, args: list[Expr]) -> str:
        """Dremio does not support MODE aggregation."""
        raise UnsupportedAggregationError("dremio", "mode")

    def current_date_sql(self) -> str:
        return "CURRENT_DATE"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        unit_sql = unit.upper()
        return f"DATE_ADD({date_sql}, INTERVAL '{count}' {unit_sql})"
