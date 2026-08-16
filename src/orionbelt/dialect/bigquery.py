"""BigQuery dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem, RawSQL
from orionbelt.dialect.base import (
    AmbiguousTableReferenceError,
    Dialect,
    DialectCapabilities,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import DecimalType, OBMLType


@DialectRegistry.register
class BigQueryDialect(Dialect):
    """BigQuery dialect — backtick identifiers, STRUCT/ARRAY support, SAFE functions."""

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "INT64",
        "integer": "INT64",
        "double": "FLOAT64",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "time": "TIME",
        "string": "STRING",
        "boolean": "BOOL",
    }

    def render_obml_type(self, obml_type: OBMLType) -> str:
        if isinstance(obml_type, DecimalType):
            # BigQuery rejects parameterized types in CAST expressions
            # ("Parameterized types are not allowed in CAST expressions"),
            # so emit a bare NUMERIC / BIGNUMERIC and let BigQuery default
            # the precision/scale. NUMERIC covers precision ≤ 38; spill
            # over to BIGNUMERIC for higher precision OBML decimals. The
            # user-specified scale is honoured separately by
            # ``cast_to_obml_type`` which wraps the CAST in ROUND.
            if obml_type.precision > 38:
                return "BIGNUMERIC"
            return "NUMERIC"
        return self._OBML_SIMPLE_TYPE_MAP.get(obml_type.name, obml_type.name.upper())

    def cast_to_obml_type(self, expr: Expr, obml_type: OBMLType) -> Expr:
        """BigQuery: wrap the CAST with ROUND for DecimalType to enforce the
        OBML-specified scale, since BigQuery's CAST drops the scale parameter.
        """
        cast_expr = Cast(expr=expr, type_name=self.render_obml_type(obml_type))
        if isinstance(obml_type, DecimalType):
            return FunctionCall(name="ROUND", args=[cast_expr, Literal.number(obml_type.scale)])
        return cast_expr

    _ABSTRACT_TYPE_MAP: dict[str, str] = {
        "string": "STRING",
        "json": "JSON",
        "int": "INT64",
        "float": "FLOAT64",
        "date": "DATE",
        "time": "TIME",
        "time_tz": "TIME",
        "timestamp": "TIMESTAMP",
        "timestamp_tz": "TIMESTAMP",
        "boolean": "BOOL",
    }

    @property
    def name(self) -> str:
        return "bigquery"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=True,
            supports_arrays=True,
            supports_window_filters=True,
            supports_ilike=False,
            supports_semi_structured=True,
            supports_group_by_all=True,
            # BigQuery exposes CORR / COVAR_POP / COVAR_SAMP and the variance /
            # stddev family. Linear regression requires ``ML.LINEAR_REG`` or
            # manual COVAR/VAR composition; we don't emulate transparently.
            # ``measure`` is Databricks Metric View specific.
            unsupported_aggregations=["regr_slope", "regr_intercept", "measure"],
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace("`", "\\`")
        return f"`{escaped}`"

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """BigQuery: three-part ``project.dataset.table``.

        An omitted project is dropped rather than backquoted empty, so
        ``dataset.table`` resolves against the connection's default project.
        A project *with* no dataset is refused: ``project.table`` is read as
        ``dataset.table``, which would silently query a different namespace.
        """
        if database and not schema:
            raise AmbiguousTableReferenceError(self.name, database, code)
        parts = [database, schema, code]
        return ".".join(self.quote_identifier(p) for p in parts if p)

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        # BigQuery DATE_TRUNC takes a date-part *keyword* (MONTH, ISOWEEK), not a
        # string literal: DATE_TRUNC(col, 'month') raises "A valid date part name
        # is required". RawSQL: emit the bare keyword (matches render_date_trunc_sql).
        part = "ISOWEEK" if grain == TimeGrain.WEEK else grain.value.upper()
        return FunctionCall(name="DATE_TRUNC", args=[column, RawSQL(sql=part)])

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        from orionbelt.ast.nodes import BinaryOp

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

    def _render_position(self, args: list[Expr]) -> str:
        """BigQuery has no ``POSITION`` in either form; ``STRPOS`` is the
        equivalent and takes the haystack first.
        """
        needle = self.compile_expr(args[0])
        haystack = self.compile_expr(args[1])
        return f"STRPOS({haystack}, {needle})"

    def _render_split_part(self, args: list[Expr]) -> str:
        """BigQuery has no ``SPLIT_PART``; ``SPLIT`` returns an array, indexed
        from 0, so the catalog's 1-based *n* becomes ``SAFE_OFFSET(n - 1)``.

        ``SAFE_OFFSET`` yields NULL rather than raising when *n* runs past the
        last field; ``IFNULL`` turns that into the empty string the catalog
        documents.
        """
        haystack = self.compile_expr(args[0])
        delimiter = self.compile_expr(args[1])
        offset = self._zero_based_offset_sql(args[2])
        return f"IFNULL(SPLIT({haystack}, {delimiter})[SAFE_OFFSET({offset})], '')"

    def _zero_based_offset_sql(self, index: Expr) -> str:
        """Render a 1-based index expression as a 0-based one.

        A literal is decremented in place — ``SAFE_OFFSET(1)`` rather than
        ``SAFE_OFFSET(2 - 1)`` — because a constant offset is what the SQL is
        read as; anything else gets the subtraction.
        """
        if isinstance(index, Literal) and type(index.value) is int:
            return str(index.value - 1)
        return f"{self.compile_expr(index, _parent_prec=self._PREC_ADD + 1)} - 1"

    def _render_log(self, args: list[Expr]) -> str:
        """BigQuery's ``LOG`` takes the value first and the base second, the
        opposite of everyone else's. Probe-verified: ``LOG(10, 100)`` is 0.5
        here and 2 on DuckDB, Postgres, MySQL and Snowflake.
        """
        base = self.compile_expr(args[0])
        value = self.compile_expr(args[1])
        return f"LOG({value}, {base})"

    # ``WEEK`` is Sunday-based here and answers 32 where the catalog documents
    # ISO week 33; ``ISOWEEK`` is the Monday-based numbering.
    _SQL_UNITS: dict[str, str] = {
        "year": "YEAR",
        "quarter": "QUARTER",
        "month": "MONTH",
        "week": "ISOWEEK",
        "day": "DAY",
        "hour": "HOUR",
        "minute": "MINUTE",
        "second": "SECOND",
    }

    def _render_in_timezone(self, value: Expr, zone: str, from_zone: str | None) -> str:
        """BigQuery has no ``AT TIME ZONE`` outside EXTRACT: ``DATETIME(x, zone)``
        reads a TIMESTAMP as wall clock in that zone, which is the same contract
        and composes with every date function unchanged.
        """
        rendered = self.compile_expr(value)
        # ``from_zone`` is ignored here, and deliberately: this dialect maps the
        # OBML ``timestamp`` type to BigQuery's TIMESTAMP, which is an instant
        # rather than a wall clock, so there is no zone left to declare.
        # ``TIMESTAMP(x, zone)`` takes a DATETIME and rejects a TIMESTAMP
        # outright ("No matching signature"), which is the loud failure a
        # genuinely naive DATETIME column would get rather than a wrong number.
        return f"DATETIME({rendered}, {self._quote_zone(zone)})"

    def _render_date_trunc(self, unit: str, value: Expr) -> str:
        """BigQuery takes the value first and the unit as a keyword."""
        return f"DATE_TRUNC({self.compile_expr(value)}, {self._SQL_UNITS[unit]})"

    def _render_week_start_sunday(self, value: Expr) -> str:
        """BigQuery is the one engine whose plain ``WEEK`` is already Sunday."""
        return f"DATE_TRUNC({self.compile_expr(value)}, WEEK)"

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """``x + INTERVAL n UNIT``: the interval qualifier is a keyword, and
        unlike DATE_ADD this form does not have to know whether *x* is a DATE,
        a DATETIME or a TIMESTAMP.

        BigQuery still refuses a month-or-larger interval on a TIMESTAMP; that
        surfaces as its own error rather than a wrong answer, and casting the
        column to DATE or DATETIME is the fix.
        """
        n = self.compile_expr(count, _parent_prec=self._PREC_MUL)
        return self._render_infix(
            f"{self.compile_expr(value, _parent_prec=self._PREC_ADD)} "
            f"+ INTERVAL {n} {self._SQL_UNITS[unit]}"
        )

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """BigQuery reverses the operands and takes the unit last."""
        return (
            f"DATE_DIFF({self.compile_expr(end)}, {self.compile_expr(start)}, "
            f"{self._SQL_UNITS[unit]})"
        )

    def _compile_median(self, args: list[Expr]) -> str:
        """BigQuery: PERCENTILE_DISC(col, 0.5) OVER()  — but as an aggregate
        we use APPROX_QUANTILES(col, 2)[OFFSET(1)]."""
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"APPROX_QUANTILES({col_sql}, 2)[OFFSET(1)]"

    def _compile_mode(self, args: list[Expr]) -> str:
        """BigQuery: APPROX_TOP_COUNT(col, 1)[OFFSET(0)].value."""
        col_sql = self.compile_expr(args[0]) if args else "NULL"
        return f"APPROX_TOP_COUNT({col_sql}, 1)[OFFSET(0)].value"

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """BigQuery: STRING_AGG([DISTINCT] col, sep [ORDER BY ...])."""
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
        return "CURRENT_DATE()"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        unit_sql = unit.upper()
        return f"DATE_ADD({date_sql}, INTERVAL {count} {unit_sql})"

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"DATE_TRUNC({column_sql}, {grain.upper()})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev = f"DATE_ADD(d, INTERVAL {offset} {offset_grain.upper()})"
        return (
            f"SELECT d AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM UNNEST(GENERATE_DATE_ARRAY("
            f"{min_date}, {max_date}, INTERVAL 1 {grain.upper()})) AS d"
        )

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """BigQuery uses ``REGEXP_CONTAINS(col, pattern)``."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        result = f"REGEXP_CONTAINS({col_sql}, {pat_sql})"
        return f"NOT {result}" if negated else result
