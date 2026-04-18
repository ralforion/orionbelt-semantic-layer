"""Core semantic model types: facts, dimensions, measures, metrics, relationships."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class DataType(StrEnum):
    STRING = "string"
    JSON = "json"
    INT = "int"
    FLOAT = "float"
    DATE = "date"
    TIME = "time"
    TIME_TZ = "time_tz"
    TIMESTAMP = "timestamp"
    TIMESTAMP_TZ = "timestamp_tz"
    BOOLEAN = "boolean"


class AggregationType(StrEnum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    ANY_VALUE = "any_value"
    MEDIAN = "median"
    MODE = "mode"
    LISTAGG = "listagg"


class JoinType(StrEnum):
    LEFT = "left"
    INNER = "inner"
    RIGHT = "right"
    FULL = "full"


class Cardinality(StrEnum):
    MANY_TO_ONE = "many-to-one"
    ONE_TO_ONE = "one-to-one"
    MANY_TO_MANY = "many-to-many"


class TimeGrain(StrEnum):
    YEAR = "year"
    QUARTER = "quarter"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    HOUR = "hour"
    MINUTE = "minute"
    SECOND = "second"


class NumClass(StrEnum):
    CATEGORICAL = "categorical"
    ADDITIVE = "additive"
    NON_ADDITIVE = "non-additive"


class MetricType(StrEnum):
    DERIVED = "derived"
    CUMULATIVE = "cumulative"
    PERIOD_OVER_PERIOD = "period_over_period"


class PeriodOverPeriodComparison(StrEnum):
    RATIO = "ratio"
    DIFFERENCE = "difference"
    PREVIOUS_VALUE = "previousValue"
    PERCENT_CHANGE = "percentChange"


class CumulativeAggType(StrEnum):
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT = "count"


class GrainToDate(StrEnum):
    YEAR = "year"
    QUARTER = "quarter"
    MONTH = "month"
    WEEK = "week"


class FilterLogic(StrEnum):
    AND = "and"
    OR = "or"


class CustomExtension(BaseModel):
    """Vendor-keyed extension data — opaque to OrionBelt.

    Used for preserving metadata from external formats (e.g. OSI ``ai_context``),
    governance tags, lineage information, or other vendor-specific data.
    """

    vendor: str
    data: str

    model_config = {"populate_by_name": True}


class DataColumnRef(BaseModel):
    """Reference to a data object column by dataObject + column pair."""

    view: str | None = Field(None, alias="dataObject")
    column: str | None = None

    model_config = {"populate_by_name": True}


class DataObjectColumn(BaseModel):
    """A column within a data object (maps to a database column or expression)."""

    label: str
    code: str
    abstract_type: DataType = Field(alias="abstractType")
    sql_type: str | None = Field(None, alias="sqlType")
    sql_precision: int | None = Field(None, alias="sqlPrecision")
    sql_scale: int | None = Field(None, alias="sqlScale")
    num_class: NumClass | None = Field(None, alias="numClass")
    description: str | None = None
    comment: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True}


class DataObjectJoin(BaseModel):
    """Join definition on a data object, connecting it to another data object."""

    join_type: Cardinality = Field(alias="joinType")
    join_to: str = Field(alias="joinTo")
    columns_from: list[str] = Field(alias="columnsFrom")
    columns_to: list[str] = Field(alias="columnsTo")
    secondary: bool = False
    path_name: str | None = Field(None, alias="pathName")

    model_config = {"populate_by_name": True}


class DataObject(BaseModel):
    """A database table or view with its columns and joins."""

    label: str
    code: str
    database: str
    schema_name: str = Field(alias="schema")
    columns: dict[str, DataObjectColumn] = {}
    joins: list[DataObjectJoin] = []
    description: str | None = None
    comment: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    @property
    def qualified_code(self) -> str:
        """Full qualified table reference: database.schema.code."""
        return f"{self.database}.{self.schema_name}.{self.code}"

    model_config = {"populate_by_name": True}


class Dimension(BaseModel):
    """A named dimension referencing a data object column."""

    label: str
    view: str = Field(alias="dataObject")
    column: str = ""
    result_type: DataType = Field(DataType.STRING, alias="resultType")
    time_grain: TimeGrain | None = Field(None, alias="timeGrain")
    description: str | None = None
    format: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True}


class FilterValue(BaseModel):
    """A typed value used in measure filters."""

    data_type: DataType = Field(alias="dataType")
    is_null: bool | None = Field(None, alias="isNull")
    value_string: str | None = Field(None, alias="valueString")
    value_int: int | None = Field(None, alias="valueInt")
    value_float: float | None = Field(None, alias="valueFloat")
    value_date: str | None = Field(None, alias="valueDate")
    value_boolean: bool | None = Field(None, alias="valueBoolean")

    model_config = {"populate_by_name": True}


class MeasureFilter(BaseModel):
    """Filter applied to a measure."""

    column: DataColumnRef | None = None
    operator: str
    values: list[FilterValue] = []

    model_config = {"populate_by_name": True}


class MeasureFilterGroup(BaseModel):
    """A group of measure filters combined with AND or OR logic.

    Supports recursive nesting for complex boolean expressions like
    ``(country = 'US' OR country = 'CA') AND status = 'Active'``.
    """

    logic: FilterLogic = FilterLogic.AND
    filters: list[MeasureFilter | MeasureFilterGroup] = []
    negated: bool = False

    model_config = {"populate_by_name": True}


# Resolve forward reference for recursive MeasureFilterGroup
MeasureFilterGroup.model_rebuild()

# Union type for measure filter items (leaf or group)
MeasureFilterItem = MeasureFilter | MeasureFilterGroup


class WithinGroup(BaseModel):
    """WITHIN GROUP ordering clause for LISTAGG measures."""

    column: DataColumnRef
    order: str = "ASC"

    model_config = {"populate_by_name": True}


class PeriodOverPeriod(BaseModel):
    """Configuration for period-over-period metric comparison.

    Defines how to shift time and compare measure values between
    the current period and a previous period.
    """

    time_dimension: str = Field(alias="timeDimension")
    grain: TimeGrain
    offset: int = -1
    offset_grain: TimeGrain = Field(alias="offsetGrain")
    comparison: PeriodOverPeriodComparison = PeriodOverPeriodComparison.PERCENT_CHANGE

    model_config = {"populate_by_name": True}


class Measure(BaseModel):
    """An aggregation measure with optional expression template."""

    label: str
    columns: list[DataColumnRef] = []
    result_type: DataType = Field(DataType.FLOAT, alias="resultType")
    aggregation: str
    expression: str | None = None
    distinct: bool = False
    total: bool = False
    filters: list[MeasureFilterItem] = []
    description: str | None = None
    format: str | None = None
    allow_fan_out: bool = Field(False, alias="allowFanOut")
    delimiter: str | None = None
    within_group: WithinGroup | None = Field(None, alias="withinGroup")
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True}


class Metric(BaseModel):
    """A metric: derived expression, cumulative window, or period-over-period comparison.

    **Derived** (default): references measures by name using ``{[Measure Name]}`` syntax.
    **Cumulative**: applies a window function to an existing measure, ordered by a time
    dimension.  Supports running totals, rolling windows, and grain-to-date resets.
    **Period-over-Period**: compares a measure's value against a prior time period using
    a synthetical date spine.  Supports ratio, difference, previous value, and percent change.
    """

    label: str
    type: MetricType = MetricType.DERIVED
    # Derived metrics
    expression: str | None = None
    # Cumulative metrics
    measure: str | None = None
    time_dimension: str | None = Field(None, alias="timeDimension")
    cumulative_type: CumulativeAggType = Field(CumulativeAggType.SUM, alias="cumulativeType")
    window: int | None = None
    grain_to_date: GrainToDate | None = Field(None, alias="grainToDate")
    # Period-over-Period metrics
    period_over_period: PeriodOverPeriod | None = Field(None, alias="periodOverPeriod")
    # Common
    description: str | None = None
    format: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_metric_type(self) -> Metric:
        if self.type == MetricType.DERIVED:
            if not self.expression:
                raise ValueError("Derived metrics require 'expression'")
        elif self.type == MetricType.CUMULATIVE:
            if not self.measure:
                raise ValueError("Cumulative metrics require 'measure'")
            if not self.time_dimension:
                raise ValueError("Cumulative metrics require 'timeDimension'")
            if self.expression:
                raise ValueError("Cumulative metrics must not have 'expression'")
            if self.window is not None and self.grain_to_date is not None:
                raise ValueError("'window' and 'grainToDate' are mutually exclusive")
            if self.window is not None and self.window < 1:
                raise ValueError("'window' must be >= 1")
        elif self.type == MetricType.PERIOD_OVER_PERIOD:
            if not self.expression:
                raise ValueError("Period-over-period metrics require 'expression'")
            if not self.period_over_period:
                raise ValueError("Period-over-period metrics require 'periodOverPeriod'")
            if self.measure:
                raise ValueError(
                    "Period-over-period metrics must not have 'measure' "
                    "(use 'expression' to reference measures)"
                )
            if self.window is not None or self.grain_to_date is not None:
                raise ValueError(
                    "Period-over-period metrics must not have 'window' or 'grainToDate'"
                )
        return self


class ModelFilter(BaseModel):
    """Static WHERE filter applied to every query against this model."""

    data_object: str = Field(alias="dataObject")
    column: str
    operator: str
    value: str | int | float | bool | None = None
    values: list[str | int | float | bool] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class SemanticModel(BaseModel):
    """Complete semantic model parsed from OBML YAML."""

    version: float = 1.0
    description: str | None = None
    data_objects: dict[str, DataObject] = Field(default={}, alias="dataObjects")
    dimensions: dict[str, Dimension] = {}
    measures: dict[str, Measure] = {}
    metrics: dict[str, Metric] = {}
    filters: list[ModelFilter] = Field(default_factory=list)
    extends_sources: list[str] = Field(default_factory=list)
    inherits_source: str | None = None
    owner: str | None = None
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True}
