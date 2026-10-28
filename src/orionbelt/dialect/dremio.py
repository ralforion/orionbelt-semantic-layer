"""Dremio dialect implementation."""

from __future__ import annotations

from decimal import Decimal

from orionbelt.ast.nodes import (
    BinaryOp,
    CaseExpr,
    Cast,
    Expr,
    FunctionCall,
    Literal,
    Unnest,
)
from orionbelt.dialect.base import (
    Dialect,
    DialectCapabilities,
    UnsupportedAggregationError,
    UnsupportedNestedAccessError,
    _dremio_access,
    _dremio_row_type,
    _json_path_of,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import DecimalType, OBMLType


@DialectRegistry.register
class DremioDialect(Dialect):
    """Dremio dialect — reduced function surface, quoting differences."""

    like_needs_escape_clause = True

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
            # ``FLATTEN`` is a projection function, so the unnest goes in the
            # SELECT list of a derived table rather than in the FROM clause -
            # see :meth:`render_unnest`. The planner reads this and falls back
            # to a nested object's ``code`` where one is declared.
            supports_from_unnest=False,
            # ``measure`` is Databricks Metric View specific.
            unsupported_aggregations=[
                "mode",
                "measure",
                # The value at the greatest / least key. Dremio has no MAX_BY,
                # takes no ORDER BY inside ARRAY_AGG, indexes arrays only by a
                # literal, and its ARRAY_AGG ... WITHIN GROUP returns an empty
                # array for the group whose key is NULL (probed: DE right, the
                # NULL group [], where MAX, ARRAY_AGG, LISTAGG and
                # PERCENTILE_DISC see its rows), which reads as a NULL first /
                # last: a wrong answer rather than an error.
                "first",
                "last",
            ],
        )

    def render_unnest(self, node: Unnest) -> str:
        """Dremio has no FROM-clause unnest, so this refuses.

        ``FLATTEN`` is a **projection** function: the unnest goes in the SELECT
        list of a derived table, and the fields are read from outside it::

            FROM (SELECT c.id, c.cost, FLATTEN(c.labels) AS l FROM charges c) f

        That restructures the query rather than extending its FROM clause, so it
        belongs with the planner rather than here. Measured to work, including
        the outer form emulated by a UNION ALL of the non-empty flatten and the
        rows whose array is empty.

        Until then a model reaches its data on Dremio through the ``code``
        fallback, which is what the fallback is for.
        """
        raise UnsupportedNestedAccessError(self.name, node.alias)

    def exact_integer_sum(self, arg: Expr) -> Expr | None:
        """``SUM`` over BIGINT accumulates in 64 bits here, and wraps.

        Measured against Dremio OSS: two rows of 9000000000000000000 sum to
        -446744073709551616, a negative total from two positive rows, and
        ``CAST(SUM(qty) AS BIGINT)`` - what OBSL emitted - returns the same,
        because the accumulator has already wrapped by the time the cast runs.
        Casting the argument first returns 18000000000000000000.

        The same overflow ``exact_integer_avg`` below already dodges inside its
        own rewrite, which is where it was first found (#318). It reaches a
        plain ``SUM`` by the same road (#338).
        """
        return self._sum_over_widened_argument(arg)

    def exact_decimal_avg(
        self, arg: Expr, obml_type: OBMLType, source_type: DecimalType | None = None
    ) -> Expr | None:
        """The decimal summed as it is, divided by :meth:`_rounded_average`.

        Dremio's ``AVG`` of a decimal is a double: 12345678901234.565 came back
        1.2345678901234564E13, and 9999999999999999.99 as 1.0E16, which a
        ``DECIMAL(18, 2)`` cast turned into 0. ``SUM`` of a decimal is exact.
        """
        integer_digits = (
            obml_type.precision - obml_type.scale if isinstance(obml_type, DecimalType) else 0
        )
        if source_type is not None:
            integer_digits = min(integer_digits, source_type.precision - source_type.scale)
        return self._rounded_average(
            FunctionCall(name="SUM", args=[arg]), arg, obml_type, integer_digits
        )

    def exact_integer_avg(self, arg: Expr, obml_type: OBMLType) -> Expr | None:
        """The integer summed as a decimal, divided by :meth:`_rounded_average`.

        Widened inside the ``SUM``: a 64-bit total wraps silently on Dremio.
        Measured: ``AVG(x)`` returns 1e+18 for an average of
        1000000000000000003, so the rewrite fixes more than the floating-point
        drift it was written for.
        """
        if not isinstance(obml_type, DecimalType):
            return None
        widened = Cast(
            expr=arg,
            type_name=self.render_obml_type(
                DecimalType(precision=self._MAX_DECIMAL_PRECISION, scale=0)
            ),
        )
        return self._rounded_average(
            FunctionCall(name="SUM", args=[widened]),
            arg,
            obml_type,
            obml_type.precision - obml_type.scale,
        )

    #: The integer digits a decimal quotient keeps: Dremio types it
    #: ``DECIMAL(38, 6)``, and 10^35 divided by 1 came back 0, silently.
    _QUOTIENT_INTEGER_DIGITS = 32

    #: The most places :meth:`_rounded_average` divides to: its fraction and
    #: the sum it ends with take two digits more than twice the scale.
    _MAX_AVERAGE_SCALE = 16

    def _rounded_average(
        self, total: Expr, arg: Expr, obml_type: OBMLType, integer_digits: int
    ) -> Expr | None:
        """*total* / ``COUNT(x)`` rounded once, half away from zero, at the result.

        Not a division and a cast. Every Dremio decimal division rounds its
        quotient, at ``max(6, scale)`` places, and the cast rounds again, so
        the two can disagree: 100 rows of 1 and 19901 of 0 average 0.0049997,
        which rounds to 0.00, but rounded at six places it is 0.005000, and
        the cast made that 0.01.

        So the average is long division in integers, as on DuckDB. The
        absolute total ``A``, truncated one place past the result (which keeps
        every rounding boundary, :meth:`Dialect.exact_decimal_avg`), splits
        into its integer part ``I`` and its fraction ``F`` in units of that
        place. ``q = (I - MOD(I, N)) / N`` is the integer part of the average,
        and ``R = (Y - MOD(Y, 20N)) / 20N`` with
        ``Y = 2 * (MOD(I, N) * 10^(scale + 1) + F) + 10N`` the fraction in
        units of the result's last place, rounded half up. Each division
        divides a multiple of its divisor, so the quotient's rounding never
        bites. The sign goes on the two integers before they are combined:
        Dremio's unary minus retypes a decimal, ``-DECIMAL(29, 12)`` as
        ``DECIMAL(38, 10)``, two places fewer.

        **Every integer is cast to DECIMAL(38, 0).** Dremio narrows the scale
        of an arithmetic result that reaches 38 digits - ``DECIMAL(38, 8) *
        BIGINT`` came back ``DECIMAL(38, 6)``, rounded - which costs an
        integer nothing, and adds a digit to a sum's type for the carry. The
        fraction is taken with ``MOD(A, 1)``, exact, where ``A -
        TRUNCATE(A, 0)`` lost places that way (0.99999999 came back 1).

        Limits, past which ``AVG`` is kept: a result of more than 16 places;
        *integer_digits*, the most the average or a declared value can have,
        past the quotient's 32, as a larger decimal quotient overflows
        silently; and those digits plus the scale past 37, the room the final
        sum has. The total itself has 38 digits, as Dremio's own ``SUM``.
        """
        if not isinstance(obml_type, DecimalType):
            return None
        scale = obml_type.scale
        integer_digits = max(integer_digits, 1)
        if (
            scale > self._MAX_AVERAGE_SCALE
            or integer_digits > self._QUOTIENT_INTEGER_DIGITS
            or integer_digits + scale > self._MAX_DECIMAL_PRECISION - 1
        ):
            return None

        def decimal(expr: Expr, precision: int, places: int = 0) -> Expr:
            return Cast(
                expr=expr,
                type_name=self.render_obml_type(DecimalType(precision=precision, scale=places)),
            )

        def integer(expr: Expr) -> Expr:
            return decimal(expr, self._MAX_DECIMAL_PRECISION)

        def constant(value: Decimal, precision: int, places: int = 0) -> Expr:
            return decimal(Literal.string(f"{value:f}"), precision, places)

        def floor_divide(dividend: Expr, divisor: Expr) -> Expr:
            remainder = FunctionCall(name="MOD", args=[dividend, divisor])
            return integer(
                BinaryOp(
                    left=integer(BinaryOp(left=dividend, op="-", right=remainder)),
                    op="/",
                    right=divisor,
                )
            )

        places = scale + 1
        shift = constant(Decimal(10) ** places, places + 1)
        count = decimal(
            FunctionCall(
                name="NULLIF",
                args=[FunctionCall(name="COUNT", args=[arg]), Literal.number(0)],
            ),
            19,
        )
        magnitude = FunctionCall(
            name="TRUNCATE",
            args=[FunctionCall(name="ABS", args=[total]), Literal.number(places)],
        )
        whole = integer(FunctionCall(name="TRUNCATE", args=[magnitude, Literal.number(0)]))
        fraction = integer(
            BinaryOp(
                left=FunctionCall(name="MOD", args=[magnitude, constant(Decimal(1), 1)]),
                op="*",
                right=shift,
            )
        )
        remainder = integer(FunctionCall(name="MOD", args=[whole, count]))
        doubled = integer(
            BinaryOp(
                left=integer(
                    BinaryOp(
                        left=constant(Decimal(2), 1),
                        op="*",
                        right=integer(
                            BinaryOp(
                                left=integer(BinaryOp(left=remainder, op="*", right=shift)),
                                op="+",
                                right=fraction,
                            )
                        ),
                    )
                ),
                op="+",
                right=integer(BinaryOp(left=constant(Decimal(10), 2), op="*", right=count)),
            )
        )
        units = floor_divide(
            doubled, integer(BinaryOp(left=constant(Decimal(20), 2), op="*", right=count))
        )
        sign = CaseExpr(
            when_clauses=[
                (BinaryOp(left=total, op="<", right=Literal.number(0)), constant(Decimal(-1), 1))
            ],
            else_clause=constant(Decimal(1), 1),
        )

        def signed(value: Expr, precision: int) -> Expr:
            return decimal(integer(BinaryOp(left=value, op="*", right=sign)), precision)

        return BinaryOp(
            left=signed(floor_divide(whole, count), integer_digits),
            op="+",
            right=BinaryOp(
                left=signed(units, scale + 2),
                op="*",
                right=constant(Decimal(1).scaleb(-scale), scale + 1, scale),
            ),
        )

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """Dremio: supports multi-level paths via the ``code`` field.

        Dremio namespaces can be arbitrarily deep (Space.Folder.SubFolder.Table).
        When ``database`` and ``schema`` are empty, ``code`` is used as the full
        path (user encodes the complete Dremio path in the OBML ``code`` field).
        Otherwise falls back to the standard 3-part format.
        All components are quoted to prevent SQL injection.

        Unlike the other three-part dialects this does *not* refuse a
        ``database`` with no ``schema``: a Dremio path has no fixed arity, so
        ``"PROD"."sales"`` is a well-formed two-level path naming exactly what
        the model named. Elsewhere the same pair would silently be read as
        ``schema.table`` - see
        :class:`~orionbelt.dialect.base.AmbiguousTableReferenceError`.
        """
        parts = [self.quote_identifier(p) for p in (database, schema) if p]
        if parts:
            return f"{'.'.join(parts)}.{self.quote_identifier(code)}"
        return self.quote_identifier(code)

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="DATE_TRUNC", args=[Literal.string(grain.value), column])

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:

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

    def compile_ilike_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """Dremio has the ``ILIKE(value, pattern, escape)`` function, not the operator.

        The escape argument is the operator's ``ESCAPE`` clause: without it the
        backslash is a literal character here too (measured).
        """
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        esc_sql = self.quote_string_literal("\\")
        call = f"ILIKE({col_sql}, {pat_sql}, {esc_sql})"
        return f"NOT {call}" if negated else call

    def _render_concat(self, args: list[Expr]) -> str:
        """Dremio's ``CONCAT`` ignores NULL arguments, where the catalog says
        NULL propagates.

        The NULL-guard form rather than a ``||`` chain: Dremio documents
        ``CONCAT`` as NULL-ignoring and says nothing about ``||``, and there is
        no Dremio instance in the test matrix to settle it. The guard gives the
        catalog's answer either way.
        """
        return self._render_concat_null_guard(args)

    def _render_in_timezone(self, value: Expr, zone: str, from_zone: str | None) -> str:
        """Dremio: ``CONVERT_TIMEZONE``, three-argument form when the source
        zone has to be declared, two-argument when the value knows its own.
        """
        rendered = self.compile_expr(value)
        if from_zone is not None:
            return (
                f"CONVERT_TIMEZONE({self._quote_zone(from_zone)}, "
                f"{self._quote_zone(zone)}, {rendered})"
            )
        return f"CONVERT_TIMEZONE({self._quote_zone(zone)}, {rendered})"

    def _render_week_start_sunday(self, value: Expr) -> str:
        """Dremio's ``DAYOFWEEK`` numbers Sunday as 1, so the offset is one
        less, applied with the TIMESTAMPADD this dialect already uses for date
        arithmetic.

        Stepping back from the start of the day rather than from the value:
        subtracting days from a timestamp keeps its time, and the start of a
        week is midnight.
        """
        rendered = self.compile_expr(value)
        return f"TIMESTAMPADD(DAY, -(DAYOFWEEK({rendered}) - 1), DATE_TRUNC('day', {rendered}))"

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """Dremio: ``TIMESTAMPADD(UNIT, n, x)``, which is already how the
        relative-date filters render here, and which takes QUARTER and WEEK
        that its interval qualifiers reject.
        """
        return (
            f"TIMESTAMPADD({unit.upper()}, {self.compile_expr(count)}, {self.compile_expr(value)})"
        )

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """Dremio: ``TIMESTAMPDIFF(UNIT, start, end)``.

        Whether it counts boundaries or complete units is not something this
        repo can run and check, so both ends are truncated to the unit first,
        which makes the two readings identical.
        """
        return (
            f"TIMESTAMPDIFF({unit.upper()}, "
            f"{self._render_date_trunc(unit, start)}, "
            f"{self._render_date_trunc(unit, end)})"
        )

    def _render_trunc(self, args: list[Expr]) -> str:
        """Dremio spells it ``TRUNCATE`` and documents it as truncating toward
        zero; it has no ``TRUNC``.
        """
        value = self.compile_expr(args[0])
        digits = self.compile_expr(args[1]) if len(args) > 1 else "0"
        return f"TRUNCATE({value}, {digits})"

    def promote_dividend(self, dividend_sql: str) -> str:
        """Every integer division truncates: ``SUM`` of an integer stays
        BIGINT, so a count, a sum or a ratio of them over an integer came
        back 0.

        No arithmetic leaves every type alone: Dremio sizes a decimal result
        by Arrow's rules, which take scale away once precision passes 38, so
        ``DECIMAL(38, 12) * 1.0`` is ``DECIMAL(38, 10)`` and 1e-11 becomes 0.
        ``COALESCE`` with a ``DECIMAL(1, 1)`` NULL only unifies types: an
        integer becomes an exact ``DECIMAL(20, 1)`` at most, a decimal keeps
        its precision and scale, and a double stays a double. The NULL has no
        integer digit, so a ``DECIMAL(38, 38)`` is not made to find room for
        one (a ``DECIMAL(1, 0)`` NULL took 1e-38 to 0); a ``DECIMAL(38, 0)``
        keeps its integer digits, the NULL's one decimal giving way instead.
        """
        return f"COALESCE({dividend_sql}, CAST(NULL AS DECIMAL(1, 1)))"

    def _render_div(self, args: list[Expr]) -> str:
        """Dremio has no integer-division function or operator.

        ``7 / 2`` is an integer division there, and one that truncates, so
        the dividend is promoted first: the quotient is then exact, and
        truncated once, toward zero, by ``SIGN * FLOOR(ABS)``.
        """
        left = self.promote_dividend(self.compile_expr(args[0]))
        right = self.compile_expr(args[1], _parent_prec=self._PREC_MUL + 1)
        quotient = f"({left} / {right})"
        return self._render_infix(f"SIGN({quotient}) * FLOOR(ABS({quotient}))")

    def _render_log(self, args: list[Expr]) -> str:
        """Dremio's ``LOG`` accepts a base, but its reference does not state
        which argument carries it, and there is no Dremio in the execution
        matrix to settle it. The base change through ``LOG10`` depends on no
        argument order at all.
        """
        base = self.compile_expr(args[0])
        value = self.compile_expr(args[1])
        return self._render_infix(f"LOG10({value}) / LOG10({base})")

    def _render_extremum(self, name: str, args: list[Expr]) -> str:
        """Dremio's NULL handling in ``GREATEST`` / ``LEAST`` is unverified
        here, and its ``CONCAT`` already skips NULLs, so the guard applies the
        catalog's rule without depending on which behaviour it has.
        """
        return self._render_null_guard(self._render_named_function(name, args), args)

    def _compile_median(self, args: list[Expr]) -> str:
        """Dremio: MEDIAN over the values as doubles.

        ``MEDIAN`` of a DECIMAL loses small values: the median of
        0.000000001 in a DECIMAL(20, 9) came back as 0.0. As a double it is
        the continuous median every engine answers.
        """
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"MEDIAN(CAST({col_sql} AS DOUBLE))"

    _percentile_cont_over_doubles = True

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

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"DATE_TRUNC('{grain}', {column_sql})"

    def render_pop_previous_value_sql(self, prev_sql: str, current_sql: str) -> str:
        # Dremio miscompiles a ``previousValue`` projection that reads *only* the
        # self-joined ``pop_prev`` alias: its executor reads the joined decimal's
        # bytes as the output date column, raising "Value <garbage> for
        # monthOfYear must be in the range [1,12]". Comparisons that also touch
        # ``pop_base`` (ratio/difference/percentChange) plan correctly, so add a
        # value-preserving reference to the base measure (``+ 0 * COALESCE(...)``
        # is always exactly 0 and never NULL-poisons the result).
        return f"{prev_sql} + 0 * COALESCE({current_sql}, 0)"

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

    def _render_safe_number_cast(self, trimmed: str) -> str:
        """A plain ``DOUBLE``: this engine has no ``TRY_CAST``.

        Measured on dremio-oss rather than assumed, after the assumption was
        wrong: ``TRY_CAST(x AS DOUBLE)`` does not parse here at all - the
        planner reads ``TRY_CAST(x`` as a call and stops at the ``AS``. Its
        plain ``CAST`` raises on text that is not a number, unlike MySQL's, so
        the test is what turns that into the NULL the entry promises.
        """
        return f"CAST({trimmed} AS DOUBLE)"

    def _render_json_value(self, args: list[Expr]) -> str:
        """``TRY_CONVERT_FROM(x AS ROW(...))`` honours the whole contract.

        Declaring the innermost field as VARCHAR is what does the work: a path
        landing on an object or array will not convert to VARCHAR, and the
        ``TRY_`` form answers NULL rather than failing, which is the catalog's
        object/array rule. A field absent from the document is NULL for the
        same reason. Both measured against a live Dremio OSS container.

        The alternatives do not work. ``CONVERT_FROM(x, 'JSON')`` with field
        access raises "Unable to find the referenced field" for a path that is
        not present, and that is the common case in tag allocation, not an edge
        one; it also returns the struct itself for an object rather than NULL.
        ``TRY_VARIANT_GET`` would be the natural fit and is what Databricks
        uses, but VARIANT is Dremio Cloud only - ``PARSE_JSON`` does not exist
        on Dremio Software, verified as "No match found for function
        signature".

        The ROW type has to be built rather than discovered, which the
        catalog's literal-path rule makes possible.

        Parenthesised because the rendering ends in a field access rather than
        a closing paren, so it is not self-delimiting: the catalog requires
        every entry to drop into a surrounding expression as one operand.
        """
        doc = self.compile_expr(args[0])
        path = _json_path_of(args[1])
        row_type = _dremio_row_type(path, self.quote_identifier)
        access = _dremio_access(path, self.quote_identifier)
        return f"(TRY_CONVERT_FROM({doc} AS {row_type}){access})"

    def _render_iso_day_of_week(self, value: Expr) -> str:
        """Dremio's EXTRACT takes no ``ISODOW``; its ``DOW`` is Sunday 1."""
        return self._render_sunday_first_as_iso(f"EXTRACT(DOW FROM {self.compile_expr(value)})")
