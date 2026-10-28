"""DuckDB / MotherDuck dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import (
    BinaryOp,
    CaseExpr,
    Cast,
    Expr,
    FunctionCall,
    Literal,
    OrderByItem,
    UnionAll,
    Unnest,
)
from orionbelt.dialect.base import (
    Dialect,
    DialectCapabilities,
    _json_path_of,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import DecimalType, OBMLType


@DialectRegistry.register
class DuckDBDialect(Dialect):
    """DuckDB dialect — PostgreSQL-like syntax, ILIKE, UNION ALL BY NAME."""

    like_needs_escape_clause = True

    @property
    def name(self) -> str:
        return "duckdb"

    def exact_integer_avg(self, arg: Expr, obml_type: OBMLType) -> Expr | None:
        """An exact average assembled from integer arithmetic (#316).

        This engine computes ``AVG`` in ``double`` whatever the input type, so
        the drift starts at the mantissa rather than at a declared type, and no
        output cast repairs it - the loss is already inside the aggregate.
        duckdb/duckdb#6829 was closed as not planned, so the exactness has to
        come from the SQL.

        **Every route through ``/`` is out.** Measured: decimal over decimal,
        ``SUM``/``COUNT`` with either operand cast, and ``AVG`` over a cast
        input all come back ``DOUBLE``. What *is* exact here is integer
        arithmetic, so the average is assembled rather than divided:

        1. ``SUM`` over a ``BIGINT`` accumulates in ``HUGEINT``, 128 bits, and
           is exact.
        2. Scaling by ``10^s`` before dividing keeps the fraction the result
           needs, in integers.
        3. ``(2n + sign(n)*d) // 2d`` is the rounded quotient. Ties go **away
           from zero**, which is what ``round`` pins and what the engines that
           are already exact answer: measured against PostgreSQL at ``2.365``
           and ``-2.365``, all three agree on 2.37 and -2.37. ``//`` truncates
           toward zero here, which is what makes the doubling trick work in
           both signs.
        4. The scale is put back by *multiplying* by ``10^-s`` as a decimal
           constant. ``HUGEINT * DECIMAL(s+1, s)`` is ``DECIMAL(38, s)`` and
           exact, where dividing by ``10^s`` would have gone back to floating
           point.

        An empty group is not a division: ``COUNT`` of zero would raise where
        ``AVG`` returns NULL, and a multi-fact plan hits that routinely, so the
        whole thing sits behind a count test. NULL rows need no handling of
        their own - ``SUM`` and ``COUNT`` both skip them, so the assembly
        averages exactly the rows ``AVG`` would have.

        The remaining limit is honest and loud: ``2 * SUM * 10^s`` has to fit
        128 bits, so a total beyond ~8.5x10^35 at scale 2 raises here rather
        than drifting quietly. The scale is capped for the same arithmetic -
        see below - since it is the other half of the same budget.
        """
        if not isinstance(obml_type, DecimalType):
            return None
        # The scale is capped for the same reason it is on the engines that
        # divide (``_exact_avg_by_sum_over_count``), and here it bites harder:
        # the sum is multiplied by ``10^s`` *before* the division, so a long
        # fraction spends the same 128 bits the total needs. Measured, an
        # uncapped scale of 37 overflowed on rows of 5 - a result the declared
        # type holds comfortably - and a scale of 38 asked for a
        # ``DECIMAL(39, 38)`` constant, which is not a type. A result asking
        # for more scale gets the extra places as zeros, which is the same
        # honest trade the other rewrites make.
        scale = min(obml_type.scale, self._MAX_DECIMAL_PRECISION - self._SUM_HEADROOM_DIGITS)
        zero = Literal.number(0)
        count: Expr = Cast(expr=FunctionCall(name="COUNT", args=[arg]), type_name="HUGEINT")
        total: Expr = Cast(expr=FunctionCall(name="SUM", args=[arg]), type_name="HUGEINT")

        scaled = BinaryOp(left=total, op="*", right=Literal.number(10**scale))
        numerator = BinaryOp(
            left=BinaryOp(left=Literal.number(2), op="*", right=scaled),
            op="+",
            right=BinaryOp(left=FunctionCall(name="SIGN", args=[total]), op="*", right=count),
        )
        quotient = BinaryOp(
            left=numerator,
            op="//",
            right=BinaryOp(left=Literal.number(2), op="*", right=count),
        )
        # ``* 10^-s`` rather than ``/ 10^s``: multiplication by a decimal
        # constant stays in decimal, division does not.
        rescaled: Expr = (
            quotient
            if scale == 0
            else BinaryOp(
                left=quotient,
                op="*",
                right=Cast(
                    expr=Literal.number(float(f"0.{'0' * (scale - 1)}1")),
                    type_name=f"DECIMAL({scale + 1}, {scale})",
                ),
            )
        )
        return CaseExpr(
            when_clauses=[(BinaryOp(left=count, op="=", right=zero), Literal.null())],
            else_clause=rescaled,
        )

    #: The fewest places a combined average keeps before the measure's cast
    #: rounds it; fewer when the measure's integer digits leave no room.
    _MEAN_SCALE = 18

    def exact_quotient(
        self, total: Expr, count: Expr, input_scale: int, obml_type: OBMLType
    ) -> Expr | None:
        """``total / count`` exactly, truncated at a long scale, for a combined average.

        The combine step of a federated query divides the sum of its legs'
        partial sums by the sum of their counts this way (``avg``). *total* is
        an integer, or a decimal at *input_scale*, the scale every leg's
        partial sum is normalised to.

        **Truncated, not rounded.** The result is the average itself, for
        HAVING, ORDER BY and the metrics that read it as a raw ``AVG`` would be
        read: at ``K`` places, the measure's own scale or 18, whichever is more,
        and fewer only where the measure's integer digits leave no room in
        ``DECIMAL(38, K)``. The measure's cast then rounds it once, half away
        from zero; rounding a value truncated at more places than the result
        keeps gives what rounding the exact value gives, since a tie at the
        result's last place is a digit the truncation keeps. Where ``K`` is
        the measure's own scale - ``decimal(38, 18)`` leaves no place to spare
        - the cast has nothing to round, so the quotient is rounded here
        instead: rounding twice could disagree with rounding once.

        **Divided in parts**, as :meth:`exact_integer_avg` cannot afford to:
        scaling the whole total by ``10^K`` before dividing overflows 128 bits
        from totals of ``10^20`` at 18 places. The integer part is divided
        first, ``q = W // C`` and ``r = W % C``; only the remainder and the
        fraction, together less than ``C`` in absolute value, are scaled, so no
        intermediate exceeds ``C * 10^K``. ``//`` truncates toward zero and
        ``%`` takes the dividend's sign here, and the whole and the fraction of
        a total share its sign, so the two parts add up in both signs.

        ``None`` for a non-decimal result. An empty group is NULL, as ``AVG``
        over no rows is.
        """
        if not isinstance(obml_type, DecimalType):
            return None
        integer_digits = obml_type.precision - obml_type.scale
        places = max(
            obml_type.scale,
            min(self._MEAN_SCALE, self._MAX_DECIMAL_PRECISION - integer_digits),
        )
        divisor: Expr = Cast(expr=count, type_name="HUGEINT")
        whole: Expr = Cast(
            expr=total if input_scale == 0 else FunctionCall(name="trunc", args=[total]),
            type_name="HUGEINT",
        )
        quotient = BinaryOp(left=whole, op="//", right=divisor)
        remainder: Expr = BinaryOp(left=whole, op="%", right=divisor)
        if input_scale:
            # The remainder and the total's fraction, in 10^-input_scale units.
            fraction = Cast(
                expr=BinaryOp(
                    left=BinaryOp(
                        left=total, op="-", right=FunctionCall(name="trunc", args=[total])
                    ),
                    op="*",
                    right=Literal.number(10**input_scale),
                ),
                type_name="HUGEINT",
            )
            remainder = BinaryOp(
                left=BinaryOp(left=remainder, op="*", right=Literal.number(10**input_scale)),
                op="+",
                right=fraction,
            )
        # The fraction's digits, in 10^-places units: the remainder over the
        # count, both scaled to those units.
        if places >= input_scale:
            numerator: Expr = BinaryOp(
                left=remainder, op="*", right=Literal.number(10 ** (places - input_scale))
            )
            denominator: Expr = divisor
        else:
            numerator = remainder
            denominator = BinaryOp(
                left=divisor, op="*", right=Literal.number(10 ** (input_scale - places))
            )
        if places > obml_type.scale:
            # Truncated: the measure's cast rounds it, at fewer places.
            digits: Expr = BinaryOp(left=numerator, op="//", right=denominator)
        else:
            # No place to spare, so nothing after this rounds: round here,
            # half away from zero, as ``(2n + sign(n)*d) // 2d``.
            digits = BinaryOp(
                left=BinaryOp(
                    left=BinaryOp(left=Literal.number(2), op="*", right=numerator),
                    op="+",
                    right=BinaryOp(
                        left=FunctionCall(name="SIGN", args=[numerator]),
                        op="*",
                        right=denominator,
                    ),
                ),
                op="//",
                right=BinaryOp(left=Literal.number(2), op="*", right=denominator),
            )
        mean: Expr = Cast(expr=quotient, type_name="DECIMAL(38, 0)")
        if places:
            mean = BinaryOp(
                left=mean,
                op="+",
                right=BinaryOp(
                    left=Cast(expr=digits, type_name="DECIMAL(38, 0)"),
                    op="*",
                    right=Cast(
                        expr=Literal.number(float(f"0.{'0' * (places - 1)}1")),
                        type_name=f"DECIMAL({places + 1}, {places})",
                    ),
                ),
            )
        return CaseExpr(
            when_clauses=[
                (BinaryOp(left=divisor, op="=", right=Literal.number(0)), Literal.null())
            ],
            else_clause=mean,
        )

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
        path = self._quote_text(_json_path_of(args[1]))
        # json_extract_string returns the serialized JSON for an object or
        # array path; json_type supplies the catalog's NULL rule.
        return (
            f"CASE WHEN json_type({doc}, {path}) IN ('OBJECT', 'ARRAY') "
            f"THEN NULL ELSE json_extract_string({doc}, {path}) END"
        )

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="date_trunc", args=[Literal.string(grain.value), column])

    def render_unnest(self, node: Unnest) -> str:
        """``UNNEST`` here aliases the *table*, not the element.

        ``AS "L"`` alone makes ``L.Key`` a binder error - measured, "Table L
        does not have a column named Key" - because the element sits in an
        unnamed column of a table called L. The two-part ``AS t(col)`` form
        names both, so the element is addressed the same way it is on the
        engines whose alias *is* the element.
        """
        source = f"UNNEST({self.unnest_path(node)})"
        alias = f"{self.quote_identifier(node.alias + '__t')}({self.quote_identifier(node.alias)})"
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
        """DuckDB: MEDIAN over the values as doubles.

        ``MEDIAN`` of a DECIMAL keeps its scale, so the mean of 2.75 and 10.10
        came back as 6.42 rather than 6.425. As a double it is the continuous
        median every engine answers, the type Postgres' ``PERCENTILE_CONT``
        returns.
        """
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"MEDIAN(CAST({col_sql} AS DOUBLE))"

    _percentile_cont_over_doubles = True

    def _compile_ordered_value(self, last: bool, value: Expr, key: Expr) -> str:
        """DuckDB: ``arg_max`` / ``arg_min`` by the (key, value) pair, which
        breaks a tie on the key by the value, over the rows with both."""
        v = self.compile_expr(value)
        k = self.compile_expr(key)
        function = "arg_max" if last else "arg_min"
        return (
            f"{function}({v}, struct_pack(k := {k}, v := {v}))"
            f" FILTER (WHERE {v} IS NOT NULL AND {k} IS NOT NULL)"
        )

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
        escaped_sep = self.quote_string_literal(sep)[1:-1]
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
