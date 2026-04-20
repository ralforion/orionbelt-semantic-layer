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
