"""Session-scoped endpoints for model management, validation, and query."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal, cast

import yaml
from fastapi import APIRouter, Depends, HTTPException, Response

from orionbelt.api.deps import (
    CacheRuntimeConfig,
    get_cache,
    get_cache_config,
    get_default_locale,
    get_preload_model_yaml,
    get_query_default_limit,
    get_session_manager,
    is_query_execute_enabled,
    is_session_list_disabled,
    is_single_model_mode,
)
from orionbelt.api.osi_support import get_converter_module, parse_yaml, run_validation
from orionbelt.api.schemas import (
    ColumnMetadata,
    ConvertResponse,
    DatabaseExplain,
    DiagramResponse,
    ExplainCflLegResponse,
    ExplainJoinResponse,
    ExplainPlanResponse,
    JoinPathStep,
    ModelLoadRequest,
    ModelLoadResponse,
    ModelSummaryResponse,
    OSIModelLoadRequest,
    OSIModelLoadResponse,
    QueryCompileResponse,
    QueryExecuteResponse,
    QueryPlanRequest,
    QueryPlanResponse,
    ResolvedInfoResponse,
    SemanticQLCompileResponse,
    SemanticQLRequest,
    SessionCreateRequest,
    SessionListResponse,
    SessionQueryExecuteRequest,
    SessionQueryRequest,
    SessionResponse,
    StructuredWarning,
    ValidateRequest,
    ValidateResponse,
)
from orionbelt.api.warnings_adapter import (
    error_info_to_detail,
    error_info_to_warning,
    health_summary_to_response,
    semantic_error_to_warning,
)
from orionbelt.cache import build_cache_key, compute_effective_ttl, is_nondeterministic_sql
from orionbelt.cache.parquet_codec import decode as cache_decode
from orionbelt.cache.protocol import Cache
from orionbelt.cache.ttl import NoCacheReason, TtlResult
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.sql_translator import SQLTranslationError, translate_sql_to_query
from orionbelt.compiler.validator import format_sql
from orionbelt.dialect.base import UnsupportedAggregationError, UnsupportedGroupingError
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionUnavailableError,
    execute_sql,
    explain_sql,
    resolve_timezone,
)
from orionbelt.service.diagram import generate_mermaid_er
from orionbelt.service.model_store import (
    LoadResult,
    ModelCapacityError,
    ModelStore,
    ModelValidationError,
)
from orionbelt.service.session_manager import (
    SessionCapacityError,
    SessionExpiredError,
    SessionInfo,
    SessionManager,
    SessionNotFoundError,
)
from orionbelt.service.value_formatting import format_row, to_tsv

logger = logging.getLogger("orionbelt.api.sessions")

router = APIRouter()


# -- helpers -----------------------------------------------------------------


def _get_store(session_id: str, mgr: SessionManager) -> ModelStore:
    """Resolve session_id to ModelStore, raise 410/404 as appropriate."""
    try:
        return mgr.get_store(session_id)
    except SessionExpiredError:
        raise HTTPException(status_code=410, detail=f"Session '{session_id}' has expired") from None
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None


def _load_obml(store: ModelStore, yaml_str: str | None = None, **kwargs: Any) -> LoadResult:
    """Load OBML into the store, mapping store errors to HTTP responses.

    Shared by the plain OBML upload and the OSI-converted upload so both
    surface capacity (429) and validation (422) failures identically.
    """
    try:
        return store.load_model(yaml_str, **kwargs)
    except ModelCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except ModelValidationError as exc:
        error_lines = "; ".join(
            f"[{e.code}] {e.message}" + (f" (at {e.path})" if e.path else "") for e in exc.errors
        )
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Invalid OBML model: {error_lines}",
                "errors": [
                    {"code": e.code, "message": e.message, "path": e.path} for e in exc.errors
                ],
                "warnings": [
                    {"code": w.code, "message": w.message, "path": w.path} for w in exc.warnings
                ],
            },
        ) from None


def _model_load_fields(result: LoadResult) -> dict[str, Any]:
    """Shared kwargs for (OSI)ModelLoadResponse from a store LoadResult."""
    return {
        "model_id": result.model_id,
        "data_objects": result.data_objects,
        "dimensions": result.dimensions,
        "measures": result.measures,
        "metrics": result.metrics,
        "warnings": [error_info_to_warning(w) for w in result.warnings],
        "model_load": result.model_load,
        "health": health_summary_to_response(result.health),
    }


def _session_response(info: SessionInfo) -> SessionResponse:
    """Convert a SessionInfo dataclass to a Pydantic response."""
    d = asdict(info)
    return SessionResponse(**d)


# -- session CRUD ------------------------------------------------------------


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    body: SessionCreateRequest | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SessionResponse:
    """Create a new session.

    In admin-curated mode (``MODEL_FILES``) with exactly one protected
    session, the protected model is copied into the new user session so
    UI / SDK callers can keep using the session-scoped query endpoints
    without uploading the YAML themselves (which is blocked with 403).
    This restores the v2.6 ``MODEL_FILE`` auto-preload UX on top of the
    v2.7 named-protected-session topology.
    """
    metadata = body.metadata if body else {}
    try:
        info = mgr.create_session(metadata=metadata)
    except SessionCapacityError:
        raise HTTPException(
            status_code=429,
            detail="Too many active sessions. Please retry later.",
            headers={"Retry-After": "60"},
        ) from None

    if is_single_model_mode():
        preload_yaml = get_preload_model_yaml()
        protected_ids = mgr.list_protected_session_ids()
        if preload_yaml and len(protected_ids) == 1:
            try:
                user_store = mgr.get_store(info.session_id)
                user_store.load_model(preload_yaml)
                info = mgr.get_session(info.session_id)
            except Exception:
                logger.exception("Failed to preload protected model into new user session")

    return _session_response(info)


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SessionListResponse:
    """List all active sessions."""
    if is_session_list_disabled():
        raise HTTPException(status_code=403, detail="Session listing is disabled")
    sessions = mgr.list_sessions()
    return SessionListResponse(sessions=[_session_response(s) for s in sessions])


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SessionResponse:
    """Get info for a specific session."""
    try:
        info = mgr.get_session(session_id)
    except SessionExpiredError:
        raise HTTPException(status_code=410, detail=f"Session '{session_id}' has expired") from None
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None
    return _session_response(info)


@router.delete("/{session_id}", status_code=204)
async def close_session(
    session_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> None:
    """Close a session and release its resources."""
    from orionbelt.api.deps import get_cache

    try:
        mgr.close_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None
    with contextlib.suppress(Exception):
        await get_cache().delete_session(session_id)


# -- model management -------------------------------------------------------


@router.post(
    "/{session_id}/models",
    response_model=ModelLoadResponse,
    status_code=201,
)
async def load_model(
    session_id: str,
    body: ModelLoadRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ModelLoadResponse:
    """Load an OBML model into a session."""
    if is_single_model_mode():
        raise HTTPException(status_code=403, detail="Single-model mode: model upload is disabled")
    if not body.model_yaml and not body.model_json:
        raise HTTPException(status_code=422, detail="Provide either model_yaml or model_json")
    store = _get_store(session_id, mgr)
    result = _load_obml(
        store,
        body.model_yaml,
        raw_dict=cast("dict[str, object] | None", body.model_json),
        extends_yaml=body.extends,
        inherits_model_id=body.inherits,
        dedup=body.dedup,
    )
    return ModelLoadResponse(**_model_load_fields(result))


@router.post(
    "/{session_id}/models/from-osi",
    response_model=OSIModelLoadResponse,
    status_code=201,
)
async def load_model_from_osi(
    session_id: str,
    body: OSIModelLoadRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> OSIModelLoadResponse:
    """Convert an OSI model to OBML and load it into a session's model store.

    Mirrors :func:`load_model` but accepts Open Semantic Interchange (OSI)
    YAML, runs it through the OSI -> OBML converter, then loads the result.
    The OSI input is schema-validated (advisory) and both the conversion
    warnings and that validation are returned alongside the model summary.
    """
    if is_single_model_mode():
        raise HTTPException(status_code=403, detail="Single-model mode: model upload is disabled")
    store = _get_store(session_id, mgr)

    data = parse_yaml(body.osi_yaml)
    mod = get_converter_module()
    input_validation = run_validation(mod.validate_osi, data)
    try:
        converter = mod.OSItoOBML(data)
        obml_dict = converter.convert()
        conversion_warnings = list(converter.warnings)
    except Exception as exc:
        logger.exception("OSI → OBML conversion failed")
        raise HTTPException(status_code=422, detail=f"OSI → OBML conversion failed: {exc}") from exc

    obml_yaml = yaml.dump(
        obml_dict, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )
    result = _load_obml(store, obml_yaml, dedup=body.dedup)

    return OSIModelLoadResponse(
        **_model_load_fields(result),
        conversion_warnings=conversion_warnings,
        input_validation=input_validation,
    )


@router.get("/{session_id}/models", response_model=list[ModelSummaryResponse])
async def list_models(
    session_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> list[ModelSummaryResponse]:
    """List all models loaded in a session."""
    store = _get_store(session_id, mgr)
    return [
        ModelSummaryResponse(
            model_id=m.model_id,
            data_objects=m.data_objects,
            dimensions=m.dimensions,
            measures=m.measures,
            metrics=m.metrics,
        )
        for m in store.list_models()
    ]


@router.get("/{session_id}/models/{model_id}")
async def describe_model(
    session_id: str,
    model_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> dict:  # type: ignore[type-arg]
    """Describe a model loaded in a session."""
    store = _get_store(session_id, mgr)
    try:
        desc = store.describe(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
    return asdict(desc)


@router.get(
    "/{session_id}/models/{model_id}/diagram/er",
    response_model=DiagramResponse,
)
async def model_diagram_er(
    session_id: str,
    model_id: str,
    show_columns: bool = True,
    theme: str = "default",
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> DiagramResponse:
    """Generate a Mermaid ER diagram for a loaded model."""
    store = _get_store(session_id, mgr)
    try:
        model = store.get_model(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
    mermaid = generate_mermaid_er(model, show_columns=show_columns, theme=theme)
    return DiagramResponse(mermaid=mermaid)


@router.get(
    "/{session_id}/models/{model_id}/osi",
    response_model=ConvertResponse,
)
async def export_model_to_osi(
    session_id: str,
    model_id: str,
    model_name: str = "semantic_model",
    model_description: str = "",
    ai_instructions: str = "",
    include_ontology: bool = False,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ConvertResponse:
    """Export a loaded model from the model store as OSI YAML.

    Converts the model's raw OBML (the faithful copy captured at load
    time, falling back to a lossy reconstruction for programmatically
    built models) to Open Semantic Interchange (OSI) format. Optional
    query params override the OSI model name, description, and AI
    instructions. Set ``include_ontology=true`` to also emit the OSI
    ontology document in ``ontology_yaml`` (a separate artefact with its
    own ``ontology_validation``); the core-spec ``output_yaml`` is unchanged.
    """
    store = _get_store(session_id, mgr)
    try:
        obml_dict = store.get_raw(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None

    mod = get_converter_module()
    try:
        converter = mod.OBMLtoOSI(
            obml_dict,
            model_name=model_name,
            model_description=model_description,
            ai_instructions=ai_instructions,
        )
        osi_dict = converter.convert()
        warnings = list(converter.warnings)
    except Exception as exc:
        logger.exception("OBML → OSI conversion failed")
        raise HTTPException(status_code=422, detail=f"OBML → OSI conversion failed: {exc}") from exc

    output_yaml = yaml.dump(
        osi_dict, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )
    validation = run_validation(mod.validate_osi, osi_dict)

    ontology_yaml: str | None = None
    ontology_validation = None
    if include_ontology:
        try:
            onto_conv = mod.OBMLtoOSIOntology(
                obml_dict,
                model_name=model_name,
                model_description=model_description,
                ai_instructions=ai_instructions,
            )
            onto_dict = onto_conv.convert()
            warnings = warnings + list(onto_conv.warnings)
        except Exception as exc:
            logger.exception("OBML → OSI ontology conversion failed")
            raise HTTPException(
                status_code=422, detail=f"OBML → OSI ontology conversion failed: {exc}"
            ) from exc
        ontology_yaml = yaml.dump(
            onto_dict, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
        )
        ontology_validation = run_validation(mod.validate_osi_ontology, onto_dict)

    return ConvertResponse(
        output_yaml=output_yaml,
        warnings=warnings,
        validation=validation,
        ontology_yaml=ontology_yaml,
        ontology_validation=ontology_validation,
    )


@router.delete("/{session_id}/models/{model_id}", status_code=204)
async def remove_model(
    session_id: str,
    model_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> None:
    """Remove a model from a session."""
    if is_single_model_mode():
        raise HTTPException(status_code=403, detail="Single-model mode: model removal is disabled")
    store = _get_store(session_id, mgr)
    try:
        store.remove_model(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None


# -- validation & query -----------------------------------------------------


@router.post("/{session_id}/validate", response_model=ValidateResponse)
async def validate_model(
    session_id: str,
    body: ValidateRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ValidateResponse:
    """Validate an OBML model within a session context."""
    if not body.model_yaml and not body.model_json:
        raise HTTPException(status_code=422, detail="Provide either model_yaml or model_json")
    store = _get_store(session_id, mgr)
    summary = store.validate(
        body.model_yaml,
        raw_dict=cast("dict[str, object] | None", body.model_json),
        extends_yaml=body.extends,
        inherits_model_id=body.inherits,
    )
    return ValidateResponse(
        valid=summary.valid,
        errors=[error_info_to_detail(e) for e in summary.errors],
        warnings=[error_info_to_detail(w) for w in summary.warnings],
    )


@router.post("/{session_id}/query/sql", response_model=QueryCompileResponse)
async def compile_query(
    session_id: str,
    body: SessionQueryRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> QueryCompileResponse:
    """Compile a query against a model loaded in a session."""
    store = _get_store(session_id, mgr)
    try:
        model_for_dialect = store.get_model(body.model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None
    logger.info("QueryObject request:\n%s", body.query.model_dump_json(by_alias=True, indent=2))
    dialect = _resolve_dialect(request_dialect=body.dialect, model=model_for_dialect)
    try:
        result = store.compile_query(body.model_id, body.query, dialect)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None
    except UnsupportedDialectError:
        raise HTTPException(status_code=400, detail=f"Unsupported dialect: '{dialect}'") from None
    except ResolutionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Query resolution failed",
                "errors": [
                    {"code": e.code, "message": e.message, "path": e.path} for e in exc.errors
                ],
            },
        ) from None
    except FanoutError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "Query fanout detected", "message": exc.message},
        ) from None
    except UnsupportedAggregationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported aggregation",
                "message": str(exc),
                "dialect": exc.dialect,
                "aggregation": exc.aggregation,
            },
        ) from None
    except UnsupportedGroupingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported grouping",
                "message": str(exc),
                "dialect": exc.dialect,
                "grouping": exc.grouping,
            },
        ) from None
    logger.info("Compiled SQL:\n%s", result.sql)
    explain_resp = None
    if result.explain:
        explain_resp = ExplainPlanResponse(
            planner=result.explain.planner,
            planner_reason=result.explain.planner_reason,
            base_object=result.explain.base_object,
            base_object_reason=result.explain.base_object_reason,
            joins=[
                ExplainJoinResponse(
                    from_object=j.from_object,
                    to_object=j.to_object,
                    join_columns=j.join_columns,
                    reason=j.reason,
                )
                for j in result.explain.joins
            ],
            where_filter_count=result.explain.where_filter_count,
            having_filter_count=result.explain.having_filter_count,
            has_totals=result.explain.has_totals,
            cfl_legs=[
                ExplainCflLegResponse(
                    measure_source=leg.measure_source,
                    common_root=leg.common_root,
                    reason=leg.reason,
                    measures=leg.measures,
                    joins=leg.joins,
                )
                for leg in result.explain.cfl_legs
            ],
        )
    return QueryCompileResponse(
        sql=format_sql(result.sql, result.dialect),
        dialect=result.dialect,
        resolved=ResolvedInfoResponse(
            fact_tables=result.resolved.fact_tables,
            dimensions=result.resolved.dimensions,
            measures=result.resolved.measures,
        ),
        warnings=[semantic_error_to_warning(w) for w in result.warnings],
        sql_valid=result.sql_valid,
        explain=explain_resp,
        physical_tables=list(result.physical_tables),
    )


_RESULT_TYPE_TO_HINT: dict[str, str] = {
    "string": "string",
    "json": "string",
    "int": "number",
    "float": "number",
    "date": "datetime",
    "time": "datetime",
    "time_tz": "datetime",
    "timestamp": "datetime",
    "timestamp_tz": "datetime",
    "boolean": "string",
}


def _build_type_map(model: Any) -> dict[str, str]:
    """Build a column-name → type-hint map from model definitions.

    Uses ``dataType`` when available (e.g. ``decimal(18, 2)``),
    then falls back to ``settings.defaultNumericDataType`` for numeric
    measures/metrics, otherwise maps ``resultType`` to a simple hint.
    """
    default_num = None
    if model.settings and model.settings.default_numeric_data_type:
        default_num = model.settings.default_numeric_data_type

    types: dict[str, str] = {}
    for label, dim in model.dimensions.items():
        types[label] = _RESULT_TYPE_TO_HINT.get(str(dim.result_type), "string")
    for label, measure in model.measures.items():
        if measure.data_type:
            types[label] = measure.data_type
        elif default_num:
            types[label] = default_num
        else:
            types[label] = _RESULT_TYPE_TO_HINT.get(str(measure.result_type), "number")
    for label, metric in model.metrics.items():
        if metric.data_type:
            types[label] = metric.data_type
        elif default_num:
            types[label] = default_num
        else:
            types[label] = "number"
    return types


def _build_format_map(model: Any) -> dict[str, str | None]:
    """Build a column-name → format-string map from model measures/metrics."""
    fmt: dict[str, str | None] = {}
    for label, dim in model.dimensions.items():
        if dim.format:
            fmt[label] = dim.format
    for label, measure in model.measures.items():
        if measure.format:
            fmt[label] = measure.format
    for label, metric in model.metrics.items():
        if metric.format:
            fmt[label] = metric.format
    return fmt


def _resolve_dialect(
    *, request_dialect: str | None, model: Any, fallback: str | None = None
) -> str:
    """Resolve the dialect for a query.

    Order: explicit request dialect → model.settings.default_dialect →
    ``fallback`` (typically ``DB_VENDOR``) → ``"postgres"``.
    """
    if request_dialect:
        return request_dialect
    settings = getattr(model, "settings", None)
    model_default = getattr(settings, "default_dialect", None) if settings else None
    if model_default:
        return str(model_default)
    if fallback:
        return fallback
    return "postgres"


def _build_execute_response(
    *,
    compile_result: Any,
    exec_result: Any,
    model: Any,
    response_format: Literal["json", "tsv"],
    format_values: bool,
    locale: str,
) -> QueryExecuteResponse | Response:
    """Build the JSON QueryExecuteResponse, or a TSV Response.

    ``format_values`` is forced True for TSV; numeric cells are rendered with
    each column's display ``format`` pattern using locale-aware separators.
    """
    model_type_map = _build_type_map(model)
    fmt_map = _build_format_map(model)
    # Auto-default for columns without an explicit model-side format — the
    # executor proposes a pattern based on the column's Arrow / driver type
    # (None for ints/strings/dates so they stay as bare ``str(val)``;
    # ``"#,##0.00"`` for floats and decimals). Raw-mode ``select.fields``
    # projections benefit most: physical columns no longer need a measure
    # to inherit a sensible locale-aware render.
    for c in exec_result.columns:
        if fmt_map.get(c.name) is None and getattr(c, "default_format", None):
            fmt_map[c.name] = c.default_format
    column_names = [c.name for c in exec_result.columns]
    columns_meta = [
        ColumnMetadata(
            name=c.name,
            type=model_type_map.get(c.name, c.type_hint),
            format=fmt_map.get(c.name),
        )
        for c in exec_result.columns
    ]
    # Merge the executor's type_hint as a fallback for columns that aren't
    # exposed via the dimension/measure/metric layer — notably raw-mode
    # ``select.fields`` projections, which reference physical columns the
    # model-level type_map doesn't list. Without this merge, a numeric raw
    # column ("decimal(18, 2)" from the driver) wouldn't be classified as
    # numeric in format_row.
    type_map: dict[str, str] = {
        c.name: model_type_map.get(c.name, c.type_hint or "") for c in exec_result.columns
    }

    if response_format == "tsv":
        formatted = [
            format_row(
                row,
                column_names=column_names,
                fmt_map=fmt_map,
                type_map=type_map,
                locale=locale,
            )
            for row in exec_result.rows
        ]
        body = to_tsv(column_names, formatted)
        return Response(content=body, media_type="text/tab-separated-values")

    if format_values:
        rows: list[list[Any]] = [
            cast(
                list[Any],
                format_row(
                    row,
                    column_names=column_names,
                    fmt_map=fmt_map,
                    type_map=type_map,
                    locale=locale,
                ),
            )
            for row in exec_result.rows
        ]
    else:
        rows = exec_result.rows

    return QueryExecuteResponse(
        sql=format_sql(compile_result.sql, compile_result.dialect),
        dialect=compile_result.dialect,
        columns=columns_meta,
        rows=rows,
        row_count=exec_result.row_count,
        execution_time_ms=exec_result.execution_time_ms,
        timezone=exec_result.timezone,
        resolved=ResolvedInfoResponse(
            fact_tables=compile_result.resolved.fact_tables,
            dimensions=compile_result.resolved.dimensions,
            measures=compile_result.resolved.measures,
        ),
        warnings=[semantic_error_to_warning(w) for w in compile_result.warnings],
        sql_valid=compile_result.sql_valid,
        explain=_build_explain_response(compile_result),
        physical_tables=list(compile_result.physical_tables),
    )


def _build_explain_response(result: Any) -> ExplainPlanResponse | None:
    """Build an ExplainPlanResponse from a CompilationResult, if explain exists."""
    if not result.explain:
        return None
    return ExplainPlanResponse(
        planner=result.explain.planner,
        planner_reason=result.explain.planner_reason,
        base_object=result.explain.base_object,
        base_object_reason=result.explain.base_object_reason,
        joins=[
            ExplainJoinResponse(
                from_object=j.from_object,
                to_object=j.to_object,
                join_columns=j.join_columns,
                reason=j.reason,
            )
            for j in result.explain.joins
        ],
        where_filter_count=result.explain.where_filter_count,
        having_filter_count=result.explain.having_filter_count,
        has_totals=result.explain.has_totals,
        cfl_legs=[
            ExplainCflLegResponse(
                measure_source=leg.measure_source,
                common_root=leg.common_root,
                reason=leg.reason,
                measures=leg.measures,
                joins=leg.joins,
            )
            for leg in result.explain.cfl_legs
        ],
    )


@router.post("/{session_id}/query/plan", response_model=QueryPlanResponse)
async def plan_query(
    session_id: str,
    body: QueryPlanRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> QueryPlanResponse:
    """Return the planner's understanding of a query without executing it.

    Cheap by default (no warehouse round trip). When
    ``include_database_explain=true`` is set, also runs ``EXPLAIN <sql>``
    against the configured warehouse and returns the raw text. See
    ``design/PLAN_agent_api_improvements.md`` §2.
    """
    from orionbelt.api.deps import get_db_vendor

    store = _get_store(session_id, mgr)
    try:
        model = store.get_model(body.model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None
    dialect = _resolve_dialect(request_dialect=body.dialect, model=model, fallback=get_db_vendor())

    try:
        result = store.compile_query(body.model_id, body.query, dialect)
    except UnsupportedDialectError:
        raise HTTPException(status_code=400, detail=f"Unsupported dialect: '{dialect}'") from None
    except ResolutionError as exc:
        return QueryPlanResponse(
            status="error",
            warnings=[semantic_error_to_warning(e) for e in exc.errors],
            would_compile=False,
        )
    except FanoutError as exc:
        return QueryPlanResponse(
            status="error",
            warnings=[
                StructuredWarning(
                    code="FANOUT_ERROR",
                    severity="error",
                    message=exc.message,
                )
            ],
            would_compile=False,
        )
    except UnsupportedAggregationError as exc:
        return QueryPlanResponse(
            status="error",
            warnings=[
                StructuredWarning(
                    code="UNSUPPORTED_AGGREGATION",
                    severity="error",
                    message=str(exc),
                    context={"dialect": exc.dialect, "aggregation": exc.aggregation},
                )
            ],
            would_compile=False,
        )
    except UnsupportedGroupingError as exc:
        return QueryPlanResponse(
            status="error",
            warnings=[
                StructuredWarning(
                    code="UNSUPPORTED_GROUPING",
                    severity="error",
                    message=str(exc),
                    context={"dialect": exc.dialect, "grouping": exc.grouping},
                )
            ],
            would_compile=False,
        )

    physical_tables = _physical_tables_for(model, result)
    join_path = _join_path_steps(result)
    plan_warnings = [semantic_error_to_warning(w) for w in result.warnings]
    explain = result.explain
    response = QueryPlanResponse(
        status="ok",
        planner=explain.planner if explain else "",
        planner_reason=explain.planner_reason if explain else "",
        physical_tables=physical_tables,
        join_path=join_path,
        filters_applied=(
            (explain.where_filter_count + explain.having_filter_count) if explain else 0
        ),
        warnings=plan_warnings,
        would_compile=True,
        compiled_sql_length_estimate=len(result.sql),
    )

    if body.include_database_explain:
        try:
            raw = await asyncio.to_thread(explain_sql, result.sql, dialect=dialect)
            response.database_explain = DatabaseExplain(
                dialect=dialect,
                compiled_sql=result.sql,
                explain_output=raw,
                explain_format="text",
            )
        except (ExecutionUnavailableError, ExecutionError) as exc:
            response.warnings = response.warnings + [
                StructuredWarning(
                    code="DATABASE_EXPLAIN_FAILED",
                    severity="warning",
                    message=str(exc),
                    hint=(
                        "Database EXPLAIN is opt-in. Disable include_database_explain "
                        "or check QUERY_EXECUTE / DB_VENDOR / driver setup."
                    ),
                )
            ]

    return response


def _physical_tables_for(model: Any, result: Any) -> list[str]:
    """Compute qualified physical tables touched by a compilation."""
    names = list(dict.fromkeys(result.resolved.fact_tables))
    if result.explain:
        for j in result.explain.joins:
            for nm in (j.from_object, j.to_object):
                if nm not in names:
                    names.append(nm)
        for leg in result.explain.cfl_legs:
            if leg.measure_source and leg.measure_source not in names:
                names.append(leg.measure_source)
    out: list[str] = []
    for nm in names:
        obj = model.data_objects.get(nm)
        if obj is None:
            out.append(nm)
            continue
        parts = [p for p in (obj.database, obj.schema_name, obj.code) if p]
        out.append(".".join(parts) if parts else nm)
    return out


def _join_path_steps(result: Any) -> list[JoinPathStep]:
    """Convert ExplainPlan joins → API JoinPathStep list."""
    if not result.explain:
        return []
    steps: list[JoinPathStep] = []
    for j in result.explain.joins:
        steps.append(
            JoinPathStep(
                from_object=j.from_object,
                to_object=j.to_object,
                cardinality=j.cardinality or "many-to-one",
                fk=", ".join(j.join_columns) if j.join_columns else None,
            )
        )
    return steps


@router.post("/{session_id}/query/execute", response_model=QueryExecuteResponse)
async def execute_query(
    session_id: str,
    body: SessionQueryExecuteRequest,
    format: Literal["json", "tsv"] = "json",  # noqa: A002 — public query parameter
    format_values: bool = False,
    locale: str | None = None,
    timezone: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    cache: Cache = Depends(get_cache),  # noqa: B008
    cache_config: CacheRuntimeConfig = Depends(get_cache_config),  # noqa: B008
) -> QueryExecuteResponse | Response:
    """Compile and execute a query against the configured database.

    Requires ``QUERY_EXECUTE=true`` (or ``FLIGHT_ENABLED=true``) and
    ``DB_VENDOR`` + credentials.

    Query parameters
    ----------------
    * ``format`` — ``json`` (default) or ``tsv``. ``tsv`` returns a tab-
      separated body; cells with tab/newline/CR/double-quote are RFC 4180
      quoted. ``tsv`` implies ``format_values=true``.
    * ``format_values`` — when true, numeric cells in the JSON response are
      rendered as locale-aware display strings using each column's
      ``format`` pattern (matches the Gradio UI). Default false.
    * ``locale`` — BCP-47 locale tag (e.g. ``de``, ``en-US``). Falls back
      to ``DEFAULT_LOCALE`` env when omitted.
    * ``timezone`` — IANA TZ name (e.g. ``Europe/Berlin``). Overrides the
      model's ``default_timezone`` for naive timestamp coercion.
    """
    if not is_query_execute_enabled():
        raise HTTPException(
            status_code=503,
            detail="Query execution is not available. Set QUERY_EXECUTE=true "
            "and configure DB_VENDOR + credentials.",
        )
    store = _get_store(session_id, mgr)

    # Enforce a configurable default limit if the query has none
    query = body.query
    if query.limit is None:
        query = query.model_copy(update={"limit": get_query_default_limit()})

    try:
        model = store.get_model(body.model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None
    from orionbelt.api.deps import get_db_vendor

    logger.info("QueryObject request:\n%s", query.model_dump_json(by_alias=True, indent=2))

    dialect = _resolve_dialect(request_dialect=body.dialect, model=model, fallback=get_db_vendor())
    try:
        result = store.compile_query(body.model_id, query, dialect)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None
    except UnsupportedDialectError:
        raise HTTPException(status_code=400, detail=f"Unsupported dialect: '{dialect}'") from None
    except ResolutionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Query resolution failed",
                "errors": [
                    {"code": e.code, "message": e.message, "path": e.path} for e in exc.errors
                ],
            },
        ) from None
    except FanoutError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "Query fanout detected", "message": exc.message},
        ) from None
    except UnsupportedAggregationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported aggregation",
                "message": str(exc),
                "dialect": exc.dialect,
                "aggregation": exc.aggregation,
            },
        ) from None
    except UnsupportedGroupingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported grouping",
                "message": str(exc),
                "dialect": exc.dialect,
                "grouping": exc.grouping,
            },
        ) from None

    logger.info("Compiled SQL:\n%s", result.sql)

    return await _run_with_cache(
        store=store,
        model=model,
        compile_result=result,
        query=query,
        session_id=session_id,
        model_id=body.model_id,
        dialect=dialect,
        cache=cache,
        cache_config=cache_config,
        response_format=format,
        format_values=format_values,
        locale=locale,
        timezone_override=timezone,
    )


# -- OrionBelt Semantic QL (OBSQL) ------------------------------------------


def _obsql_translation_errors(exc: SQLTranslationError) -> HTTPException:
    """Map a SQLTranslationError to an HTTP 400 with structured error list."""
    return HTTPException(
        status_code=400,
        detail={
            "error": "OrionBelt Semantic QL translation failed",
            "errors": [
                {"code": e.code, "message": e.message, "context": e.context} for e in exc.errors
            ],
        },
    )


@router.post(
    "/{session_id}/query/semantic-ql/compile",
    response_model=SemanticQLCompileResponse,
    tags=["query"],
)
async def compile_semantic_ql(
    session_id: str,
    body: SemanticQLRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SemanticQLCompileResponse:
    """Translate OrionBelt Semantic QL (OBSQL) to a QueryObject and compile.

    Does not execute. The response includes the translated QueryObject
    JSON so callers can see *what their SQL became*. See
    ``design/PLAN_flight_natural_sql.md``.
    """
    from orionbelt.api.deps import get_db_vendor

    store = _get_store(session_id, mgr)
    try:
        model = store.get_model(body.model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None

    logger.info("OBSQL request:\n%s", body.sql)

    try:
        query = translate_sql_to_query(body.sql, model)
    except SQLTranslationError as exc:
        raise _obsql_translation_errors(exc) from None

    dialect = _resolve_dialect(request_dialect=body.dialect, model=model, fallback=get_db_vendor())
    try:
        result = store.compile_query(body.model_id, query, dialect)
    except UnsupportedDialectError:
        raise HTTPException(status_code=400, detail=f"Unsupported dialect: '{dialect}'") from None
    except ResolutionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Query resolution failed",
                "errors": [
                    {"code": e.code, "message": e.message, "path": e.path} for e in exc.errors
                ],
            },
        ) from None
    except FanoutError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "Query fanout detected", "message": exc.message},
        ) from None
    except UnsupportedAggregationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported aggregation",
                "message": str(exc),
                "dialect": exc.dialect,
                "aggregation": exc.aggregation,
            },
        ) from None
    except UnsupportedGroupingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported grouping",
                "message": str(exc),
                "dialect": exc.dialect,
                "grouping": exc.grouping,
            },
        ) from None

    logger.info("Compiled SQL:\n%s", result.sql)

    return SemanticQLCompileResponse(
        sql=format_sql(result.sql, result.dialect),
        dialect=result.dialect,
        query=query.model_dump(by_alias=True, mode="json"),
        resolved=ResolvedInfoResponse(
            fact_tables=result.resolved.fact_tables,
            dimensions=result.resolved.dimensions,
            measures=result.resolved.measures,
        ),
        warnings=[semantic_error_to_warning(w) for w in result.warnings],
        sql_valid=result.sql_valid,
        explain=_build_explain_response(result),
        physical_tables=list(result.physical_tables),
    )


@router.post(
    "/{session_id}/query/semantic-ql",
    response_model=QueryExecuteResponse,
    tags=["query"],
)
async def execute_semantic_ql(
    session_id: str,
    body: SemanticQLRequest,
    format: Literal["json", "tsv"] = "json",  # noqa: A002 — public query parameter
    format_values: bool = False,
    locale: str | None = None,
    timezone: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    cache: Cache = Depends(get_cache),  # noqa: B008
    cache_config: CacheRuntimeConfig = Depends(get_cache_config),  # noqa: B008
) -> QueryExecuteResponse | Response:
    """Translate OrionBelt Semantic QL (OBSQL) → QueryObject and execute.

    Same response shape as ``POST /query/execute`` (rows + schema +
    compiled SQL + explain + cache metadata). Supports ``?format=tsv``,
    ``?format_values=true``, ``?locale=``, ``?timezone=``.

    Requires ``QUERY_EXECUTE=true``. See ``design/PLAN_flight_natural_sql.md``.
    """
    if not is_query_execute_enabled():
        raise HTTPException(
            status_code=503,
            detail="Query execution is not available. Set QUERY_EXECUTE=true "
            "and configure DB_VENDOR + credentials.",
        )

    from orionbelt.api.deps import get_db_vendor

    store = _get_store(session_id, mgr)
    try:
        model = store.get_model(body.model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None

    logger.info("OBSQL request:\n%s", body.sql)

    try:
        query = translate_sql_to_query(body.sql, model)
    except SQLTranslationError as exc:
        raise _obsql_translation_errors(exc) from None

    # Default row limit if the user didn't specify LIMIT
    if query.limit is None:
        query = query.model_copy(update={"limit": get_query_default_limit()})

    dialect = _resolve_dialect(request_dialect=body.dialect, model=model, fallback=get_db_vendor())
    try:
        result = store.compile_query(body.model_id, query, dialect)
    except UnsupportedDialectError:
        raise HTTPException(status_code=400, detail=f"Unsupported dialect: '{dialect}'") from None
    except ResolutionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Query resolution failed",
                "errors": [
                    {"code": e.code, "message": e.message, "path": e.path} for e in exc.errors
                ],
            },
        ) from None
    except FanoutError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "Query fanout detected", "message": exc.message},
        ) from None
    except UnsupportedAggregationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported aggregation",
                "message": str(exc),
                "dialect": exc.dialect,
                "aggregation": exc.aggregation,
            },
        ) from None
    except UnsupportedGroupingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported grouping",
                "message": str(exc),
                "dialect": exc.dialect,
                "grouping": exc.grouping,
            },
        ) from None

    logger.info("Compiled SQL:\n%s", result.sql)

    return await _run_with_cache(
        store=store,
        model=model,
        compile_result=result,
        query=query,
        session_id=session_id,
        model_id=body.model_id,
        dialect=dialect,
        cache=cache,
        cache_config=cache_config,
        response_format=format,
        format_values=format_values,
        locale=locale,
        timezone_override=timezone,
    )


# -- cache helpers ----------------------------------------------------------


async def _run_with_cache(
    *,
    store: ModelStore,
    model: Any,
    compile_result: Any,
    query: Any,
    session_id: str,
    model_id: str,
    dialect: str,
    cache: Cache,
    cache_config: CacheRuntimeConfig,
    response_format: Literal["json", "tsv"],
    format_values: bool,
    locale: str | None,
    timezone_override: str | None,
) -> QueryExecuteResponse | Response:
    """Cache-aware execute pipeline shared by session and shortcut endpoints.

    Looks up the cache before executing, stores on miss, and surfaces the
    ``cached`` / ``ttl_*`` metadata. Only the canonical JSON shape is
    cached (TSV + value-formatted JSON skip caching to avoid locale-keyed
    proliferation).
    """
    model_default_tz: str | None = None
    override_db_tz = False
    if model.settings:
        model_default_tz = model.settings.default_timezone
        override_db_tz = model.settings.override_database_timezone
    tz = resolve_timezone(default_timezone=timezone_override or model_default_tz)

    cacheable = response_format == "json" and not format_values
    cache_key: str | None = None
    ttl_outcome = None
    if cacheable:
        # Non-deterministic SQL (RAND, NOW, CURRENT_DATE, TABLESAMPLE, ...) must
        # bypass the cache — same SQL, different answer per run. The cache key
        # is the compiled SQL hash, so caching would freeze one stale slice
        # forever. See ``cache/determinism.py``.
        nondet, name = is_nondeterministic_sql(compile_result.sql)
        if nondet:
            logger.info(
                "cache skipped for %s/%s: non-deterministic SQL (%s)",
                session_id,
                model_id,
                name,
            )
            ttl_outcome = TtlResult(
                ttl=None,
                no_cache_reason=NoCacheReason.NON_DETERMINISTIC_SQL,
            )
        else:
            cache_key = build_cache_key(
                session_id=session_id,
                model_id=model_id,
                dialect=dialect,
                sql=compile_result.sql,
            )
            ttl_outcome = _resolve_ttl(
                store=store,
                model_id=model_id,
                cache=cache,
                cache_config=cache_config,
                physical_tables=compile_result.physical_tables,
            )
            _cache_t0 = time.monotonic()
            cached_envelope = await _try_cache_get(cache, cache_key)
            if cached_envelope is not None:
                return _build_cached_response(
                    envelope=cached_envelope,
                    cache_key=cache_key,
                    ttl_outcome=ttl_outcome,
                    fetch_elapsed_ms=round((time.monotonic() - _cache_t0) * 1000, 2),
                )

    try:
        exec_result = await asyncio.to_thread(
            execute_sql,
            compile_result.sql,
            dialect=dialect,
            tz=tz,
            override_db_tz=override_db_tz,
        )
    except ExecutionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ExecutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    effective_locale = locale if locale is not None else get_default_locale()
    response = _build_execute_response(
        compile_result=compile_result,
        exec_result=exec_result,
        model=model,
        response_format=response_format,
        format_values=format_values,
        locale=effective_locale,
    )
    if (
        cacheable
        and cache_key is not None
        and ttl_outcome is not None
        and ttl_outcome.ttl is not None
        and isinstance(response, QueryExecuteResponse)
    ):
        _apply_ttl_metadata(response, ttl_outcome)
        await _try_cache_set(
            cache=cache,
            key=cache_key,
            response=response,
            ttl_seconds=ttl_outcome.ttl.seconds,
            session_id=session_id,
            model_id=model_id,
            dialect=dialect,
            query=query,
        )
    elif cacheable and ttl_outcome is not None and isinstance(response, QueryExecuteResponse):
        _apply_no_cache_metadata(response, ttl_outcome)
    return response


def _resolve_ttl(
    *,
    store: ModelStore,
    model_id: str,
    cache: Cache,
    cache_config: CacheRuntimeConfig,
    physical_tables: list[str],
) -> Any:
    """Compose the effective TTL for a query, merging contracts + heartbeats."""
    contracts = {}
    try:
        contracts = store.refresh_contracts(model_id)
    except Exception:
        logger.debug("refresh_contracts failed", exc_info=True)
    heartbeats: dict[str, datetime] = {}
    snapshot = getattr(cache, "heartbeats_snapshot", None)
    if callable(snapshot):
        try:
            heartbeats = snapshot()
        except Exception:
            heartbeats = {}
    return compute_effective_ttl(
        physical_tables=physical_tables,
        contracts=contracts,
        heartbeats=heartbeats,
        min_ttl_seconds=cache_config.min_ttl_seconds,
        max_ttl_seconds=cache_config.max_ttl_seconds,
        unknown_policy=cache_config.unknown_policy,
        unknown_default_ttl_seconds=cache_config.unknown_default_ttl_seconds,
    )


async def _try_cache_get(cache: Cache, key: str) -> Any:
    """Best-effort cache lookup; failures degrade to a miss.

    Returns the decoded envelope with ``cached_at_iso`` attached, or None.
    """
    try:
        result = await cache.get(key)
    except Exception:
        logger.debug("cache.get error", exc_info=True)
        return None
    if result is None:
        return None
    try:
        envelope = cache_decode(result.payload)
    except Exception:
        logger.debug("cache decode failed", exc_info=True)
        return None
    # Canonical Z suffix for UTC, matching /v1/settings.timezone.now and
    # /v1/cache/stats. datetime.isoformat() defaults to "+00:00".
    envelope.cached_at_iso = result.cached_at.isoformat().replace("+00:00", "Z")
    return envelope


async def _try_cache_set(
    *,
    cache: Cache,
    key: str,
    response: QueryExecuteResponse,
    ttl_seconds: int,
    session_id: str,
    model_id: str,
    dialect: str,
    query: Any,
) -> None:
    """Encode and store a response payload. Failures are logged and ignored."""
    from orionbelt.cache import key as cache_key_mod
    from orionbelt.cache import parquet_codec

    try:
        payload = parquet_codec.encode(
            columns=[c.model_dump() for c in response.columns],
            rows=response.rows,
            sql=response.sql,
            dialect=response.dialect,
            explain=response.explain.model_dump() if response.explain else None,
            warnings=[w.model_dump() for w in response.warnings],
            sql_valid=response.sql_valid,
            execution_time_ms=response.execution_time_ms,
            timezone=response.timezone,
            resolved=response.resolved.model_dump(),
            physical_tables=list(response.physical_tables),
        )
    except Exception:
        logger.debug("cache encode failed", exc_info=True)
        return
    try:
        await cache.set(
            key,
            payload,
            ttl_seconds=ttl_seconds,
            physical_tables=list(response.physical_tables),
            session_id=session_id,
            model_id=model_id,
            query_hash=cache_key_mod.query_hash(sql=response.sql),
            dialect=dialect,
            row_count=response.row_count,
        )
    except Exception:
        logger.debug("cache.set error", exc_info=True)


def _apply_ttl_metadata(response: QueryExecuteResponse, ttl_outcome: Any) -> None:
    """Surface TTL fields on a fresh (non-cached) response."""
    ttl = ttl_outcome.ttl
    if ttl is None:
        return
    response.ttl_seconds = ttl.seconds
    response.ttl_source = ttl.source
    response.ttl_limiting_table = ttl.limiting_table


def _apply_no_cache_metadata(response: QueryExecuteResponse, ttl_outcome: Any) -> None:
    """Document why a response was not cached."""
    reason = ttl_outcome.no_cache_reason
    if reason is None:
        return
    response.ttl_source = (
        "no_cache" if reason.value == "unknown_freshness" else f"no_cache:{reason.value}"
    )
    response.ttl_limiting_table = ttl_outcome.no_cache_table


def _build_cached_response(
    *,
    envelope: Any,
    cache_key: str,
    ttl_outcome: Any,
    fetch_elapsed_ms: float,
) -> QueryExecuteResponse:
    """Reconstruct a :class:`QueryExecuteResponse` from a cached Parquet entry.

    ``fetch_elapsed_ms`` is the wall-clock time spent reading + decoding the
    cache entry. It replaces the original DB execution time on the wire so
    callers see a realistic "this came from cache" duration; the original is
    preserved on disk in the Parquet sidecar for forensic inspection.
    """
    from orionbelt.api.schemas import StructuredWarning

    columns = [
        ColumnMetadata(
            name=c.get("name", ""),
            type=c.get("type", "string"),
            format=c.get("format"),
        )
        for c in envelope.columns
    ]
    explain_resp: ExplainPlanResponse | None = None
    if envelope.explain:
        try:
            explain_resp = ExplainPlanResponse(**envelope.explain)
        except Exception:
            explain_resp = None
    warnings_resp: list[StructuredWarning] = []
    for w in envelope.warnings or []:
        try:
            warnings_resp.append(StructuredWarning(**w))
        except Exception:
            continue
    cached_at_iso = envelope.cached_at_iso if hasattr(envelope, "cached_at_iso") else None
    response = QueryExecuteResponse(
        sql=envelope.sql,
        dialect=envelope.dialect,
        columns=columns,
        rows=envelope.rows,
        row_count=envelope.row_count,
        execution_time_ms=fetch_elapsed_ms,
        timezone=envelope.timezone,
        resolved=ResolvedInfoResponse(**(envelope.resolved or {})),
        warnings=warnings_resp,
        sql_valid=envelope.sql_valid,
        explain=explain_resp,
        physical_tables=list(envelope.physical_tables),
        cached=True,
        cached_at=cached_at_iso,
    )
    if ttl_outcome.ttl is not None:
        response.ttl_seconds = ttl_outcome.ttl.seconds
        response.ttl_source = ttl_outcome.ttl.source
        response.ttl_limiting_table = ttl_outcome.ttl.limiting_table
    return response
