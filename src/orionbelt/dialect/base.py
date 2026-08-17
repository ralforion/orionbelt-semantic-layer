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
    OrderByItem,
    RawSQL,
    RegexMatch,
    RelativeDateRange,
    Select,
    Star,
    SubqueryExpr,
    UnaryOp,
    UnionAll,
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
    unsupported_aggregations: list[str] = field(default_factory=list)
    # Canonical names from the portable function catalog
    # (``models/functions.py``) this engine has no equivalent for. Empty for
    # every dialect today — the string group renders on all eight — but the
    # catalog admits a function on the strength of the majority, so a later
    # group can leave one engine behind without dropping the entry.
    unsupported_functions: list[str] = field(default_factory=list)


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
                return self._render_div(args)
            case "log":
                return self._render_log(args)
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

    def _render_round(self, args: list[Expr]) -> str:
        """Default: native ``ROUND``, which rounds ties away from zero on every
        engine but ClickHouse, where ties go to even.
        """
        return self._render_named_function("round", args)

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

    @staticmethod
    def _quote_zone(zone: str) -> str:
        """A time zone name as a SQL string literal."""
        return "'" + zone.replace("'", "''") + "'"

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
        escaped_sep = sep.replace("'", "''")
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
        return f"{left_sql} {op} {right_sql}"

    def render_decimal_division_sql(self, left_sql: str, right_sql: str) -> str:
        """Render ``left / right`` for decimal-typed operands, given raw SQL.

        Used by code paths that build division as string SQL (e.g. PoP
        comparison CTEs) rather than as ``BinaryOp`` AST nodes. Default
        is plain SQL division; ClickHouse overrides to widen both sides
        to ``Decimal(38, 10)`` first so ratio precision survives.
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

        # JOINs
        for join in node.joins:
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
                escaped = v.replace("'", "''")
                return f"'{escaped}'"
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
