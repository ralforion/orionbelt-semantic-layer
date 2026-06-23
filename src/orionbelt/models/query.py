"""Query object models for the YAML-based query language."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from orionbelt.models.semantic import FilterLogic, TimeGrain


class Grouping(StrEnum):
    """Hierarchical grouping modifier — emits GROUP BY ROLLUP/CUBE in SQL."""

    ROLLUP = "rollup"
    CUBE = "cube"


class FilterOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "notequals"
    IN_LIST = "inlist"
    NOT_IN_LIST = "notinlist"
    CONTAINS = "contains"
    NOT_CONTAINS = "notcontains"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    SET = "set"
    NOT_SET = "notset"
    BETWEEN = "between"
    NOT_BETWEEN = "notbetween"
    LIKE = "like"
    NOT_LIKE = "notlike"
    # Simplified operators from spec §4.2
    EQ = "="
    NEQ = "!="
    GREATER = ">"
    GREATER_EQ = ">="
    LESS = "<"
    LESS_EQ = "<="
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    RELATIVE = "relative"
    # Regex match (per-dialect implementation; pattern is the value).
    REGEX = "regex"
    NOT_REGEX = "notregex"
    # Blank check: NULL or empty after trimming whitespace.
    BLANK = "blank"
    NOT_BLANK = "notblank"
    # String length comparisons; value is an int.
    LENGTH_EQ = "length_eq"
    LENGTH_GT = "length_gt"
    LENGTH_LT = "length_lt"
    # Correlated subquery existence checks. Use ``subquery:`` (not ``value:``)
    # to carry the target data object, optional pathName, and optional filter.
    EXISTS = "exists"
    NONEXISTS = "nonexists"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class NullsPosition(StrEnum):
    """Where NULL values sort in an ORDER BY clause.

    Mirrors SQL standard ``NULLS FIRST`` / ``NULLS LAST``. ``None`` on
    ``QueryOrderBy.nulls`` lets the dialect default apply: Postgres
    treats NULLs as larger than non-NULLs (so ASC → last, DESC → first);
    MySQL is the opposite. Setting this explicitly forces deterministic
    behavior across dialects.
    """

    FIRST = "first"
    LAST = "last"


class DimensionRef(BaseModel):
    """Reference to a dimension, optionally with time grain.

    Supports notation like "customer.country" or "order.order_date:month".
    """

    name: str
    grain: TimeGrain | None = None

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @classmethod
    def parse(cls, raw: str) -> DimensionRef:
        """Parse 'name:grain' notation."""
        if ":" in raw:
            name, grain_str = raw.rsplit(":", 1)
            return cls(name=name, grain=TimeGrain(grain_str))
        return cls(name=raw)


class Subquery(BaseModel):
    """Payload for ``exists`` / ``nonexists`` filter operators.

    The filter's ``field`` (subject column) names the *outer* row being
    tested.  ``Subquery`` describes the inner row to check: a target data
    object reachable from the subject via the model's join graph, plus
    optional secondary-join selection and optional predicates restricting
    which target rows count.

    The join columns themselves are **not** restated here — they are
    resolved by walking the model's existing ``joins:`` from the subject's
    data object to ``dataObject`` (same path-resolution machinery the
    query planner uses).
    """

    data_object: str = Field(alias="dataObject")
    path_name: str | None = Field(None, alias="pathName")
    filter: list[QueryFilter] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class QueryFilter(BaseModel):
    """A filter condition in a query."""

    field: str
    op: FilterOperator
    value: Any = None
    subquery: Subquery | None = None

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("value", mode="before")
    @classmethod
    def _validate_filter_value(cls, v: Any) -> Any:
        """Reject arbitrary nested objects — allow scalars, lists of scalars, and dicts
        (for RELATIVE filters which use ``{unit, count, direction}`` objects).
        Date/datetime values are coerced to ISO strings.
        """
        if v is None:
            return v
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, list):
            coerced = [i.isoformat() if isinstance(i, (date, datetime)) else i for i in v]
            if all(isinstance(i, (str, int, float, bool)) for i in coerced):
                return coerced
        if isinstance(v, dict) and all(isinstance(k, str) for k in v):
            return v
        msg = "Filter value must be a scalar, list of scalars, or object"
        raise ValueError(msg)

    @model_validator(mode="after")
    def _validate_subquery_exclusivity(self) -> QueryFilter:
        """``exists`` / ``nonexists`` require ``subquery`` (and reject ``value``).

        All other operators reject ``subquery`` — the payload would be silently
        ignored, which would mask typos.
        """
        is_subquery_op = self.op in (FilterOperator.EXISTS, FilterOperator.NONEXISTS)
        if is_subquery_op:
            if self.subquery is None:
                raise ValueError(
                    f"Operator '{self.op}' requires a 'subquery' object with 'dataObject'"
                )
            if self.value is not None:
                raise ValueError(f"Operator '{self.op}' takes 'subquery', not 'value' / 'values'")
        elif self.subquery is not None:
            raise ValueError(
                f"Operator '{self.op}' does not accept 'subquery' — use 'exists' or 'nonexists'"
            )
        return self


# Resolve forward reference so Subquery.filter (list[QueryFilter]) is fully bound.
Subquery.model_rebuild()


class QueryFilterGroup(BaseModel):
    """A group of query filters combined with AND or OR logic.

    Supports recursive nesting for complex boolean expressions like
    ``(country = 'US' OR region = 'EMEA') AND status != 'Cancelled'``.
    """

    logic: FilterLogic = FilterLogic.AND
    filters: list[QueryFilter | QueryFilterGroup] = []
    negated: bool = False

    model_config = {"populate_by_name": True, "extra": "forbid"}


# Resolve forward reference for recursive QueryFilterGroup
QueryFilterGroup.model_rebuild()

# Union type for query filter items (leaf or group)
QueryFilterItem = QueryFilter | QueryFilterGroup


class QueryOrderBy(BaseModel):
    """Order-by clause in a query."""

    field: str
    direction: SortDirection = SortDirection.ASC
    nulls: NullsPosition | None = None

    model_config = {"populate_by_name": True, "extra": "forbid"}


class CoalesceDimension(BaseModel):
    """Combines multiple role-playing dimensions into a single output column.

    Each named dimension must already exist in the model and resolve to the same
    abstract column type.  In CFL queries, each leg projects only the
    constituent dimension whose ``via:`` matches its fact (others NULL); the
    outer wrapper emits ``COALESCE(d1, d2, ...) AS <alias>`` and groups by the
    alias, collapsing same-person rows that would otherwise stay split across
    role-playing roles.
    """

    coalesce: list[str]
    alias: str = Field(alias="as")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class QuerySelect(BaseModel):
    """The SELECT part of a query.

    Two mutually exclusive modes:

    * **Aggregate mode** (default): ``dimensions`` + ``measures`` produce a
      grouped, aggregated result (GROUP BY dimensions, aggregate measures).
    * **Raw mode**: ``fields`` returns un-aggregated rows from one or more
      data objects joined per the model. Set ``distinct: true`` for
      ``SELECT DISTINCT``. Raw mode rejects ``dimensions``, ``measures``,
      ``metrics``, and ``HAVING``.
    """

    dimensions: list[str | CoalesceDimension] = []
    measures: list[str] = []
    fields: list[str] = []
    distinct: bool = False

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @property
    def is_raw(self) -> bool:
        """True when this select is in raw mode (fields-based projection)."""
        return bool(self.fields)


class UsePathName(BaseModel):
    """Selects a named secondary join path for a specific (source, target) pair."""

    source: str
    target: str
    path_name: str = Field(alias="pathName")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class QueryObject(BaseModel):
    """A complete YAML analytical query."""

    select: QuerySelect
    where: list[QueryFilterItem] = []
    having: list[QueryFilterItem] = []
    order_by: list[QueryOrderBy] = Field([], alias="orderBy")
    limit: int | None = None
    offset: int | None = None
    use_path_names: list[UsePathName] = Field([], alias="usePathNames")
    dimensions_exclude: bool = Field(False, alias="dimensionsExclude")
    grouping: Grouping | None = Field(
        default=None,
        description=(
            "Hierarchical grouping modifier. 'rollup' emits GROUP BY ROLLUP(...) "
            "for hierarchical subtotals + grand total. 'cube' emits GROUP BY CUBE(...) "
            "for the full cross-tab. Adds one GROUPING(dim) AS _g_<dim> column per "
            "selected dimension so callers can distinguish subtotal/grand-total rows."
        ),
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def _validate_grouping(self) -> QueryObject:
        """Reject grouping with no dimensions or in raw mode."""
        if self.grouping is None:
            return self
        if self.select.is_raw:
            raise ValueError(
                "select.fields (raw mode) cannot be combined with grouping (rollup/cube)"
            )
        if not self.select.dimensions:
            raise ValueError(
                "grouping (rollup/cube) requires at least one dimension in select.dimensions"
            )
        return self

    @model_validator(mode="after")
    def _validate_raw_mode_exclusivity(self) -> QueryObject:
        """Raw mode (``select.fields``) is mutually exclusive with aggregate
        features. Catch misuse early so the resolver can assume a clean shape.
        """
        if self.select.is_raw:
            if self.select.dimensions:
                raise ValueError(
                    "select.fields (raw mode) cannot be combined with select.dimensions"
                )
            if self.select.measures:
                raise ValueError("select.fields (raw mode) cannot be combined with select.measures")
            if self.having:
                raise ValueError("select.fields (raw mode) cannot be combined with having")
            if self.dimensions_exclude:
                raise ValueError(
                    "select.fields (raw mode) cannot be combined with dimensionsExclude"
                )
        elif self.select.distinct:
            raise ValueError("select.distinct is only valid in raw mode (with select.fields)")
        return self
