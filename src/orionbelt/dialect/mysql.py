"""MySQL 8.0+ dialect implementation."""

from __future__ import annotations

import re

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem, Unnest
from orionbelt.dialect.base import (
    PORTABLE_DECIMAL_PRECISION,
    Dialect,
    DialectCapabilities,
    UnsupportedAggregationError,
    _json_path_of,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import DecimalType, OBMLType


def _json_source_and_row_path(dialect: MySQLDialect, node: Unnest) -> tuple[str, str]:
    """Split a dotted column into the JSON document and the path within it.

    ``x_Labels`` reads the whole column as the array: ``JSON_TABLE(col, '$[*]'
    ...)``. ``x_Project.Ancestors`` reads the array *inside* the document:
    ``JSON_TABLE(col, '$."Ancestors"[*]' ...)``. Every engine but this one and
    Snowflake takes the deeper segments as identifiers; MySQL is the only one
    where they change which argument they belong to.
    """
    first, *rest = node.column.split(".")
    source = f"{dialect.quote_identifier(node.parent_alias)}.{dialect.quote_identifier(first)}"
    inner = "".join(f".{_json_member(segment)}" for segment in rest)
    return source, _sql_literal(f"${inner}[*]")


def _json_member(name: str) -> str:
    """One JSON-path member, quoted and escaped for the JSON layer."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sql_literal(text: str) -> str:
    """*text* as a MySQL single-quoted literal.

    MySQL treats a backslash as an escape inside string literals unless
    ``NO_BACKSLASH_ESCAPES`` is set, so both it and the quote are doubled.
    """
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _json_path_literal(code: str) -> str:
    """A ``JSON_TABLE`` PATH argument for *code*, escaped for **both** layers.

    The path is a JSON-path expression *inside* a SQL string literal, so it
    passes through two escaping regimes and needs both. Escaping only the JSON
    layer looked right and failed three ways against MySQL 8, measured:

    ==========  ===============================================================
    ``q"t``     ``\\"`` is consumed by the SQL layer, leaving an unbalanced
                quote and an invalid JSON path
    ``q't``     the apostrophe closes the SQL literal: syntax error
    ``a\\b``     the backslash is a SQL escape, and the path silently reads
                NULL
    ==========  ===============================================================

    Order matters in both layers: backslashes first, then the quote character,
    or the escape introduced by the first pass is escaped again by the second.

    MySQL treats a backslash as an escape inside string literals unless
    ``NO_BACKSLASH_ESCAPES`` is set, which is why the SQL layer doubles it.
    """
    return _sql_literal(f"$.{_json_member(code)}")


_VARCHAR_RE = re.compile(r"^\s*VARCHAR\s*(?:\(\s*(\d+)\s*\))?\s*$", re.IGNORECASE)
_MYSQL_CAST_CHAR_MAX = 255


@DialectRegistry.register
class MySQLDialect(Dialect):
    """MySQL 8.0+ dialect — backtick quoting, DATE_FORMAT time grains, GROUP_CONCAT.

    **Dialect deviations from the SQL standard that this class works around.**
    Read this list before changing any compile_* method below.

    1. **No CUBE.** MySQL supports ``GROUP BY ... WITH ROLLUP`` (trailing form
       only) and does **not** support ``GROUP BY CUBE`` in any version. The
       ANSI function form ``GROUP BY ROLLUP(a, b)`` is also unsupported.
       See ``compile_group_by``.

    2. **No ``NULLS FIRST`` / ``NULLS LAST`` keywords.** Standard SQL nulls-
       position syntax is rejected by MySQL's parser. MySQL has its own
       implicit rule: NULLs sort as the smallest value, so ``ASC`` puts
       NULLs first and ``DESC`` puts them last. When the requested NULLs
       position matches that default, we emit plain ``ASC``/``DESC``;
       otherwise we use the ``<expr> IS NULL`` boolean-coercion workaround.
       See ``compile_order_by``.

    Both behaviours surfaced together when the auto-order rule for
    ROLLUP/CUBE was added (v2.4.0). Tests live in
    ``tests/unit/test_dialects.py::TestMySQLDialect`` for compile-time
    correctness and ``tests/integration/test_mysql_execution.py`` for
    real-MySQL execution.
    """

    _MAX_DECIMAL_PRECISION: int = 65

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "SIGNED",
        "integer": "SIGNED",
        "double": "DOUBLE",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "time": "TIME",
        "string": "VARCHAR(65535)",
        "boolean": "TINYINT(1)",
    }

    # MySQL-specific type overrides
    _ABSTRACT_TYPE_MAP: dict[str, str] = {
        "string": "VARCHAR(255)",
        "json": "JSON",
        "int": "INT",
        "float": "DOUBLE",
        "date": "DATE",
        "time": "TIME",
        "time_tz": "TIME",
        "timestamp": "DATETIME",
        "timestamp_tz": "DATETIME",
        "boolean": "TINYINT(1)",
    }

    avg_over_integers_is_exact = True

    backslash_escapes_strings = True

    @property
    def name(self) -> str:
        return "mysql"

    def compile_group_by(self, group_by: list[Expr], grouping: str | None) -> str:
        """MySQL uses ``GROUP BY ... WITH ROLLUP`` (trailing form), not the
        ANSI ``GROUP BY ROLLUP(...)`` function form, and does not support
        CUBE at all.
        """
        from orionbelt.dialect.base import UnsupportedGroupingError

        groups = ", ".join(self.compile_expr(e) for e in group_by)
        if grouping == "rollup":
            return f"GROUP BY {groups} WITH ROLLUP"
        if grouping == "cube":
            raise UnsupportedGroupingError(dialect="mysql", grouping="cube")
        return f"GROUP BY {groups}"

    def compile_order_by(self, node: OrderByItem) -> str:
        """MySQL doesn't accept ``NULLS FIRST`` / ``NULLS LAST`` keywords.

        MySQL treats NULLs as the smallest value, so its default ordering
        already matches half of what callers ask for:

            * ASC  → NULLs first  (matches ``NULLS FIRST``)
            * DESC → NULLs last   (matches ``NULLS LAST``)

        When the requested position matches the default, we emit plain
        ``<expr> ASC/DESC`` — no workaround needed, no extra sort key.

        When it disagrees (``ASC NULLS LAST`` or ``DESC NULLS FIRST``)
        we exploit ``<expr> IS NULL``'s boolean coercion (1 for NULL,
        0 otherwise) as a primary sort key:

            ASC NULLS LAST   → ``<expr> IS NULL ASC, <expr> ASC``
                              (0s first = non-NULL first, then ascending)
            DESC NULLS FIRST → ``<expr> IS NULL DESC, <expr> DESC``
                              (1s first = NULL first, then descending)

        ``nulls_last=None`` (caller has no preference) falls through to
        MySQL's default ordering.
        """
        expr_sql = self.compile_expr(node.expr)
        direction = "DESC" if node.desc else "ASC"
        # No preference, or request matches MySQL default → plain sort.
        if node.nulls_last is None:
            return f"{expr_sql} {direction}"
        nulls_first_requested = not node.nulls_last
        matches_default = (nulls_first_requested and not node.desc) or (
            node.nulls_last and node.desc
        )
        if matches_default:
            return f"{expr_sql} {direction}"
        # Disagreement: use the IS NULL workaround.
        # nulls_last=True  → IS NULL ASC (0s first = non-NULL first, NULLS LAST)
        # nulls_last=False → IS NULL DESC (1s first = NULL first, NULLS FIRST)
        null_dir = "ASC" if node.nulls_last else "DESC"
        return f"{expr_sql} IS NULL {null_dir}, {expr_sql} {direction}"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=False,
            supports_arrays=False,
            supports_window_filters=False,
            supports_ilike=False,
            supports_time_travel=False,
            supports_semi_structured=False,
            supports_union_all_by_name=False,
            unsupported_aggregations=[
                "mode",
                "median",
                # MySQL has no first-class correlation, covariance, or regression
                # aggregates. Variance / standard deviation are supported natively.
                "corr",
                "covar_pop",
                "covar_samp",
                "regr_slope",
                "regr_intercept",
                # ``measure`` is Databricks Metric View specific.
                "measure",
            ],
        )

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """MySQL: two-part ``schema.code`` (schema == database in MySQL terminology).

        An omitted schema collapses to the bare table rather than an empty
        quoted component, so the reference resolves against the connection's
        search path. ``database`` is not part of the name on this dialect, so
        setting it without a schema is not ambiguous here.
        """
        if not schema:
            return self.quote_identifier(code)
        return f"{self.quote_identifier(schema)}.{self.quote_identifier(code)}"

    def quote_identifier(self, name: str) -> str:
        """MySQL uses backtick quoting."""
        escaped = name.replace("`", "``")
        return f"`{escaped}`"

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        """MySQL time grain truncation via DATE_FORMAT or DATE_ADD+MAKEDATE for quarters."""
        grain_format_map: dict[TimeGrain, str | None] = {
            TimeGrain.SECOND: "%Y-%m-%d %H:%i:%s",
            TimeGrain.MINUTE: "%Y-%m-%d %H:%i:00",
            TimeGrain.HOUR: "%Y-%m-%d %H:00:00",
            TimeGrain.DAY: "%Y-%m-%d",
            TimeGrain.WEEK: "%Y-%u",
            TimeGrain.MONTH: "%Y-%m-01",
            TimeGrain.QUARTER: None,  # handled below
            TimeGrain.YEAR: "%Y-01-01",
        }

        if grain == TimeGrain.QUARTER:
            from orionbelt.ast.nodes import RawSQL

            col_sql = self.compile_expr(column)
            # RawSQL: MySQL quarter truncation via nested DATE_ADD/MAKEDATE/QUARTER
            # date arithmetic — no single typed FunctionCall expresses it. Covered
            # by dialect drift snapshots. See tests/architecture/test_rawsql_guard.py.
            return RawSQL(
                sql=(
                    f"DATE_ADD(MAKEDATE(YEAR({col_sql}), 1), "
                    f"INTERVAL (QUARTER({col_sql}) - 1) * 3 MONTH)"
                )
            )

        fmt = grain_format_map.get(grain) or "%Y-%m-%d"
        return FunctionCall(
            name="DATE_FORMAT",
            args=[column, Literal.string(fmt)],
        )

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_unnest(self, node: Unnest) -> str:
        """``JSON_TABLE``, which extracts a declared shape rather than
        inferring one.

        The only dialect whose unnest needs the child object's **columns**: the
        others hand back the element and let a field reference read it, while
        this one has to be told which paths to pull out and at what type. That
        is why :class:`Unnest` carries them.
        """
        cols = (
            ", ".join(
                f"{self.quote_identifier(code)} {sql_type} PATH {_json_path_literal(code)}"
                for code, sql_type in node.columns
            )
            or "value VARCHAR(1024) PATH '$'"
        )
        # A dotted column is an array inside an object, and here that is not a
        # deeper *identifier* - it is a deeper JSON path. `C`.`x_Project`.
        # `Ancestors` is "Unknown column"; the member has to move out of the
        # source expression and into the row path, leaving the column itself as
        # the document being read.
        source, row_path = _json_source_and_row_path(self, node)
        table = (
            f"JSON_TABLE({source}, {row_path} COLUMNS ({cols})) "
            f"AS {self.quote_identifier(node.alias)}"
        )
        return f"LEFT JOIN {table} ON TRUE" if node.outer else f", {table}"

    def cast_to_obml_type(self, expr: Expr, obml_type: OBMLType) -> Expr:
        """MySQL: a measure's decimal cast carries at least 38 digits.

        Every other supported engine refuses a value the target type cannot
        hold - "Conversion Error" on DuckDB, "numeric field overflow" on
        Postgres, code 407 on ClickHouse. MySQL saturates instead and returns
        the largest value the type can express as an ordinary row: measured,
        ``CAST(SUM(amt) AS DECIMAL(18, 2))`` over a true 100000000000000001.10
        gives 9999999999999999.99. It attaches warning 1264, but a warning is
        not an error and no driver on this stack surfaces one, so what reaches
        a dashboard is a plausible wrong number (#336).

        Nothing here can make MySQL raise. There is no SELECT-time strictness
        to switch on - ``STRICT_ALL_TABLES``, ``STRICT_TRANS_TABLES`` and
        ``TRADITIONAL`` were each measured saturating exactly as the default
        does - and a range check around every measure cast buys the NULL at the
        cost of a ``CASE`` on one dialect and no other.

        So the cast is widened rather than guarded, and the overflow stops
        being reachable instead of being caught. Only the **precision** moves;
        the scale is what shapes the value and is left exactly as declared, so
        ``decimal(18, 2)`` still rounds to two places and only stops refusing
        totals the source holds legally.

        Widened to 38 and no further on purpose, though MySQL itself allows 65:
        38 is what every other supported engine accepts, so a value MySQL now
        returns is one a portable model could have carried anyway. Going to 65
        would let this one engine answer where the other seven cannot, which is
        the divergence this is meant to remove rather than reverse. A model
        that declares more than 38 keeps what it declared.

        This puts MySQL where BigQuery already sits, which returns the true
        value for the same query because its ``NUMERIC`` is (38, 9).
        """
        return Cast(expr=expr, type_name=self.render_obml_type(self._widened(obml_type)))

    @staticmethod
    def _widened(obml_type: OBMLType) -> OBMLType:
        """The declared type with room for a total the source holds legally.

        The scale is carried through untouched, so a large declared scale still
        limits the integer digits available inside the precision - a
        ``decimal(38, 20)`` leaves 18 either way. That is the same trade
        ``_widen_to_integer_range`` makes in the type resolver, and widening
        the precision cannot fix it without changing the rounding the model
        asked for.
        """
        if not isinstance(obml_type, DecimalType):
            return obml_type
        if obml_type.precision >= PORTABLE_DECIMAL_PRECISION:
            return obml_type
        return DecimalType(precision=PORTABLE_DECIMAL_PRECISION, scale=obml_type.scale)

    def _render_decimal_division(self, left_sql: str, right_sql: str) -> str:
        """MySQL's ``div_precision_increment`` defaults to 4, capping
        ratio results at the operand scale plus 4 fractional digits.
        For ``DECIMAL(18, 2) / DECIMAL(18, 2)`` that's 6 dp — too few
        for the 11-sig-fig cross-vendor comparison. Widening both
        operands to ``DECIMAL(38, 14)`` lifts the result scale to 18
        dp without changing session state.
        """
        wide = "DECIMAL(38, 14)"
        return f"CAST({left_sql} AS {wide}) / CAST({right_sql} AS {wide})"

    def _compile_cast(self, inner: Expr, type_name: str) -> str:
        """MySQL ``CAST`` accepts a fixed vocabulary that excludes ``VARCHAR``.

        Allowed target types in MySQL are ``BINARY``, ``CHAR``, ``DATE``,
        ``DATETIME``, ``DECIMAL``, ``JSON``, ``NCHAR``, ``SIGNED``,
        ``TIME``, ``UNSIGNED``, and ``YEAR``. Other type maps in this
        dialect (e.g. ``string → VARCHAR(65535)`` for DDL) work fine in
        ``CREATE TABLE`` but cause a parse error inside ``CAST``.

        Rewrite ``VARCHAR[(N)]`` → ``CHAR[(N)]`` at cast time only; DDL
        paths keep the wider VARCHAR type. CHAR's documented column
        limit is 255 characters, so any length above that — including
        the 65535 used for OBML's unbounded ``string`` — is dropped and
        plain ``CHAR`` is emitted to let MySQL pick a safe internal
        width without truncating the value.
        """
        resolved = self._resolve_type_name(type_name)
        match = _VARCHAR_RE.match(resolved)
        if match is not None:
            length_group = match.group(1)
            if length_group is None:
                resolved = "CHAR"
            else:
                length = int(length_group)
                resolved = f"CHAR({length})" if length <= _MYSQL_CAST_CHAR_MAX else "CHAR"
        return f"CAST({self.compile_expr(inner)} AS {resolved})"

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        """MySQL: LIKE with CONCAT (MySQL's || is logical OR by default)."""
        from orionbelt.ast.nodes import BinaryOp

        return BinaryOp(
            left=column,
            op="LIKE",
            right=FunctionCall(
                name="CONCAT",
                args=[Literal.string("%"), pattern, Literal.string("%")],
            ),
        )

    # ``LENGTH`` counts bytes on MySQL (``LENGTH('äbcd')`` is 5); the catalog
    # counts characters, which is ``CHAR_LENGTH``.
    def _render_json_value(self, args: list[Expr]) -> str:
        """MySQL's ``JSON_EXTRACT`` keeps the JSON quoting, so the catalog's
        string result needs ``JSON_UNQUOTE`` around it, and ``JSON_TYPE``
        supplies the object/array rule that ``JSON_UNQUOTE`` alone would miss.

        Verified against a live MySQL 8.0 container: all seven catalog
        examples return the pinned value.
        """
        doc = self.compile_expr(args[0])
        path = self._quote_text(_json_path_of(args[1]))
        # JSON_TYPE supplies the catalog's object/array rule; JSON_UNQUOTE
        # alone would return the serialized JSON for a non-scalar path.
        return (
            f"CASE WHEN JSON_TYPE(JSON_EXTRACT({doc}, {path})) IN ('OBJECT', 'ARRAY') "
            f"THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT({doc}, {path})) END"
        )

    _SCALAR_FUNCTION_NAMES: dict[str, str] = {"length": "CHAR_LENGTH"}

    def _render_starts_with(self, args: list[Expr]) -> str:
        """MySQL has no ``STARTS_WITH``; compare the leading characters.

        NULL propagates through both operands: ``CHAR_LENGTH(NULL)`` is NULL,
        so ``LEFT(x, NULL)`` is NULL and the comparison yields NULL — the same
        answer the native function gives on the engines that have one.
        """
        haystack = self.compile_expr(args[0])
        prefix = self.compile_expr(args[1])
        return f"(LEFT({haystack}, CHAR_LENGTH({prefix})) = {prefix})"

    def _render_ends_with(self, args: list[Expr]) -> str:
        """MySQL has no ``ENDS_WITH``; compare the trailing characters."""
        haystack = self.compile_expr(args[0])
        suffix = self.compile_expr(args[1])
        return f"(RIGHT({haystack}, CHAR_LENGTH({suffix})) = {suffix})"

    def _render_split_part(self, args: list[Expr]) -> str:
        """MySQL has no ``SPLIT_PART``; ``SUBSTRING_INDEX`` nested twice takes
        the *n*-th field — but only while *n* is within range.

        Asked for a field past the last one, ``SUBSTRING_INDEX(x, d, n)``
        returns the whole string and the inner ``-1`` then hands back the
        *last* field, where the catalog documents an empty string. The guard
        counts the fields (length minus the length with the delimiters removed,
        over the delimiter's own length, plus one) and short-circuits.
        """
        haystack = self.compile_expr(args[0])
        delimiter = self.compile_expr(args[1])
        index = self.compile_expr(args[2], _parent_prec=self._PREC_CMP + 1)
        field_count = (
            f"(CHAR_LENGTH({haystack}) - CHAR_LENGTH(REPLACE({haystack}, {delimiter}, ''))) "
            f"/ CHAR_LENGTH({delimiter}) + 1"
        )
        part = (
            f"SUBSTRING_INDEX(SUBSTRING_INDEX({haystack}, {delimiter}, {index}), {delimiter}, -1)"
        )
        return f"CASE WHEN {index} > {field_count} THEN '' ELSE {part} END"

    # MySQL's own WEEK() defaults to a Sunday-based numbering that answers 32
    # where the catalog documents ISO week 33; mode 3 is the ISO one, applied
    # in ``_render_extract``.
    _DATE_FORMAT_BY_UNIT: dict[str, str] = {
        "year": "%Y-01-01",
        "month": "%Y-%m-01",
        "day": "%Y-%m-%d",
        "hour": "%Y-%m-%d %H:00:00",
        "minute": "%Y-%m-%d %H:%i:00",
        "second": "%Y-%m-%d %H:%i:%s",
    }

    def _render_in_timezone(self, value: Expr, zone: str, from_zone: str | None) -> str:
        """MySQL: ``CONVERT_TZ``, which needs both ends named.

        A value with no declared source zone is read in the session's, which is
        what MySQL itself does with a TIMESTAMP column; ``@@session.time_zone``
        names it without assuming which. Applying this twice moves the value
        twice - measured, 00:30 becoming 02:30 - which is why it attaches to a
        column rather than wrapping an expression.
        """
        rendered = self.compile_expr(value)
        source = self._quote_zone(from_zone) if from_zone is not None else "@@session.time_zone"
        return f"CONVERT_TZ({rendered}, {source}, {self._quote_zone(zone)})"

    def _render_date_trunc(self, unit: str, value: Expr) -> str:
        """MySQL has no DATE_TRUNC at all.

        Most units are a DATE_FORMAT away. A quarter has no format string, so
        it is built from the first of the year plus whole quarters, and a week
        is the ISO Monday, which ``WEEKDAY`` numbers from 0.
        """
        rendered = self.compile_expr(value)
        if unit == "quarter":
            return (
                f"DATE_ADD(MAKEDATE(YEAR({rendered}), 1), INTERVAL QUARTER({rendered}) - 1 QUARTER)"
            )
        if unit == "week":
            return f"DATE(DATE_SUB({rendered}, INTERVAL WEEKDAY({rendered}) DAY))"
        pattern = self._DATE_FORMAT_BY_UNIT[unit]
        formatted = f"DATE_FORMAT({rendered}, '{pattern}')"
        # DATE_FORMAT returns a string; cast back so the result compares and
        # sorts as a date rather than lexically.
        return (
            f"CAST({formatted} AS {'DATETIME' if unit in ('hour', 'minute', 'second') else 'DATE'})"
        )

    def _render_week_start_sunday(self, value: Expr) -> str:
        """MySQL: ``DAYOFWEEK`` numbers Sunday as 1, so the offset is one less.

        Not ``WEEKDAY``, which the Monday form uses and which numbers Monday
        as 0.
        """
        rendered = self.compile_expr(value)
        return f"DATE(DATE_SUB({rendered}, INTERVAL DAYOFWEEK({rendered}) - 1 DAY))"

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """MySQL: ``DATE_ADD(x, INTERVAL n UNIT)``. The qualifier is a keyword,
        but *n* may be an expression, unlike the interval literals other
        engines require.
        """
        n = self.compile_expr(count, _parent_prec=self._PREC_MUL)
        return f"DATE_ADD({self.compile_expr(value)}, INTERVAL {n} {unit.upper()})"

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """MySQL's ``TIMESTAMPDIFF`` counts *complete* units where the catalog
        counts boundaries crossed: 23:00 to 01:00 the next morning is 0 days
        here and 1 everywhere else, and 2026-01-31 to 2026-03-01 is 1 month
        rather than 2.

        Truncating both ends to the unit first turns one into the other, which
        is what this does.
        """
        return (
            f"TIMESTAMPDIFF({unit.upper()}, "
            f"{self._render_date_trunc(unit, start)}, "
            f"{self._render_date_trunc(unit, end)})"
        )

    def _render_extract(self, unit: str, value: Expr) -> str:
        """MySQL's ``EXTRACT(WEEK FROM x)`` is Sunday-based and answers 32 for
        2026-08-15, where the catalog documents ISO week 33. ``WEEK(x, 3)`` is
        the ISO numbering.
        """
        if unit == "week":
            return f"WEEK({self.compile_expr(value)}, 3)"
        return super()._render_extract(unit, value)

    def _render_trunc(self, args: list[Expr]) -> str:
        """MySQL spells it ``TRUNCATE`` and always requires the digit count,
        where the catalog's second argument is optional.
        """
        value = self.compile_expr(args[0])
        digits = self.compile_expr(args[1]) if len(args) > 1 else "0"
        return f"TRUNCATE({value}, {digits})"

    def _round_decimal_cast(self, value_sql: str) -> str | None:
        """MySQL rounds ties to even for ``DOUBLE`` and away from zero for
        ``DECIMAL``, both documented.

        ``DECIMAL(65, 18)`` rather than the maximum scale of ``DECIMAL(65, 30)``:
        65 is the widest MySQL takes, so scale buys itself with integer digits,
        and a cast that overflows here **saturates silently** rather than
        raising. Measured, ``CAST(1e35 AS DECIMAL(65, 30))`` returns
        99999999999999999999999999999999999.999999999999999999999999999999,
        where scale 18 carries 1e40 intact.
        """
        return f"CAST({value_sql} AS DECIMAL(65, 18))"

    def _render_div(self, args: list[Expr]) -> str:
        """MySQL's integer division is the ``DIV`` operator, which truncates
        toward zero (``-7 DIV 2`` is -3). Probe-verified.
        """
        return self._render_div_operator(args, "DIV")

    def _compile_median(self, args: list[Expr]) -> str:
        """MySQL does not support MEDIAN aggregation."""
        raise UnsupportedAggregationError("mysql", "median")

    def _compile_mode(self, args: list[Expr]) -> str:
        """MySQL does not support MODE aggregation at the dialect level."""
        raise UnsupportedAggregationError("mysql", "mode")

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """MySQL: GROUP_CONCAT([DISTINCT] col [ORDER BY ...] SEPARATOR sep)."""
        sep = separator if separator is not None else ","
        col_sql = self.compile_expr(args[0]) if args else "''"
        distinct_sql = "DISTINCT " if distinct else ""
        escaped_sep = self.quote_string_literal(sep)[1:-1]

        parts = [f"GROUP_CONCAT({distinct_sql}{col_sql}"]
        if order_by:
            ob = ", ".join(self.compile_order_by(o) for o in order_by)
            parts.append(f" ORDER BY {ob}")
        parts.append(f" SEPARATOR '{escaped_sep}')")

        return "".join(parts)

    def _compile_multi_field_count(self, args: list[Expr], distinct: bool) -> str:
        """MySQL: use CONCAT instead of || for multi-field COUNT."""
        parts = [f"CAST({self.compile_expr(a)} AS CHAR)" for a in args]
        concat = f"CONCAT({', '.join(parts)})"
        if distinct:
            return f"COUNT(DISTINCT {concat})"
        return f"COUNT({concat})"

    def current_date_sql(self) -> str:
        return "CURDATE()"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        """MySQL: DATE_ADD(date, INTERVAL n unit) / DATE_SUB for negative."""
        if count < 0:
            return f"DATE_SUB({date_sql}, INTERVAL {abs(count)} {unit.upper()})"
        return f"DATE_ADD({date_sql}, INTERVAL {count} {unit.upper()})"

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        grain_map = {
            "year": f"DATE_FORMAT({column_sql}, '%Y-01-01')",
            "quarter": (
                f"DATE_ADD(MAKEDATE(YEAR({column_sql}), 1), "
                f"INTERVAL (QUARTER({column_sql}) - 1) * 3 MONTH)"
            ),
            "month": f"DATE_FORMAT({column_sql}, '%Y-%m-01')",
            "week": f"DATE_SUB({column_sql}, INTERVAL WEEKDAY({column_sql}) DAY)",
            "day": f"DATE({column_sql})",
        }
        return grain_map.get(grain, f"DATE({column_sql})")

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev = self.date_add_sql("spine_date", offset_grain, offset)
        return (
            f"SELECT spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (\n"
            f"  WITH RECURSIVE dates AS (\n"
            f"    SELECT {min_date} AS spine_date\n"
            f"    UNION ALL\n"
            f"    SELECT DATE_ADD(spine_date, INTERVAL 1 {grain.upper()})\n"
            f"    FROM dates WHERE spine_date < {max_date}\n"
            f"  )\n"
            f"  SELECT spine_date FROM dates\n"
            f") AS spine"
        )

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """MySQL uses ``REGEXP`` / ``NOT REGEXP``."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        op = "NOT REGEXP" if negated else "REGEXP"
        return f"({col_sql} {op} {pat_sql})"
