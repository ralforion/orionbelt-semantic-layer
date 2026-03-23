"""Abstract base dialect with capability flags and default SQL compilation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from orionbelt.ast.nodes import (
    AliasedExpr,
    Between,
    BinaryOp,
    CaseExpr,
    Cast,
    ColumnRef,
    Except,
    Expr,
    From,
    FunctionCall,
    InList,
    IsNull,
    Join,
    Literal,
    OrderByItem,
    RawSQL,
    RelativeDateRange,
    Select,
    Star,
    SubqueryExpr,
    UnaryOp,
    UnionAll,
    WindowFunction,
)
from orionbelt.models.semantic import TimeGrain


class UnsupportedAggregationError(Exception):
    """Raised when a dialect does not support a specific aggregation function."""

    def __init__(self, dialect: str, aggregation: str) -> None:
        self.dialect = dialect
        self.aggregation = aggregation
        super().__init__(f"Dialect '{dialect}' does not support {aggregation.upper()} aggregation")


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
    unsupported_aggregations: list[str] = field(default_factory=list)


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
        """
        return f"{database}.{schema}.{code}"

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> DialectCapabilities: ...

    @abstractmethod
    def quote_identifier(self, name: str) -> str:
        """Quote an identifier per dialect rules."""

    @abstractmethod
    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        """Wrap a column expression for the given time grain."""

    @abstractmethod
    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        """Render a CAST expression."""

    @abstractmethod
    def current_date_sql(self) -> str:
        """Return SQL for the current date."""

    @abstractmethod
    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        """Return SQL that adds count units to date_sql."""

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
                if isinstance(cte.query, UnionAll):
                    cte_sql = self.compile_union_all(cte.query)
                elif isinstance(cte.query, Except):
                    cte_sql = self.compile_except(cte.query)
                else:
                    cte_sql = self.compile_select(cte.query)
                cte_parts.append(f"{self.quote_identifier(cte.name)} AS (\n{cte_sql}\n)")
            parts.append("WITH " + ",\n".join(cte_parts))

        # SELECT
        if node.columns:
            cols = ", ".join(self.compile_expr(c) for c in node.columns)
            parts.append(f"SELECT {cols}")
        else:
            parts.append("SELECT *")

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
            groups = ", ".join(self.compile_expr(g) for g in node.group_by)
            parts.append(f"GROUP BY {groups}")

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

    def compile_from(self, node: From) -> str:
        if isinstance(node.source, Select):
            sub = self.compile_select(node.source)
            result = f"(\n{sub}\n)"
        else:
            result = str(node.source)
        if node.alias:
            result += f" AS {self.quote_identifier(node.alias)}"
        return result

    def compile_join(self, node: Join) -> str:
        if isinstance(node.source, Select):
            source = f"(\n{self.compile_select(node.source)}\n)"
        else:
            source = str(node.source)
        if node.alias:
            source += f" AS {self.quote_identifier(node.alias)}"

        parts = [f"{node.join_type.value} JOIN {source}"]
        if node.on:
            parts.append(f"ON {self.compile_expr(node.on)}")
        return " ".join(parts)

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

    def compile_expr(self, expr: Expr) -> str:
        """Compile an expression node to SQL string."""
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
                fname = self._map_function_name(fname)
                args_sql = ", ".join(self.compile_expr(a) for a in args)
                if distinct:
                    return f"{fname}(DISTINCT {args_sql})"
                return f"{fname}({args_sql})"
            case BinaryOp(left=left, op=op, right=right):
                return f"({self.compile_expr(left)} {op} {self.compile_expr(right)})"
            case UnaryOp(op=op, operand=operand):
                return f"({op} {self.compile_expr(operand)})"
            case IsNull(expr=inner, negated=False):
                return f"({self.compile_expr(inner)} IS NULL)"
            case IsNull(expr=inner, negated=True):
                return f"({self.compile_expr(inner)} IS NOT NULL)"
            case InList(expr=inner, values=values, negated=negated):
                vals = ", ".join(self.compile_expr(v) for v in values)
                op = "NOT IN" if negated else "IN"
                return f"({self.compile_expr(inner)} {op} ({vals}))"
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
                resolved_type = self._resolve_type_name(type_name)
                return f"CAST({self.compile_expr(inner)} AS {resolved_type})"
            case SubqueryExpr(query=query):
                return f"(\n{self.compile_select(query)}\n)"
            case RawSQL(sql=sql):
                return sql
            case Between(expr=inner, low=low, high=high, negated=negated):
                op = "NOT BETWEEN" if negated else "BETWEEN"
                return (
                    f"({self.compile_expr(inner)} {op} "
                    f"{self.compile_expr(low)} AND {self.compile_expr(high)})"
                )
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
