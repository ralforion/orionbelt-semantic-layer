"""Query object models for the YAML-based query language."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from orionbelt.models.semantic import FilterLogic, TimeGrain


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


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class DimensionRef(BaseModel):
    """Reference to a dimension, optionally with time grain.

    Supports notation like "customer.country" or "order.order_date:month".
    """

    name: str
    grain: TimeGrain | None = None

    @classmethod
    def parse(cls, raw: str) -> DimensionRef:
        """Parse 'name:grain' notation."""
        if ":" in raw:
            name, grain_str = raw.rsplit(":", 1)
            return cls(name=name, grain=TimeGrain(grain_str))
        return cls(name=raw)


class QueryFilter(BaseModel):
    """A filter condition in a query."""

    field: str
    op: FilterOperator
    value: Any = None

    model_config = {"populate_by_name": True}

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


class QueryFilterGroup(BaseModel):
    """A group of query filters combined with AND or OR logic.

    Supports recursive nesting for complex boolean expressions like
    ``(country = 'US' OR region = 'EMEA') AND status != 'Cancelled'``.
    """

    logic: FilterLogic = FilterLogic.AND
    filters: list[QueryFilter | QueryFilterGroup] = []
    negated: bool = False

    model_config = {"populate_by_name": True}


# Resolve forward reference for recursive QueryFilterGroup
QueryFilterGroup.model_rebuild()

# Union type for query filter items (leaf or group)
QueryFilterItem = QueryFilter | QueryFilterGroup


class QueryOrderBy(BaseModel):
    """Order-by clause in a query."""

    field: str
    direction: SortDirection = SortDirection.ASC


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

    model_config = {"populate_by_name": True}


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

    @property
    def is_raw(self) -> bool:
        """True when this select is in raw mode (fields-based projection)."""
        return bool(self.fields)


class UsePathName(BaseModel):
    """Selects a named secondary join path for a specific (source, target) pair."""

    source: str
    target: str
    path_name: str = Field(alias="pathName")

    model_config = {"populate_by_name": True}


class QueryObject(BaseModel):
    """A complete YAML analytical query."""

    select: QuerySelect
    where: list[QueryFilterItem] = []
    having: list[QueryFilterItem] = []
    order_by: list[QueryOrderBy] = Field([], alias="order_by")
    limit: int | None = None
    offset: int | None = None
    use_path_names: list[UsePathName] = Field([], alias="usePathNames")
    dimensions_exclude: bool = Field(False, alias="dimensionsExclude")

    model_config = {"populate_by_name": True}

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
