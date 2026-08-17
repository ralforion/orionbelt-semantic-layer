"""Snowflake dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, UnionAll
from orionbelt.dialect.base import (
    Dialect,
    DialectCapabilities,
    _json_path_of,
    _json_path_segments,
)
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

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
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

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"DATE_TRUNC('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        spine = f"DATEADD('{grain}', rn - 1, {min_date})::date"
        prev = f"DATEADD('{offset_grain}', {offset}, {spine})::date"
        # GENERATOR(ROWCOUNT => n) requires a *constant* n; the row count is only
        # known at run time (it depends on the date_range scalar subqueries), so
        # generate a fixed upper bound and filter to the range — 100000 rows
        # covers ~270 years at daily grain, far beyond any practical spine.
        return (
            f"SELECT {spine} AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (\n"
            f"  SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS rn\n"
            f"  FROM TABLE(GENERATOR(ROWCOUNT => 100000))\n"
            f") AS t\n"
            f"WHERE {spine} <= {max_date}"
        )

    # Snowflake spells the prefix/suffix tests without the underscore;
    # ``STARTS_WITH`` / ``ENDS_WITH`` are not recognised (probe-verified).
    def _render_json_value(self, args: list[Expr]) -> str:
        """Snowflake's ``JSON_EXTRACT_PATH_TEXT`` takes the path dotted and
        without the leading ``$``, and accepts a VARCHAR document directly, so
        no ``PARSE_JSON`` wrapper is needed.
        """
        doc = self.compile_expr(args[0])
        segments = _json_path_segments(_json_path_of(args[1]))
        return f"JSON_EXTRACT_PATH_TEXT({doc}, {self._quote_text('.'.join(segments))})"

    _SCALAR_FUNCTION_NAMES: dict[str, str] = {
        "starts_with": "STARTSWITH",
        "ends_with": "ENDSWITH",
    }

    def _render_in_timezone(self, value: Expr, zone: str, from_zone: str | None) -> str:
        """Snowflake: ``CONVERT_TIMEZONE``, whose two-argument form reads an
        aware value in *zone* and whose three-argument form declares a naive
        one to be in *from_zone* first.
        """
        rendered = self.compile_expr(value)
        if from_zone is not None:
            return (
                f"CONVERT_TIMEZONE({self._quote_zone(from_zone)}, "
                f"{self._quote_zone(zone)}, {rendered})"
            )
        return f"CONVERT_TIMEZONE({self._quote_zone(zone)}, {rendered})"

    def _render_date_trunc(self, unit: str, value: Expr) -> str:
        """Snowflake's ``DATE_TRUNC('week', …)`` follows the WEEK_START session
        parameter, so a session set to Sunday would silently override a model
        that says Monday. Every other unit is unaffected and uses the native
        call.
        """
        if unit == "week":
            return self._render_week_floor_by_offset("DAYOFWEEKISO({0}) - 1", value)
        return super()._render_date_trunc(unit, value)

    def _render_week_start_sunday(self, value: Expr) -> str:
        """Sunday, likewise without consulting the session.

        ``DAYOFWEEKISO`` numbers Monday 1 through Sunday 7, which ``% 7`` turns
        into the days since Sunday; ``DAYOFWEEK`` would have followed
        WEEK_START.
        """
        return self._render_week_floor_by_offset("MOD(DAYOFWEEKISO({0}), 7)", value)

    def _render_week_floor_by_offset(self, offset_template: str, value: Expr) -> str:
        """Step back *offset* days from the start of *value*'s day.

        From the day rather than from the value itself: subtracting days from a
        timestamp keeps its time, and the start of a week is midnight.
        """
        rendered = self.compile_expr(value)
        offset = offset_template.format(rendered)
        return f"DATEADD('day', -({offset}), DATE_TRUNC('day', {rendered}))"

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """Snowflake: ``DATEADD('unit', n, x)``, quoted unit, value last."""
        return f"DATEADD('{unit}', {self.compile_expr(count)}, {self.compile_expr(value)})"

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """Snowflake spells it ``DATEDIFF`` and counts boundaries, as the
        catalog documents.
        """
        return f"DATEDIFF('{unit}', {self.compile_expr(start)}, {self.compile_expr(end)})"

    def _render_div(self, args: list[Expr]) -> str:
        """Snowflake rejects ``DIV`` in every form ("Unsupported feature
        'DIV'"), but its ``/`` is float division even on integers, so
        truncating the quotient gives the catalog's answer.
        """
        return self._render_div_by_truncation(args)

    def _compile_multi_field_count(self, args: list[Expr], distinct: bool) -> str:
        """Snowflake supports native multi-arg COUNT(col1, col2)."""
        args_sql = ", ".join(self.compile_expr(a) for a in args)
        if distinct:
            return f"COUNT(DISTINCT {args_sql})"
        return f"COUNT({args_sql})"

    def compile_union_all(self, node: UnionAll) -> str:
        """Snowflake uses UNION ALL BY NAME to match columns by name."""
        return "\nUNION ALL BY NAME\n".join(self.compile_select(q) for q in node.queries)
