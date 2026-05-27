"""Snowflake dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, UnionAll
from orionbelt.dialect.base import Dialect, DialectCapabilities
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import DecimalType, OBMLType


@DialectRegistry.register
class SnowflakeDialect(Dialect):
    """Snowflake dialect — QUALIFY, case-sensitive identifiers, semi-structured types."""

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "NUMBER(38, 0)",
        "integer": "NUMBER(38, 0)",
        "double": "FLOAT",
        "date": "DATE",
        "timestamp": "TIMESTAMP_TZ",
        "time": "TIME",
        "string": "VARCHAR",
        "boolean": "BOOLEAN",
    }

    def render_obml_type(self, obml_type: OBMLType) -> str:
        if isinstance(obml_type, DecimalType):
            p = min(obml_type.precision, self._MAX_DECIMAL_PRECISION)
            s = min(obml_type.scale, p)
            return f"NUMBER({p}, {s})"
        return self._OBML_SIMPLE_TYPE_MAP.get(obml_type.name, obml_type.name.upper())

    @property
    def name(self) -> str:
        return "snowflake"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=True,
            supports_arrays=True,
            supports_window_filters=True,
            supports_ilike=True,
            supports_time_travel=True,
            supports_semi_structured=True,
            supports_union_all_by_name=True,
            supports_group_by_all=True,
            # ``aggregation: measure`` requires Databricks Metric Views.
            # Snowflake Semantic Views use the ``SEMANTIC_VIEW(view DIMENSIONS
            # ... METRICS ...)`` table function instead; bare ``MEASURE()`` is
            # only valid inside that table function's projection. Publishing
            # OBML as a Snowflake Semantic View is a separate feature.
            unsupported_aggregations=["measure"],
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="DATE_TRUNC", args=[Literal.string(grain.value), column])

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        return FunctionCall(name="CONTAINS", args=[column, pattern])

    def current_date_sql(self) -> str:
        return "CURRENT_DATE()"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        unit_sql = unit.lower()
        return f"DATEADD('{unit_sql}', {count}, {date_sql})"

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"DATE_TRUNC('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        spine = f"DATEADD('{grain}', rn - 1, {min_date})::date"
        prev = f"DATEADD('{offset_grain}', {offset}, {spine})::date"
        return (
            f"SELECT {spine} AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (\n"
            f"  SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS rn\n"
            f"  FROM TABLE(GENERATOR(ROWCOUNT => "
            f"DATEDIFF('{grain}', {min_date}, {max_date}) + 1))\n"
            f") AS t"
        )

    def _compile_multi_field_count(self, args: list[Expr], distinct: bool) -> str:
        """Snowflake supports native multi-arg COUNT(col1, col2)."""
        args_sql = ", ".join(self.compile_expr(a) for a in args)
        if distinct:
            return f"COUNT(DISTINCT {args_sql})"
        return f"COUNT({args_sql})"

    def compile_union_all(self, node: UnionAll) -> str:
        """Snowflake uses UNION ALL BY NAME to match columns by name."""
        return "\nUNION ALL BY NAME\n".join(self.compile_select(q) for q in node.queries)
