"""Immutable SQL AST nodes. All SQL is generated from these — never string concatenation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class JoinType(StrEnum):
    LEFT = "LEFT"
    INNER = "INNER"
    RIGHT = "RIGHT"
    FULL = "FULL"
    CROSS = "CROSS"


# Forward-declare the union type; actual definition at module bottom.
# We use strings in annotations and resolve at runtime.


@dataclass(frozen=True)
class Literal:
    """A literal value: number, string, boolean, or NULL."""

    value: str | int | float | bool | None

    @classmethod
    def string(cls, v: str) -> Literal:
        return cls(value=v)

    @classmethod
    def number(cls, v: int | float) -> Literal:
        return cls(value=v)

    @classmethod
    def null(cls) -> Literal:
        return cls(value=None)

    @classmethod
    def boolean(cls, v: bool) -> Literal:
        return cls(value=v)


@dataclass(frozen=True)
class Star:
    """SELECT * or table.*"""

    table: str | None = None


@dataclass(frozen=True)
class ColumnRef:
    """Reference to a column, optionally qualified by table/alias."""

    name: str
    table: str | None = None


@dataclass(frozen=True)
class AliasedExpr:
    """An expression with an alias: expr AS alias."""

    expr: Expr
    alias: str


@dataclass(frozen=True)
class FunctionCall:
    """SQL function call, e.g. SUM(col), DATE_TRUNC('month', col)."""

    name: str
    args: list[Expr] = field(default_factory=list)
    distinct: bool = False
    order_by: list[OrderByItem] = field(default_factory=list)
    separator: str | None = None


@dataclass(frozen=True)
class BinaryOp:
    """Binary operation: left op right."""

    left: Expr
    op: str  # +, -, *, /, =, <>, AND, OR, LIKE, etc.
    right: Expr


@dataclass(frozen=True)
class UnaryOp:
    """Unary operation: NOT expr, - expr."""

    op: str
    operand: Expr


@dataclass(frozen=True)
class IsNull:
    """IS NULL / IS NOT NULL check."""

    expr: Expr
    negated: bool = False  # True = IS NOT NULL


@dataclass(frozen=True)
class InList:
    """expr IN (v1, v2, ...) or NOT IN."""

    expr: Expr
    values: list[Expr] = field(default_factory=list)
    negated: bool = False


@dataclass(frozen=True)
class CaseExpr:
    """CASE WHEN ... THEN ... ELSE ... END."""

    when_clauses: list[tuple[Expr, Expr]] = field(default_factory=list)
    else_clause: Expr | None = None


@dataclass(frozen=True)
class Cast:
    """CAST(expr AS type)."""

    expr: Expr
    type_name: str


@dataclass(frozen=True)
class SubqueryExpr:
    """A subquery used as an expression."""

    query: Select


@dataclass(frozen=True)
class Exists:
    """``[NOT ]EXISTS (<subquery>)`` predicate.

    Used by the ``exists`` / ``nonexists`` filter operators.  Codegen is
    portable across all 8 dialects; the only dialect-specific surface is
    identifier quoting inside ``subquery``.
    """

    subquery: Select
    negated: bool = False


@dataclass(frozen=True)
class RawSQL:
    """Escape hatch for dialect-specific raw SQL fragments.

    Use sparingly — prefer AST nodes for correctness.
    """

    sql: str


@dataclass(frozen=True)
class Between:
    """expr BETWEEN low AND high."""

    expr: Expr
    low: Expr
    high: Expr
    negated: bool = False


@dataclass(frozen=True)
class Unnest:
    """A parent's array column, unnested into rows beside it.

    Not a :class:`Join`, because the engines do not agree that it is one. It is
    a comma-lateral on BigQuery, DuckDB, Postgres and Snowflake, a ``LATERAL
    VIEW`` on Databricks, an ``ARRAY JOIN`` on ClickHouse, and a ``JSON_TABLE``
    on MySQL - four different clause shapes, only some of which take an ``ON``.
    ``compile_join`` renders ``<type> JOIN <source> ON <expr>`` and cannot spell
    the rest, so this is its own node with its own per-dialect renderer.

    There is no join predicate for the same reason there is no join key: the
    correlation is containment. Each output row pairs one parent row with one
    element of that parent's own array.
    """

    parent_alias: str
    column: str
    """The parent's array column. Dotted for an array inside a struct."""
    alias: str
    """Alias the unnested element is addressed by."""
    columns: tuple[tuple[str, str], ...] = ()
    """``(code, sql_type)`` per child column. Only MySQL needs them: its
    ``JSON_TABLE`` declares the shape it is extracting rather than inferring it.
    """
    outer: bool = True
    """Keep a parent row whose array is empty. The default, because a charge
    with no labels still contributes its cost to an unfiltered total - measured,
    61% of the rows in a real billing export carry none.
    """


@dataclass(frozen=True)
class NestedField:
    """A column of an unnested element, addressed the way its dialect wants.

    Not a :class:`ColumnRef`, because the engines do not agree that it is one.
    ``L."Key"`` reads the field on six of the seven that unnest, but Snowflake's
    ``FLATTEN`` hands back a row whose ``value`` column holds the element as a
    VARIANT, so there the same reference is ``L.value:"Key"::string`` - measured,
    the column form does not compile at all.

    The node carries the *abstract* type rather than a rendered one so it stays
    dialect-free: only the VARIANT dialect needs a type here, and only it knows
    how to spell one.
    """

    alias: str
    """The unnest's alias - the element, or the row holding it."""
    field: str
    """The element field's physical name (``DataObjectColumn.code``)."""
    abstract_type: str | None = None
    """The column's ``abstractType``, as its OBML spelling."""


@dataclass(frozen=True)
class RegexMatch:
    """Regex match predicate. Each dialect renders its native syntax.

    Postgres uses the ``~`` / ``!~`` operators; Snowflake / BigQuery /
    Dremio / DuckDB use ``REGEXP_LIKE`` (or ``REGEXP_CONTAINS``);
    MySQL / Databricks use ``REGEXP`` / ``RLIKE``; ClickHouse uses ``match``.
    """

    column: Expr
    pattern: str
    negated: bool = False


@dataclass(frozen=True)
class InTimeZone:
    """Read *expr* as wall clock in *zone*. Each dialect renders its own form.

    ``AT TIME ZONE`` on DuckDB and Postgres, ``toTimeZone`` on ClickHouse,
    ``CONVERT_TIMEZONE`` on Snowflake and Dremio, ``CONVERT_TZ`` on MySQL,
    ``DATETIME(x, zone)`` on BigQuery, ``from_utc_timestamp`` on Databricks.

    ``from_zone`` is set when the value carries no zone of its own: a naive
    timestamp only means something once the model says which zone it was
    written in. An engine converting an already-aware value needs no such
    hint, and giving it one would convert twice - measured on MySQL, where
    applying the same conversion twice moves 00:30 to 02:30.
    """

    expr: Expr
    zone: str
    from_zone: str | None = None


@dataclass(frozen=True)
class RelativeDateRange:
    """Relative date range predicate on a column (half-open interval)."""

    column: Expr
    unit: str  # day, week, month, year
    count: int
    direction: str  # past or future
    include_current: bool = True


@dataclass(frozen=True)
class WindowFrame:
    """ROWS/RANGE BETWEEN start AND end."""

    mode: str = "ROWS"  # ROWS | RANGE
    start: str = "UNBOUNDED PRECEDING"
    end: str = "CURRENT ROW"


@dataclass(frozen=True)
class WindowFunction:
    """Window function: func(args) OVER ([PARTITION BY ...] [ORDER BY ...] [frame])."""

    func_name: str
    args: list[Expr] = field(default_factory=list)
    partition_by: list[Expr] = field(default_factory=list)
    order_by: list[OrderByItem] = field(default_factory=list)
    frame: WindowFrame | None = None
    distinct: bool = False


# The union of all expression types.
Expr = (
    Literal
    | Star
    | ColumnRef
    | AliasedExpr
    | FunctionCall
    | BinaryOp
    | UnaryOp
    | IsNull
    | InList
    | CaseExpr
    | Cast
    | SubqueryExpr
    | Exists
    | RawSQL
    | Between
    | RegexMatch
    | RelativeDateRange
    | InTimeZone
    | WindowFunction
    | NestedField
)


@dataclass(frozen=True)
class From:
    """FROM clause: a table name or subquery with optional alias."""

    source: str | Select
    alias: str | None = None


@dataclass(frozen=True)
class Join:
    """JOIN clause."""

    join_type: JoinType
    source: str | Select
    alias: str | None = None
    on: Expr | None = None


@dataclass(frozen=True)
class OrderByItem:
    """ORDER BY item with direction."""

    expr: Expr
    desc: bool = False
    nulls_last: bool | None = None


@dataclass(frozen=True)
class CTE:
    """Common Table Expression: WITH name AS (query or UNION ALL)."""

    name: str
    query: Select | UnionAll | Except | RawSQL


@dataclass(frozen=True)
class Select:
    """A complete SELECT statement."""

    columns: list[Expr] = field(default_factory=list)
    from_: From | None = None
    joins: list[Join | Unnest] = field(default_factory=list)
    """Join clauses and unnests, in the order the planner walked the path.

    One list rather than two, because the order between them matters: an unnest
    names its parent, so it has to follow whatever put that parent in scope.
    Keeping them apart would make the planner interleave them again at render
    time, from information it no longer has.
    """
    where: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    having: Expr | None = None
    order_by: list[OrderByItem] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    ctes: list[CTE] = field(default_factory=list)
    distinct: bool = False
    grouping: str | None = None
    """Hierarchical grouping modifier: 'rollup' or 'cube'.

    When set, the dialect emits ``GROUP BY ROLLUP(...)`` / ``GROUP BY CUBE(...)``
    (or ClickHouse-style ``GROUP BY ... WITH ROLLUP``) instead of plain
    ``GROUP BY``. The planner is responsible for appending the
    ``GROUPING(dim) AS _g_<dim>`` columns to the SELECT projection."""


@dataclass(frozen=True)
class UnionAll:
    """UNION ALL of multiple SELECT statements."""

    queries: list[Select] = field(default_factory=list)


@dataclass(frozen=True)
class Except:
    """EXCEPT of two SELECT statements: left EXCEPT right."""

    left: Select
    right: Select
