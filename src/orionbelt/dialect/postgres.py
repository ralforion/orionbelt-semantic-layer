"""PostgreSQL dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem
from orionbelt.dialect.base import Dialect, DialectCapabilities
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import DecimalType, OBMLType


@DialectRegistry.register
class PostgresDialect(Dialect):
    """PostgreSQL dialect — strict GROUP BY, date_trunc, ILIKE."""

    _MAX_DECIMAL_PRECISION: int = 131072

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "BIGINT",
        "integer": "INTEGER",
        "double": "DOUBLE PRECISION",
        "date": "DATE",
        "timestamp": "TIMESTAMPTZ",
        "time": "TIME",
        "string": "TEXT",
        "boolean": "BOOLEAN",
    }

    def render_obml_type(self, obml_type: OBMLType) -> str:
        if isinstance(obml_type, DecimalType):
            p = min(obml_type.precision, self._MAX_DECIMAL_PRECISION)
            s = min(obml_type.scale, p)
            return f"DECIMAL({p}, {s})"
        return self._OBML_SIMPLE_TYPE_MAP.get(obml_type.name, obml_type.name.upper())

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """PostgreSQL: two-part ``schema.code`` (skip database).

        An omitted schema collapses to the bare table rather than an empty
        quoted component, so the reference resolves against the connection's
        search path. ``database`` is not part of the name on this dialect, so
        setting it without a schema is not ambiguous here.
        """
        if not schema:
            return self.quote_identifier(code)
        return f"{self.quote_identifier(schema)}.{self.quote_identifier(code)}"

    @property
    def name(self) -> str:
        return "postgres"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=False,
            supports_arrays=True,
            supports_window_filters=False,
            supports_ilike=True,
            # ``aggregation: measure`` is Databricks Metric View specific.
            unsupported_aggregations=["measure"],
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
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
        """Postgres's ``CONCAT`` skips NULL arguments (``concat('a', NULL,
        'c')`` is ``'ac'``); ``||`` propagates NULL as the catalog requires.
        Both probe-verified — see ``scripts/probe_functions.py``.
        """
        return self._render_concat_operator_chain(args)

    def _render_ends_with(self, args: list[Expr]) -> str:
        """Postgres has ``starts_with`` (since 11) but no ``ends_with``.

        ``RIGHT(x, LENGTH(suffix)) = suffix`` is the equivalent: it is NULL
        when either side is NULL, true for an empty suffix, and — unlike
        ``LIKE '%' || suffix`` — treats ``%`` and ``_`` in the suffix as
        ordinary characters.
        """
        haystack = self.compile_expr(args[0])
        suffix = self.compile_expr(args[1])
        return f"(RIGHT({haystack}, LENGTH({suffix})) = {suffix})"

    _MONTHS_PER_UNIT: dict[str, int] = {"year": 12, "quarter": 3, "month": 1}
    _SECONDS_PER_UNIT: dict[str, int] = {"day": 86400, "hour": 3600, "minute": 60, "second": 1}

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """Postgres has no date_diff, datediff or TIMESTAMPDIFF in any form.

        Both halves of the rewrite count boundaries, as the catalog documents,
        by truncating each end to the unit before measuring. Calendar units go
        through month arithmetic, because an interval between two dates cannot
        be converted to months without knowing which months; the rest divide
        the elapsed seconds, which is exact once both ends sit on a boundary.
        """
        left = self._render_date_trunc(unit, start)
        right = self._render_date_trunc(unit, end)
        if unit in self._MONTHS_PER_UNIT:
            months = (
                f"(EXTRACT(YEAR FROM {right}) - EXTRACT(YEAR FROM {left})) * 12 "
                f"+ (EXTRACT(MONTH FROM {right}) - EXTRACT(MONTH FROM {left}))"
            )
            step = self._MONTHS_PER_UNIT[unit]
            inner = months if step == 1 else f"({months}) / {step}"
            return f"CAST(TRUNC({inner}) AS INTEGER)"
        seconds = f"EXTRACT(EPOCH FROM ({right} - {left}))"
        if unit == "week":
            return f"CAST(TRUNC({seconds} / 604800) AS INTEGER)"
        return f"CAST(TRUNC({seconds} / {self._SECONDS_PER_UNIT[unit]}) AS INTEGER)"

    def _render_extract(self, unit: str, value: Expr) -> str:
        """Postgres returns a numeric where the catalog documents an int."""
        return f"CAST({super()._render_extract(unit, value)} AS INTEGER)"

    def _render_last_day(self, value: Expr) -> str:
        """Postgres has no LAST_DAY: the day before the start of next month."""
        month_start = self._render_date_trunc("month", value)
        return f"CAST({month_start} + INTERVAL '1 month' - INTERVAL '1 day' AS DATE)"

    def _render_current_date(self) -> str:
        """Postgres rejects ``CURRENT_DATE()``; the keyword takes no parens."""
        return "CURRENT_DATE"

    def _render_extremum(self, name: str, args: list[Expr]) -> str:
        """Postgres's ``GREATEST`` / ``LEAST`` skip NULL arguments; the catalog
        propagates NULL. ``div`` needs no override: Postgres has it natively
        and it truncates toward zero.
        """
        return self._render_null_guard(self._render_named_function(name, args), args)

    def _compile_median(self, args: list[Expr]) -> str:
        """PostgreSQL: PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY col)."""
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY {col_sql})"

    def _compile_mode(self, args: list[Expr]) -> str:
        """PostgreSQL: MODE() WITHIN GROUP (ORDER BY col)."""
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"MODE() WITHIN GROUP (ORDER BY {col_sql})"

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """PostgreSQL: STRING_AGG([DISTINCT] col, sep [ORDER BY ...])."""
        sep = separator if separator is not None else ","
        col_sql = self.compile_expr(args[0]) if args else "''"
        distinct_sql = "DISTINCT " if distinct else ""
        escaped_sep = sep.replace("'", "''")
        inner = f"{distinct_sql}{col_sql}, '{escaped_sep}'"
        if order_by:
            ob = ", ".join(self.compile_order_by(o) for o in order_by)
            inner += f" ORDER BY {ob}"
        return f"STRING_AGG({inner})"

    def current_date_sql(self) -> str:
        return "CURRENT_DATE"

    @staticmethod
    def _interval_parts(unit: str, count: int) -> tuple[int, str]:
        # Postgres interval input rejects 'quarter' ("invalid input syntax for
        # type interval"); express a quarter as three months instead.
        if unit == "quarter":
            return count * 3, "month"
        return count, unit

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        n, u = self._interval_parts(unit, count)
        return f"{date_sql} + INTERVAL '{n} {u}'"

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"date_trunc('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev_n, prev_u = self._interval_parts(offset_grain, offset)
        step_n, step_u = self._interval_parts(grain, 1)
        prev = f"d + INTERVAL '{prev_n} {prev_u}'"
        return (
            f"SELECT d::date AS spine_date,\n"
            f"       CASE WHEN ({prev})::date >= {min_date}\n"
            f"            THEN ({prev})::date END AS spine_date_prev\n"
            f"FROM generate_series({min_date}::timestamp, "
            f"{max_date}::timestamp, INTERVAL '{step_n} {step_u}') AS d"
        )

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """Postgres uses the ``~`` and ``!~`` operators for regex match."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        op = "!~" if negated else "~"
        return f"({col_sql} {op} {pat_sql})"
