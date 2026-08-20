"""Abstract base dialect with capability flags and default SQL compilation."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from orionbelt.ast.nodes import (
    AliasedExpr,
    Between,
    BinaryOp,
    CaseExpr,
    Cast,
    ColumnRef,
    Except,
    Exists,
    Expr,
    From,
    FunctionCall,
    InList,
    InTimeZone,
    IsNull,
    Join,
    Literal,
    NestedField,
    OrderByItem,
    RawSQL,
    RegexMatch,
    RelativeDateRange,
    Select,
    Star,
    SubqueryExpr,
    UnaryOp,
    UnionAll,
    Unnest,
    WindowFunction,
)
from orionbelt.models.functions import JSON_PATH_RE, TIME_UNITS, lookup_function
from orionbelt.models.semantic import TimeGrain, WeekStart
from orionbelt.models.types import DecimalType, OBMLType


def _unit_of(arg: Expr) -> str:
    """The canonical time unit a literal argument names.

    Only called for a call ``compile_expr`` already checked with
    :func:`_is_unit_literal`, so the cast is safe.
    """
    assert isinstance(arg, Literal) and isinstance(arg.value, str)
    return arg.value.lower()


def _is_unit_literal(arg: Expr) -> bool:
    """Whether *arg* is a literal naming one of the catalog's time units."""
    return (
        isinstance(arg, Literal) and isinstance(arg.value, str) and arg.value.lower() in TIME_UNITS
    )


_JSON_SEGMENT_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]")


def _is_json_path_literal(arg: Expr) -> bool:
    """Whether *arg* is a literal holding a JSONPath the catalog accepts.

    The accepted subset is object member access and array subscripts rooted at
    ``$``. Filters and wildcards are excluded deliberately: the engines diverge
    on them, and a catalog entry has to pin one meaning.
    """
    return (
        isinstance(arg, Literal)
        and isinstance(arg.value, str)
        and JSON_PATH_RE.match(arg.value) is not None
    )


def _json_path_of(arg: Expr) -> str:
    """The JSONPath text a literal argument holds.

    Only called once :func:`_is_json_path_literal` holds, so the cast is safe.
    """
    assert isinstance(arg, Literal) and isinstance(arg.value, str)
    return arg.value


def _json_path_segments(path: str) -> list[tuple[str, bool]]:
    """``$.a.b[0]`` -> ``[("a", False), ("b", False), ("0", True)]``.

    Each segment carries whether it was an array subscript, because the engines
    that take a path apart also spell the two kinds differently. Snowflake wants
    ``a[0]`` and rejects ``a.0`` outright with "Invalid extraction path", so
    flattening the distinction away is a hard error there, not a wrong value.
    """
    return [
        (index, True) if index else (name, False) for name, index in _JSON_SEGMENT_RE.findall(path)
    ]


def _dremio_row_type(path: str, quote: Callable[[str], str]) -> str:
    """``$.a[0].b`` -> ``ROW("a" LIST(ROW("b" VARCHAR)))``.

    Dremio needs the shape declared up front rather than discovered, and the
    catalog's literal-path rule is what makes that possible: the type is built
    from the segments at compile time. Innermost is always VARCHAR, which is
    what turns a non-scalar into NULL under ``TRY_CONVERT_FROM``.

    Member names are **quoted**. Dremio puts them in identifier position, unlike
    every other dialect where the path rides inside a string literal, so a
    member named ``select`` or ``date`` is a parse error unquoted. Measured on a
    live container: ``ROW(select VARCHAR)`` fails with ``Encountered "select"
    ... Was expecting <IDENTIFIER>``, while the quoted form returns the value.
    Quoting a name that is not reserved is harmless.
    """
    rendered = "VARCHAR"
    for value, is_index in reversed(_json_path_segments(path)):
        rendered = f"LIST({rendered})" if is_index else f"ROW({quote(value)} {rendered})"
    return rendered


def _dremio_access(path: str, quote: Callable[[str], str]) -> str:
    """``$.a[0].b`` -> ``."a"[0]."b"``, the field walk over the converted row.

    Quoted for the same reason as the row type: these are identifiers.
    """
    return "".join(
        f"[{value}]" if is_index else f".{quote(value)}"
        for value, is_index in _json_path_segments(path)
    )


def _snowflake_path(path: str) -> str:
    """``$.a[0].b`` -> ``a[0].b``, Snowflake's extraction-path spelling."""
    out = ""
    for value, is_index in _json_path_segments(path):
        if is_index:
            out += f"[{value}]"
        else:
            out += value if not out else f".{value}"
    return out


class UnsupportedAggregationError(Exception):
    """Raised when a dialect does not support a specific aggregation function."""

    def __init__(self, dialect: str, aggregation: str) -> None:
        self.dialect = dialect
        self.aggregation = aggregation
        super().__init__(f"Dialect '{dialect}' does not support {aggregation.upper()} aggregation")


class CrossColumnOrderNotSupportedError(UnsupportedAggregationError):
    """Raised when a dialect can only order a LISTAGG by the column it aggregates.

    ClickHouse (``arraySort``) and Databricks (``sort_array``) sort the array of
    aggregated values, so ``ORDER BY`` on any other column cannot be expressed.
    A domain error rather than a bare ``ValueError`` so routers surface the same
    422 as every other unsupported-aggregation case instead of a 500.
    """

    def __init__(self, dialect: str, aggregated: str, order_by: str) -> None:
        self.dialect = dialect
        self.aggregation = "listagg"
        Exception.__init__(
            self,
            f"Dialect '{dialect}' can only order LISTAGG by the column it "
            f"aggregates (aggregated: {aggregated}, order by: {order_by}). "
            f"Order the measure by its own column, or query it on a dialect "
            f"that supports WITHIN GROUP ordering.",
        )


class UnsupportedNestedAccessError(Exception):
    """A dialect cannot unnest an array column in its FROM clause."""

    def __init__(self, dialect: str, alias: str, detail: str | None = None) -> None:
        super().__init__(
            f"Dialect '{dialect}' has no FROM-clause unnest, so data object "
            f"'{alias}' cannot take its rows from a parent's array column here. "
            + (
                detail
                or "Declare 'code' alongside 'nestedIn' to read a flattening view on this dialect."
            )
        )
        self.dialect = dialect
        self.alias = alias
        self.detail = detail


class UnsupportedFunctionError(Exception):
    """Raised when a dialect cannot render a catalog scalar function.

    The counterpart of :class:`UnsupportedAggregationError` for the portable
    function catalog (``models/functions.py``): a function the catalog admits
    but this engine has no equivalent for, listed in
    ``capabilities.unsupported_functions``. A domain error rather than a bare
    ``ValueError`` so routers surface a 422 like every other
    unsupported-feature case instead of a 500.
    """

    def __init__(self, dialect: str, function: str) -> None:
        self.dialect = dialect
        self.function = function
        super().__init__(
            f"Dialect '{dialect}' does not support the '{function}' function. "
            f"Rewrite the expression, or query it on a dialect that has it."
        )


class UnsupportedGroupingError(Exception):
    """Raised when a dialect does not support a specific grouping modifier
    (``CUBE`` / ``ROLLUP`` / ``GROUPING SETS``). Routers translate this to
    a 422 with the dialect + grouping in the response body; without this
    domain error the underlying ``NotImplementedError`` would surface as a
    500.
    """

    def __init__(self, dialect: str, grouping: str) -> None:
        self.dialect = dialect
        self.grouping = grouping
        super().__init__(f"Dialect '{dialect}' does not support GROUP BY {grouping.upper()}")


class AmbiguousTableReferenceError(Exception):
    """Raised when a table reference cannot be qualified unambiguously.

    On a three-part dialect a data object with a ``database`` but no ``schema``
    has no correct rendering. Emitting two parts would be read as
    ``schema.table`` (Snowflake, Databricks) or ``dataset.table`` (BigQuery),
    silently pointing at a different namespace than the model names; emitting
    three with an empty middle is not valid syntax. Only Snowflake offers
    ``db..table`` for "the default schema", and that is not portable.

    A domain error rather than a bare ``ValueError`` so routers surface a 422,
    matching every other unsupported-model case, instead of a 500.
    """

    def __init__(self, dialect: str, database: str, code: str) -> None:
        self.dialect = dialect
        self.database = database
        self.code = code
        super().__init__(
            f"Data object '{code}' sets database '{database}' but no schema, which "
            f"dialect '{dialect}' cannot qualify: a two-part name would be read as "
            f"schema.table, not database.table. Set 'schema' on the data object, or "
            f"drop 'database' and let the connection's current database apply."
        )


@dataclass
class DialectCapabilities:
    """Flags indicating what SQL features a dialect supports."""

    supports_cte: bool = True
    supports_qualify: bool = False
    supports_arrays: bool = False
    supports_window_filters: bool = False
    supports_ilike: bool = False
    supports_time_travel: bool = False
    supports_semi_structured: bool = False
    supports_union_all_by_name: bool = False
    # ``GROUP BY ALL`` (Snowflake 2022+, Databricks/Spark 3.4+, DuckDB 0.7+,
    # BigQuery, ClickHouse 22.6+) auto-derives the grouping list from the
    # SELECT clause. Functionally equivalent to the explicit list but much
    # shorter on queries with computed dimensions, where the explicit form
    # repeats the full expression. Postgres, MySQL, Dremio do not support it.
    supports_group_by_all: bool = False
    # A FROM-clause unnest of an array column. True everywhere but Dremio,
    # whose ``FLATTEN`` is a projection function and needs a derived table
    # rather than an extension of the FROM clause. The planner reads this to
    # decide between the unnest and a nested object's ``code`` fallback,
    # because that choice has to be made while the plan is built rather than
    # when it is rendered.
    supports_from_unnest: bool = True
    unsupported_aggregations: list[str] = field(default_factory=list)
    # Canonical names from the portable function catalog
    # (``models/functions.py``) this engine has no equivalent for. Empty for
    # every dialect today — the string group renders on all eight — but the
    # catalog admits a function on the strength of the majority, so a later
    # group can leave one engine behind without dropping the entry.
    unsupported_functions: list[str] = field(default_factory=list)


#: The widest decimal precision every supported engine accepts. Where a dialect
#: has to choose a width of its own - widening a wrapping accumulator (#338), or
#: a cast that would otherwise saturate (#336) - this is the ceiling, so that
#: a value one engine now returns is one a portable model could have carried.
PORTABLE_DECIMAL_PRECISION = 38


class Dialect(ABC):
    """Abstract base for all SQL dialects.

    Provides default SQL compilation; dialects override specific methods.
    """

    _ABSTRACT_TYPE_MAP: dict[str, str] = {
        "string": "VARCHAR",
        "json": "VARCHAR",
        "int": "INTEGER",
        "float": "FLOAT",
        "date": "DATE",
        "time": "TIME",
        "time_tz": "TIME",
        "timestamp": "TIMESTAMP",
        "timestamp_tz": "TIMESTAMP",
        "boolean": "BOOLEAN",
    }

    _MAX_DECIMAL_PRECISION: int = 38

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "BIGINT",
        "integer": "INTEGER",
        "double": "DOUBLE",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "time": "TIME",
        "string": "VARCHAR",
        "boolean": "BOOLEAN",
    }

    @property
    def max_decimal_precision(self) -> int:
        """The widest decimal precision this engine accepts.

        Public because the CFL union alignment has to reason about it from
        outside the dialect layer (#339).
        """
        return self._MAX_DECIMAL_PRECISION

    def render_obml_type(self, obml_type: OBMLType) -> str:
        """Render an OBMLType to a dialect-specific SQL type string.

        Handles precision clamping for decimal types.
        """
        if isinstance(obml_type, DecimalType):
            p = min(obml_type.precision, self._MAX_DECIMAL_PRECISION)
            s = min(obml_type.scale, p)
            return f"DECIMAL({p}, {s})"
        return self._OBML_SIMPLE_TYPE_MAP.get(obml_type.name, obml_type.name.upper())

    def cast_to_obml_type(self, expr: Expr, obml_type: OBMLType) -> Expr:
        """Build an Expr that coerces ``expr`` to the given OBML type.

        Default form is a plain ``CAST(expr AS <type>)``. Dialects whose
        ``CAST`` doesn't accept a parameterized decimal (notably BigQuery
        — "Parameterized types are not allowed in CAST expressions") can
        override to wrap the cast with a ROUND to honour the user-specified
        scale.
        """
        return Cast(expr=expr, type_name=self.render_obml_type(obml_type))

    # Widest decimal every supported engine accepts, and the integer digits kept
    # free for a running total. A sum is as many digits as its rows make it, so
    # it needs room the average it divides down to does not.
    _MAX_DECIMAL_PRECISION = 38
    _SUM_HEADROOM_DIGITS = 24

    def _exact_avg_by_sum_over_count(self, arg: Expr, obml_type: OBMLType) -> Expr | None:
        """``SUM(x) / COUNT(x)`` in decimal, for engines that divide exactly.

        The textbook rewrite, shared by Dremio and Databricks. It is *not*
        available on DuckDB, where every division returns DOUBLE whatever the
        operands, which is why that engine has no exact route at all (#316).

        **The cast goes inside the SUM.** ``SUM`` over a 64-bit column
        accumulates in 64 bits, and casting afterwards only widens a number
        that has already wrapped: two rows of 9000000000000000000 summed to
        -446744073709551616 on Dremio, silently, and raise ARITHMETIC_OVERFLOW
        on Databricks. Both are fixed by widening the argument first.

        The running total then needs integer room the average will not, so the
        scale is capped to leave ``_SUM_HEADROOM_DIGITS`` for the integer part:
        38 digits cannot hold both a large total and a long fraction. A result
        asking for more scale gets the extra places as zeros, which is the
        honest trade against overflowing on a total the source holds legally.

        An empty group divides to NULL on both engines, measured, so no
        zero-count guard is needed here - unlike ClickHouse, whose
        ``divideDecimal`` raises.
        """
        if not isinstance(obml_type, DecimalType):
            return None
        scale = min(obml_type.scale, self._MAX_DECIMAL_PRECISION - self._SUM_HEADROOM_DIGITS)
        accumulated = FunctionCall(
            name="SUM",
            args=[
                Cast(
                    expr=arg,
                    type_name=self.render_obml_type(
                        DecimalType(precision=self._MAX_DECIMAL_PRECISION, scale=0)
                    ),
                )
            ],
        )
        return BinaryOp(
            left=Cast(
                expr=accumulated,
                type_name=self.render_obml_type(
                    DecimalType(precision=self._MAX_DECIMAL_PRECISION, scale=scale)
                ),
            ),
            op="/",
            right=FunctionCall(name="COUNT", args=[arg]),
        )

    #: Whether a UNION column whose legs carry different numeric types
    #: resolves to a common type here.
    #:
    #: True everywhere but ClickHouse, and measured rather than assumed: a
    #: ``numeric(38, 20)`` NULL pad beside an uncast ``numeric`` column
    #: resolves to plain ``numeric`` on Postgres and carries a 21-integer-digit
    #: value through intact, and DuckDB widens to accommodate the leg the same
    #: way. ClickHouse instead builds ``Variant(Decimal(38, 20), Float64)`` and
    #: refuses to ``SUM`` it with ILLEGAL_TYPE_OF_ARGUMENT.
    #:
    #: That difference decides whether a CFL leg has to cast the measure it
    #: owns. Where the engine unifies, casting can only lose - it rounded
    #: pre-aggregation rows (#305) and then overflowed a value the source held
    #: legally (#311) - so the leg is left alone. Where it does not, one type
    #: per union column has to be spelled out (#339).
    unions_resolve_leg_types: bool = True

    #: Whether ``AVG`` over a 64-bit integer column is exact natively.
    #:
    #: True on Postgres (``numeric``), MySQL (``decimal``) and Snowflake, which
    #: need no rewrite - but still need the **result type** widened, because an
    #: exact average the declared type cannot hold is no better than an
    #: inexact one. Measured on MySQL, ``CAST(AVG(qty) AS DECIMAL(18, 2))``
    #: returns 9999999999999999.99 for a true 1000000000000000003, saturating
    #: silently with no warning at all; Postgres raises instead.
    #:
    #: False where the aggregate itself drifts. Those either get an exact
    #: rewrite (:meth:`exact_integer_avg`) or, on DuckDB alone, keep the
    #: default so the overflow stays loud rather than becoming a quiet wrong
    #: number (#316).
    avg_over_integers_is_exact: bool = False

    def integer_avg_is_exact(self) -> bool:
        """Whether an integer ``AVG`` ends up exact here, natively or rewritten.

        Deliberately independent of any expression. The **type** a measure is
        cast to has to be decided the same way wherever the cast happens, and
        by the time a wrapper composes - a window over a period-over-period,
        say - the expression it holds is a CTE alias rather than the aggregate.
        Asking "is this a bare AVG I can rewrite?" answers no there, and the
        cast fell back to the narrow default even though the value inside the
        CTE had already been computed exactly.

        Detected by introspection rather than a second flag, so a dialect that
        overrides :meth:`exact_integer_avg` cannot forget to declare it.
        """
        return self.avg_over_integers_is_exact or (
            type(self).exact_integer_avg is not Dialect.exact_integer_avg
        )

    def exact_integer_avg(self, arg: Expr, obml_type: OBMLType) -> Expr | None:
        """An exact ``AVG(arg)`` over an integer column, or ``None`` for none.

        ``AVG`` is a floating-point aggregate on several engines whatever the
        input type, so it drifts once the average passes a ``double`` mantissa,
        around fifteen significant digits. That is not a defect any of them is
        likely to change - duckdb/duckdb#6829 was closed as not planned - and
        no output cast repairs it, because the loss is already inside the
        aggregate.

        Dialects that offer exact arithmetic override this to say how. The
        three that do are all different: BigQuery only needs its **input** cast
        to NUMERIC, Dremio divides decimals exactly so ``SUM``/``COUNT``
        works, and ClickHouse needs its own ``divideDecimal``. Returning
        ``None`` - the default - keeps the plain ``AVG``, which is right both
        for the engines that are already exact (Postgres, MySQL, Snowflake) and
        for DuckDB, where no formulation is exact and a widened result would
        only convert a loud overflow into a quiet wrong number (#316).

        ``obml_type`` is the type the result will be cast to, already widened
        to hold a 64-bit integer part, and carries the scale an engine needs
        when it wants one explicitly.
        """
        return None

    def _sum_over_widened_argument(self, arg: Expr) -> Expr:
        """``SUM(CAST(arg AS DECIMAL(38, 0)))``.

        The plain form of the same move :meth:`_exact_avg_by_sum_over_count`
        makes, for the engines whose only problem is the accumulator. Dremio
        uses it; ClickHouse spells the widening ``toDecimal128`` and overrides.
        """
        widened = Cast(
            expr=arg,
            type_name=self.render_obml_type(
                DecimalType(precision=PORTABLE_DECIMAL_PRECISION, scale=0)
            ),
        )
        return FunctionCall(name="SUM", args=[widened])

    def integer_sum_is_widened(self) -> bool:
        """Whether an integer ``SUM`` is rewritten to a wider accumulator here.

        Deliberately independent of any expression, for the same reason
        :meth:`integer_avg_is_exact` is. The **type** such a measure is cast to
        has to be decided the same way wherever the cast happens, and by the
        time a wrapper composes - a cumulative over a period-over-period, say -
        what it holds is a CTE alias rather than the aggregate. Asking "is this
        a bare SUM I can rewrite?" answers no there, and the cast fell back to
        the inferred ``bigint``, narrowing an exact 128-bit total straight back
        into the 64 bits the rewrite existed to escape.

        Detected by introspection rather than a second flag, so a dialect that
        overrides :meth:`exact_integer_sum` cannot forget to declare it.
        """
        return type(self).exact_integer_sum is not Dialect.exact_integer_sum

    def exact_integer_sum(self, arg: Expr) -> Expr | None:
        """An exact ``SUM(arg)`` over an integer column, and its type.

        ``None`` - the default - keeps the plain ``SUM``, which is right for
        every engine that either computes the total exactly or refuses it. Most
        do one or the other: measured on two rows of 9000000000000000000,
        DuckDB, Postgres, BigQuery and Databricks raise, and Snowflake returns
        18000000000000000000 intact.

        A dialect overrides this where its accumulator **wraps** instead. That
        is the one outcome no output type can repair, for the same reason
        :meth:`exact_integer_avg` exists: the loss is inside the aggregate, and
        a cast only widens a number that has already gone wrong. Measured on
        ClickHouse, ``SUM`` over Int64 returns -446744073709551616 for that
        pair, and casting the result to ``Decimal(38, 0)`` returns it
        unchanged, while casting the **argument** returns the true total.

        Returns the expression only. An integer ``SUM`` infers ``bigint``
        (#315), which would cast the exact 128-bit total straight back into the
        64 bits the rewrite escaped, so the result type has to move too - but
        it moves through :meth:`integer_sum_is_widened`, which answers without
        looking at an expression and so still answers inside a wrapper.

        Takes no ``obml_type``, unlike its ``AVG`` counterpart: an average
        needs a scale to divide to, and a sum of integers has no fractional
        part to declare one for.
        """
        return None

    #: Whether a backslash escapes the next character inside a string literal.
    #:
    #: False is the SQL standard: a backslash is an ordinary character and a
    #: quote is escaped by doubling it. True on MySQL, ClickHouse, BigQuery,
    #: Snowflake and Databricks, where a backslash starts an escape sequence and
    #: has to be doubled itself.
    #:
    #: Measured on all seven reachable engines, and each convention is *wrong*
    #: on the other side rather than merely unnecessary: doubling a quote breaks
    #: on BigQuery, which reads ``'it''s'`` as two concatenated literals and
    #: raises, and on Databricks, which silently returns ``its``. Backslash
    #: escaping breaks on Postgres and DuckDB, which take the backslash
    #: literally and would double it.
    backslash_escapes_strings: bool = False

    def quote_string_literal(self, value: str) -> str:
        """*value* as a quoted string literal for this engine.

        The single place a string becomes SQL text, so a filter value, a
        LISTAGG separator and a time-zone name cannot disagree about escaping.
        They did: every one of them doubled the quote and left the backslash
        alone, which is right on two engines out of seven.

        Measured, with the old rendering: ``a\\b`` came back as ``a\x08`` - a
        backspace - on MySQL, ClickHouse, BigQuery, Snowflake and Databricks,
        and ``C:\\temp\\x`` raised on three of them. A Windows path, a regex or
        an escaped delimiter in a filter was silently wrong on five engines.
        """
        if self.backslash_escapes_strings:
            escaped = (
                value.replace("\\", "\\\\")
                .replace("'", "\\'")
                # A quoted string cannot span lines on BigQuery: a real newline
                # or carriage return closes it, and the query fails with
                # "Unclosed string literal". Measured, it is the only engine of
                # the seven that minds - the other six take a raw newline, tab,
                # form feed or control byte and hand it back unchanged. Written
                # as escapes for all five backslash dialects rather than only
                # BigQuery, because in this convention that is simply how a
                # control character is spelled, and all five read it back.
                .replace("\n", "\\n")
                .replace("\r", "\\r")
            )
        else:
            # Standard SQL has no escape sequences here, so a control character
            # rides through literally. Measured working on Postgres, DuckDB and
            # Dremio, including a newline: a quoted string may span lines.
            escaped = value.replace("'", "''")
        return f"'{escaped}'"

    def _resolve_type_name(self, type_name: str) -> str:
        """Map an abstract type name to a dialect-specific SQL type.

        Looks up ``_ABSTRACT_TYPE_MAP`` first; if *type_name* is not found
        (e.g. already a concrete SQL type like ``VARCHAR``), returns it as-is.
        """
        return self._ABSTRACT_TYPE_MAP.get(type_name, type_name)

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """Format a fully-qualified table reference.

        Default: three-part ``database.schema.code`` (Snowflake/Databricks/Dremio).
        Postgres and ClickHouse override to two-part naming.
        All components are quoted to prevent SQL injection.

        An omitted component is dropped rather than emitted as an empty
        identifier. ``database`` is optional in OBML, and quoting it anyway
        produced ``""."schema"."table"``, which Snowflake rejects with
        ``Database '""' does not exist``. Leaving it out lets the reference
        resolve against the connection's current database, which is how a
        single model serves several deployments of the same schema.
        """
        if database and not schema:
            raise AmbiguousTableReferenceError(self.name, database, code)
        parts = [database, schema, code]
        return ".".join(self.quote_identifier(p) for p in parts if p)

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> DialectCapabilities: ...

    @abstractmethod
    def quote_identifier(self, name: str) -> str:
        """Quote an identifier per dialect rules."""

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        """Wrap a column expression for the given time grain.

        A week is routed through the model's calendar rather than the dialect's
        own weekly truncation, so a ``timeGrain: week`` dimension, a weekly
        period-over-period and an explicit ``date_trunc('week', …)`` all bucket
        the same rows the same way. Left to the dialects, they did not: BigQuery
        hard-coded ISOWEEK, ClickHouse ``toMonday``, MySQL a ``%Y-%u`` label,
        and Snowflake a ``DATE_TRUNC('week')`` that follows its WEEK_START
        session parameter.
        """
        if grain is TimeGrain.WEEK:
            # RawSQL: re-wraps SQL this dialect just rendered, so the weekly
            # floor has one implementation rather than one per entry point.
            return RawSQL(sql=self._render_week_floor(column))
        return self._render_time_grain(column, grain)

    @abstractmethod
    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        """Wrap a column expression for a grain other than a week."""

    def render_unnest(self, node: Unnest) -> str:
        """A FROM-clause fragment that unnests a parent's array column.

        The default is the comma-lateral every engine but four accepts::

            , UNNEST(`c`.`labels`) AS `l`

        with the outer form spelled as a ``LEFT JOIN ... ON TRUE``, which keeps
        a parent row whose array is empty. Measured on BigQuery, DuckDB and
        Postgres; ClickHouse, Databricks, MySQL and Snowflake override.

        Dremio has no FROM-clause form at all - ``FLATTEN`` is a projection
        function, so the unnest goes in the SELECT list of a derived table -
        and refuses here rather than emitting something that will not parse.
        """
        source = f"UNNEST({self.unnest_path(node)})"
        alias = self.quote_identifier(node.alias)
        if node.outer:
            return f"LEFT JOIN {source} AS {alias} ON TRUE"
        return f", {source} AS {alias}"

    def nested_field(self, alias: str, field: str, sql_type: str | None = None) -> Expr:
        """How a column of an unnested element is addressed.

        Ordinary column access almost everywhere: measured, ``L."Key"`` reads
        the field on BigQuery, DuckDB, Postgres, MySQL, ClickHouse and
        Databricks, because the alias *is* the element. Snowflake overrides,
        because there the alias is a row whose ``value`` holds the element as a
        VARIANT.

        ``sql_type`` is what the field should be read as. Only the VARIANT
        dialect needs it; the rest carry their own types.
        """
        return ColumnRef(name=field, table=alias)

    def render_nested_field(self, node: NestedField) -> Expr:
        """How a nested object's column is addressed in a plan this dialect built.

        Two different things, depending on which source the planner chose. Where
        the FROM clause carries an unnest, this is a field of the element -
        :meth:`nested_field`. Where it cannot, the planner read the object's
        ``code`` fallback instead and put that table in FROM under the same
        alias, so the column is an ordinary one and reading it as an element
        field would name something that does not exist.
        """
        if not self.capabilities.supports_from_unnest:
            return ColumnRef(name=node.field, table=node.alias)
        return self.nested_field(
            node.alias, node.field, self.nested_column_type(node.abstract_type)
        )

    def nested_column_type(self, abstract_type: str | None) -> str:
        """The SQL type a field of an unnested element is read as.

        Two dialects need one and the other five ignore it: MySQL's
        ``JSON_TABLE`` declares the shape it extracts rather than inferring it,
        and Snowflake's VARIANT path has to be cast or a string field comes back
        with its JSON quotes still on. Both are served by the abstract type map
        every other cast already goes through, so a nested column is typed the
        same way an ordinary one is.
        """
        return self._resolve_type_name(abstract_type or "string")

    def unnest_path(self, node: Unnest) -> str:
        """The parent's array column, quoted segment by segment.

        A dotted ``column`` addresses an array inside a struct, and each segment
        is an identifier in its own right: ``x_Project.Ancestors`` becomes two
        quoted identifiers joined by a dot, rather than one quoted string
        containing a dot, which would name a column that does not exist.

        The dotted chain is the majority form, measured on DuckDB, BigQuery,
        Databricks and ClickHouse. Three engines cannot read it and override:
        Postgres needs the composite parenthesised, Snowflake needs a VARIANT
        ``:`` path, and MySQL has to move the member into the JSON path
        entirely - see :meth:`MySQLDialect.render_unnest`.
        """
        parts = [node.parent_alias, *node.column.split(".")]
        return ".".join(self.quote_identifier(p) for p in parts)

    @abstractmethod
    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        """Render a CAST expression."""

    @abstractmethod
    def current_date_sql(self) -> str:
        """Return SQL for the current date."""

    @abstractmethod
    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        """Return SQL that adds count units to date_sql."""

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        """Truncate a date/timestamp to the given grain, as a SQL string.

        String-level helper (not AST) for use in raw SQL CTEs like date_range
        and the period-over-period spine. A week goes through the model's
        calendar for the same reason it does in ``render_time_grain``: a weekly
        PoP and a weekly dimension have to agree on where a week starts.
        """
        if grain == TimeGrain.WEEK.value:
            # RawSQL: the caller already has SQL text, and the floor is defined
            # over expressions.
            return self._render_week_floor(RawSQL(sql=column_sql))
        return self._render_date_trunc_sql(column_sql, grain)

    @abstractmethod
    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        """Truncate to a grain other than a week, as a SQL string."""

    @abstractmethod
    def render_date_spine_cte_sql(
        self,
        min_date: str,
        max_date: str,
        grain: str,
        offset: int,
        offset_grain: str,
    ) -> str:
        """Return the SQL body for a date spine CTE.

        Must produce two columns: ``spine_date`` and ``spine_date_prev``.
        ``spine_date_prev`` is NULL when the offset date falls before min_date.

        Parameters
        ----------
        min_date : str
            SQL expression referencing the minimum date (e.g. ``date_range.min_date``).
        max_date : str
            SQL expression referencing the maximum date.
        grain : str
            Time grain string: ``day``, ``week``, ``month``, ``quarter``, ``year``.
        offset : int
            Signed period offset (e.g. ``-1`` for previous period).
        offset_grain : str
            Grain of the offset (e.g. ``year`` for YoY).
        """

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        """Default: column LIKE '%' || pattern || '%'."""
        return BinaryOp(
            left=column,
            op="LIKE",
            right=BinaryOp(
                left=BinaryOp(left=Literal.string("%"), op="||", right=pattern),
                op="||",
                right=Literal.string("%"),
            ),
        )

    def _map_function_name(self, name: str) -> str:
        """Map a function name to the dialect-specific equivalent.

        Override in subclasses to remap names (e.g. ANY_VALUE → any in ClickHouse).
        """
        return name

    # Canonical catalog name → this dialect's spelling, for entries whose only
    # difference from the ANSI default is the name (ClickHouse ``lengthUTF8``,
    # Snowflake ``STARTSWITH``). An entry whose *shape* differs — argument
    # order, an operator instead of a call — overrides the matching
    # ``_render_<name>`` method instead.
    _SCALAR_FUNCTION_NAMES: dict[str, str] = {}

    def _check_function_supported(self, name: str) -> None:
        """Raise ``UnsupportedFunctionError`` when this dialect has no
        equivalent for the catalog function *name* (lowercase canonical).
        """
        if name in {f.lower() for f in self.capabilities.unsupported_functions}:
            raise UnsupportedFunctionError(self.name, name)

    def _render_function(self, name: str, args: list[Expr]) -> str:
        """Render a call to a portable-catalog scalar function.

        *name* is the canonical lowercase catalog name (``models/functions.py``)
        and *args* are already in canonical order with an arity the entry
        accepts — ``compile_expr`` only routes a call here once
        :meth:`FunctionSpec.accepts` holds, so each renderer can index its
        arguments directly.

        The signature takes arguments rather than just a name because a
        portable catalog needs more than a rename table: ``position(needle,
        haystack)`` is ``POSITION(needle IN haystack)`` here and
        ``STRPOS(haystack, needle)`` on BigQuery, and ``concat`` has to become
        an operator chain on the engines whose ``CONCAT`` skips NULLs. Renaming
        is the trivial case, handled by ``_SCALAR_FUNCTION_NAMES``.
        """
        self._check_function_supported(name)
        match name:
            case "concat":
                return self._render_concat(args)
            case "length":
                return self._render_length(args)
            case "position":
                return self._render_position(args)
            case "split_part":
                return self._render_split_part(args)
            case "starts_with":
                return self._render_starts_with(args)
            case "ends_with":
                return self._render_ends_with(args)
            case "round":
                return self._render_round(args)
            case "trunc":
                return self._render_trunc(args)
            case "div":
                return self._render_div(self._with_guarded_divisor(args))
            case "log":
                return self._guard_log_domain(args)
            case "greatest" | "least":
                return self._render_extremum(name, args)
            case "date_trunc":
                unit = _unit_of(args[0])
                if unit == "week":
                    return self._render_week_floor(args[1])
                return self._render_date_trunc(unit, args[1])
            case "date_add":
                return self._render_date_add(_unit_of(args[0]), args[1], args[2])
            case "date_diff":
                unit = _unit_of(args[0])
                if unit == "week":
                    return self._render_week_diff(args[1], args[2])
                return self._render_date_diff(unit, args[1], args[2])
            case "extract":
                return self._render_extract(_unit_of(args[0]), args[1])
            case "last_day":
                return self._render_last_day(args[0])
            case "current_date":
                return self._render_current_date()
            case "json_value":
                return self._render_json_value(args)
            case _:
                return self._render_named_function(name, args)

    def _render_json_value(self, args: list[Expr]) -> str:
        """Default: ANSI ``JSON_VALUE(x, path)``, taking the path verbatim.

        Correct as measured on BigQuery. ClickHouse accepts the same spelling
        but returns the empty string rather than NULL for an absent path, and
        DuckDB's ``JSON_VALUE`` leaves the result quoted, so both override, as
        do Postgres, Snowflake, Databricks and MySQL, which have no
        ``JSON_VALUE`` at all.
        """
        doc = self.compile_expr(args[0])
        path = _json_path_of(args[1])
        return f"JSON_VALUE({doc}, {self._quote_text(path)})"

    def _quote_text(self, text: str) -> str:
        """A string literal carrying *text*, escaped for this dialect."""
        return self.compile_expr(Literal.string(text))

    def _render_named_function(self, name: str, args: list[Expr]) -> str:
        """Render ``NAME(arg, ...)`` using this dialect's spelling of *name*."""
        sql_name = self._SCALAR_FUNCTION_NAMES.get(name, name.upper())
        rendered = ", ".join(self.compile_expr(a) for a in args)
        return f"{sql_name}({rendered})"

    def _render_concat(self, args: list[Expr]) -> str:
        """Default: native ``CONCAT``, which propagates NULL on ClickHouse,
        MySQL, Snowflake, BigQuery and Databricks — the catalog's rule.
        DuckDB, Postgres and Dremio skip NULL arguments and override.
        """
        return self._render_named_function("concat", args)

    def _render_infix(self, sql: str) -> str:
        """Parenthesise a rewrite that emits an infix operator.

        ``compile_expr`` hands a ``FunctionCall``'s rendering straight to the
        surrounding expression and treats it as an atom, so a renderer that
        expands a call into ``a * b`` or ``a / b`` has to bracket itself or the
        surrounding operators bind into it: ``10 / trunc(2.5)`` on Databricks
        would compile to ``10 / SIGN(2.5) * FLOOR(ABS(2.5))``, which is 20
        rather than 5, and ``10 / log(2, 8)`` on Dremio to
        ``10 / LOG10(8) / LOG10(2)``.

        A call that stays a call needs nothing; this is only for the rewrites
        that do not.
        """
        return f"({sql})"

    def _render_concat_operator_chain(self, args: list[Expr]) -> str:
        """``(a || b || ...)`` — the NULL-propagating form on engines whose
        ``CONCAT`` skips NULLs but whose ``||`` does not.

        Operands are rendered one level above ``||``'s own precedence so a
        child that binds equally loosely keeps its parens: Postgres reads
        ``'x' || a - b`` as ``('x' || a) - b``. The chain itself is wrapped
        because ``compile_expr`` treats a function call as an atom and gives
        the result no parens of its own.
        """
        chain = " || ".join(self.compile_expr(a, _parent_prec=self._PREC_ADD + 1) for a in args)
        return f"({chain})"

    def _render_null_guard(self, inner_sql: str, args: list[Expr]) -> str:
        """``CASE WHEN a IS NULL OR ... THEN NULL ELSE <inner_sql> END``.

        The portable way to make a NULL-skipping function propagate NULL: used
        by ``concat`` on Dremio and by ``greatest`` / ``least`` on the four
        engines that skip. Verbose, but it does not depend on the engine having
        a NULL-aware alternative, and it is type-agnostic where a sentinel
        value (a negative infinity for a numeric ``greatest``) would not be.
        """
        guards = " OR ".join(
            f"{self.compile_expr(a, _parent_prec=self._PREC_CMP)} IS NULL" for a in args
        )
        return f"CASE WHEN {guards} THEN NULL ELSE {inner_sql} END"

    def _render_concat_null_guard(self, args: list[Expr]) -> str:
        """``concat`` for an engine whose ``CONCAT`` skips NULL arguments and
        whose ``||`` cannot be shown to behave differently.
        """
        return self._render_null_guard(self._render_named_function("concat", args), args)

    def _render_length(self, args: list[Expr]) -> str:
        """Default: ``LENGTH``, which counts characters everywhere except
        ClickHouse and MySQL — both of which override.
        """
        return self._render_named_function("length", args)

    def _render_position(self, args: list[Expr]) -> str:
        """Default: ANSI ``POSITION(needle IN haystack)``.

        Accepted by DuckDB, Postgres, ClickHouse, MySQL, Snowflake, Databricks
        and Dremio; BigQuery has no ``POSITION`` at all and overrides.
        """
        needle = self.compile_expr(args[0])
        haystack = self.compile_expr(args[1])
        return f"POSITION({needle} IN {haystack})"

    def _render_split_part(self, args: list[Expr]) -> str:
        """Default: native ``SPLIT_PART(x, delim, n)``."""
        return self._render_named_function("split_part", args)

    def _render_starts_with(self, args: list[Expr]) -> str:
        """Default: native ``STARTS_WITH(x, prefix)``."""
        return self._render_named_function("starts_with", args)

    def _render_ends_with(self, args: list[Expr]) -> str:
        """Default: native ``ENDS_WITH(x, suffix)``."""
        return self._render_named_function("ends_with", args)

    #: Fractional digits the decimal cast keeps when the caller does not say.
    #: 18 covers a float's own precision with room to spare, and is low enough
    #: that a Float-to-Decimal conversion still lands on the value it was given
    #: (see :meth:`ClickHouseDialect._round_decimal_cast`).
    _ROUND_CAST_SCALE = 18

    def _round_decimal_cast(self, value_sql: str, scale: int) -> str | None:
        """The exact-decimal cast this engine needs before a native ``ROUND``.

        ``None`` when the engine's own ``ROUND`` already rounds ties away from
        zero for every numeric type, which is the case for DuckDB, BigQuery,
        Snowflake, Databricks and Dremio.

        *scale* is the fractional digits the cast must keep: never fewer than
        the caller asked ``round`` for, or the cast would drop the very digit
        being rounded to.
        """
        return None

    #: Widest fractional scale this engine's decimal type can express at all.
    #: ``None`` where the type is unbounded, as PostgreSQL's ``numeric`` is.
    #: Exceeding it is not a wrong number but invalid SQL.
    _MAX_ROUND_CAST_SCALE: int | None = None

    #: Widest scale that is safe for *every* input type, used when the digit
    #: count is not known while the SQL is being built. Defaults to the plain
    #: scale; an engine raises it only as far as it can without trading one
    #: kind of wrong answer for another.
    _UNKNOWN_ROUND_CAST_SCALE: int | None = None

    def _round_cast_scale(self, args: list[Expr]) -> int:
        """Fractional digits the decimal cast has to preserve.

        A fixed scale is wrong the moment the caller asks for more digits than
        it keeps: ``round(x, 19)`` under a scale-18 cast has already lost the
        19th digit before ``ROUND`` runs, which regresses a high-scale decimal
        column that the engine rounds correctly on its own. So a known digit
        count sizes the cast, bounded by what the engine's decimal type can
        express.

        The digit count is read only when it is an integer literal, which is
        what a model formula writes. A scale is part of a *type*, so it has to
        be known when the SQL is built and cannot wait for a value: a computed
        one falls back to the widest scale that is safe whatever arrives.
        """
        scale = self._ROUND_CAST_SCALE
        if len(args) > 1:
            digits = args[1]
            # bool is a subclass of int, and `round(x, true)` is not a scale.
            if (
                isinstance(digits, Literal)
                and isinstance(digits.value, int)
                and not isinstance(digits.value, bool)
            ):
                scale = max(scale, digits.value)
            else:
                scale = max(scale, self._UNKNOWN_ROUND_CAST_SCALE or scale)
        if self._MAX_ROUND_CAST_SCALE is not None:
            scale = min(scale, self._MAX_ROUND_CAST_SCALE)
        return scale

    def _render_round(self, args: list[Expr]) -> str:
        """Native ``ROUND``, over an exact-decimal cast where the engine needs
        one to round ties away from zero.

        Measured, not assumed. ClickHouse, PostgreSQL and MySQL all use
        banker's rounding for their *float* type and away from zero for their
        *decimal* type - ``round(2.5)`` is 2 on a double and 3 on a numeric, on
        the same engine, and all three document it. ClickHouse is not the odd
        one out it was once described as.

        The rewrite therefore moves the value to the decimal type rather than
        doing float arithmetic. An earlier ClickHouse-only fix computed
        ``sign(x) * floor(abs(x) * pow(10, n) + 0.5) / pow(10, n)``, which fixes
        the tie but drags every Decimal into Float64 on the way, because both
        ``pow`` and the ``0.5`` literal are Float64 there - measured, a
        Decimal128 of 12345678901234567.885 rounded to 2 places came back as
        1.2345678901234568e16. Rounding a decimal must not cost the digits that
        made it a decimal, so the native ``ROUND`` does the work and the cast
        only guarantees the type it sees.
        """
        cast_sql = self._round_decimal_cast(
            self.compile_expr(args[0]), self._round_cast_scale(args)
        )
        if cast_sql is None:
            return self._render_named_function("round", args)
        # RawSQL: re-wraps SQL this dialect just rendered, so that the argument
        # goes back through _render_named_function and picks up the engine's own
        # spelling of ROUND rather than being formatted a second way here.
        return self._render_named_function("round", [RawSQL(sql=cast_sql), *args[1:]])

    def _render_trunc(self, args: list[Expr]) -> str:
        """Default: native ``TRUNC(x[, n])``, truncating toward zero."""
        return self._render_named_function("trunc", args)

    def _render_trunc_by_floor(self, args: list[Expr]) -> str:
        """``(sign(x) * floor(abs(x) * 10^n) / 10^n)`` — truncation for an
        engine with no numeric truncation of its own.

        Via the absolute value so the result goes toward zero rather than down:
        ``floor(-1.9)`` is -2 where the catalog documents -1. ``sign(0)`` is 0,
        which keeps zero at zero.

        Wrapped, like every rewrite that emits an infix operator: see
        :meth:`_render_infix`.
        """
        value = self.compile_expr(args[0], _parent_prec=self._PREC_MUL)
        if len(args) == 1:
            return self._render_infix(f"SIGN({value}) * FLOOR(ABS({value}))")
        scale = f"POWER(10, {self.compile_expr(args[1])})"
        return self._render_infix(f"SIGN({value}) * FLOOR(ABS({value}) * {scale}) / {scale}")

    def _with_guarded_divisor(self, args: list[Expr]) -> list[Expr]:
        """``div(a, b)`` with its divisor wrapped so a zero yields NULL.

        ``div`` is a named function, so it never reaches the guard the ``/``
        operator gets (#319), and the engines disagree just as widely: measured,
        ``div(7, 0)`` returns NULL on DuckDB and MySQL and raises on PostgreSQL,
        BigQuery, Snowflake and ClickHouse.

        Guarded by rewriting the **argument** rather than the rendering, because
        every dialect spells this function differently - ``a // b``, ``DIV(a,
        b)``, ``intDiv``, ``a DIV b``, ``TRUNC(a / b)`` - and a guard applied to
        the argument is carried by all of them. ``nullif`` is itself a catalog
        entry, so it renders per dialect too.

        Applied at the dispatch site rather than inside ``_render_div``, so a
        dialect that overrides the rendering cannot drop the guard by doing so.
        The internal caller in ``_render_days_to_weeks`` divides by a literal 7
        and goes straight to ``_render_div``, unguarded, which is right.
        """
        if len(args) != 2:
            return args
        return [args[0], FunctionCall(name="nullif", args=[args[1], Literal.number(0)])]

    def _guard_log_domain(self, args: list[Expr]) -> str:
        """``log(base, x)`` outside its domain yields NULL rather than nonsense.

        The catalog exists to pin one meaning per function, and this one had
        four. Measured, for every undefined input - base of 1, base of 0, x of
        0, negative x - PostgreSQL, DuckDB, BigQuery and Snowflake raise, MySQL
        answers NULL, and **ClickHouse returns a number**: ``inf``, ``-0.0``,
        ``-inf`` and ``nan`` respectively. A silent ``inf`` flowing into an
        aggregate is the worst of the three, and the same reason #319 chose NULL
        for a zero divisor.

        Guarding only the base of 1 - the case that is literally a zero divisor,
        since ClickHouse and Dremio rewrite this as ``log10(x) / log10(base)`` -
        would leave its three neighbours silently wrong on the same engine, so
        the whole undefined domain is guarded together.

        A ``CASE`` is used rather than NULLIF-ing the arguments because the
        domain is not just "not zero": a negative ``x`` has no logarithm either.
        Verified that the guard holds for literal arguments too, on PostgreSQL,
        DuckDB and ClickHouse - constant folding does not evaluate the ``ELSE``
        branch and raise before the ``WHEN`` is considered.
        """
        rendered = self._render_log(args)
        if len(args) != 2:
            return rendered
        base = self.compile_expr(args[0])
        value = self.compile_expr(args[1])
        return self._render_infix(
            f"CASE WHEN {base} <= 0 OR {base} = 1 OR {value} <= 0 THEN NULL ELSE {rendered} END"
        )

    def _render_div(self, args: list[Expr]) -> str:
        """Default: native ``DIV(a, b)`` (BigQuery, Postgres)."""
        return self._render_named_function("div", args)

    def _render_div_by_truncation(self, args: list[Expr]) -> str:
        """``TRUNC(a / b)`` — integer division for an engine with no operator
        or function of its own, and whose ``/`` is float division.
        """
        left = self.compile_expr(args[0], _parent_prec=self._PREC_MUL)
        right = self.compile_expr(args[1], _parent_prec=self._PREC_MUL + 1)
        return f"TRUNC({left} / {right})"

    def _render_div_operator(self, args: list[Expr], operator: str) -> str:
        """``(a <op> b)`` for the engines whose integer division is an operator.

        Wrapped because ``compile_expr`` treats a function call as an atom and
        gives the result no parens of its own.
        """
        left = self.compile_expr(args[0], _parent_prec=self._PREC_MUL)
        right = self.compile_expr(args[1], _parent_prec=self._PREC_MUL + 1)
        return f"({left} {operator} {right})"

    def _render_log(self, args: list[Expr]) -> str:
        """Default: native ``LOG(base, x)``."""
        return self._render_named_function("log", args)

    # ---- date/time ---------------------------------------------------------
    #
    # These take the unit already extracted and lower-cased, because every one
    # of them has to switch on it: the unit is a keyword on BigQuery and
    # ClickHouse, a quoted string on Snowflake, an interval qualifier on MySQL,
    # and a different expression per unit on Postgres. A call whose unit is not
    # a literal from the vocabulary never reaches here — ``compile_expr``
    # leaves it to the pass-through path, and the validator reports it.

    _SQL_UNITS: dict[str, str] = {unit: unit.upper() for unit in TIME_UNITS}
    """Canonical unit → the keyword this dialect spells it with."""

    week_start: WeekStart = WeekStart.MONDAY
    """Which day ``date_trunc('week', …)`` rounds down to.

    Set per compile from ``settings.weekStart`` by the pipeline, which builds a
    fresh dialect for each query, so one model's calendar cannot leak into
    another's.
    """

    def _render_in_timezone(self, value: Expr, zone: str, from_zone: str | None) -> str:
        """Default: ANSI ``AT TIME ZONE``, which DuckDB and Postgres share.

        A naive value is first declared to be in *from_zone*, then read in
        *zone*; an aware one already knows its instant and is only read.
        """
        rendered = self.compile_expr(value, _parent_prec=self._PREC_CMP + 1)
        if from_zone is not None:
            rendered = f"{rendered} AT TIME ZONE {self._quote_zone(from_zone)}"
        return self._render_infix(f"{rendered} AT TIME ZONE {self._quote_zone(zone)}")

    def _quote_zone(self, zone: str) -> str:
        """A time zone name as a SQL string literal."""
        return self.quote_string_literal(zone)

    def _render_date_trunc(self, unit: str, value: Expr) -> str:
        """Default: ``DATE_TRUNC('unit', x)``, unit first and quoted.

        Only ever called for the model's own week start; a Sunday week is
        routed to :meth:`_render_week_start_sunday` by the dispatcher, so a
        dialect overriding this one does not have to remember the calendar.
        """
        return f"DATE_TRUNC('{unit}', {self.compile_expr(value)})"

    def _render_week_start_sunday(self, value: Expr) -> str:
        """Default: step back to the preceding Sunday by the ANSI day-of-week.

        ``EXTRACT(DOW …)`` numbers Sunday as 0 on DuckDB and Postgres, so the
        offset is the number itself. Engines that number differently, or that
        have a week-start argument of their own, override.
        """
        rendered = self.compile_expr(value)
        return self._render_infix(
            f"DATE_TRUNC('day', {rendered}) - EXTRACT(DOW FROM {rendered}) * INTERVAL '1 day'"
        )

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """Default: ``x + n * INTERVAL '1 unit'``.

        Multiplication rather than ``INTERVAL n unit`` because *n* is an
        expression in a real model, and Postgres and DuckDB only accept a
        constant inside an interval literal.
        """
        n = self.compile_expr(count, _parent_prec=self._PREC_MUL)
        return self._render_infix(
            f"{self.compile_expr(value, _parent_prec=self._PREC_ADD)} + {n} * INTERVAL '1 {unit}'"
        )

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """Default: ``DATE_DIFF('unit', start, end)``, counting boundaries."""
        return f"DATE_DIFF('{unit}', {self.compile_expr(start)}, {self.compile_expr(end)})"

    def _render_week_floor(self, value: Expr) -> str:
        """The start of *value*'s week, per the model's calendar."""
        if self.week_start is WeekStart.SUNDAY:
            return self._render_week_start_sunday(value)
        return self._render_date_trunc("week", value)

    def _render_week_diff(self, start: Expr, end: Expr) -> str:
        """Week boundaries crossed, for every dialect and both calendars.

        Not the engine's own week difference, for two reasons. It counts the
        engine's week boundaries, Monday's on all but BigQuery, so it answers
        the wrong number as soon as the model says Sunday. And the engines do
        not even agree on the question: from Sunday 2026-08-09 to Saturday
        2026-08-15, one Monday apart, ClickHouse, Snowflake and BigQuery count
        the boundary and answer 1, while DuckDB and MySQL count whole seven-day
        spans and answer 0, and Postgres has no week difference at all.

        Truncating both ends to the model's week start and dividing the day
        difference by seven gives the boundary count the catalog documents,
        through this dialect's own truncation, day difference and integer
        division rather than an eighth dialect-specific rewrite.
        """
        # RawSQL: re-wraps SQL this dialect just rendered so the composition
        # runs through its own truncation, day difference and integer division
        # rather than an eighth copy of per-engine week arithmetic. Nothing
        # user-authored enters here.
        left = RawSQL(sql=self._render_week_floor(start))
        right = RawSQL(sql=self._render_week_floor(end))
        days = RawSQL(sql=self._render_date_diff("day", left, right))
        return self._render_div([days, Literal.number(7)])

    def _render_extract(self, unit: str, value: Expr) -> str:
        """Default: ANSI ``EXTRACT(UNIT FROM x)``."""
        return f"EXTRACT({self._SQL_UNITS[unit]} FROM {self.compile_expr(value)})"

    def _render_last_day(self, value: Expr) -> str:
        """Default: native ``LAST_DAY(x)``."""
        return f"LAST_DAY({self.compile_expr(value)})"

    def _render_current_date(self) -> str:
        """Default: ``CURRENT_DATE()``. Postgres rejects the parentheses."""
        return "CURRENT_DATE()"

    def _render_extremum(self, name: str, args: list[Expr]) -> str:
        """Default: native ``GREATEST`` / ``LEAST``, which propagate NULL on
        MySQL, Snowflake and BigQuery — the catalog's rule. DuckDB, Postgres,
        ClickHouse and Databricks skip NULL arguments and override.
        """
        return self._render_named_function(name, args)

    def _check_aggregation_supported(self, name: str) -> None:
        """Raise ``UnsupportedAggregationError`` when the dialect doesn't support
        the given aggregation. Matches case-insensitively against
        ``capabilities.unsupported_aggregations`` (lowercase OBML names).

        Existing per-function compile overrides (``_compile_mode``,
        ``_compile_median``) still raise directly — this generic gate is a
        catch-all for purely-standard aggregations like ``REGR_SLOPE`` where
        no special compile path exists.
        """
        if name.lower() in {a.lower() for a in self.capabilities.unsupported_aggregations}:
            raise UnsupportedAggregationError(self.name, name.lower())

    def _compile_median(self, args: list[Expr]) -> str:
        """Compile MEDIAN — default uses MEDIAN(col).

        Works for Snowflake, ClickHouse, Databricks, and Dremio. Postgres overrides.
        """
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"MEDIAN({col_sql})"

    def _compile_mode(self, args: list[Expr]) -> str:
        """Compile MODE — default uses MODE(col).

        Works for Snowflake and Databricks. Postgres, ClickHouse, and Dremio override.
        """
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"MODE({col_sql})"

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """Compile LISTAGG — default uses LISTAGG(col, sep) WITHIN GROUP (ORDER BY ...).

        Works for Snowflake and Dremio. Postgres, ClickHouse, and Databricks override.
        """
        sep = separator if separator is not None else ","
        col_sql = self.compile_expr(args[0]) if args else "''"
        distinct_sql = "DISTINCT " if distinct else ""
        escaped_sep = self.quote_string_literal(sep)[1:-1]
        result = f"LISTAGG({distinct_sql}{col_sql}, '{escaped_sep}')"
        if order_by:
            ob = ", ".join(self.compile_order_by(o) for o in order_by)
            result += f" WITHIN GROUP (ORDER BY {ob})"
        return result

    def _compile_cast(self, inner: Expr, type_name: str) -> str:
        """Render ``CAST(expr AS type)``. Dialects override to handle nullability."""
        resolved_type = self._resolve_type_name(type_name)
        return f"CAST({self.compile_expr(inner)} AS {resolved_type})"

    # SQL operator precedence (higher = binds tighter). Used by the
    # precedence-aware emitter in ``compile_expr`` to skip wrapping a
    # child whose precedence is higher than its parent's required level.
    # Pre-v2.7.4 the emitter wrapped *every* operator unconditionally,
    # producing deeply-nested unreadable SQL (issue #79).
    _CLAUSE_ROOT_PREC = 0  # no surrounding context → no wrap
    _PREC_OR = 1
    _PREC_AND = 2
    _PREC_NOT = 3
    _PREC_CMP = 4  # =, <>, <, <=, >, >=, IS NULL, IN, BETWEEN, LIKE
    _PREC_ADD = 5  # +, -, ||
    _PREC_MUL = 6  # *, /, %
    _PREC_UNARY = 7  # unary -, +
    _PREC_ATOM = 100  # literals, column refs, function calls, CAST(...), CASE...END

    @staticmethod
    def _wrap_if_lower(sql: str, self_prec: int, parent_prec: int) -> str:
        """Wrap ``sql`` in ``(...)`` only when it would bind weaker than
        its parent — i.e. its precedence is strictly less than the
        parent's required level. ``parent_prec = 0`` (clause root) is
        always satisfied so the outermost expression never gets a
        redundant outer wrap.
        """
        if self_prec < parent_prec:
            return f"({sql})"
        return sql

    @classmethod
    def _binary_op_precedence(cls, op: str) -> int:
        """Return the precedence of a ``BinaryOp.op`` value."""
        up = op.upper().strip()
        if up == "OR":
            return cls._PREC_OR
        if up == "AND":
            return cls._PREC_AND
        if up in ("=", "<>", "!=", "<", "<=", ">", ">=", "LIKE", "NOT LIKE"):
            return cls._PREC_CMP
        if up in ("+", "-", "||"):
            return cls._PREC_ADD
        if up in ("*", "/", "%"):
            return cls._PREC_MUL
        # Unknown operator — wrap defensively (treat as lowest precedence).
        return cls._CLAUSE_ROOT_PREC

    # Non-associative operators — children at the same precedence must
    # be wrapped on BOTH sides. SQL forbids chained comparisons
    # (``a >= b = c`` is a syntax error in every dialect we support),
    # subtraction and division are left-associative but ``a - (b - c)``
    # differs from ``a - b - c``, so the right operand is wrapped at
    # equal precedence — see the left-associative branch below.
    _NON_ASSOCIATIVE_OPS: frozenset[str] = frozenset(
        {"=", "<>", "!=", "<", "<=", ">", ">=", "LIKE", "NOT LIKE"}
    )

    def _compile_binary_op(self, left: Expr, op: str, right: Expr) -> str:
        """Render an infix binary expression *without* an outer wrap.

        The dispatcher in ``compile_expr`` decides whether to add an outer
        ``(...)`` wrap based on the parent's precedence. Dialects override
        to widen operand precision (e.g. ClickHouse decimal division) or
        special-case operators that don't translate one-to-one (e.g. MySQL
        string concat).
        """
        self_prec = self._binary_op_precedence(op)
        # Comparison + LIKE forbid chaining — wrap any equal-precedence
        # child on either side. Other ops are left-associative: left at
        # self_prec, right at self_prec + 1 so ``a - (b - c)`` keeps its
        # required parens.
        op_upper = op.upper().strip()
        if op_upper in self._NON_ASSOCIATIVE_OPS:
            left_sql = self.compile_expr(left, _parent_prec=self_prec + 1)
            right_sql = self.compile_expr(right, _parent_prec=self_prec + 1)
        else:
            left_sql = self.compile_expr(left, _parent_prec=self_prec)
            right_sql = self.compile_expr(right, _parent_prec=self_prec + 1)
        if op.strip() == "/":
            # NULLIF is an atom, so the divisor no longer needs its own parens.
            right_sql = self.guard_zero_divisor(right, self.compile_expr(right))
        return f"{left_sql} {op} {right_sql}"

    def guard_zero_divisor(self, right: Expr | None, right_sql: str) -> str:
        """Wrap a divisor so that dividing by zero yields NULL, not chaos.

        Left alone, the same ratio means five different things across the
        supported engines: measured, ``SUM(a) / SUM(b)`` with a zero divisor
        returns ``inf`` on DuckDB, NULL on MySQL, and raises on PostgreSQL,
        BigQuery and ClickHouse. A semantic layer cannot promise that a measure
        means one thing everywhere and then hand back a number, a null and an
        error depending on the warehouse behind it.

        NULL is the answer chosen (#319). It reads naturally as "no value" in a
        BI tool, it is what MySQL already does, and it removes DuckDB's
        ``inf`` - the only one of the three outcomes that can silently corrupt
        a downstream figure rather than stopping.

        Applied where divisions are *compiled* rather than where they are
        built, so it covers a modeller's expression, the divisions OBSL
        generates itself, and all eight dialects without being remembered at
        each site. ``NULLIF`` renders identically everywhere, verified.

        A literal divisor that is plainly not zero is left unwrapped - there is
        nothing to guard, and the noise would show up in every snapshot.
        """
        if right is not None and isinstance(right, Literal):
            value = right.value
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0:
                return right_sql
        return f"NULLIF({right_sql}, 0)"

    def render_decimal_division_sql(self, left_sql: str, right_sql: str) -> str:
        """Render ``left / right`` for decimal-typed operands, given raw SQL.

        Used by code paths that build division as string SQL (e.g. PoP
        comparison CTEs) rather than as ``BinaryOp`` AST nodes.

        **Do not override this** - override :meth:`_render_decimal_division`
        instead. This method exists to apply the zero-divisor guard (#319) in
        one place that a dialect cannot forget. It was overridden directly by
        ClickHouse and MySQL for operand widening, and when the guard moved
        here from ``pop_wrap`` those two overrides silently dropped it: a
        period-over-period ratio against a zero previous value went from NULL
        back to ILLEGAL_DIVISION on ClickHouse. Splitting the two concerns
        makes the guard structural rather than remembered.
        """
        return self._render_decimal_division(left_sql, self.guard_zero_divisor(None, right_sql))

    def _render_decimal_division(self, left_sql: str, right_sql: str) -> str:
        """The division itself, for dialects that need the operands widened.

        Default is plain SQL division; ClickHouse and MySQL override to widen
        both sides first so ratio precision survives. The divisor arrives
        already guarded.
        """
        return f"{left_sql} / {right_sql}"

    def render_pop_previous_value_sql(self, prev_sql: str, current_sql: str) -> str:
        """Render a ``previousValue`` PoP projection (the prior period's measure).

        Default is the prior value verbatim. Dremio overrides this because its
        executor miscompiles a self-joined CTE column projected on its own (see
        ``DremioDialect``); ``current_sql`` (``pop_base``'s measure) is supplied
        so a dialect can reference it in a value-preserving way if needed.
        """
        return prev_sql

    def _compile_multi_field_count(self, args: list[Expr], distinct: bool) -> str:
        """Compile COUNT with multiple fields by concatenating with ``||``.

        Default (non-Snowflake) strategy: cast each field to VARCHAR and
        join with ``'|'`` separator so the database sees a single expression.
        Snowflake overrides this to emit native ``COUNT(col1, col2)``.
        """
        parts = [f"CAST({self.compile_expr(a)} AS VARCHAR)" for a in args]
        concat = " || '|' || ".join(parts)
        if distinct:
            return f"COUNT(DISTINCT {concat})"
        return f"COUNT({concat})"

    def compile(self, ast: Select) -> str:
        """Render a complete SQL AST to a dialect-specific string."""
        return self.compile_select(ast)

    def compile_select(self, node: Select) -> str:
        """Compile a SELECT statement."""
        parts: list[str] = []

        # CTEs
        if node.ctes:
            cte_parts = []
            for cte in node.ctes:
                if isinstance(cte.query, RawSQL):
                    cte_sql = cte.query.sql
                elif isinstance(cte.query, UnionAll):
                    cte_sql = self.compile_union_all(cte.query)
                elif isinstance(cte.query, Except):
                    cte_sql = self.compile_except(cte.query)
                else:
                    cte_sql = self.compile_select(cte.query)
                cte_parts.append(f"{self.quote_identifier(cte.name)} AS (\n{cte_sql}\n)")
            parts.append("WITH " + ",\n".join(cte_parts))

        # SELECT
        keyword = "SELECT DISTINCT" if node.distinct else "SELECT"
        if node.columns:
            cols = ", ".join(self.compile_expr(c) for c in node.columns)
            parts.append(f"{keyword} {cols}")
        else:
            parts.append(f"{keyword} *")

        # FROM
        if node.from_:
            parts.append(f"FROM {self.compile_from(node.from_)}")

        # JOINs, and the unnests that ride between them
        for join in node.joins:
            if isinstance(join, Unnest):
                parts.append(self.render_unnest(join))
            else:
                parts.append(self.compile_join(join))

        # WHERE
        if node.where:
            parts.append(f"WHERE {self.compile_expr(node.where)}")

        # GROUP BY
        if node.group_by:
            parts.append(self.compile_group_by(node.group_by, node.grouping))

        # HAVING
        if node.having:
            parts.append(f"HAVING {self.compile_expr(node.having)}")

        # ORDER BY
        if node.order_by:
            orders = ", ".join(self.compile_order_by(o) for o in node.order_by)
            parts.append(f"ORDER BY {orders}")

        # LIMIT
        if node.limit is not None:
            parts.append(f"LIMIT {node.limit}")

        # OFFSET
        if node.offset is not None:
            parts.append(f"OFFSET {node.offset}")

        return "\n".join(parts)

    def compile_group_by(self, group_by: list[Expr], grouping: str | None) -> str:
        """Render the GROUP BY clause.

        Default ANSI form (Postgres, Snowflake, DuckDB, BigQuery, Databricks,
        Dremio, MySQL): ``GROUP BY ROLLUP(a, b)`` / ``GROUP BY CUBE(a, b)``.
        ClickHouse overrides to the trailing-modifier form
        (``GROUP BY a, b WITH ROLLUP``).

        When ``capabilities.supports_group_by_all`` is set and no grouping
        modifier is requested, emits ``GROUP BY ALL`` — the engine
        auto-derives the grouping list from the SELECT. Equivalent SQL
        with a much shorter and more idiomatic form on modern OLAP
        engines, especially for queries with computed dimensions.
        """
        if grouping == "rollup":
            groups = ", ".join(self.compile_expr(g) for g in group_by)
            return f"GROUP BY ROLLUP({groups})"
        if grouping == "cube":
            groups = ", ".join(self.compile_expr(g) for g in group_by)
            return f"GROUP BY CUBE({groups})"
        if self.capabilities.supports_group_by_all:
            return "GROUP BY ALL"
        groups = ", ".join(self.compile_expr(g) for g in group_by)
        return f"GROUP BY {groups}"

    def compile_from(self, node: From) -> str:
        if isinstance(node.source, Select):
            sub = self.compile_select(node.source)
            result = f"(\n{sub}\n)"
        else:
            result = self._render_source_string(node.source)
        if node.alias:
            result += f" AS {self.quote_identifier(node.alias)}"
        return result

    def compile_join(self, node: Join) -> str:
        if isinstance(node.source, Select):
            source = f"(\n{self.compile_select(node.source)}\n)"
        else:
            source = self._render_source_string(node.source)
        if node.alias:
            source += f" AS {self.quote_identifier(node.alias)}"

        parts = [f"{node.join_type.value} JOIN {source}"]
        if node.on:
            parts.append(f"ON {self.compile_expr(node.on)}")
        return " ".join(parts)

    def _render_source_string(self, source: str) -> str:
        """Render a ``From``/``Join`` string source.

        Wrap modules emit bare CTE names (e.g. ``base``); the star/CFL
        planners emit pre-quoted qualified table strings (e.g.
        ``"DB"."SCHEMA"."TABLE"``). Quote the former so case-sensitive
        dialects like Snowflake match the CTE declaration; pass the latter
        through unchanged.
        """
        if source.isidentifier():
            return self.quote_identifier(source)
        return source

    def compile_order_by(self, node: OrderByItem) -> str:
        result = self.compile_expr(node.expr)
        if node.desc:
            result += " DESC"
        else:
            result += " ASC"
        if node.nulls_last is True:
            result += " NULLS LAST"
        elif node.nulls_last is False:
            result += " NULLS FIRST"
        return result

    def compile_union_all(self, node: UnionAll) -> str:
        """Compile a UNION ALL of multiple SELECT statements."""
        return "\nUNION ALL\n".join(self.compile_select(q) for q in node.queries)

    def compile_except(self, node: Except) -> str:
        """Compile an EXCEPT of two SELECT statements."""
        return self.compile_select(node.left) + "\nEXCEPT\n" + self.compile_select(node.right)

    def compile_expr(self, expr: Expr, _parent_prec: int = 0) -> str:
        """Compile an expression node to SQL string.

        ``_parent_prec`` is the precedence of the surrounding operator
        (or ``_CLAUSE_ROOT_PREC = 0`` when called at the root of a SELECT
        projection, ON / WHERE / HAVING clause, GROUP BY / ORDER BY item,
        or function argument). Each operator branch wraps its own SQL in
        ``(...)`` only when its precedence is strictly less than the
        parent's required level; atoms (literals, column refs, function
        calls, CAST, CASE) are at ``_PREC_ATOM`` and never wrap.

        Pre-v2.7.4 every ``BinaryOp`` / ``IsNull`` / ``InList`` /
        ``Between`` / ``UnaryOp`` wrapped itself unconditionally,
        producing deeply-nested unreadable SQL — issue #79.
        """
        match expr:
            case Literal(value=None):
                return "NULL"
            case Literal(value=True):
                return "TRUE"
            case Literal(value=False):
                return "FALSE"
            case Literal(value=v) if isinstance(v, str):
                return self.quote_string_literal(v)
            case Literal(value=v):
                return str(v)
            case Star(table=None):
                return "*"
            case Star(table=t) if t is not None:
                return f"{self.quote_identifier(t)}.*"
            case ColumnRef(name=name, table=None):
                return self.quote_identifier(name)
            case ColumnRef(name=name, table=table) if table is not None:
                return f"{self.quote_identifier(table)}.{self.quote_identifier(name)}"
            case NestedField():
                # Routed through the dialect rather than rendered here: the
                # element is a column on six engines and a VARIANT path on
                # Snowflake, and the planner cannot know which without one.
                return self.compile_expr(self.render_nested_field(expr))
            case AliasedExpr(expr=inner, alias=alias):
                return f"{self.compile_expr(inner)} AS {self.quote_identifier(alias)}"
            case FunctionCall(
                name=fname,
                args=args,
                distinct=distinct,
                order_by=order_by,
                separator=separator,
            ):
                # Reject aggregations explicitly listed as unsupported by the dialect.
                # Per-function overrides (_compile_mode etc.) still apply for cases
                # that have a special compile path; this catches plain aggregates
                # like REGR_SLOPE that have no override.
                self._check_aggregation_supported(fname)
                # LISTAGG: dialect-specific rendering
                if fname.upper() == "LISTAGG":
                    return self._compile_listagg(args, distinct, order_by, separator)
                # MODE: dialect-specific rendering
                if fname.upper() == "MODE":
                    return self._compile_mode(args)
                # MEDIAN: dialect-specific rendering
                if fname.upper() == "MEDIAN":
                    return self._compile_median(args)
                # Multi-field COUNT: concatenate fields for portability
                # (Snowflake overrides to use native multi-arg syntax)
                if fname.upper() == "COUNT" and len(args) > 1:
                    return self._compile_multi_field_count(args, distinct)
                # Portable scalar catalog (``models/functions.py``): a call the
                # catalog defines is rendered per its pinned semantics rather
                # than passed through. A wrong arity falls through to the
                # verbatim path below — the model validator reports it, and
                # emitting the author's own call keeps the database error
                # recognisable instead of raising from codegen.
                spec = lookup_function(fname)
                if (
                    spec is not None
                    and not distinct
                    and spec.accepts(len(args))
                    and (spec.unit_argument is None or _is_unit_literal(args[spec.unit_argument]))
                    and (
                        spec.path_argument is None
                        or _is_json_path_literal(args[spec.path_argument])
                    )
                ):
                    return self._render_function(spec.name, args)
                # Everything else stays pass-through: removing the escape
                # hatch would break every model built before the catalog.
                fname = self._map_function_name(fname)
                args_sql = ", ".join(self.compile_expr(a) for a in args)
                if distinct:
                    return f"{fname}(DISTINCT {args_sql})"
                return f"{fname}({args_sql})"
            case BinaryOp(left=left, op=op, right=right):
                self_prec = self._binary_op_precedence(op)
                sql = self._compile_binary_op(left, op, right)
                return self._wrap_if_lower(sql, self_prec, _parent_prec)
            case UnaryOp(op=op, operand=operand):
                self_prec = self._PREC_NOT if op.upper() == "NOT" else self._PREC_UNARY
                sql = f"{op} {self.compile_expr(operand, _parent_prec=self_prec)}"
                return self._wrap_if_lower(sql, self_prec, _parent_prec)
            case IsNull(expr=inner, negated=False):
                sql = f"{self.compile_expr(inner, _parent_prec=self._PREC_CMP)} IS NULL"
                return self._wrap_if_lower(sql, self._PREC_CMP, _parent_prec)
            case IsNull(expr=inner, negated=True):
                sql = f"{self.compile_expr(inner, _parent_prec=self._PREC_CMP)} IS NOT NULL"
                return self._wrap_if_lower(sql, self._PREC_CMP, _parent_prec)
            case InList(expr=inner, values=values, negated=negated):
                vals = ", ".join(self.compile_expr(v) for v in values)
                op = "NOT IN" if negated else "IN"
                sql = f"{self.compile_expr(inner, _parent_prec=self._PREC_CMP)} {op} ({vals})"
                return self._wrap_if_lower(sql, self._PREC_CMP, _parent_prec)
            case CaseExpr(when_clauses=whens, else_clause=else_):
                parts = ["CASE"]
                for when_cond, then_val in whens:
                    parts.append(
                        f"WHEN {self.compile_expr(when_cond)} THEN {self.compile_expr(then_val)}"
                    )
                if else_ is not None:
                    parts.append(f"ELSE {self.compile_expr(else_)}")
                parts.append("END")
                return " ".join(parts)
            case Cast(expr=inner, type_name=type_name):
                return self._compile_cast(inner, type_name)
            case SubqueryExpr(query=query):
                return f"(\n{self.compile_select(query)}\n)"
            case Exists(subquery=subq, negated=False):
                return f"EXISTS (\n{self.compile_select(subq)}\n)"
            case Exists(subquery=subq, negated=True):
                return f"NOT EXISTS (\n{self.compile_select(subq)}\n)"
            case RawSQL(sql=sql):
                return sql
            case Between(expr=inner, low=low, high=high, negated=negated):
                op = "NOT BETWEEN" if negated else "BETWEEN"
                inner_sql = self.compile_expr(inner, _parent_prec=self._PREC_CMP)
                low_sql = self.compile_expr(low, _parent_prec=self._PREC_CMP)
                high_sql = self.compile_expr(high, _parent_prec=self._PREC_CMP)
                sql = f"{inner_sql} {op} {low_sql} AND {high_sql}"
                return self._wrap_if_lower(sql, self._PREC_CMP, _parent_prec)
            case InTimeZone(expr=inner, zone=zone, from_zone=from_zone):
                return self._render_in_timezone(inner, zone, from_zone)
            case RegexMatch(column=column, pattern=pattern, negated=negated):
                return self.compile_regex_match(column, pattern, negated=negated)
            case RelativeDateRange(
                column=column,
                unit=unit,
                count=count,
                direction=direction,
                include_current=include_current,
            ):
                return self.compile_relative_date_range(
                    column=column,
                    unit=unit,
                    count=count,
                    direction=direction,
                    include_current=include_current,
                )
            case WindowFunction(
                func_name=fname,
                args=args,
                partition_by=partition_by,
                order_by=order_by,
                frame=frame,
                distinct=distinct,
            ):
                args_sql = ", ".join(self.compile_expr(a) for a in args)
                func_sql = f"{fname}(DISTINCT {args_sql})" if distinct else f"{fname}({args_sql})"
                over_parts: list[str] = []
                if partition_by:
                    pb = ", ".join(self.compile_expr(p) for p in partition_by)
                    over_parts.append(f"PARTITION BY {pb}")
                if order_by:
                    ob = ", ".join(self.compile_order_by(o) for o in order_by)
                    over_parts.append(f"ORDER BY {ob}")
                if frame is not None:
                    over_parts.append(f"{frame.mode} BETWEEN {frame.start} AND {frame.end}")
                over_clause = " ".join(over_parts)
                return f"{func_sql} OVER ({over_clause})"
            case _:
                raise ValueError(f"Unknown AST node type: {type(expr).__name__}")

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """Compile a regex predicate. Default uses ``REGEXP_LIKE`` — overridden
        per dialect that needs a different syntax (Postgres ``~``, MySQL
        ``REGEXP``, ClickHouse ``match`` etc.).

        The pattern is rendered as a SQL string literal; callers pass it
        as ``RegexMatch.pattern`` (already a Python ``str``).
        """
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        op_sql = f"REGEXP_LIKE({col_sql}, {pat_sql})"
        return f"NOT {op_sql}" if negated else op_sql

    def compile_relative_date_range(
        self,
        column: Expr,
        unit: str,
        count: int,
        direction: str,
        include_current: bool,
    ) -> str:
        """Compile a relative date range predicate to SQL."""
        col_sql = self.compile_expr(column)
        base = self.current_date_sql()

        if direction == "future":
            start = base if include_current else self.date_add_sql(base, "day", 1)
            end = self.date_add_sql(start, unit, count)
        else:
            end = self.date_add_sql(base, "day", 1) if include_current else base
            start = self.date_add_sql(end, unit, -count)

        return f"({col_sql} >= {start} AND {col_sql} < {end})"
