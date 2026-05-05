"""API request/response Pydantic schemas."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from orionbelt.models.query import QueryObject


class StructuredWarning(BaseModel):
    """Structured warning shape used uniformly across the API.

    Mirrors ``SemanticError`` from ``models/errors.py`` but is the public-facing
    API model. Agents can branch on ``code`` (stable identifier from the
    warning taxonomy in ``models/warnings.py``) without parsing ``message``.
    """

    code: str = Field(description="Stable identifier from the warning taxonomy")
    severity: str = Field(default="warning", description="One of: error, warning, info")
    message: str = Field(description="Human-readable description")
    path: str | None = Field(
        default=None,
        description="JSON path into the request body or model location",
    )
    hint: str | None = Field(default=None, description="Optional remediation suggestion")
    context: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured detail (which measure, dataObject, etc.)",
    )


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
    warnings: list[StructuredWarning] = Field(default_factory=list)
    sql_valid: bool = True
    explain: ExplainPlanResponse | None = None
    physical_tables: list[str] = Field(
        default_factory=list,
        description=(
            "Deduplicated DATABASE.SCHEMA.CODE strings the query touches. "
            "Drives freshness-cache TTL composition and heartbeat invalidation."
        ),
    )


class ColumnMetadata(BaseModel):
    """Metadata for a single result column."""

    name: str
    type: str = Field(description="Type hint: string, number, datetime, binary")
    format: str | None = Field(
        default=None,
        description="Display format pattern from model (e.g. '#,##0.00', '0.00%')",
    )


class QueryExecuteResponse(BaseModel):
    """Response body for POST /query/execute."""

    sql: str
    dialect: str
    columns: list[ColumnMetadata] = Field(default_factory=list)
    rows: list[list[object]] = Field(default_factory=list)
    row_count: int = 0
    execution_time_ms: float = 0.0
    timezone: str | None = Field(
        default=None,
        description="IANA timezone used to label naive timestamps in results",
    )
    resolved: ResolvedInfoResponse = Field(default_factory=ResolvedInfoResponse)
    warnings: list[StructuredWarning] = Field(default_factory=list)
    sql_valid: bool = True
    explain: ExplainPlanResponse | None = None
    physical_tables: list[str] = Field(
        default_factory=list,
        description="Deduplicated DATABASE.SCHEMA.CODE strings the query touched.",
    )
    cached: bool = Field(
        default=False,
        description="Whether the result came from the freshness-driven cache.",
    )
    cached_at: str | None = Field(
        default=None,
        description="ISO 8601 timestamp the cached result was first computed.",
    )
    ttl_seconds: int | None = Field(
        default=None,
        description="Effective TTL applied to this entry, in seconds.",
    )
    ttl_source: str | None = Field(
        default=None,
        description=(
            "Where the TTL came from: freshness_derived | caller_capped | "
            "default_unknown | no_cache | floor_below_min."
        ),
    )
    ttl_limiting_table: str | None = Field(
        default=None,
        description="Physical table whose contract drove the effective TTL.",
    )


class SessionQueryExecuteRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/query/execute."""

    model_id: str
    query: QueryObject
    dialect: str | None = Field(
        default=None,
        description=(
            "SQL dialect. Resolution: explicit value → model.settings.defaultDialect → "
            "DB_VENDOR env → 'postgres'."
        ),
    )


class ValidateRequest(BaseModel):
    """Request body for POST /validate."""

    model_yaml: str | None = Field(
        default=None,
        description="OBML model as YAML string (provide model_yaml OR model_json)",
        max_length=5_000_000,
    )
    model_json: dict[str, object] | str | None = Field(
        default=None,
        description="OBML model as JSON object or JSON string (auto-parsed)",
    )
    extends: list[str] | None = Field(
        default=None,
        description="Optional inline YAML strings of analytical fragments to merge",
    )
    inherits: str | None = Field(
        default=None,
        description="Optional model ID of an already-loaded parent model in the session",
    )

    @model_validator(mode="after")
    def _parse_model_json_string(self) -> ValidateRequest:
        if isinstance(self.model_json, str):
            self.model_json = json.loads(self.model_json)
        return self


class ValidateResponse(BaseModel):
    """Response body for POST /validate."""

    valid: bool
    errors: list[ErrorDetail] = Field(default_factory=list)
    warnings: list[ErrorDetail] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    """A single validation error or warning detail.

    Same shape as :class:`StructuredWarning`. Used by the validation endpoint
    so callers see errors and warnings with the same fields.
    """

    code: str
    message: str
    path: str | None = None
    severity: str = "error"
    hint: str | None = None
    context: dict[str, Any] | None = None
    suggestions: list[str] = Field(default_factory=list)


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


class CacheStatsResponse(BaseModel):
    """Response body for GET /v1/cache/stats."""

    backend: str
    entry_count: int = 0
    total_size_bytes: int = 0
    max_size_bytes: int = 0
    hit_count_total: int = 0
    miss_count_total: int = 0
    hit_rate: float = 0.0
    oldest_entry: str | None = None
    next_sweep_at: str | None = None
    tracked_physical_tables: int = 0
    heartbeat_invalidations_total: int = 0


class CacheSweepResponse(BaseModel):
    """Response body for POST /v1/cache/sweep."""

    backend: str
    ttl_evicted: int = 0
    capacity_evicted: int = 0


class CacheClearResponse(BaseModel):
    """Response body for POST /v1/cache/clear."""

    backend: str
    entries_cleared: int = 0


class HeartbeatRequest(BaseModel):
    """Request body for POST /v1/heartbeat.

    Identifies a *physical* table by its database/schema/code triple. The
    ETL job knows what it just refreshed; OBSL maps that to every cached
    query that touched the table. See PLAN_freshness_driven_cache.md §9.
    """

    database: str = Field(min_length=1, max_length=255)
    schema_name: str = Field(min_length=1, max_length=255, alias="schema")
    table: str = Field(min_length=1, max_length=255)
    timestamp: str | None = Field(
        default=None,
        description=(
            "ISO 8601 timestamp the refresh completed. Defaults to server "
            "now() when omitted; clamped to now() when in the future."
        ),
    )

    model_config = {"populate_by_name": True}


class HeartbeatResponse(BaseModel):
    """Response body for POST /v1/heartbeat."""

    table_ref: str
    recorded_at: str
    invalidated_cache_entries: int
    affected_data_objects: list[str] = Field(default_factory=list)


class CacheSettingsInfo(BaseModel):
    """Public view of result-cache configuration (GET /v1/settings).

    Always present so clients can see whether caching is on. Sensitive
    fields (heartbeat token) are reported as a flag, not the value.
    """

    backend: str = Field(description="'noop' (disabled) or 'file' (enabled)")
    enabled: bool = Field(description="True when ``backend != 'noop'``")
    min_ttl_seconds: int
    max_ttl_seconds: int
    max_value_bytes: int
    max_disk_bytes: int
    sweep_interval_seconds: int
    unknown_freshness_policy: str = Field(
        description="'no_cache' or 'default_ttl' — how to treat tables without a refresh: block"
    )
    unknown_freshness_default_ttl: int
    heartbeat_endpoint_enabled: bool = Field(
        description="True when HEARTBEAT_AUTH_TOKEN is configured (POST /v1/heartbeat live)"
    )


class FlightSettingsInfo(BaseModel):
    """Arrow Flight SQL server status (included when FLIGHT_ENABLED=true)."""

    enabled: bool = True
    port: int = 8815
    auth_mode: str = "none"
    db_vendor: str = "duckdb"


class ModelSettingsInfo(BaseModel):
    """Model-level ``settings:`` block from the loaded OBML model."""

    model_config = {"populate_by_name": True}

    default_numeric_data_type: str | None = Field(
        default=None,
        alias="defaultNumericDataType",
        description="Default decimal(p, s) type used when a column omits dataType",
    )
    default_timezone: str | None = Field(
        default=None,
        alias="defaultTimezone",
        description="IANA timezone applied to naive timestamps in results",
    )
    override_database_timezone: bool = Field(
        default=False,
        alias="overrideDatabaseTimezone",
        description="When true, model timezone wins over the DB session timezone",
    )
    default_dialect: str | None = Field(
        default=None,
        alias="defaultDialect",
        description="SQL dialect used when callers omit `dialect` on query requests",
    )


class TimezoneResolutionInfo(BaseModel):
    """Timezone resolution chain for naive timestamp coercion at execute time.

    Effective timezone (in priority order):
    - ``override_database_timezone`` true: ``model`` → ``host`` → UTC
    - else: ``database`` (if detected) → ``model`` → ``host`` → UTC

    ``database`` is populated lazily on first query execution per dialect; it
    is ``null`` until then. Reading this endpoint never probes the database.
    """

    model: str | None = Field(default=None, description="settings.defaultTimezone")
    host: str | None = Field(default=None, description="OS / process timezone")
    database: str | None = Field(
        default=None,
        description="Detected database session timezone (null if not probed yet)",
    )
    effective: str = Field(
        description="The timezone that resolve_timezone() returns at this moment"
    )
    override_database_timezone: bool = Field(
        default=False,
        description="Whether the model overrides the DB session timezone",
    )
    now: str = Field(description="Current wall-clock time in the effective TZ (ISO 8601)")
    utc: str = Field(description="Current UTC time (ISO 8601, Z suffix) for reference")
    database_detected: bool = Field(
        default=False,
        description="Whether DB session TZ detection has run for this dialect",
    )
    database_raw: str | None = Field(
        default=None,
        description=(
            "Raw cached DB session TZ value (for diagnostics). "
            "When `database_detected` is true and this is null, detection ran "
            "but did not store a value (query failed or returned SYSTEM)."
        ),
    )


class DialectResolutionInfo(BaseModel):
    """Dialect resolution chain for query compilation.

    Order on each request: explicit ``dialect`` body field →
    ``settings.defaultDialect`` → ``DB_VENDOR`` env → ``"postgres"``.
    ``effective`` here is what gets used when a caller omits ``dialect``.
    """

    model: str | None = Field(default=None, description="settings.defaultDialect")
    env: str | None = Field(default=None, description="DB_VENDOR env (server config)")
    effective: str = Field(description="Dialect used when request omits `dialect`")


class OneshotBatchLimits(BaseModel):
    """Server-side limits for the one-shot batch endpoint."""

    max_queries: int
    max_parallelism: int
    default_timeout_ms: int
    batch_timeout_ms: int


class SettingsResponse(BaseModel):
    """Response for GET /settings — public configuration for clients."""

    version: str = Field(default="", description="OrionBelt Semantic Layer release version")
    api_version: str = Field(default="v1", description="REST API version prefix")
    single_model_mode: bool = False
    model_yaml: str | None = Field(
        default=None,
        description="Pre-loaded OBML YAML content (only when single_model_mode is true)",
    )
    session_ttl_seconds: int = 1800
    session_max_age_seconds: int = Field(
        default=86400,
        description="Absolute max session lifetime in seconds",
    )
    max_sessions: int = Field(
        default=500,
        description="Global concurrent session cap",
    )
    max_models_per_session: int = Field(
        default=10,
        description="Maximum models per session",
    )
    query_execute: bool = Field(
        default=False,
        description="Whether POST /query/execute is available",
    )
    flight: FlightSettingsInfo | None = Field(
        default=None,
        description="Arrow Flight SQL server info (present only when Flight is enabled)",
    )
    model_settings: ModelSettingsInfo | None = Field(
        default=None,
        description="Loaded model's `settings:` block (single-model mode only)",
    )
    timezone: TimezoneResolutionInfo | None = Field(
        default=None,
        description="Timezone resolution chain (single-model mode only)",
    )
    dialect: DialectResolutionInfo | None = Field(
        default=None,
        description="SQL dialect resolution chain",
    )
    oneshot_batch: OneshotBatchLimits | None = Field(
        default=None,
        description="Limits for POST /v1/oneshot/batch",
    )
    cache: CacheSettingsInfo | None = Field(
        default=None,
        description="Result-cache configuration. Always present.",
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
    expires_at: datetime = Field(description="Idle TTL deadline (refreshed on each access)")
    max_expires_at: datetime = Field(description="Absolute lifetime deadline (fixed at creation)")


class SessionListResponse(BaseModel):
    """Response for GET /sessions."""

    sessions: list[SessionResponse]


class FanTrapRisk(BaseModel):
    """A potential fan-trap: two facts joined to a shared dim via the same FK."""

    tables: list[str] = Field(description="Physical tables involved in the risk")
    reason: str = Field(description="Plain-text explanation of the risk")
    suggested_pattern: str = Field(
        default="composite_fact_layer",
        description="Recommended OBSL pattern for resolving the risk",
    )


class ModelHealth(BaseModel):
    """Structural health summary of a loaded model's join graph.

    Fields are computed during model load (no extra round trip needed). See
    ``design/PLAN_agent_api_improvements.md`` §1.4.
    """

    status: str = Field(
        default="ok",
        description="One of: ok, warnings, errors",
    )
    data_objects: int = 0
    joins: int = 0
    orphan_data_objects: list[str] = Field(default_factory=list)
    fan_trap_risks: list[FanTrapRisk] = Field(default_factory=list)
    unreachable_dimensions: list[str] = Field(default_factory=list)
    warnings_count: int = 0


class ModelLoadRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/models."""

    model_yaml: str | None = Field(
        default=None,
        description="OBML model as YAML string (provide model_yaml OR model_json)",
        max_length=5_000_000,
    )
    model_json: dict[str, object] | str | None = Field(
        default=None,
        description="OBML model as JSON object or JSON string (auto-parsed)",
    )
    extends: list[str] | None = Field(
        default=None,
        description="Optional inline YAML strings of analytical fragments to merge",
    )
    inherits: str | None = Field(
        default=None,
        description="Optional model ID of an already-loaded parent model in the session",
    )
    dedup: bool = Field(
        default=True,
        description=(
            "When True (default), identical OBML content already loaded in this session "
            "reuses the existing model_id (response.model_load == 'reused'). "
            "When False, always loads fresh."
        ),
    )

    @model_validator(mode="after")
    def _parse_model_json_string(self) -> ModelLoadRequest:
        if isinstance(self.model_json, str):
            self.model_json = json.loads(self.model_json)
        return self


class ModelLoadResponse(BaseModel):
    """Response for POST /sessions/{session_id}/models."""

    model_id: str
    data_objects: int
    dimensions: int
    measures: int
    metrics: int
    warnings: list[StructuredWarning] = Field(default_factory=list)
    model_load: str = Field(
        default="fresh",
        description=(
            "Whether the load parsed a fresh model or reused an existing one. "
            "Values: 'fresh' | 'reused'."
        ),
    )
    health: ModelHealth | None = Field(
        default=None,
        description=(
            "Structural health of the model's join graph: orphan dataObjects, "
            "fan-trap risks, unreachable dimensions. Always present on a fresh load."
        ),
    )


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
    dialect: str | None = Field(
        default=None,
        description=(
            "SQL dialect. Resolution: explicit value → model.settings.defaultDialect → "
            "DB_VENDOR env → 'postgres'."
        ),
    )


class DiagramResponse(BaseModel):
    """Response for GET /sessions/{session_id}/models/{model_id}/diagram/er."""

    mermaid: str = Field(description="Mermaid ER diagram script")


# ---------------------------------------------------------------------------
# OSI ↔ OBML conversion schemas
# ---------------------------------------------------------------------------


class ExampleSummary(BaseModel):
    """Short summary of a model example (list endpoint)."""

    name: str
    description: str
    intent_tags: list[str] = Field(default_factory=list)


class ExampleDetail(BaseModel):
    """Full detail of a single example, including the query payload."""

    name: str
    description: str
    intent_tags: list[str] = Field(default_factory=list)
    query: dict[str, Any]
    compiled_sql_preview: str | None = Field(
        default=None,
        description=(
            "Compiled SQL when the example resolves cleanly against the loaded model. "
            "Null when compilation fails."
        ),
    )


class ExampleListResponse(BaseModel):
    """Response body for GET .../examples and GET .../examples?intent=..."""

    examples: list[ExampleSummary] = Field(default_factory=list)
    suggestion: str | None = Field(
        default=None,
        description=(
            "When ?intent= matches no examples, lists the available tags so callers "
            "can adjust the query."
        ),
    )


class JoinPathStep(BaseModel):
    """A single step in the planner's join path."""

    from_object: str
    to_object: str
    cardinality: str = Field(description="many-to-one, one-to-one, or many-to-many")
    fk: str | None = Field(default=None, description="Join key columns as 'a, b'")


class DatabaseExplain(BaseModel):
    """Raw EXPLAIN output from the warehouse for the compiled SQL.

    OBSL does not normalize across dialects — the ``explain_output`` is
    opaque text in the dialect's native EXPLAIN format.
    """

    dialect: str
    compiled_sql: str
    explain_output: str
    explain_format: str = Field(default="text", description="Format of explain_output")


class QueryPlanRequest(BaseModel):
    """Request body for POST /sessions/{sid}/query/plan.

    See ``design/PLAN_agent_api_improvements.md`` §2.
    """

    model_id: str
    query: QueryObject
    dialect: str | None = Field(
        default=None,
        description=(
            "SQL dialect. Resolution: explicit value → model.settings.defaultDialect → "
            "DB_VENDOR env → 'postgres'."
        ),
    )
    include_database_explain: bool = Field(
        default=False,
        description=(
            "When true, also run EXPLAIN <sql> against the configured warehouse and "
            "include the raw output. Off by default — opt in costs a warehouse round trip."
        ),
    )


class QueryPlanResponse(BaseModel):
    """Response body for POST /sessions/{sid}/query/plan."""

    status: str = Field(default="ok", description="ok | error")
    planner: str = ""
    planner_reason: str = ""
    physical_tables: list[str] = Field(default_factory=list)
    join_path: list[JoinPathStep] = Field(default_factory=list)
    filters_applied: int = 0
    warnings: list[StructuredWarning] = Field(default_factory=list)
    would_compile: bool = True
    compiled_sql_length_estimate: int = 0
    database_explain: DatabaseExplain | None = None


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
    via: str | None = None
    description: str | None = None
    format: str | None = None
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class MeasureDetail(BaseModel):
    """Detail of a measure."""

    model_config = {"populate_by_name": True}

    name: str
    result_type: str
    aggregation: str
    expression: str | None = None
    columns: list[dict[str, str]] = Field(default_factory=list)
    distinct: bool = False
    total: bool = False
    description: str | None = None
    format: str | None = None
    data_type: str | None = Field(default=None, alias="dataType")
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
    data_type: str | None = Field(default=None, alias="dataType")
    owner: str | None = None
    synonyms: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ModelFilterDetail(BaseModel):
    """Detail of a static model filter."""

    data_object: str
    column: str
    operator: str
    value: str | int | float | bool | None = None
    values: list[str | int | float | bool] = Field(default_factory=list)


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
    filters: list[ModelFilterDetail] = Field(default_factory=list)
    extends: list[str] = Field(default_factory=list)
    inherits: str | None = None


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


class FuzzyMatch(BaseModel):
    """A near-miss fuzzy match (no exact/synonym hit)."""

    name: str
    kind: str
    score: float
    reason: str


class SearchResponse(BaseModel):
    """Response for POST /find.

    When the query produced zero exact and synonym matches, ``fuzzy_matches``
    surfaces near-miss suggestions ordered by score. See
    ``design/PLAN_agent_api_improvements.md`` §4.
    """

    query: str = ""
    results: list[SearchResultItem] = Field(default_factory=list)
    exact_matches: list[SearchResultItem] = Field(
        default_factory=list,
        description="Subset of results matched on name (kept for clarity).",
    )
    synonym_matches: list[SearchResultItem] = Field(
        default_factory=list,
        description="Subset of results matched on a synonym.",
    )
    fuzzy_matches: list[FuzzyMatch] = Field(
        default_factory=list,
        description=(
            "Near-miss candidates returned only when no exact or synonym "
            "match was found. Ordered by score descending."
        ),
    )


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


# ---------------------------------------------------------------------------
# OBSL graph / SPARQL schemas
# ---------------------------------------------------------------------------


class SPARQLRequest(BaseModel):
    """Request body for POST /sparql."""

    query: str = Field(description="SPARQL query (SELECT or ASK only)", max_length=100_000)


class SPARQLResponse(BaseModel):
    """Response body for POST /sparql."""

    type: str = Field(description="Query type: select or ask")
    variables: list[str] = Field(default_factory=list, description="Binding variable names")
    results: list[dict[str, str | None]] = Field(
        default_factory=list, description="Rows of variable bindings"
    )
    boolean: bool | None = Field(default=None, description="ASK query result")


# ---------------------------------------------------------------------------
# One-shot batch schemas (PLAN_oneshot_batch.md)
# ---------------------------------------------------------------------------


class OneshotBatchQueryItem(BaseModel):
    """A single query in a one-shot batch."""

    id: str | None = Field(
        default=None,
        description=(
            "Optional caller-provided ID. Must be unique within the batch when supplied. "
            "When omitted, the server assigns 'q0', 'q1', ... based on submission order."
        ),
    )
    query: QueryObject
    execute: bool | None = Field(
        default=None,
        description="Per-query override for compile-only vs. execute (default inherits batch).",
    )
    dialect: str | None = Field(
        default=None,
        description="Per-query dialect override (default inherits batch).",
    )


class OneshotBatchRequest(BaseModel):
    """Request body for POST /v1/oneshot/batch."""

    session_id: str | None = Field(
        default=None,
        description="Existing session to use. If omitted, a new session is created.",
    )
    model_yaml: str | None = Field(
        default=None,
        description=(
            "OBML YAML. Mutually exclusive with `model_id`. One of them must be provided."
        ),
        max_length=5_000_000,
    )
    model_id: str | None = Field(
        default=None,
        description=(
            "ID of an already-loaded model in the given session. "
            "Mutually exclusive with `model_yaml`."
        ),
    )
    queries: list[OneshotBatchQueryItem] = Field(
        description="List of queries to run. Min 1, server caps maximum.",
        min_length=1,
    )
    dialect: str | None = Field(
        default=None,
        description="Default dialect for all queries in the batch.",
    )
    execute: bool = Field(
        default=False,
        description="Default execute flag for all queries.",
    )
    max_parallelism: int | None = Field(
        default=None,
        description="Max concurrent query executions. Server caps this.",
        ge=1,
    )
    fail_fast: bool = Field(
        default=False,
        description="If true, cancel remaining queries on first failure.",
    )
    persist_model: bool = Field(
        default=False,
        description=(
            "If true, a model loaded via `model_yaml` is kept in the session after the call. "
            "Ignored when `model_id` is supplied."
        ),
    )
    dedup: bool = Field(
        default=True,
        description=(
            "When true, identical OBML content already loaded in the resolved session reuses "
            "the existing model_id. When false, always loads fresh. Ignored when `model_id` "
            "is supplied."
        ),
    )

    @model_validator(mode="after")
    def _validate_request(self) -> OneshotBatchRequest:
        # Exactly one of model_yaml / model_id must be provided.
        has_yaml = bool(self.model_yaml)
        has_id = bool(self.model_id)
        if has_yaml and has_id:
            raise ValueError("Provide either model_yaml or model_id, not both")
        if not has_yaml and not has_id:
            raise ValueError("Provide either model_yaml or model_id")
        # Auto-assign an id (q0, q1, ...) for any query that omits one. Then
        # check uniqueness across the resulting set so a caller-supplied
        # "q3" can't collide silently with an auto-assigned "q3".
        for idx, q in enumerate(self.queries):
            if q.id is None:
                q.id = f"q{idx}"
        seen: set[str] = set()
        for q in self.queries:
            assert q.id is not None  # filled in above
            if q.id in seen:
                raise ValueError(f"Duplicate query id: '{q.id}'")
            seen.add(q.id)
        return self


class OneshotBatchQueryError(BaseModel):
    """Error envelope for a single failed query in a batch."""

    code: str
    message: str
    path: str | None = None
    hint: str | None = None


class OneshotBatchQueryResult(BaseModel):
    """Result of a single query in a one-shot batch."""

    id: str
    status: str = Field(description="One of: 'ok', 'error', 'cancelled'")
    sql: str | None = None
    dialect: str | None = None
    sql_valid: bool | None = None
    explain: ExplainPlanResponse | None = None
    columns: list[ColumnMetadata] | None = None
    rows: list[list[object]] | None = None
    row_count: int | None = None
    execution_time_ms: float | None = None
    executed: bool | None = Field(
        default=None,
        description="Whether this query executed (vs compile-only). Only set when status='ok'.",
    )
    warnings: list[StructuredWarning] = Field(default_factory=list)
    error: OneshotBatchQueryError | None = None
    physical_tables: list[str] = Field(default_factory=list)
    cached: bool = False
    cached_at: str | None = None
    ttl_seconds: int | None = None
    ttl_source: str | None = None
    ttl_limiting_table: str | None = None


class OneshotBatchResponse(BaseModel):
    """Response body for POST /v1/oneshot/batch."""

    session_id: str
    model_id: str
    model_persisted: bool
    model_load: str = Field(
        default="fresh",
        description=(
            "How the model was acquired: 'fresh' (parsed and loaded), 'reused' (dedup hit), "
            "or 'referenced' (existing model_id supplied by caller)."
        ),
    )
    results: list[OneshotBatchQueryResult] = Field(default_factory=list)
    batch_warnings: list[StructuredWarning] = Field(default_factory=list)
