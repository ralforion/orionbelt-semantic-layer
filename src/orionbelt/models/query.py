"""Query object models for the YAML-based query language."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

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
        """
        if v is None:
            return v
        if isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, list) and all(isinstance(i, (str, int, float, bool)) for i in v):
            return v
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


class QuerySelect(BaseModel):
    """The SELECT part of a query: dimensions + measures."""

    dimensions: list[str] = []
    measures: list[str] = []


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
