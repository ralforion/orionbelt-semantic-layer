"""Databricks SQL dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem
from orionbelt.dialect.base import CrossColumnOrderNotSupportedError, Dialect, DialectCapabilities
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

    _ABSTRACT_TYPE_MAP: dict[str, str] = {
        "string": "STRING",
        "json": "STRING",
        "int": "INT",
        "float": "FLOAT",
        "date": "DATE",
        "time": "STRING",
        "time_tz": "STRING",
        "timestamp": "TIMESTAMP",
        "timestamp_tz": "TIMESTAMP",
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
            supports_group_by_all=True,
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

    # Databricks spells the prefix/suffix tests without the underscore
    # (``startswith`` / ``endswith``, Databricks Runtime 10.4 LTS and above).
    _SCALAR_FUNCTION_NAMES: dict[str, str] = {
        "starts_with": "STARTSWITH",
        "ends_with": "ENDSWITH",
    }

    def _render_week_start_sunday(self, value: Expr) -> str:
        """Spark's ``dayofweek`` numbers Sunday as 1, so the offset is one less."""
        rendered = self.compile_expr(value)
        return self._render_infix(
            f"DATE_TRUNC('day', {rendered}) "
            f"- make_interval(0, 0, 0, dayofweek({rendered}) - 1, 0, 0, 0)"
        )

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """Spark's interval literals want a constant, and its ``date_add`` adds
        days only, so the interval is built with ``make_interval``, whose
        arguments are ordinary expressions.
        """
        # At multiplication precedence, because the quarter slot multiplies:
        # a bare ``1 + 1`` there rendered as ``1 + 1 * 3``, which is four
        # months rather than six.
        n = self.compile_expr(count, _parent_prec=self._PREC_MUL)
        slots = {
            "year": f"{n}, 0, 0, 0, 0, 0, 0",
            "quarter": f"0, {n} * 3, 0, 0, 0, 0, 0",
            "month": f"0, {n}, 0, 0, 0, 0, 0",
            "week": f"0, 0, {n}, 0, 0, 0, 0",
            "day": f"0, 0, 0, {n}, 0, 0, 0",
            "hour": f"0, 0, 0, 0, {n}, 0, 0",
            "minute": f"0, 0, 0, 0, 0, {n}, 0",
            "second": f"0, 0, 0, 0, 0, 0, {n}",
        }
        return self._render_infix(
            f"{self.compile_expr(value, _parent_prec=self._PREC_ADD)} "
            f"+ make_interval({slots[unit]})"
        )

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """Databricks: ``date_diff(unit, start, end)`` with a keyword unit."""
        return f"date_diff({unit.upper()}, {self.compile_expr(start)}, {self.compile_expr(end)})"

    def _render_trunc(self, args: list[Expr]) -> str:
        """Databricks has no numeric truncation: its ``trunc`` truncates a
        *date* to a format, so ``trunc(1.9)`` is a type error rather than 1.

        ``floor`` takes an optional target scale here, so the rewrite is the
        floor of the magnitude with the sign restored, which goes toward zero
        the way the catalog documents.
        """
        return self._render_trunc_by_floor(args)

    def _render_div(self, args: list[Expr]) -> str:
        """Databricks: the ``div`` operator, "the integral part of the
        division", per the SQL function reference.
        """
        return self._render_div_operator(args, "div")

    def _render_extremum(self, name: str, args: list[Expr]) -> str:
        """Spark's ``greatest`` / ``least`` skip NULL arguments ("skipping null
        values"); the catalog propagates NULL.
        """
        return self._render_null_guard(self._render_named_function(name, args), args)

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
                raise CrossColumnOrderNotSupportedError("databricks", col_sql, ob_sql)
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
        if unit == "quarter":
            return f"add_months({date_sql}, {count * 3})"
        if unit == "year":
            return f"add_months({date_sql}, {count * 12})"
        raise ValueError(f"Unsupported unit '{unit}' for Databricks")

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"date_trunc('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev = self.date_add_sql("d", offset_grain, offset)
        # Spark's SEQUENCE step interval has no QUARTER unit; use 3 MONTH.
        step = "INTERVAL 3 MONTH" if grain == "quarter" else f"INTERVAL 1 {grain.upper()}"
        return (
            f"SELECT d AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (SELECT EXPLODE(SEQUENCE("
            f"{min_date}, {max_date}, {step})) AS d)"
        )

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """Databricks uses ``RLIKE`` / ``NOT RLIKE``."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        op = "NOT RLIKE" if negated else "RLIKE"
        return f"({col_sql} {op} {pat_sql})"
