"""Snowflake dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, RawSQL, UnionAll, Unnest
from orionbelt.dialect.base import (
    Dialect,
    DialectCapabilities,
    _json_path_of,
    _snowflake_path,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain
from orionbelt.models.types import DecimalType, OBMLType


@DialectRegistry.register
class SnowflakeDialect(Dialect):
    """Snowflake dialect — QUALIFY, case-sensitive identifiers, semi-structured types."""

    _OBML_SIMPLE_TYPE_MAP: dict[str, str] = {
        "bigint": "NUMBER(38, 0)",
        "integer": "NUMBER(38, 0)",
        "double": "FLOAT",
        "date": "DATE",
        # The naive type, matching every other dialect and this driver's own
        # note to "use TIMESTAMP_NTZ for timezone-naive datetimes in generated
        # SQL". TIMESTAMP_TZ attached the session zone to a value the model
        # declared without one.
        "timestamp": "TIMESTAMP_NTZ",
        "time": "TIME",
        "string": "VARCHAR",
        "boolean": "BOOLEAN",
    }

    #: A product keeps at most 12 places when its operands have more together
    #: (``min(s1 + s2, max(s1, s2, 12))``), so values of 6 places square
    #: exactly, measured, and of 7 would not.
    _EXACT_SQUARE = ("NUMBER(38, {scale})", 6)

    #: Measured: a ``SUM`` of ``NUMBER(38, 0)`` past 128 bits raises "Value
    #: overflow in a SUM aggregate", inside a ``CASE`` branch not taken too.
    #: Snowflake's own ``VARIANCE`` of three BIGINTs of 9e18 raised it.
    _MOMENTS_ROW_DIGITS = 13

    def exact_statistic(self, aggregate: FunctionCall, scales: tuple[int, ...]) -> Expr | None:
        """A covariance, ``CORR`` or regression from exact moments
        (:meth:`Dialect._statistic_from_moments`).

        Snowflake subtracts sums of products as doubles, over integers and
        decimals alike: over 10^9 + 0..9 against a second column, a covariance
        of 18.5 came back 0, and the correlation and regression NULL, their
        variance 0 too; over 10^15 + 0..9, -1.25e14. Its own variance and
        standard deviation of a ``NUMBER`` are exact, to six places for
        integers, and are kept.
        """
        if len(aggregate.args) != 2:
            return None
        return self._statistic_from_moments(aggregate, scales)

    _percentile_cont_over_doubles = True

    def _compile_ordered_value(self, last: bool, value: Expr, key: Expr) -> str:
        """Snowflake: ``MAX_BY`` / ``MIN_BY`` by the (key, value) array, which
        breaks a tie on the key by the value. Arrays order as their elements do
        (probed against the plain key over numbers, dates, timestamps with and
        without a zone, strings and booleans). A row without both is skipped."""
        v = self.compile_expr(value)
        k = self.compile_expr(key)
        function = "MAX_BY" if last else "MIN_BY"
        pair = f"IFF({k} IS NULL OR {v} IS NULL, NULL, ARRAY_CONSTRUCT({k}, {v}))"
        return f"{function}({v}, {pair})"

    def render_obml_type(self, obml_type: OBMLType) -> str:
        if isinstance(obml_type, DecimalType):
            p = min(obml_type.precision, self._MAX_DECIMAL_PRECISION)
            s = min(obml_type.scale, p)
            return f"NUMBER({p}, {s})"
        return self._OBML_SIMPLE_TYPE_MAP.get(obml_type.name, obml_type.name.upper())

    avg_over_integers_is_exact = True

    avg_rounds_exact_input = True

    def exact_integer_avg(self, arg: Expr, obml_type: OBMLType) -> Expr | None:
        """The integer summed as a decimal, divided by :meth:`Dialect._truncated_average`.

        ``AVG`` of an integer is exact but rounded, a ``NUMBER(38, 6)``, and
        the measure's cast rounds it again: 100 ones among 20001 rows averaged
        0.005000 and then 0.01, where 0.00 is right. Its integer part has the
        native quotient's 32 digits; past 17 places, or where the route has
        less room than that, the native ``AVG`` is kept.
        """
        if not isinstance(obml_type, DecimalType):
            return None
        return self._truncated_average(
            self._sum_over_widened_argument(arg),
            arg,
            obml_type,
            min(obml_type.precision - obml_type.scale, self._QUOTIENT_INTEGER_DIGITS),
        )

    def exact_decimal_avg(
        self, arg: Expr, obml_type: OBMLType, source_type: DecimalType | None = None
    ) -> Expr | None:
        """The decimal divided by :meth:`Dialect._truncated_average`, rounded once.

        ``AVG`` of a ``NUMBER(18, 2)`` is a ``NUMBER(36, 8)``, rounded there,
        and the measure's cast rounds it again: 10^6 values of 0.01 among
        2000001 rows averaged 0.01, where 0.00 is right.
        """
        return self._divided_decimal_average(arg, obml_type, source_type)

    backslash_escapes_strings = True

    like_needs_escape_clause = True

    @property
    def name(self) -> str:
        return "snowflake"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=True,
            supports_arrays=True,
            supports_window_filters=True,
            supports_ilike=True,
            supports_time_travel=True,
            supports_semi_structured=True,
            supports_union_all_by_name=True,
            supports_group_by_all=True,
            # ``aggregation: measure`` requires Databricks Metric Views.
            # Snowflake Semantic Views use the ``SEMANTIC_VIEW(view DIMENSIONS
            # ... METRICS ...)`` table function instead; bare ``MEASURE()`` is
            # only valid inside that table function's projection. Publishing
            # OBML as a Snowflake Semantic View is a separate feature.
            unsupported_aggregations=["measure"],
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def _render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="DATE_TRUNC", args=[Literal.string(grain.value), column])

    def nested_field(self, alias: str, field: str, sql_type: str | None = None) -> Expr:
        """``L.value:"Key"::string`` - a VARIANT path, not a column.

        ``FLATTEN`` yields a row per element whose ``value`` column holds the
        element itself, so the ordinary ``L."Key"`` every other engine accepts
        does not compile here at all: measured, "SQL compilation error".

        The field name is quoted inside the path so one containing a space is
        addressable, and the cast is not optional: without it a string field
        comes back as ``"team"`` with its JSON quotes still on.
        """
        escaped = field.replace('"', '""')
        # RawSQL: Snowflake's `:` VARIANT path has no typed AST node, and the
        # cast has to bind to the path rather than to a column reference.
        return RawSQL(
            sql=f'{self.quote_identifier(alias)}.value:"{escaped}"::{sql_type or "string"}'
        )

    def unnest_path(self, node: Unnest) -> str:
        """Only the first segment is a column; the rest are VARIANT steps.

        ``"C"."x_Project"."Ancestors"`` does not compile - the object's members
        are reached with ``:`` rather than ``.``, the same operator
        :meth:`nested_field` uses for a field of an element.
        """
        parts = node.column.split(".")
        path = f"{self.quote_identifier(node.parent_alias)}.{self.quote_identifier(parts[0])}"
        for segment in parts[1:]:
            escaped = segment.replace('"', '""')
            path = f'{path}:"{escaped}"'
        return path

    def render_unnest(self, node: Unnest) -> str:
        """``LATERAL FLATTEN``, whose outer form is an argument rather than a
        join type.

        The element arrives as ``<alias>.value``, a VARIANT, so a column on a
        Snowflake nested object is read with ``:field`` rather than ``.field``.
        """
        outer = ", outer => TRUE" if node.outer else ""
        return (
            f", LATERAL FLATTEN(input => {self.unnest_path(node)}{outer}) "
            f"{self.quote_identifier(node.alias)}"
        )

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        return FunctionCall(name="CONTAINS", args=[column, pattern])

    def current_date_sql(self) -> str:
        return "CURRENT_DATE()"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        unit_sql = unit.lower()
        return f"DATEADD('{unit_sql}', {count}, {date_sql})"

    def _render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"DATE_TRUNC('{grain}', {column_sql})"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        spine = f"DATEADD('{grain}', rn - 1, {min_date})::date"
        prev = f"DATEADD('{offset_grain}', {offset}, {spine})::date"
        # GENERATOR(ROWCOUNT => n) requires a *constant* n; the row count is only
        # known at run time (it depends on the date_range scalar subqueries), so
        # generate a fixed upper bound and filter to the range — 100000 rows
        # covers ~270 years at daily grain, far beyond any practical spine.
        return (
            f"SELECT {spine} AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (\n"
            f"  SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS rn\n"
            f"  FROM TABLE(GENERATOR(ROWCOUNT => 100000))\n"
            f") AS t\n"
            f"WHERE {spine} <= {max_date}"
        )

    # Snowflake spells the prefix/suffix tests without the underscore;
    # ``STARTS_WITH`` / ``ENDS_WITH`` are not recognised (probe-verified).
    def _render_json_value(self, args: list[Expr]) -> str:
        """``JSON_EXTRACT_PATH_TEXT`` takes the path without the leading ``$``
        and accepts a VARCHAR document directly, so no ``PARSE_JSON`` wrapper is
        needed for the extraction itself.

        Array subscripts keep bracket notation: Snowflake rejects ``arr.0``
        with "Invalid extraction path", so a dotted join is a hard error rather
        than a wrong value.

        ``TYPEOF(GET_PATH(...))`` supplies the catalog's object/array rule,
        which the bare call does not: it returns the serialized JSON for a
        non-scalar path.
        """
        doc = self.compile_expr(args[0])
        path = self._quote_text(_snowflake_path(_json_path_of(args[1])))
        return (
            f"CASE WHEN TYPEOF(GET_PATH(PARSE_JSON({doc}), {path})) IN ('OBJECT', 'ARRAY') "
            f"THEN NULL ELSE JSON_EXTRACT_PATH_TEXT({doc}, {path}) END"
        )

    _SCALAR_FUNCTION_NAMES: dict[str, str] = {
        "starts_with": "STARTSWITH",
        "ends_with": "ENDSWITH",
    }

    def _render_in_timezone(self, value: Expr, zone: str, from_zone: str | None) -> str:
        """Snowflake: ``CONVERT_TIMEZONE``, whose two-argument form reads an
        aware value in *zone* and whose three-argument form declares a naive
        one to be in *from_zone* first.
        """
        rendered = self.compile_expr(value)
        if from_zone is not None:
            return (
                f"CONVERT_TIMEZONE({self._quote_zone(from_zone)}, "
                f"{self._quote_zone(zone)}, {rendered})"
            )
        return f"CONVERT_TIMEZONE({self._quote_zone(zone)}, {rendered})"

    def _render_date_trunc(self, unit: str, value: Expr) -> str:
        """Snowflake's ``DATE_TRUNC('week', …)`` follows the WEEK_START session
        parameter, so a session set to Sunday would silently override a model
        that says Monday. Every other unit is unaffected and uses the native
        call.
        """
        if unit == "week":
            return self._render_week_floor_by_offset("DAYOFWEEKISO({0}) - 1", value)
        return super()._render_date_trunc(unit, value)

    def _render_week_start_sunday(self, value: Expr) -> str:
        """Sunday, likewise without consulting the session.

        ``DAYOFWEEKISO`` numbers Monday 1 through Sunday 7, which ``% 7`` turns
        into the days since Sunday; ``DAYOFWEEK`` would have followed
        WEEK_START.
        """
        return self._render_week_floor_by_offset("MOD(DAYOFWEEKISO({0}), 7)", value)

    def _render_week_floor_by_offset(self, offset_template: str, value: Expr) -> str:
        """Step back *offset* days from the start of *value*'s day.

        From the day rather than from the value itself: subtracting days from a
        timestamp keeps its time, and the start of a week is midnight.
        """
        rendered = self.compile_expr(value)
        offset = offset_template.format(rendered)
        return f"DATEADD('day', -({offset}), DATE_TRUNC('day', {rendered}))"

    def _render_date_add(self, unit: str, count: Expr, value: Expr) -> str:
        """Snowflake: ``DATEADD('unit', n, x)``, quoted unit, value last."""
        return f"DATEADD('{unit}', {self.compile_expr(count)}, {self.compile_expr(value)})"

    def _render_date_diff(self, unit: str, start: Expr, end: Expr) -> str:
        """Snowflake spells it ``DATEDIFF`` and counts boundaries, as the
        catalog documents.
        """
        return f"DATEDIFF('{unit}', {self.compile_expr(start)}, {self.compile_expr(end)})"

    def _render_div(self, args: list[Expr]) -> str:
        """Snowflake rejects ``DIV`` in every form ("Unsupported feature
        'DIV'"), but its ``/`` is float division even on integers, so
        truncating the quotient gives the catalog's answer.
        """
        return self._render_div_by_truncation(args)

    def _compile_multi_field_count(self, args: list[Expr], distinct: bool) -> str:
        """Snowflake supports native multi-arg COUNT(col1, col2)."""
        args_sql = ", ".join(self.compile_expr(a) for a in args)
        if distinct:
            return f"COUNT(DISTINCT {args_sql})"
        return f"COUNT({args_sql})"

    def compile_union_all(self, node: UnionAll) -> str:
        """Snowflake uses UNION ALL BY NAME to match columns by name."""
        return "\nUNION ALL BY NAME\n".join(self.compile_select(q) for q in node.queries)

    def _render_iso_day_of_week(self, value: Expr) -> str:
        """Snowflake has no ``ISODOW``; ``DAYOFWEEKISO`` is Monday 1 to Sunday 7."""
        return f"EXTRACT(DAYOFWEEKISO FROM {self.compile_expr(value)})"
