"""PostgreSQL dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem, Unnest
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

    avg_over_integers_is_exact = True

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

    def _render_to_number(self, args: list[Expr]) -> str:
        """A pattern test, then ``numeric``: this engine has no safe cast.

        ``numeric`` rather than ``double precision``, and that is what makes the
        guard sufficient here. A pattern can say whether text names a number; it
        cannot say whether the number fits, and ``'1e999'::double precision``
        raises out of range while ``'1e999'::numeric`` is exact, this engine's
        ``numeric`` being unbounded. The same consequence ``round`` has here,
        for the same reason.
        """
        # Through the text form, because this engine has no ``trim(numeric)``
        # and the pattern test is a question about text either way.
        as_text: Expr = Cast(expr=args[0], type_name="TEXT")
        trimmed = self._render_named_function("trim", [as_text])
        return self._render_numeric_text_guard(as_text, f"CAST({trimmed} AS NUMERIC)")

    def _render_json_value(self, args: list[Expr]) -> str:
        """Postgres has no ``JSON_VALUE``; ``json_extract_path_text`` takes the
        path as separate arguments, which is why the catalog pins it to a
        literal. The ``::json`` cast lets a text column carry the document.
        """
        doc = self.compile_expr(args[0])
        segments = _json_path_segments(_json_path_of(args[1]))
        # An array subscript is just another text element of the path here.
        rendered = "".join(f", {self._quote_text(value)}" for value, _ in segments)
        # json_extract_path_text returns the serialized JSON for an object or
        # array path; json_typeof supplies the catalog's NULL rule.
        return (
            f"CASE WHEN json_typeof(json_extract_path({doc}::json{rendered})) "
            f"IN ('object', 'array') THEN NULL "
            f"ELSE json_extract_path_text({doc}::json{rendered}) END"
        )

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="date_trunc", args=[Literal.string(grain.value), column])

    def unnest_path(self, node: Unnest) -> str:
        """A field of a composite has to be reached through parentheses.

        ``"C"."x_Project"."Ancestors"`` parses as a three-part *table* name, so
        Postgres reports "missing FROM-clause entry for table x_Project".
        ``("C"."x_Project")."Ancestors"`` is composite field access and reads
        the array. A single-segment column needs no parentheses and gets none.
        """
        parts = node.column.split(".")
        path = f"{self.quote_identifier(node.parent_alias)}.{self.quote_identifier(parts[0])}"
        for segment in parts[1:]:
            path = f"({path}).{self.quote_identifier(segment)}"
        return path

    def render_unnest(self, node: Unnest) -> str:
        """``LATERAL`` is not optional here; the alias is otherwise ordinary.

        Without ``LATERAL`` the parent is invisible to the function: measured, a
        plain ``LEFT JOIN unnest(C.x_Labels)`` fails with "missing FROM-clause
        entry for table C".

        The alias stays one-part, unlike DuckDB's. Postgres expands a composite
        array into columns named after its fields, so ``L."Key"`` reads
        directly - and the two-part ``AS t(col)`` form actively breaks that,
        collapsing the element to its first field: "column notation .Key applied
        to type text".
        """
        source = f"LATERAL UNNEST({self.unnest_path(node)})"
        alias = self.quote_identifier(node.alias)
        return f"LEFT JOIN {source} AS {alias} ON TRUE" if node.outer else f", {source} AS {alias}"

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

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """Postgres's interval parser rejects ``quarter``: ``INTERVAL '1
        quarter'`` is "invalid input syntax for type interval".

        Three months is the same interval and is accepted, which is what
        ``_interval_parts`` already does for the relative-date filters. Every
        other unit takes the shared form.
        """
        if unit != "quarter":
            return super()._render_date_add(unit, count, value)
        n = self.compile_expr(count, _parent_prec=self._PREC_MUL)
        return self._render_infix(
            f"{self.compile_expr(value, _parent_prec=self._PREC_ADD)} + "
            f"{n} * 3 * INTERVAL '1 month'"
        )

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
        escaped_sep = self.quote_string_literal(sep)[1:-1]
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

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
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

    def _round_decimal_cast(self, value_sql: str) -> str | None:
        """PostgreSQL rounds ties to even for ``double precision`` and away from
        zero for ``numeric``, both documented.

        The cast names no width because it does not have to: PostgreSQL's
        ``numeric`` is arbitrary precision, so it keeps every digit it is given
        and there is no scale to trade against magnitude. Measured on 16, an
        infinity, the ties and 1e19 all come back intact.

        The cast also supplies a function that does not otherwise exist: there
        is no ``round(double precision, integer)`` in PostgreSQL at all, so a
        two-argument ``round`` over a float column was a hard
        ``UndefinedFunction`` error rather than a wrong number.

        Deliberately *not* the arithmetic rewrite used for a float engine.
        ``power(10, n)`` is ``double precision`` here, and even with an integer
        literal scale PostgreSQL caps the scale of a numeric division: measured,
        12345678901234567.885 rounded to 2 places came back as
        12345678901234568 instead of 12345678901234567.89. Over a value that is
        already ``numeric`` this cast is the identity, so nothing is lost.
        """
        return f"CAST({value_sql} AS numeric)"

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """Postgres uses the ``~`` and ``!~`` operators for regex match."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        op = "!~" if negated else "~"
        return f"({col_sql} {op} {pat_sql})"
