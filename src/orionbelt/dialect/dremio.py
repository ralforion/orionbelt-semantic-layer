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
            # ``measure`` is Databricks Metric View specific.
            unsupported_aggregations=["mode", "measure"],
        )

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """Dremio: supports multi-level paths via the ``code`` field.

        Dremio namespaces can be arbitrarily deep (Space.Folder.SubFolder.Table).
        When ``database`` and ``schema`` are empty, ``code`` is used as the full
        path (user encodes the complete Dremio path in the OBML ``code`` field).
        Otherwise falls back to the standard 3-part format.
        All components are quoted to prevent SQL injection.
        """
        parts = [self.quote_identifier(p) for p in (database, schema) if p]
        if parts:
            return f"{'.'.join(parts)}.{self.quote_identifier(code)}"
        return self.quote_identifier(code)

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
        # TIMESTAMPADD (not DATE_ADD + INTERVAL) because Dremio/Calcite interval
        # qualifiers are limited to YEAR/MONTH/DAY/HOUR/MINUTE/SECOND — QUARTER
        # and WEEK are rejected as ``INTERVAL '-1' QUARTER`` but accepted as a
        # TIMESTAMPADD unit. CAST back to DATE to preserve DATE typing (matches
        # the forward spine in render_date_spine_cte_sql).
        return f"CAST(TIMESTAMPADD({unit.upper()}, {count}, {date_sql}) AS DATE)"

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"DATE_TRUNC('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev = self.date_add_sql("d", offset_grain, offset)
        grain_upper = grain.upper()
        # Cross-join of three 10-row value sets produces 1000 rows (0-999),
        # enough for any practical date range and grain combination.
        # Uses TIMESTAMPADD instead of WITH RECURSIVE (unsupported by Dremio).
        return (
            f"SELECT d AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (\n"
            f"  SELECT CAST(TIMESTAMPADD({grain_upper}, n, {min_date}) AS DATE) AS d\n"
            f"  FROM (\n"
            f"    SELECT a.n + b.n * 10 + c.n * 100 AS n\n"
            f"    FROM (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) a(n)\n"
            f"    CROSS JOIN (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) b(n)\n"
            f"    CROSS JOIN (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) c(n)\n"
            f"  ) AS nums\n"
            f"  WHERE TIMESTAMPADD({grain_upper}, n, {min_date}) <= {max_date}\n"
            f") AS spine"
        )
