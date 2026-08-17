"""DuckDB / MotherDuck dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem, UnionAll
from orionbelt.dialect.base import (
    Dialect,
    DialectCapabilities,
    _json_path_of,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain


@DialectRegistry.register
class DuckDBDialect(Dialect):
    """DuckDB dialect — PostgreSQL-like syntax, ILIKE, UNION ALL BY NAME."""

    @property
    def name(self) -> str:
        return "duckdb"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=True,
            supports_arrays=True,
            supports_window_filters=True,
            supports_ilike=True,
            supports_union_all_by_name=True,
            supports_group_by_all=True,
            # ``aggregation: measure`` is Databricks Metric View specific.
            unsupported_aggregations=["measure"],
        )

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """DuckDB: two-part ``schema.code`` (skip database for local mode).

        An omitted schema collapses to the bare table rather than an empty
        quoted component, so the reference resolves against the connection's
        search path. ``database`` is not part of the name on this dialect, so
        setting it without a schema is not ambiguous here.
        """
        if not schema:
            return self.quote_identifier(code)
        return f"{self.quote_identifier(schema)}.{self.quote_identifier(code)}"

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def _render_json_value(self, args: list[Expr]) -> str:
        """DuckDB has ``JSON_VALUE`` but it leaves the result quoted (``'"x"'``),
        so ``json_extract_string`` is the one that matches the catalog.
        """
        doc = self.compile_expr(args[0])
        path = _json_path_of(args[1])
        return f"json_extract_string({doc}, {self._quote_text(path)})"

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="date_trunc", args=[Literal.string(grain.value), column])

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        from orionbelt.ast.nodes import BinaryOp

        return BinaryOp(
            left=column,
            op="ILIKE",
            right=BinaryOp(
                left=BinaryOp(left=Literal.string("%"), op="||", right=pattern),
                op="||",
                right=Literal.string("%"),
            ),
        )

    def _render_concat(self, args: list[Expr]) -> str:
        """DuckDB's ``CONCAT`` skips NULL arguments (``concat('a', NULL, 'c')``
        is ``'ac'``); the catalog says NULL propagates, and ``||`` propagates
        it here. Both probe-verified — see ``scripts/probe_functions.py``.
        """
        return self._render_concat_operator_chain(args)

    def _render_div(self, args: list[Expr]) -> str:
        """DuckDB has no ``div`` function; ``//`` is its integer division, and
        it truncates toward zero (``-7 // 2`` is -3), which is the catalog's
        rule. Probe-verified.
        """
        return self._render_div_operator(args, "//")

    def _render_extremum(self, name: str, args: list[Expr]) -> str:
        """DuckDB's ``GREATEST`` / ``LEAST`` skip NULL arguments; the catalog
        propagates NULL, as it does for ``concat``.
        """
        return self._render_null_guard(self._render_named_function(name, args), args)

    def _compile_median(self, args: list[Expr]) -> str:
        """DuckDB: MEDIAN(col) — native support."""
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"MEDIAN({col_sql})"

    def _compile_mode(self, args: list[Expr]) -> str:
        """DuckDB: MODE(col) — native support."""
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"MODE({col_sql})"

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """DuckDB: STRING_AGG([DISTINCT] col, sep [ORDER BY ...]).

        DuckDB uses PostgreSQL-compatible STRING_AGG syntax.
        """
        sep = separator if separator is not None else ","
        col_sql = self.compile_expr(args[0]) if args else "''"
        distinct_sql = "DISTINCT " if distinct else ""
        escaped_sep = sep.replace("'", "''")
        inner = f"{distinct_sql}{col_sql}, '{escaped_sep}'"
        if order_by:
            ob = ", ".join(self.compile_order_by(o) for o in order_by)
            inner += f" ORDER BY {ob}"
        return f"STRING_AGG({inner})"

    def compile_union_all(self, node: UnionAll) -> str:
        """DuckDB supports UNION ALL BY NAME natively."""
        return "\nUNION ALL BY NAME\n".join(self.compile_select(q) for q in node.queries)

    def current_date_sql(self) -> str:
        return "CURRENT_DATE"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        return f"{date_sql} + INTERVAL '{count} {unit}'"

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"date_trunc('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev = f"d + INTERVAL '{offset} {offset_grain}'"
        return (
            f"SELECT d::date AS spine_date,\n"
            f"       CASE WHEN ({prev})::date >= {min_date}\n"
            f"            THEN ({prev})::date END AS spine_date_prev\n"
            f"FROM generate_series({min_date}::timestamp, "
            f"{max_date}::timestamp, INTERVAL '1 {grain}') AS t(d)"
        )

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """DuckDB uses ``regexp_matches(col, pattern)``."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        result = f"regexp_matches({col_sql}, {pat_sql})"
        return f"NOT {result}" if negated else result
