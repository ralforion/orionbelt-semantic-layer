"""ClickHouse dialect implementation."""

from __future__ import annotations

import re

from orionbelt.ast.nodes import (
    BinaryOp,
    Cast,
    Expr,
    FunctionCall,
    Literal,
    OrderByItem,
    RawSQL,
    Unnest,
)
from orionbelt.dialect.base import (
    CrossColumnOrderNotSupportedError,
    Dialect,
    DialectCapabilities,
    _json_path_of,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import DecimalType, OBMLType

_GRAIN_FUNCTIONS: dict[TimeGrain, str] = {
    TimeGrain.YEAR: "toStartOfYear",
    TimeGrain.QUARTER: "toStartOfQuarter",
    TimeGrain.MONTH: "toStartOfMonth",
    TimeGrain.WEEK: "toMonday",
    TimeGrain.DAY: "toDate",
    TimeGrain.HOUR: "toStartOfHour",
    TimeGrain.MINUTE: "toStartOfMinute",
    TimeGrain.SECOND: "toStartOfSecond",
}


def _is_integer_literal(value: object) -> bool:
    """``True`` for a whole-number literal, excluding ``bool``.

    ``bool`` is a subclass of ``int`` in Python, and ``True`` is not an integer
    to ClickHouse - the same trap #351 hit with ``round(x, true)``.
    """
    return isinstance(value, int) and not isinstance(value, bool)


#: Integer cast targets this dialect renders. ``accurateCast`` is used for these
#: rather than ``CAST`` because ``CAST`` saturates on overflow (#356).
_INT_TYPE_RE = re.compile(r"^\s*U?Int(?:8|16|32|64|128|256)\s*$", re.IGNORECASE)


@DialectRegistry.register
class ClickHouseDialect(Dialect):
    """ClickHouse dialect — custom date functions, aggregation differences."""

    _MAX_DECIMAL_PRECISION: int = 76

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "Int64",
        "integer": "Int32",
        "double": "Float64",
        "date": "Date",
        "timestamp": "DateTime64(3)",
        "time": "String",
        "string": "String",
        "boolean": "Bool",
    }

    unions_resolve_leg_types = False

    def render_unnest(self, node: Unnest) -> str:
        """``ARRAY JOIN``, which is its own clause rather than a join.

        No ``ON``, and the keyword order is ``LEFT ARRAY JOIN`` rather than
        ``ARRAY LEFT JOIN``. Measured: the outer form keeps a parent whose array
        is empty but fills the child with the type's **default** rather than
        NULL - `''` for a String - which is the same behaviour
        ``DataObjectJoin.required`` already documents for an unmatched row here.
        """
        prefix = "LEFT ARRAY JOIN" if node.outer else "ARRAY JOIN"
        return f"{prefix} {self.unnest_path(node)} AS {self.quote_identifier(node.alias)}"

    def exact_integer_sum(self, arg: Expr) -> Expr | None:
        """``SUM`` over Int64 accumulates in Int64 here, and wraps.

        The same overflow ``exact_integer_avg`` below already dodges inside its
        own rewrite, reached by the plainer road: measured, two rows of
        9000000000000000000 summed to -446744073709551616, a negative total
        from two positive rows, and ``sumWithOverflow`` returns the same. So
        does an outer ``CAST(SUM(x) AS Decimal(38, 0))``, because by then the
        accumulator has already wrapped. Casting the **argument** returns
        18000000000000000000.

        Every other engine either answers exactly or refuses; this is the only
        one that hands back a plausible wrong number, which is why it is the
        only override (#338).

        The result type moves with it, to ``PORTABLE_DECIMAL_PRECISION`` rather
        than this engine's own 76: a total the rewrite lets through should be
        one every supported engine could carry. That is decided by
        ``integer_sum_is_widened`` rather than returned here, so it holds
        inside a wrapper too, where no aggregate is visible to key on.
        """
        return FunctionCall(
            name="SUM", args=[FunctionCall(name="toDecimal128", args=[arg, Literal.number(0)])]
        )

    def exact_integer_avg(self, arg: Expr, obml_type: OBMLType) -> Expr | None:
        """ClickHouse needs its own function, and two guards around it.

        ``avg`` returns Float64, and ordinary ``/`` on Decimals is no help
        either - it preserves the operand scale and pre-scales the numerator,
        which is the overflow ``render_decimal_division_sql`` below exists to
        dodge. ``divideDecimal(a, b, scale)`` is exact, measured, and handles
        negatives correctly.

        The two guards are both cases where a plain ``AVG`` answers and a naive
        rewrite does not:

        **The cast goes inside the SUM.** ``SUM`` over Int64 accumulates in
        Int64 and wraps: two rows of 9000000000000000000 summed to
        -446744073709551616, so ``toDecimal128(SUM(x), 0)`` was widening a
        number that had already overflowed. ``SUM(toDecimal128(x, 0))``
        accumulates in 128 bits and returns 18000000000000000000.

        **An empty group is not a division.** ``divideDecimal`` by a count of
        zero raises ILLEGAL_DIVISION where ``AVG`` returns NULL, which a
        multi-fact plan hits routinely: a group carrying only another fact's
        rows has no values for this measure at all.
        """
        if not isinstance(obml_type, DecimalType):
            return None
        zero = Literal.number(0)
        count: Expr = FunctionCall(name="COUNT", args=[arg])
        quotient: Expr = FunctionCall(
            name="divideDecimal",
            args=[
                FunctionCall(
                    name="SUM", args=[FunctionCall(name="toDecimal128", args=[arg, zero])]
                ),
                FunctionCall(name="toDecimal128", args=[count, zero]),
                Literal.number(obml_type.scale),
            ],
        )
        return FunctionCall(
            name="if",
            args=[BinaryOp(left=count, op="=", right=zero), Literal.null(), quotient],
        )

    def render_obml_type(self, obml_type: OBMLType) -> str:
        if isinstance(obml_type, DecimalType):
            p = min(obml_type.precision, self._MAX_DECIMAL_PRECISION)
            s = min(obml_type.scale, p)
            return f"Decimal({p}, {s})"
        return self._OBML_SIMPLE_TYPE_MAP.get(obml_type.name, obml_type.name.upper())

    _ABSTRACT_TYPE_MAP: dict[str, str] = {
        "string": "String",
        "json": "String",
        "int": "Int64",
        "float": "Float64",
        "date": "Date",
        "time": "String",
        "time_tz": "String",
        "timestamp": "DateTime",
        "timestamp_tz": "DateTime",
        "boolean": "Bool",
    }

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """ClickHouse: two-part ``schema.code`` (OBML schema maps to CH database).

        An omitted schema collapses to the bare table rather than an empty
        quoted component, so the reference resolves against the connection's
        search path. ``database`` is not part of the name on this dialect, so
        setting it without a schema is not ambiguous here.
        """
        if not schema:
            return self.quote_identifier(code)
        return f"{self.quote_identifier(schema)}.{self.quote_identifier(code)}"

    backslash_escapes_strings = True

    @property
    def name(self) -> str:
        return "clickhouse"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=False,
            supports_arrays=True,
            supports_window_filters=False,
            supports_ilike=True,
            supports_group_by_all=True,
            # ClickHouse offers ``simpleLinearRegression(x, y)`` returning a
            # ``(k, b)`` tuple. Composing transparent ``REGR_SLOPE`` /
            # ``REGR_INTERCEPT`` would mean silently tuple-indexing — better
            # to reject and let the user opt in via a DERIVED metric.
            # ``measure`` is Databricks Metric View specific.
            unsupported_aggregations=["regr_slope", "regr_intercept", "measure"],
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        func_name = _GRAIN_FUNCTIONS.get(grain)
        if func_name:
            return FunctionCall(name=func_name, args=[column])
        return column

    def compile_group_by(self, group_by: list[Expr], grouping: str | None) -> str:
        """ClickHouse uses trailing-modifier form for ROLLUP / CUBE, not the
        ANSI ``GROUP BY ROLLUP(...)`` function form. Plain ``GROUP BY``
        (no modifier) delegates to the base implementation so the
        ``GROUP BY ALL`` capability flag applies uniformly.
        """
        if grouping == "rollup":
            groups = ", ".join(self.compile_expr(g) for g in group_by)
            return f"GROUP BY {groups} WITH ROLLUP"
        if grouping == "cube":
            groups = ", ".join(self.compile_expr(g) for g in group_by)
            return f"GROUP BY {groups} WITH CUBE"
        return super().compile_group_by(group_by, grouping)

    def _render_decimal_division(self, left_sql: str, right_sql: str) -> str:
        """Widen operands for raw-SQL decimal division — same fix as the
        BinaryOp override but applied where SQL is built as text (e.g.
        the PoP comparison CTE).

        ClickHouse stores ``Decimal(P, S)`` as an integer scaled by
        ``10^S``; division pre-scales the numerator by ``10^S`` again
        to preserve scale, so very wide scales overflow on values with
        ~10+ integer digits (we hit ``Decimal(38, 16)`` overflowing on
        ``$42M / 10k``). ``Decimal(38, 14)`` is the sweet spot —
        13 fractional digits in the result (enough for the 12-sig-fig
        cross-vendor comparison) and ``38 - 14 = 24`` integer digits
        of headroom (plenty for any aggregate in this corpus).
        """
        wide = "Nullable(Decimal(38, 14))"
        return f"CAST({left_sql} AS {wide}) / CAST({right_sql} AS {wide})"

    def _compile_binary_op(self, left: Expr, op: str, right: Expr) -> str:
        """Widen division operands so ratio precision survives.

        ClickHouse's Decimal arithmetic preserves the operand scale on
        ``/``: ``Decimal(18, 2) / Decimal(18, 2) = Decimal(18, 2)``,
        which truncates ratios to 2-dp (e.g. ``0.0365`` becomes
        ``0.03``). Other engines either widen automatically (Postgres,
        DuckDB) or use float division. To match the cross-engine
        contract OBSL promises — ratios at the metric's declared
        ``decimal(18, 4)`` precision — we cast both operands to
        ``Decimal(38, 10)`` before dividing. The outer measure CAST
        then narrows back to the declared type.

        Returns the SQL fragment *without* an outer ``(...)`` wrap —
        the dispatcher in ``compile_expr`` adds one only when the
        surrounding precedence requires it (v2.7.4 #79).
        """
        if op == "/":
            wide = "Nullable(Decimal(38, 14))"
            # CAST(...) is itself an atom from a precedence standpoint —
            # no risk of the children needing a higher parent prec here.
            l_sql = f"CAST({self.compile_expr(left)} AS {wide})"
            r_sql = f"CAST({self.compile_expr(right)} AS {wide})"
            # Guarded after widening, not before: ClickHouse is the engine that
            # raises on a zero decimal divisor, so the widening is exactly what
            # turns an inert 0 into ILLEGAL_DIVISION (#319).
            return f"{l_sql} / {self.guard_zero_divisor(right, r_sql)}"
        return super()._compile_binary_op(left, op, right)

    def _compile_cast(self, inner: Expr, type_name: str) -> str:
        """ClickHouse: wrap target type in ``Nullable(...)`` and round to
        the target Decimal scale before casting.

        Two ClickHouse-specific quirks the wrapping handles:

        * Base types are non-nullable by default; CFL UNION-ALL legs and
          outer aggregations over empty groups need ``Nullable(...)`` to
          accept ``NULL`` without raising.
        * ``CAST(x AS Decimal(P, S))`` *truncates* the input (e.g.
          ``CAST(4323.99 AS Decimal(18, 0)) → 4323``), which diverges
          from DuckDB / Postgres / MySQL whose decimal CAST rounds. To
          align cross-vendor rounding (and stay consistent with the
          metric's declared precision), pre-round the inner expression
          to the target scale before casting.
        """
        resolved_type = self._resolve_type_name(type_name)
        nullable = resolved_type
        if not nullable.startswith("Nullable("):
            nullable = f"Nullable({resolved_type})"
        inner_sql = self.compile_expr(inner)
        # Detect Decimal(P, S) targets and round to scale S first. A NULL
        # literal is exempt: rounding it is a no-op - measured, ``round(NULL,
        # 20)`` is NULL typed ``Nullable(Nothing)`` and casts cleanly - and
        # every CFL NULL pad carries a decimal type, so the rule as written put
        # ``round(NULL, 20)`` in every union leg for nothing.
        upper = resolved_type.upper()
        is_null_literal = isinstance(inner, Literal) and inner.value is None
        if not is_null_literal and (
            upper.startswith("DECIMAL") or upper.startswith("NULLABLE(DECIMAL")
        ):
            scale_token = resolved_type.split(",")[-1].rstrip(") ")
            scale = int(scale_token.strip())
            inner_sql = f"round({inner_sql}, {scale})"
        elif not is_null_literal and _INT_TYPE_RE.match(resolved_type):
            # An integer CAST saturates here where every other engine raises
            # (#356): CAST(3e9 AS Nullable(Int32)) is 2147483647, not an error.
            # ``accurateCast`` raises instead, but it also rejects any value
            # carrying a fraction, so the input is truncated first - which is
            # what the plain CAST already did to it, so no answer changes.
            # ``trunc`` preserves the argument's type (measured: UInt64 stays
            # UInt64, Decimal stays Decimal), so a COUNT above 2^53 is not
            # rounded through a float on the way.
            #
            # An integer literal is exempt, for the reason the NULL literal
            # above is: every CFL count pad is one, so the rule as written put
            # ``trunc(1)`` in every union leg for nothing. Only integers -
            # ``accurateCast(2.5, 'Int32')`` raises, so a fractional literal
            # still needs the truncation.
            if isinstance(inner, Literal) and _is_integer_literal(inner.value):
                return f"accurateCast({inner_sql}, '{nullable}')"
            return f"accurateCast(trunc({inner_sql}), '{nullable}')"
        return f"CAST({inner_sql} AS {nullable})"

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        # ClickHouse uses toType functions for common casts
        type_map: dict[str, str] = {
            "INT": "toInt64",
            "INTEGER": "toInt64",
            "FLOAT": "toFloat64",
            "STRING": "toString",
            "DATE": "toDate",
        }
        func_name = type_map.get(target_type.upper())
        if func_name:
            return FunctionCall(name=func_name, args=[expr])
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        return BinaryOp(
            left=column,
            op="ILIKE",
            right=BinaryOp(
                left=BinaryOp(left=Literal.string("%"), op="||", right=pattern),
                op="||",
                right=Literal.string("%"),
            ),
        )

    _FUNCTION_NAME_MAP: dict[str, str] = {
        "ANY_VALUE": "any",
        # Statistical aggregates: ClickHouse uses camelCase rather than the
        # SQL-standard underscore names. Mappings cover every supported
        # function in OBML's aggregation surface.
        "STDDEV": "stddevSamp",
        "STDDEV_SAMP": "stddevSamp",
        "STDDEV_POP": "stddevPop",
        "VARIANCE": "varSamp",
        "VAR_SAMP": "varSamp",
        "VAR_POP": "varPop",
        "CORR": "corr",
        "COVAR_POP": "covarPop",
        "COVAR_SAMP": "covarSamp",
    }

    def _map_function_name(self, name: str) -> str:
        return self._FUNCTION_NAME_MAP.get(name.upper(), name)

    # ``length`` counts bytes on ClickHouse (``length('äbcd')`` is 5), and
    # ``startsWith`` / ``endsWith`` are the camelCase-only spellings — unlike
    # ``substring`` or ``upper``, which have case-insensitive ANSI aliases.
    def _render_json_value(self, args: list[Expr]) -> str:
        """ClickHouse returns the empty string for an absent path, not NULL.

        Measured, not assumed. ``nullIf(..., '')`` restores the catalog's NULL
        rule for the common case, at the cost of not distinguishing an absent
        path from a genuine empty-string value: both come back NULL here.
        """
        doc = self.compile_expr(args[0])
        path = _json_path_of(args[1])
        return f"nullIf(JSON_VALUE({doc}, {self._quote_text(path)}), '')"

    _SCALAR_FUNCTION_NAMES: dict[str, str] = {
        "length": "lengthUTF8",
        "starts_with": "startsWith",
        "ends_with": "endsWith",
    }

    #: ClickHouse rounds ties to even for Float* and away from zero for
    #: Decimal*, so it takes the add-half-and-truncate shape too.
    _ROUND_TRUNCATE_FN = "truncate"

    #: Decimal256 carries 76 digits, wherever the point sits. At 76 places
    #: rounding is the identity and no half is needed, which is just as well:
    #: the half wants one place more than the count and 77 is out of range.
    _MAX_ROUND_DIGITS: int = 76

    def _coerce_text_argument(self, expr: Expr) -> Expr:
        """Read a ``FixedString`` by value rather than by storage.

        ClickHouse is the one engine here with a fixed-width string type, and
        it pads to that width with NUL bytes that then count as content. A
        ``FixedString(50)`` holding 'Books' answers 50 to ``lengthUTF8``, comes
        back from ``upper`` still carrying 45 NULs, makes ``endsWith(x, 'ks')``
        **false**, and raises outright from ``replaceAll`` and
        ``splitByString``. Measured, 13 of the catalog's 15 string functions
        disagree with the same value held as a ``String``.

        That matters because it is how a real schema arrives: TPC-DS types its
        CHAR columns as ``FixedString``, following ClickHouse's own published
        DDL, so a model over one gets the padded answers.

        ``toString`` is exact and cheap: it strips the padding, is the identity
        on a ``String``, and carries ``Nullable`` and NULL through unchanged.
        """
        # RawSQL: re-wraps SQL this dialect just rendered, so the coercion sits
        # outside whatever spelling the argument itself compiled to.
        return RawSQL(sql=f"toString({self.compile_expr(expr)})")

    def _round_half_sql(self, half: str) -> str:
        """A bare ``0.005`` is a Float64 here, unlike MySQL, and adding one to a
        Decimal turns it into a Float64 - which is exactly how 2.25.0 lost
        12345678901234567.885 to 1.2345678901234568e16. Typing the half instead
        makes ClickHouse's own promotion do the work: ``Decimal + Decimal`` is
        a Decimal and ``Float64 + Decimal`` is a Float64, so one expression
        preserves whichever type it is handed.

        Known limit, and there is no expression that avoids it. Adding a half
        of scale n+1 promotes a Decimal256 to Decimal(76, n+1), so a value with
        more than 76-(n+1) integer digits wraps - silently, returning a
        sign-flipped number rather than raising. Guarding it is not possible
        either: ClickHouse resolves the result type and converts before it
        evaluates any condition, so an ``if`` overflows in the branch it was
        meant to avoid, and ``toDecimal256`` wraps rather than raising while
        also drifting floats (measured, 1e19 converts to
        10000000000000000000.10 at scale 2).

        What bounds it is that the two conditions cannot both be interesting.
        Decimal256 holds 76 digits, so needing that many integer digits forces
        the scale to n or less, and a value whose scale is already n or less is
        unchanged by rounding to n places. Every value this corrupts is one it
        did not need to touch, which is pinned by
        ``test_clickhouse_round_agrees_with_native_wherever_rounding_matters``.
        """
        scale = len(half.split(".", 1)[1]) if "." in half else 0
        # toDecimal256, not toDecimal64: the latter caps at 18 fractional
        # digits and raises ARGUMENT_OUT_OF_BOUND past it. Quoted, so the
        # engine parses the decimal rather than reading a Float64 literal.
        return f"toDecimal256('{half}', {scale})"

    def _render_div(self, args: list[Expr]) -> str:
        """ClickHouse: ``intDiv`` truncates toward zero. Not ``a // b``, which
        ClickHouse reads as a line comment (``-7 // 2`` returns -7).
        """
        left = self.compile_expr(args[0])
        right = self.compile_expr(args[1])
        return f"intDiv({left}, {right})"

    def _render_log(self, args: list[Expr]) -> str:
        """ClickHouse has no two-argument ``log`` (its one-argument form is the
        natural logarithm), so the base change is written out.

        Via ``log10`` rather than ``ln``: ClickHouse's ``ln`` is a fast
        approximation, and ``ln(100) / ln(10)`` returns 1.9999999996784485
        where ``log10(100) / log10(10)`` returns exactly 2.
        """
        base = self.compile_expr(args[0])
        value = self.compile_expr(args[1])
        return self._render_infix(f"log10({value}) / log10({base})")

    def _render_extremum(self, name: str, args: list[Expr]) -> str:
        """ClickHouse's ``greatest`` / ``least`` skip NULL arguments; the
        catalog propagates NULL.
        """
        return self._render_null_guard(self._render_named_function(name, args), args)

    def _render_in_timezone(self, value: Expr, zone: str, from_zone: str | None) -> str:
        """ClickHouse: ``toTimeZone``. A naive ``DateTime`` carries the column's
        own zone, so *from_zone* is declared with ``toDateTime`` first.
        """
        rendered = self.compile_expr(value)
        if from_zone is not None:
            rendered = f"toDateTime({rendered}, {self._quote_zone(from_zone)})"
        return f"toTimeZone({rendered}, {self._quote_zone(zone)})"

    def _render_week_start_sunday(self, value: Expr) -> str:
        """ClickHouse: ``toStartOfWeek(x, 0)``, where mode 0 is a Sunday week."""
        return f"toStartOfWeek({self.compile_expr(value)}, 0)"

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """ClickHouse takes the unit as a keyword, not a string: ``date_add('day',
        …)`` is a type error where ``date_add(DAY, …)`` works.
        """
        return f"date_add({unit.upper()}, {self.compile_expr(count)}, {self.compile_expr(value)})"

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """ClickHouse: ``date_diff('unit', start, end)``, counting boundaries."""
        return f"date_diff('{unit}', {self.compile_expr(start)}, {self.compile_expr(end)})"

    def _render_split_part(self, args: list[Expr]) -> str:
        """ClickHouse has no ``split_part``; ``splitByString`` plus an array
        index is the equivalent, and its argument order is delimiter-first.

        Indexing past the end of a ``Array(String)`` yields ``''`` on
        ClickHouse, which is what the catalog documents for an out-of-range
        part.
        """
        haystack = self.compile_expr(args[0])
        delimiter = self.compile_expr(args[1])
        index = self.compile_expr(args[2])
        return f"splitByString({delimiter}, {haystack})[{index}]"

    def _compile_mode(self, args: list[Expr]) -> str:
        """ClickHouse: topK(1)(col)[1] — returns the most frequent value."""
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"topK(1)({col_sql})[1]"

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """ClickHouse: arrayStringConcat([arraySort](groupArray/groupUniqArray(col)), sep).

        ClickHouse does not support ORDER BY inside aggregate functions.
        Only self-ordering (sorting the aggregated column) is supported via arraySort.
        Cross-column ordering raises an error.
        """
        sep = separator if separator is not None else ","
        col_sql = self.compile_expr(args[0]) if args else "''"
        escaped_sep = self.quote_string_literal(sep)[1:-1]
        group_fn = "groupUniqArray" if distinct else "groupArray"
        inner = f"{group_fn}({col_sql})"
        if order_by:
            ob_expr = order_by[0]
            ob_sql = self.compile_expr(ob_expr.expr)
            if ob_sql != col_sql:
                raise CrossColumnOrderNotSupportedError("clickhouse", col_sql, ob_sql)
            sort_fn = "arrayReverseSort" if ob_expr.desc else "arraySort"
            inner = f"{sort_fn}({inner})"
        return f"arrayStringConcat({inner}, '{escaped_sep}')"

    def current_date_sql(self) -> str:
        return "today()"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        funcs: dict[str, str] = {
            "day": "addDays",
            "week": "addWeeks",
            "month": "addMonths",
            "quarter": "addQuarters",
            "year": "addYears",
        }
        func = funcs.get(unit)
        if func is None:
            raise ValueError(f"Unsupported unit '{unit}' for ClickHouse")
        return f"{func}({date_sql}, {count})"

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        grain_func_map: dict[str, str] = {
            "year": "toStartOfYear",
            "quarter": "toStartOfQuarter",
            "month": "toStartOfMonth",
            "week": "toMonday",
            "day": "toDate",
        }
        func = grain_func_map.get(grain, "toDate")
        return f"{func}({column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        diff_map: dict[str, str] = {
            "day": "day",
            "week": "week",
            "month": "month",
            "quarter": "quarter",
            "year": "year",
        }
        diff_grain = diff_map.get(grain, "day")
        add_grain_fn = {
            "day": "addDays",
            "week": "addWeeks",
            "month": "addMonths",
            "quarter": "addMonths",
            "year": "addYears",
        }
        add_fn = add_grain_fn.get(grain, "addDays")
        add_mul = ", n * 3)" if grain == "quarter" else ", n)"

        offset_fn = {
            "day": "addDays",
            "week": "addWeeks",
            "month": "addMonths",
            "quarter": "addMonths",
            "year": "addYears",
        }
        off_fn = offset_fn.get(offset_grain, "addDays")
        off_mul = f", {offset} * 3)" if offset_grain == "quarter" else f", {offset})"

        n_expr = f"dateDiff('{diff_grain}', {min_date}, {max_date})"
        if grain == "quarter":
            n_expr = f"intDiv(dateDiff('month', {min_date}, {max_date}), 3)"

        spine_date = f"{add_fn}({min_date}{add_mul}"
        prev_date = f"{off_fn}({spine_date}{off_mul}"

        return (
            f"SELECT {spine_date} AS spine_date,\n"
            f"       CASE WHEN {prev_date} >= {min_date}\n"
            f"            THEN {prev_date} END AS spine_date_prev\n"
            f"FROM (SELECT arrayJoin(range(0, toUInt32({n_expr}) + 1)) AS n)"
        )

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """ClickHouse uses ``match(col, pattern)``."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        result = f"match({col_sql}, {pat_sql})"
        return f"NOT {result}" if negated else result
