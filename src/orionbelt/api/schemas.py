"""API request/response Pydantic schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from orionbelt.models.query import QueryObject


class ResolvedInfoResponse(BaseModel):
    """Information about what was resolved during compilation."""

    fact_tables: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)


class ExplainJoinResponse(BaseModel):
    """Explanation of a single join step."""

    from_object: str
    to_object: str
    join_columns: list[str] = Field(default_factory=list)
    reason: str


class ExplainCflLegResponse(BaseModel):
    """Explanation of a single CFL leg."""

    measure_source: str
    common_root: str
    reason: str
    measures: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)


class ExplainPlanResponse(BaseModel):
    """Full query plan explanation with reasoning."""

    planner: str
    planner_reason: str
    base_object: str
    base_object_reason: str
    joins: list[ExplainJoinResponse] = Field(default_factory=list)
    where_filter_count: int = 0
    having_filter_count: int = 0
    has_totals: bool = False
    cfl_legs: list[ExplainCflLegResponse] = Field(default_factory=list)


class QueryCompileResponse(BaseModel):
    """Response body for POST /query/sql."""

    sql: str
    dialect: str
    resolved: ResolvedInfoResponse
    warnings: list[str] = Field(default_factory=list)
    sql_valid: bool = True
    explain: ExplainPlanResponse | None = None


class ColumnMetadata(BaseModel):
    """Metadata for a single result column."""

    name: str
    type: str = Field(description="Type hint: string, number, datetime, binary")


class QueryExecuteResponse(BaseModel):
    """Response body for POST /query/execute."""

    sql: str
    dialect: str
    columns: list[ColumnMetadata] = Field(default_factory=list)
    rows: list[list[object]] = Field(default_factory=list)
    row_count: int = 0
    execution_time_ms: float = 0.0
    resolved: ResolvedInfoResponse = Field(default_factory=ResolvedInfoResponse)
    warnings: list[str] = Field(default_factory=list)
    sql_valid: bool = True
    explain: ExplainPlanResponse | None = None


class SessionQueryExecuteRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/query/execute."""

    model_id: str
    query: QueryObject
    dialect: str = Field(default="postgres")


class ValidateRequest(BaseModel):
    """Request body for POST /validate."""

    model_yaml: str = Field(
        description="YAML semantic model content to validate", max_length=5_000_000
    )


class ValidateResponse(BaseModel):
    """Response body for POST /validate."""

    valid: bool
    errors: list[ErrorDetail] = Field(default_factory=list)
    warnings: list[ErrorDetail] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    """A single validation error detail."""

    code: str
    message: str
    path: str | None = None


class ErrorResponse(BaseModel):
    """Standard error response per spec §7.5."""

    error: str
    message: str
    path: str | None = None


class DialectInfo(BaseModel):
    """Information about a supported dialect."""

    name: str
    capabilities: dict[str, bool] = Field(default_factory=dict)
    unsupported_aggregations: list[str] = Field(default_factory=list)


class DialectListResponse(BaseModel):
    """Response for GET /dialects."""

    dialects: list[DialectInfo] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str = ""


class FlightSettingsInfo(BaseModel):
    """Arrow Flight SQL server status (included when FLIGHT_ENABLED=true)."""

    enabled: bool = True
    port: int = 8815
    auth_mode: str = "none"
    db_vendor: str = "duckdb"


class SettingsResponse(BaseModel):
    """Response for GET /settings — public configuration for clients."""

    single_model_mode: bool = False
    model_yaml: str | None = Field(
        default=None,
        description="Pre-loaded OBML YAML content (only when single_model_mode is true)",
    )
    session_ttl_seconds: int = 1800
    query_execute: bool = Field(
        default=False,
        description="Whether POST /query/execute is available",
    )
    flight: FlightSettingsInfo | None = Field(
        default=None,
        description="Arrow Flight SQL server info (present only when Flight is enabled)",
    )


# ---------------------------------------------------------------------------
# Session schemas
# ---------------------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    """Request body for POST /sessions."""

    metadata: dict[str, str] = Field(default_factory=dict)


class SessionResponse(BaseModel):
    """Single session info."""

    session_id: str
    created_at: datetime
    last_accessed_at: datetime
    model_count: int
    metadata: dict[str, str] = Field(default_factory=dict)


class SessionListResponse(BaseModel):
    """Response for GET /sessions."""

    sessions: list[SessionResponse]


class ModelLoadRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/models."""

    model_yaml: str = Field(description="OBML YAML content", max_length=5_000_000)


class ModelLoadResponse(BaseModel):
    """Response for POST /sessions/{session_id}/models."""

    model_id: str
    data_objects: int
    dimensions: int
    measures: int
    metrics: int
    warnings: list[str] = Field(default_factory=list)


class ModelSummaryResponse(BaseModel):
    """Short model summary for listing."""

    model_id: str
    data_objects: int
    dimensions: int
    measures: int
    metrics: int


class SessionQueryRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/query/sql."""

    model_id: str
    query: QueryObject
    dialect: str = Field(default="postgres")


class DiagramResponse(BaseModel):
    """Response for GET /sessions/{session_id}/models/{model_id}/diagram/er."""

    mermaid: str = Field(description="Mermaid ER diagram script")


# ---------------------------------------------------------------------------
# OSI ↔ OBML conversion schemas
# ---------------------------------------------------------------------------


class ConvertRequest(BaseModel):
    """Request body for POST /convert/osi-to-obml."""

    input_yaml: str = Field(description="Source YAML content to convert", max_length=5_000_000)


class OBMLtoOSIRequest(ConvertRequest):
    """Request body for POST /convert/obml-to-osi."""

    model_name: str = Field(default="semantic_model", description="Name for the OSI model")
    model_description: str = Field(default="", description="Description for the OSI model")
    ai_instructions: str = Field(default="", description="AI instructions for the OSI model")


class ValidationDetail(BaseModel):
    """Validation result from conversion."""

    schema_valid: bool = True
    semantic_valid: bool = True
    schema_errors: list[str] = Field(default_factory=list)
    semantic_errors: list[str] = Field(default_factory=list)
    semantic_warnings: list[str] = Field(default_factory=list)


class ConvertResponse(BaseModel):
    """Response body for conversion endpoints."""

    output_yaml: str = Field(description="Converted YAML content")
    warnings: list[str] = Field(default_factory=list, description="Conversion warnings")
    validation: ValidationDetail = Field(
        default_factory=ValidationDetail, description="Validation results"
    )


# ---------------------------------------------------------------------------
# Model discovery schemas
# ---------------------------------------------------------------------------


class ColumnDetail(BaseModel):
    """Detail of a data object column."""

    name: str
    code: str
    abstract_type: str
    num_class: str | None = None
    description: str | None = None
    comment: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class DataObjectDetail(BaseModel):
    """Detail of a data object."""

    name: str
    code: str
    database: str
    schema_name: str = Field(alias="schema")
    columns: list[ColumnDetail] = Field(default_factory=list)
    join_targets: list[str] = Field(default_factory=list)
    description: str | None = None
    comment: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class DimensionDetail(BaseModel):
    """Detail of a dimension."""

    name: str
    data_object: str
    column: str
    result_type: str
    time_grain: str | None = None
    description: str | None = None
    format: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class MeasureDetail(BaseModel):
    """Detail of a measure."""

    name: str
    result_type: str
    aggregation: str
    expression: str | None = None
    columns: list[dict[str, str]] = Field(default_factory=list)
    distinct: bool = False
    total: bool = False
    description: str | None = None
    format: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class MetricDetail(BaseModel):
    """Detail of a metric."""

    name: str
    type: str = "derived"
    expression: str | None = None
    measure: str | None = None
    time_dimension: str | None = Field(None, alias="timeDimension")
    component_measures: list[str] = Field(default_factory=list)
    description: str | None = None
    format: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class SchemaResponse(BaseModel):
    """Response for GET /schema — full model structure."""

    model_id: str
    version: float = 1.0
    description: str | None = None
    owner: str | None = None
    data_objects: list[DataObjectDetail] = Field(default_factory=list)
    dimensions: list[DimensionDetail] = Field(default_factory=list)
    measures: list[MeasureDetail] = Field(default_factory=list)
    metrics: list[MetricDetail] = Field(default_factory=list)


class ExplainLineageItem(BaseModel):
    """A single item in the lineage chain."""

    type: str
    name: str
    detail: str | None = None


class ExplainResponse(BaseModel):
    """Response for GET /explain/{name} — lineage & composition."""

    name: str
    type: str
    lineage: list[ExplainLineageItem] = Field(default_factory=list)


class SearchRequest(BaseModel):
    """Request body for POST /find."""

    query: str = Field(description="Search term")
    types: list[str] = Field(
        default_factory=lambda: ["dimension", "measure", "metric", "data_object"],
        description="Object types to search (dimension, measure, metric, data_object)",
    )


class SearchResultItem(BaseModel):
    """A single search result."""

    type: str
    name: str
    match_field: str
    score: float = 1.0


class SearchResponse(BaseModel):
    """Response for POST /find."""

    results: list[SearchResultItem] = Field(default_factory=list)


class JoinEdge(BaseModel):
    """A single edge in the join graph."""

    from_object: str
    to_object: str
    cardinality: str
    columns_from: list[str] = Field(default_factory=list)
    columns_to: list[str] = Field(default_factory=list)
    secondary: bool = False
    path_name: str | None = None


class JoinGraphResponse(BaseModel):
    """Response for GET /join-graph — adjacency list."""

    nodes: list[str] = Field(default_factory=list)
    edges: list[JoinEdge] = Field(default_factory=list)
