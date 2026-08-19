"""Databricks SQL dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem, Unnest
from orionbelt.dialect.base import (
    CrossColumnOrderNotSupportedError,
    Dialect,
    DialectCapabilities,
    _json_path_of,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import OBMLType


@DialectRegistry.register
class DatabricksDialect(Dialect):
    """Databricks SQL dialect — Spark SQL semantics, backtick identifiers."""

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "BIGINT",
        "integer": "INT",
        "double": "DOUBLE",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "time": "STRING",
        "string": "STRING",
        "boolean": "BOOLEAN",
    }

    _ABSTRACT_TYPE_MAP: dict[str, str] = {
        "string": "STRING",
        "json": "STRING",
        "int": "INT",
        "float": "FLOAT",
        "date": "DATE",
        "time": "STRING",
        "time_tz": "STRING",
        "timestamp": "TIMESTAMP",
        "timestamp_tz": "TIMESTAMP",
        "boolean": "BOOLEAN",
    }

    @property
    def name(self) -> str:
        return "databricks"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=False,
            supports_arrays=True,
            supports_window_filters=False,
            supports_ilike=False,
            supports_group_by_all=True,
        )

    def exact_integer_avg(self, arg: Expr, obml_type: OBMLType) -> Expr | None:
        """Databricks drifts like the others, and was classified without being run.

        #318 left it with the plain ``AVG`` because its warehouse would not
        start, so the classification was an assumption. Measured once the
        workspace was reachable: ``AVG`` over BIGINT returns 1.0E18 where the
        true average is 1000000000000000003, so it belongs with BigQuery,
        ClickHouse and Dremio rather than with the engines that are exact.

        It is the only engine so far where **both** routes work - casting the
        input and rewriting as SUM/COUNT are each sufficient. SUM/COUNT is used
        because it carries more scale (decimal(38, 6) against the input cast's
        decimal(38, 4)) and shares Dremio's implementation.

        Both hazards checked: an empty group divides to NULL, and a sum past 64
        bits is exact once the cast is inside the SUM. Worth noting that a raw
        ``SUM`` over BIGINT raises ARITHMETIC_OVERFLOW here rather than wrapping
        silently as it does on Dremio and ClickHouse.
        """
        return self._exact_avg_by_sum_over_count(arg, obml_type)

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace("`", "``")
        return f"`{escaped}`"

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="date_trunc", args=[Literal.string(grain.value), column])

    def render_unnest(self, node: Unnest) -> str:
        """``LATERAL VIEW explode``, which takes no ``ON`` and names its own
        generated table as well as the column.

        ``OUTER`` goes between ``VIEW`` and the function, not on the join.
        """
        keyword = "LATERAL VIEW OUTER" if node.outer else "LATERAL VIEW"
        alias = self.quote_identifier(node.alias)
        table_alias = self.quote_identifier(f"{node.alias}__t")
        return f"{keyword} explode({self.unnest_path(node)}) {table_alias} AS {alias}"

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        from orionbelt.ast.nodes import BinaryOp

        return BinaryOp(
            left=FunctionCall(name="lower", args=[column]),
            op="LIKE",
            right=BinaryOp(
                left=BinaryOp(
                    left=Literal.string("%"),
                    op="||",
                    right=FunctionCall(name="lower", args=[pattern]),
                ),
                op="||",
                right=Literal.string("%"),
            ),
        )

    # Databricks spells the prefix/suffix tests without the underscore
    # (``startswith`` / ``endswith``, Databricks Runtime 10.4 LTS and above).
    def _render_json_value(self, args: list[Expr]) -> str:
        """``try_variant_get`` honours the catalog's contract natively.

        Asking for ``'string'`` makes the object/array rule fall out of the
        cast: a path resolving to an object or array cannot be cast to STRING,
        and the ``try_`` form answers NULL rather than raising
        ``INVALID_VARIANT_CAST``. A missing path is NULL for the same reason.
        No CASE guard is needed, unlike every other engine here.

        ``get_json_object`` and the ``:`` operator both return the JSON *text*
        for a non-scalar path, so neither can express the contract.

        Available on **Databricks SQL** unconditionally, which is the surface
        OrionBelt connects to, and on Databricks Runtime 15.3 and above. The
        version floor applies only to the Runtime path; the published "Applies
        to" badge carries no qualifier next to Databricks SQL.

        **That claim was wrong**, and it was documentation-derived: the
        warehouse would not start while the json group was measured, so nobody
        ran it. Measured once it came back, ``try_variant_get(..., 'string')``
        does *not* refuse a non-scalar - it returns the serialized JSON, the
        same as ``get_json_object``:

            $.o  on {"o": {"b": "y"}}  ->  {"b":"y"}   contract says NULL
            $.arr on {"arr": ["z"]}    ->  ["z"]       contract says NULL

        So it needs the same guard the other four engines carry, spelled with
        ``schema_of_variant``, which answers ``STRING`` for a scalar,
        ``OBJECT<...>`` or ``ARRAY<...>`` for a non-scalar, and NULL for an
        absent path. Matching on the prefix keeps the scalar types open, since
        a number or boolean is a legitimate scalar the catalog returns as text.
        """
        doc = self.compile_expr(args[0])
        path = self._quote_text(_json_path_of(args[1]))
        value = f"try_variant_get(parse_json({doc}), {path}, 'string')"
        schema = f"schema_of_variant(try_variant_get(parse_json({doc}), {path}))"
        return (
            f"CASE WHEN {schema} LIKE 'OBJECT%' OR {schema} LIKE 'ARRAY%' "
            f"THEN NULL ELSE {value} END"
        )

    _SCALAR_FUNCTION_NAMES: dict[str, str] = {
        "starts_with": "STARTSWITH",
        "ends_with": "ENDSWITH",
    }

    def _render_in_timezone(self, value: Expr, zone: str, from_zone: str | None) -> str:
        """Databricks: ``from_utc_timestamp`` reads a value as wall clock in a
        zone, and ``to_utc_timestamp`` declares a naive one first.
        """
        rendered = self.compile_expr(value)
        if from_zone is not None:
            rendered = f"to_utc_timestamp({rendered}, {self._quote_zone(from_zone)})"
        return f"from_utc_timestamp({rendered}, {self._quote_zone(zone)})"

    def _render_week_start_sunday(self, value: Expr) -> str:
        """Spark's ``dayofweek`` numbers Sunday as 1, so the offset is one less."""
        rendered = self.compile_expr(value)
        return self._render_infix(
            f"DATE_TRUNC('day', {rendered}) "
            f"- make_interval(0, 0, 0, dayofweek({rendered}) - 1, 0, 0, 0)"
        )

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """Spark's interval literals want a constant, and its ``date_add`` adds
        days only, so the interval is built with ``make_interval``, whose
        arguments are ordinary expressions.
        """
        # At multiplication precedence, because the quarter slot multiplies:
        # a bare ``1 + 1`` there rendered as ``1 + 1 * 3``, which is four
        # months rather than six.
        n = self.compile_expr(count, _parent_prec=self._PREC_MUL)
        slots = {
            "year": f"{n}, 0, 0, 0, 0, 0, 0",
            "quarter": f"0, {n} * 3, 0, 0, 0, 0, 0",
            "month": f"0, {n}, 0, 0, 0, 0, 0",
            "week": f"0, 0, {n}, 0, 0, 0, 0",
            "day": f"0, 0, 0, {n}, 0, 0, 0",
            "hour": f"0, 0, 0, 0, {n}, 0, 0",
            "minute": f"0, 0, 0, 0, 0, {n}, 0",
            "second": f"0, 0, 0, 0, 0, 0, {n}",
        }
        return self._render_infix(
            f"{self.compile_expr(value, _parent_prec=self._PREC_ADD)} "
            f"+ make_interval({slots[unit]})"
        )

    # Calendar units where Databricks counts whole elapsed units rather than
    # boundaries crossed, which is what the catalog pins.
    _BOUNDARY_UNITS = frozenset({"month", "quarter", "year"})

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """``date_diff(unit, start, end)``, with the calendar units corrected.

        Databricks counts **whole elapsed units** for the calendar grains,
        where the catalog pins **boundaries crossed**. Measured, the two
        disagree wherever the range starts late in a unit::

            date_diff(MONTH,   2026-01-31, 2026-03-01)  ->  1, catalog says 2
            date_diff(YEAR,    2026-12-31, 2027-01-01)  ->  0, catalog says 1
            date_diff(QUARTER, 2026-03-31, 2026-04-01)  ->  0, catalog says 1

        Truncating both ends to the unit first makes the two definitions
        coincide, which is the same correction Postgres applies for the same
        reason. Day and smaller grains already agree and are left alone, as is
        the week grain, which the catalog routes through
        ``_render_week_diff`` rather than here.
        """
        left, right = start, end
        if unit.lower() in self._BOUNDARY_UNITS:
            return (
                f"date_diff({unit.upper()}, {self._render_date_trunc(unit, left)}, "
                f"{self._render_date_trunc(unit, right)})"
            )
        return f"date_diff({unit.upper()}, {self.compile_expr(left)}, {self.compile_expr(right)})"

    def _render_trunc(self, args: list[Expr]) -> str:
        """Databricks has no numeric truncation: its ``trunc`` truncates a
        *date* to a format, so ``trunc(1.9)`` is a type error rather than 1.

        ``floor`` takes an optional target scale here, so the rewrite is the
        floor of the magnitude with the sign restored, which goes toward zero
        the way the catalog documents.
        """
        return self._render_trunc_by_floor(args)

    def _render_div(self, args: list[Expr]) -> str:
        """Databricks: the ``div`` operator, "the integral part of the
        division", per the SQL function reference.
        """
        return self._render_div_operator(args, "div")

    def _render_extremum(self, name: str, args: list[Expr]) -> str:
        """Spark's ``greatest`` / ``least`` skip NULL arguments ("skipping null
        values"); the catalog propagates NULL.
        """
        return self._render_null_guard(self._render_named_function(name, args), args)

    def _compile_listagg(
        self,
        args: list[Expr],
        distinct: bool,
        order_by: list[OrderByItem],
        separator: str | None,
    ) -> str:
        """Databricks: ARRAY_JOIN(COLLECT_LIST/COLLECT_SET(col), sep).

        Databricks does not support ORDER BY inside COLLECT_LIST/COLLECT_SET.
        Only self-ordering (sorting the aggregated column) is supported via SORT_ARRAY.
        Cross-column ordering raises an error.
        """
        sep = separator if separator is not None else ","
        col_sql = self.compile_expr(args[0]) if args else "''"
        escaped_sep = sep.replace("'", "''")
        collect_fn = "COLLECT_SET" if distinct else "COLLECT_LIST"
        inner = f"{collect_fn}({col_sql})"
        if order_by:
            ob_expr = order_by[0]
            ob_sql = self.compile_expr(ob_expr.expr)
            if ob_sql != col_sql:
                raise CrossColumnOrderNotSupportedError("databricks", col_sql, ob_sql)
            inner = f"SORT_ARRAY({inner}, false)" if ob_expr.desc else f"SORT_ARRAY({inner})"
        return f"ARRAY_JOIN({inner}, '{escaped_sep}')"

    def current_date_sql(self) -> str:
        return "current_date()"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        if unit == "day":
            return f"date_add({date_sql}, {count})"
        if unit == "week":
            return f"date_add({date_sql}, {count * 7})"
        if unit == "month":
            return f"add_months({date_sql}, {count})"
        if unit == "quarter":
            return f"add_months({date_sql}, {count * 3})"
        if unit == "year":
            return f"add_months({date_sql}, {count * 12})"
        raise ValueError(f"Unsupported unit '{unit}' for Databricks")

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"date_trunc('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev = self.date_add_sql("d", offset_grain, offset)
        # Spark's SEQUENCE step interval has no QUARTER unit; use 3 MONTH.
        step = "INTERVAL 3 MONTH" if grain == "quarter" else f"INTERVAL 1 {grain.upper()}"
        return (
            f"SELECT d AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (SELECT EXPLODE(SEQUENCE("
            f"{min_date}, {max_date}, {step})) AS d)"
        )

    def compile_regex_match(self, column: Expr, pattern: str, *, negated: bool) -> str:
        """Databricks uses ``RLIKE`` / ``NOT RLIKE``."""
        col_sql = self.compile_expr(column)
        pat_sql = self.compile_expr(Literal.string(pattern))
        op = "NOT RLIKE" if negated else "RLIKE"
        return f"({col_sql} {op} {pat_sql})"
