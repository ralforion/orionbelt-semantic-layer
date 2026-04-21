"""BigQuery dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal, OrderByItem
from orionbelt.dialect.base import Dialect, DialectCapabilities
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
            p = min(obml_type.precision, self._MAX_DECIMAL_PRECISION)
            s = min(obml_type.scale, p)
            return f"NUMERIC({p}, {s})"
        return self._OBML_SIMPLE_TYPE_MAP.get(obml_type.name, obml_type.name.upper())

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
        )

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace("`", "\\`")
        return f"`{escaped}`"

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """BigQuery: three-part ``project.dataset.table``."""
        return (
            f"{self.quote_identifier(database)}"
            f".{self.quote_identifier(schema)}"
            f".{self.quote_identifier(code)}"
        )

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        if grain == TimeGrain.WEEK:
            return FunctionCall(name="DATE_TRUNC", args=[column, Literal.string("ISOWEEK")])
        return FunctionCall(name="DATE_TRUNC", args=[column, Literal.string(grain.value)])

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

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
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
