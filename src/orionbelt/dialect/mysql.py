"""MySQL 8.0+ dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem
from orionbelt.dialect.base import Dialect, DialectCapabilities, UnsupportedAggregationError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain


@DialectRegistry.register
class MySQLDialect(Dialect):
    """MySQL 8.0+ dialect — backtick quoting, DATE_FORMAT time grains, GROUP_CONCAT."""

    # MySQL-specific type overrides
    _ABSTRACT_TYPE_MAP: dict[str, str] = {
        "string": "VARCHAR(255)",
        "json": "JSON",
        "int": "INT",
        "float": "DOUBLE",
        "date": "DATE",
        "time": "TIME",
        "time_tz": "TIME",
        "timestamp": "DATETIME",
        "timestamp_tz": "DATETIME",
        "boolean": "TINYINT(1)",
    }

    @property
    def name(self) -> str:
        return "mysql"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=False,
            supports_arrays=False,
            supports_window_filters=False,
            supports_ilike=False,
            supports_time_travel=False,
            supports_semi_structured=False,
            supports_union_all_by_name=False,
            unsupported_aggregations=["mode"],
        )

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """MySQL: two-part ``schema.code`` (schema == database in MySQL terminology)."""
        return f"{self.quote_identifier(schema)}.{self.quote_identifier(code)}"

    def quote_identifier(self, name: str) -> str:
        """MySQL uses backtick quoting."""
        escaped = name.replace("`", "``")
        return f"`{escaped}`"

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        """MySQL time grain truncation via DATE_FORMAT or DATE_ADD+MAKEDATE for quarters."""
        grain_format_map: dict[TimeGrain, str | None] = {
            TimeGrain.SECOND: "%Y-%m-%d %H:%i:%s",
            TimeGrain.MINUTE: "%Y-%m-%d %H:%i:00",
            TimeGrain.HOUR: "%Y-%m-%d %H:00:00",
            TimeGrain.DAY: "%Y-%m-%d",
            TimeGrain.WEEK: "%Y-%u",
            TimeGrain.MONTH: "%Y-%m-01",
            TimeGrain.QUARTER: None,  # handled below
            TimeGrain.YEAR: "%Y-01-01",
        }

        if grain == TimeGrain.QUARTER:
            from orionbelt.ast.nodes import RawSQL

            col_sql = self.compile_expr(column)
            return RawSQL(
                sql=(
                    f"DATE_ADD(MAKEDATE(YEAR({col_sql}), 1), "
                    f"INTERVAL (QUARTER({col_sql}) - 1) * 3 MONTH)"
                )
            )

        fmt = grain_format_map.get(grain) or "%Y-%m-%d"
        return FunctionCall(
            name="DATE_FORMAT",
            args=[column, Literal.string(fmt)],
        )

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        """MySQL: LIKE with CONCAT (MySQL's || is logical OR by default)."""
        from orionbelt.ast.nodes import BinaryOp

        return BinaryOp(
            left=column,
            op="LIKE",
            right=FunctionCall(
                name="CONCAT",
                args=[Literal.string("%"), pattern, Literal.string("%")],
            ),
        )

    def _compile_median(self, args: list[Expr]) -> str:
        """MySQL 8.0+: emulate MEDIAN via PERCENTILE_CONT window function."""
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"MAX(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {col_sql}))"

    def _compile_mode(self, args: list[Expr]) -> str:
        """MySQL does not support MODE aggregation at the dialect level."""
        raise UnsupportedAggregationError("mysql", "mode")

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """MySQL: GROUP_CONCAT([DISTINCT] col [ORDER BY ...] SEPARATOR sep)."""
        sep = separator if separator is not None else ","
        col_sql = self.compile_expr(args[0]) if args else "''"
        distinct_sql = "DISTINCT " if distinct else ""
        escaped_sep = sep.replace("'", "''")

        parts = [f"GROUP_CONCAT({distinct_sql}{col_sql}"]
        if order_by:
            ob = ", ".join(self.compile_order_by(o) for o in order_by)
            parts.append(f" ORDER BY {ob}")
        parts.append(f" SEPARATOR '{escaped_sep}')")

        return "".join(parts)

    def _compile_multi_field_count(self, args: list[Expr], distinct: bool) -> str:
        """MySQL: use CONCAT instead of || for multi-field COUNT."""
        parts = [f"CAST({self.compile_expr(a)} AS CHAR)" for a in args]
        concat = f"CONCAT({', '.join(parts)})"
        if distinct:
            return f"COUNT(DISTINCT {concat})"
        return f"COUNT({concat})"

    def current_date_sql(self) -> str:
        return "CURDATE()"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        """MySQL: DATE_ADD(date, INTERVAL n unit) / DATE_SUB for negative."""
        if count < 0:
            return f"DATE_SUB({date_sql}, INTERVAL {abs(count)} {unit.upper()})"
        return f"DATE_ADD({date_sql}, INTERVAL {count} {unit.upper()})"
