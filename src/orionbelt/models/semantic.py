"""Core semantic model types: facts, dimensions, measures, metrics, relationships."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from orionbelt.models.types import parse_data_type


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
    # Statistical aggregates (v2.6+) — spread, association, regression
    STDDEV = "stddev"
    STDDEV_POP = "stddev_pop"
    VARIANCE = "variance"
    VAR_POP = "var_pop"
    CORR = "corr"
    COVAR_POP = "covar_pop"
    COVAR_SAMP = "covar_samp"
    REGR_SLOPE = "regr_slope"
    REGR_INTERCEPT = "regr_intercept"


# Aggregations that take two columns. Compiled to ``AGG(col_a, col_b)`` —
# the order in ``Measure.columns`` is significant.
TWO_COLUMN_AGGREGATIONS: frozenset[str] = frozenset(
    {
        AggregationType.CORR.value,
        AggregationType.COVAR_POP.value,
        AggregationType.COVAR_SAMP.value,
        AggregationType.REGR_SLOPE.value,
        AggregationType.REGR_INTERCEPT.value,
    }
)

# Aggregations that require exactly one column.
SINGLE_COLUMN_STATISTICAL_AGGREGATIONS: frozenset[str] = frozenset(
    {
        AggregationType.STDDEV.value,
        AggregationType.STDDEV_POP.value,
        AggregationType.VARIANCE.value,
        AggregationType.VAR_POP.value,
    }
)


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
    WINDOW = "window"


class WindowFunctionKind(StrEnum):
    """SQL window-function family for ``MetricType.WINDOW``.

    Single-row-output window functions (no aggregation):
    ranking, offsetting, and positional. Aggregating window
    functions (SUM/AVG over a frame) belong to
    ``MetricType.CUMULATIVE``.
    """

    RANK = "rank"
    DENSE_RANK = "dense_rank"
    ROW_NUMBER = "row_number"
    NTILE = "ntile"
    LAG = "lag"
    LEAD = "lead"
    FIRST_VALUE = "first_value"
    LAST_VALUE = "last_value"


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


class GrainMode(StrEnum):
    RELATIVE = "RELATIVE"
    FIXED = "FIXED"


class FilterContextMode(StrEnum):
    RELATIVE = "RELATIVE"
    FIXED = "FIXED"


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

    model_config = {"populate_by_name": True, "extra": "forbid"}


class GrainOverride(BaseModel):
    """Grain override for a measure — controls aggregation grain independently from query."""

    mode: GrainMode = GrainMode.RELATIVE
    exclude: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    keep_only: list[str] = Field(default_factory=list, alias="keepOnly")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def _validate_grain_override(self) -> GrainOverride:
        if self.mode == GrainMode.FIXED and self.exclude:
            raise ValueError("grain.mode FIXED cannot have 'exclude' (nothing to exclude from)")
        return self


class FilterContextFilter(BaseModel):
    """A static filter to include in a filterContext — same shape as a query filter."""

    field: str
    op: str
    value: object = None

    model_config = {"populate_by_name": True, "extra": "forbid"}


class FilterContext(BaseModel):
    """Filter context override for a measure — controls which query WHERE filters apply."""

    mode: FilterContextMode = FilterContextMode.RELATIVE
    exclude: list[str] = Field(default_factory=list)
    include: list[FilterContextFilter] = Field(default_factory=list)
    keep_only: list[str] = Field(default_factory=list, alias="keepOnly")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def _validate_filter_context(self) -> FilterContext:
        if self.mode == FilterContextMode.FIXED and self.exclude:
            raise ValueError(
                "filterContext.mode FIXED cannot have 'exclude' (nothing to exclude from)"
            )
        return self


class DataColumnRef(BaseModel):
    """Reference to a data object column by dataObject + column pair."""

    view: str | None = Field(None, alias="dataObject")
    column: str | None = None

    model_config = {"populate_by_name": True, "extra": "forbid"}


class DataObjectColumn(BaseModel):
    """A column within a data object (maps to a database column or expression).

    When ``expression`` is set, the column is **computed** — the column's
    ``code`` is ignored and the SQL expression is inlined wherever the
    column is referenced. ``{name}`` placeholders inside the expression
    refer to other columns of the same data object (by their label) and
    are substituted with the referenced column's physical ``code``,
    table-qualified at codegen time.

    Example::

        columns:
          reportingdateyear:  { code: reportingdateyear, abstractType: int }
          reportingdatemonth: { code: reportingdatemonth, abstractType: int }
          reporting_period:
            expression: "({reportingdateyear} * 100 + {reportingdatemonth})"
            abstractType: int

    Note that an expression is dialect-leaky — pin the model's
    ``settings.defaultDialect`` if your expression uses vendor-specific
    syntax (``regexp_replace``, ``btrim``, etc.).
    """

    label: str
    code: str = ""
    abstract_type: DataType = Field(alias="abstractType")
    sql_type: str | None = Field(None, alias="sqlType")
    sql_precision: int | None = Field(None, alias="sqlPrecision")
    sql_scale: int | None = Field(None, alias="sqlScale")
    num_class: NumClass | None = Field(None, alias="numClass")
    primary_key: bool = Field(False, alias="primaryKey")
    description: str | None = None
    comment: str | None = None
    owner: str | None = None
    expression: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @property
    def is_computed(self) -> bool:
        """True when the column is defined by an inline SQL expression."""
        return self.expression is not None


class DataObjectJoin(BaseModel):
    """Join definition on a data object, connecting it to another data object."""

    join_type: Cardinality = Field(alias="joinType")
    join_to: str = Field(alias="joinTo")
    columns_from: list[str] = Field(alias="columnsFrom")
    columns_to: list[str] = Field(alias="columnsTo")
    secondary: bool = False
    path_name: str | None = Field(None, alias="pathName")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class RefreshPolicy(BaseModel):
    """Source-table freshness contract.

    Lives on the :class:`DataObject` that maps to a physical table. See
    ``design/PLAN_freshness_driven_cache.md`` §5 for the full design and §8
    for how multiple contracts compose at query time.

    Modes:
    - ``interval``: table refreshes on a fixed cadence; ``interval`` required.
    - ``heartbeat``: table refreshes irregularly; ``max_staleness`` required.
    - ``static``: table effectively never changes (lookup tables).
    """

    mode: str = Field(description="One of: interval | heartbeat | static")
    interval: str | None = Field(
        default=None,
        description="ISO 8601 duration or shorthand (e.g. '1h', '15m', '1d')",
    )
    anchor: str | None = Field(
        default=None,
        description="Optional time-of-day anchor 'HH:MM' for interval mode",
    )
    timezone: str | None = Field(
        default=None,
        description="IANA TZ name. Used only when anchor is set. Default UTC.",
    )
    max_staleness: str | None = Field(
        default=None,
        alias="maxStaleness",
        description="Max time between heartbeats before the table is stale",
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


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
    refresh: RefreshPolicy | None = Field(
        default=None,
        description=(
            "Optional freshness contract for the physical table this dataObject maps to. "
            "Drives result-cache TTL composition. PLAN_freshness_driven_cache.md §5."
        ),
    )

    @property
    def qualified_code(self) -> str:
        """Full qualified table reference: database.schema.code."""
        return f"{self.database}.{self.schema_name}.{self.code}"

    model_config = {"populate_by_name": True, "extra": "forbid"}


class Dimension(BaseModel):
    """A named dimension referencing a data object column."""

    label: str
    view: str = Field(alias="dataObject")
    column: str = ""
    result_type: DataType = Field(DataType.STRING, alias="resultType")
    time_grain: TimeGrain | None = Field(None, alias="timeGrain")
    description: str | None = None
    format: str | None = None
    via: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class FilterValue(BaseModel):
    """A typed value used in measure filters."""

    data_type: DataType = Field(alias="dataType")
    is_null: bool | None = Field(None, alias="isNull")
    value_string: str | None = Field(None, alias="valueString")
    value_int: int | None = Field(None, alias="valueInt")
    value_float: float | None = Field(None, alias="valueFloat")
    value_date: str | None = Field(None, alias="valueDate")
    value_boolean: bool | None = Field(None, alias="valueBoolean")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class MeasureFilter(BaseModel):
    """Filter applied to a measure."""

    column: DataColumnRef | None = None
    operator: str
    values: list[FilterValue] = []

    model_config = {"populate_by_name": True, "extra": "forbid"}


class MeasureFilterGroup(BaseModel):
    """A group of measure filters combined with AND or OR logic.

    Supports recursive nesting for complex boolean expressions like
    ``(country = 'US' OR country = 'CA') AND status = 'Active'``.
    """

    logic: FilterLogic = FilterLogic.AND
    filters: list[MeasureFilter | MeasureFilterGroup] = []
    negated: bool = False

    model_config = {"populate_by_name": True, "extra": "forbid"}


# Resolve forward reference for recursive MeasureFilterGroup
MeasureFilterGroup.model_rebuild()

# Union type for measure filter items (leaf or group)
MeasureFilterItem = MeasureFilter | MeasureFilterGroup


class WithinGroup(BaseModel):
    """WITHIN GROUP ordering clause for LISTAGG measures."""

    column: DataColumnRef
    order: str = "ASC"

    model_config = {"populate_by_name": True, "extra": "forbid"}


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

    model_config = {"populate_by_name": True, "extra": "forbid"}


class Measure(BaseModel):
    """An aggregation measure with optional expression template."""

    label: str
    columns: list[DataColumnRef] = []
    result_type: DataType = Field(DataType.FLOAT, alias="resultType")
    aggregation: str
    expression: str | None = None
    distinct: bool = False
    total: bool = False
    grain: GrainOverride | None = None
    filter_context: FilterContext | None = Field(None, alias="filterContext")
    filters: list[MeasureFilterItem] = []
    data_type: str | None = Field(None, alias="dataType")
    description: str | None = None
    format: str | None = None
    allow_fan_out: bool = Field(False, alias="allowFanOut")
    delimiter: str | None = None
    within_group: WithinGroup | None = Field(None, alias="withinGroup")
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("data_type", mode="before")
    @classmethod
    def _validate_data_type(cls, v: str | None) -> str | None:
        if v is not None:
            parse_data_type(v)
        return v

    @model_validator(mode="after")
    def _validate_total_grain_exclusion(self) -> Measure:
        if self.total and self.grain is not None:
            raise ValueError("'total: true' and 'grain' are mutually exclusive")
        return self

    @model_validator(mode="after")
    def _validate_statistical_aggregation_arity(self) -> Measure:
        """Reject malformed statistical aggregates at model-load time.

        Two-column aggregates (``corr``, ``covar_*``, ``regr_*``) require
        exactly two entries in ``columns``. Single-column statistical
        aggregates (``stddev``, ``stddev_pop``, ``variance``, ``var_pop``)
        require exactly one.

        ``expression:`` form is **not allowed** for two-column
        aggregates — a single expression string collapses to one scalar
        argument, producing invalid SQL like ``CORR((a + b))`` instead
        of ``CORR(a, b)``. To express per-argument transformations on
        two-column aggregates, define the inputs as computed columns on
        the data object and reference them via ``columns:``.

        Single-column statistical aggregates (``stddev`` etc.) DO accept
        ``expression:`` — the result ``STDDEV(<scalar expression>)`` is
        valid SQL.
        """
        agg = self.aggregation.lower()
        if self.expression is not None:
            if agg in TWO_COLUMN_AGGREGATIONS:
                raise ValueError(
                    f"Aggregation '{agg}' requires exactly 2 columns and cannot be "
                    "combined with 'expression:'. Use the 'columns:' list with two "
                    "entries (define computed columns on the data object if you need "
                    "per-argument transformations) so the aggregate's argument order "
                    "is explicit."
                )
            return self
        if agg in TWO_COLUMN_AGGREGATIONS and len(self.columns) != 2:
            raise ValueError(
                f"Aggregation '{agg}' requires exactly 2 columns, got {len(self.columns)}"
            )
        if agg in SINGLE_COLUMN_STATISTICAL_AGGREGATIONS and len(self.columns) != 1:
            raise ValueError(
                f"Aggregation '{agg}' requires exactly 1 column, got {len(self.columns)}"
            )
        return self


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
    # Per-dimension partitioning for cumulative + window metrics. Each entry
    # must be a model dimension reachable from the measure's source object.
    partition_by: list[str] = Field(default_factory=list, alias="partitionBy")
    # Period-over-Period metrics
    period_over_period: PeriodOverPeriod | None = Field(None, alias="periodOverPeriod")
    # Window metrics (rank / lag / lead / ntile / first_value / last_value)
    window_function: WindowFunctionKind | None = Field(None, alias="windowFunction")
    offset: int | None = None
    buckets: int | None = None
    order_direction: str = Field("desc", alias="orderDirection")
    default_value: str | int | float | bool | None = Field(None, alias="defaultValue")
    # Common
    data_type: str | None = Field(None, alias="dataType")
    description: str | None = None
    format: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("data_type", mode="before")
    @classmethod
    def _validate_data_type(cls, v: str | None) -> str | None:
        if v is not None:
            parse_data_type(v)
        return v

    @model_validator(mode="after")
    def _validate_metric_type(self) -> Metric:
        if self.type == MetricType.DERIVED:
            if not self.expression:
                raise ValueError("Derived metrics require 'expression'")
            if self.partition_by:
                raise ValueError("Derived metrics must not have 'partitionBy'")
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
            if self.partition_by:
                raise ValueError("Period-over-period metrics must not have 'partitionBy'")
        elif self.type == MetricType.WINDOW:
            if self.window_function is None:
                raise ValueError("Window metrics require 'windowFunction'")
            if not self.measure and self.window_function not in {
                WindowFunctionKind.ROW_NUMBER,
                WindowFunctionKind.NTILE,
            }:
                # row_number / ntile can rank without an explicit measure, falling back
                # to ordering on the time dimension. All other window functions take
                # the measure as their argument or ORDER BY input.
                raise ValueError(
                    f"Window metric with function '{self.window_function.value}' requires 'measure'"
                )
            if self.expression:
                raise ValueError("Window metrics must not have 'expression'")
            if self.window is not None or self.grain_to_date is not None:
                raise ValueError("Window metrics must not have 'window' or 'grainToDate'")
            if self.window_function in {WindowFunctionKind.LAG, WindowFunctionKind.LEAD}:
                if self.offset is None or self.offset < 1:
                    raise ValueError(
                        f"Window metric with function '{self.window_function.value}' "
                        f"requires positive 'offset'"
                    )
                if not self.time_dimension:
                    raise ValueError(
                        f"Window metric with function '{self.window_function.value}' "
                        f"requires 'timeDimension'"
                    )
            if self.window_function == WindowFunctionKind.NTILE and (
                self.buckets is None or self.buckets < 2
            ):
                raise ValueError("Window metric with function 'ntile' requires 'buckets' >= 2")
            if self.order_direction.lower() not in {"asc", "desc"}:
                raise ValueError("'orderDirection' must be 'asc' or 'desc'")
        return self


class ModelSettings(BaseModel):
    """Model-level settings controlling compilation and execution behavior."""

    default_numeric_data_type: str | None = Field(None, alias="defaultNumericDataType")
    default_timezone: str | None = Field(None, alias="defaultTimezone")
    override_database_timezone: bool = Field(False, alias="overrideDatabaseTimezone")
    default_dialect: str | None = Field(None, alias="defaultDialect")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("default_numeric_data_type", mode="before")
    @classmethod
    def _validate_default_type(cls, v: str | None) -> str | None:
        if v is not None:
            from orionbelt.models.types import DecimalType

            parsed = parse_data_type(v)
            if not isinstance(parsed, DecimalType):
                raise ValueError(f"defaultNumericDataType must be a decimal(p, s) type, got '{v}'")
        return v

    @field_validator("default_timezone", mode="before")
    @classmethod
    def _validate_timezone(cls, v: str | None) -> str | None:
        if v is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, KeyError):
                raise ValueError(
                    f"defaultTimezone must be a valid IANA timezone, got '{v}'"
                ) from None
        return v

    @field_validator("default_dialect", mode="before")
    @classmethod
    def _validate_default_dialect(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Importing the dialect package triggers the 8 self-registrations as
        # a side effect; deferred here to avoid a parser → dialect import
        # dependency at module load time.
        import orionbelt.dialect  # noqa: F401
        from orionbelt.dialect.registry import DialectRegistry

        registered = DialectRegistry.available()
        if v not in registered:
            raise ValueError(f"defaultDialect must be one of: {', '.join(registered)} — got '{v}'")
        return v


class ModelFilter(BaseModel):
    """Static WHERE filter applied to every query against this model."""

    data_object: str = Field(alias="dataObject")
    column: str
    operator: str
    value: str | int | float | bool | None = None
    values: list[str | int | float | bool] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ModelExample(BaseModel):
    """Canonical example query authored alongside a model.

    See ``design/PLAN_agent_api_improvements.md`` §5. Stored on the
    :class:`SemanticModel` and surfaced via the examples endpoints so agents
    can discover the kinds of questions a model is designed to answer.
    """

    name: str = Field(description="Snake_case identifier, unique within the model")
    description: str = Field(description="One- or two-sentence explanation")
    intent_tags: list[str] = Field(
        default_factory=list,
        alias="intentTags",
        description="Free-form tags used by ?intent= filters",
    )
    query: dict[str, object] = Field(
        description="Full QueryObject payload, valid against this model"
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


class SemanticModel(BaseModel):
    """Complete semantic model parsed from OBML YAML."""

    version: float = 1.0
    name: str | None = Field(
        default=None,
        description=(
            "Optional addressing identifier for multi-model mode (v2.4.0+). "
            "When unset, the multi-model loader uses the filename stem. "
            "After normalization (lowercase + spaces/dots/dashes → "
            "underscores + trim) must match ``^[a-z][a-z0-9_]{0,62}$``. "
            "BI tools select this model via the Flight `database` catalog "
            "or pgwire `database=` URL parameter."
        ),
    )
    description: str | None = None
    settings: ModelSettings | None = None
    data_objects: dict[str, DataObject] = Field(default={}, alias="dataObjects")
    dimensions: dict[str, Dimension] = {}
    measures: dict[str, Measure] = {}
    metrics: dict[str, Metric] = {}
    filters: list[ModelFilter] = Field(default_factory=list)
    examples: list[ModelExample] = Field(default_factory=list)
    extends_sources: list[str] = Field(default_factory=list)
    inherits_source: str | None = None
    owner: str | None = None
    custom_extensions: list[CustomExtension] = Field(default_factory=list, alias="customExtensions")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, v: str | None) -> str | None:
        """Reject invalid names early. Pydantic validators raise ValueError
        which the loader turns into a model-validation error.

        Empty / whitespace-only strings are treated as ``None`` rather than
        passed through, so an empty ``name:`` in YAML falls back to the
        filename stem at startup.
        """
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("name must be a string")
        if not v.strip():
            return None
        # Use the same normalization pipeline the loader uses, so an OBML
        # `name:` that's invalid surfaces during parse-time rather than
        # only at startup. The normalized value is stored on the model.
        from orionbelt.models.identifiers import (
            ModelNameError,
            normalize_model_name,
        )

        try:
            return normalize_model_name(v, source="OBML `name:` field")
        except ModelNameError as exc:
            raise ValueError(str(exc)) from None
