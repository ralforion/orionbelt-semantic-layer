"""MySQL 8.0+ dialect implementation."""

from __future__ import annotations

import re

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem
from orionbelt.dialect.base import Dialect, DialectCapabilities, UnsupportedAggregationError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain

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
            ],
        )

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """MySQL: two-part ``schema.code`` (schema == database in MySQL terminology)."""
        return f"{self.quote_identifier(schema)}.{self.quote_identifier(code)}"

    def quote_identifier(self, name: str) -> str:
        """MySQL uses backtick quoting."""
        escaped = name.replace("`", "``")
        return f"`{escaped}`"

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
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

    def render_decimal_division_sql(self, left_sql: str, right_sql: str) -> str:
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
        escaped_sep = sep.replace("'", "''")

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

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
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
