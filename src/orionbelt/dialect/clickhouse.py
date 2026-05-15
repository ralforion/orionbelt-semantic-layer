"""ClickHouse dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import BinaryOp, Cast, Expr, FunctionCall, Literal, OrderByItem
from orionbelt.dialect.base import Dialect, DialectCapabilities
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
        """ClickHouse: two-part ``schema.code`` (OBML schema maps to CH database)."""
        return f"{self.quote_identifier(schema)}.{self.quote_identifier(code)}"

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
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        func_name = _GRAIN_FUNCTIONS.get(grain)
        if func_name:
            return FunctionCall(name=func_name, args=[column])
        return column

    def compile_group_by(self, group_by: list[Expr], grouping: str | None) -> str:
        """ClickHouse uses trailing-modifier form, not ROLLUP()/CUBE() functions."""
        groups = ", ".join(self.compile_expr(g) for g in group_by)
        if grouping == "rollup":
            return f"GROUP BY {groups} WITH ROLLUP"
        if grouping == "cube":
            return f"GROUP BY {groups} WITH CUBE"
        return f"GROUP BY {groups}"

    def render_decimal_division_sql(self, left_sql: str, right_sql: str) -> str:
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
        """
        if op == "/":
            wide = "Nullable(Decimal(38, 14))"
            l_sql = f"CAST({self.compile_expr(left)} AS {wide})"
            r_sql = f"CAST({self.compile_expr(right)} AS {wide})"
            return f"({l_sql} / {r_sql})"
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
        # Detect Decimal(P, S) targets and round to scale S first.
        upper = resolved_type.upper()
        if upper.startswith("DECIMAL") or upper.startswith("NULLABLE(DECIMAL"):
            scale_token = resolved_type.split(",")[-1].rstrip(") ")
            scale = int(scale_token.strip())
            inner_sql = f"round({inner_sql}, {scale})"
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
    }

    def _map_function_name(self, name: str) -> str:
        return self._FUNCTION_NAME_MAP.get(name.upper(), name)

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
        escaped_sep = sep.replace("'", "''")
        group_fn = "groupUniqArray" if distinct else "groupArray"
        inner = f"{group_fn}({col_sql})"
        if order_by:
            ob_expr = order_by[0]
            ob_sql = self.compile_expr(ob_expr.expr)
            if ob_sql != col_sql:
                raise ValueError(
                    f"ClickHouse LISTAGG does not support ORDER BY on a different column "
                    f"(aggregated: {col_sql}, order by: {ob_sql})"
                )
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
            "year": "addYears",
        }
        func = funcs.get(unit)
        if func is None:
            raise ValueError(f"Unsupported unit '{unit}' for ClickHouse")
        return f"{func}({date_sql}, {count})"

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
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
