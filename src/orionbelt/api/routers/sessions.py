"""Session-scoped endpoints for model management, validation, and query."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from orionbelt.api.deps import (
    get_preload_model_yaml,
    get_query_default_limit,
    get_session_manager,
    is_query_execute_enabled,
    is_session_list_disabled,
    is_single_model_mode,
)
from orionbelt.api.schemas import (
    ColumnMetadata,
    DiagramResponse,
    ErrorDetail,
    ExplainCflLegResponse,
    ExplainJoinResponse,
    ExplainPlanResponse,
    ModelLoadRequest,
    ModelLoadResponse,
    ModelSummaryResponse,
    QueryCompileResponse,
    QueryExecuteResponse,
    ResolvedInfoResponse,
    SessionCreateRequest,
    SessionListResponse,
    SessionQueryExecuteRequest,
    SessionQueryRequest,
    SessionResponse,
    ValidateRequest,
    ValidateResponse,
)
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.dialect.base import UnsupportedAggregationError
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.service.db_executor import ExecutionError, ExecutionUnavailableError, execute_sql
from orionbelt.service.diagram import generate_mermaid_er
from orionbelt.service.model_store import ModelStore, ModelValidationError
from orionbelt.service.session_manager import SessionInfo, SessionManager, SessionNotFoundError

router = APIRouter()


# -- helpers -----------------------------------------------------------------


def _get_store(session_id: str, mgr: SessionManager) -> ModelStore:
    """Resolve session_id to ModelStore, raise 404 if missing/expired."""
    try:
        return mgr.get_store(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None


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
    """Create a new session."""
    metadata = body.metadata if body else {}
    info = mgr.create_session(metadata=metadata)

    # Single-model mode: pre-load the configured model into the new session
    preload_yaml = get_preload_model_yaml()
    if preload_yaml is not None:
        store = mgr.get_store(info.session_id)
        store.load_model(preload_yaml)
        # Refresh info to reflect the loaded model count
        info = mgr.get_session(info.session_id)

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
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None
    return _session_response(info)


@router.delete("/{session_id}", status_code=204)
async def close_session(
    session_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> None:
    """Close a session and release its resources."""
    try:
        mgr.close_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None


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
    store = _get_store(session_id, mgr)
    try:
        result = store.load_model(body.model_yaml)
    except ModelValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Invalid OBML model: parsing or validation failed",
                "errors": [
                    {"code": e.code, "message": e.message, "path": e.path} for e in exc.errors
                ],
                "warnings": [
                    {"code": w.code, "message": w.message, "path": w.path} for w in exc.warnings
                ],
            },
        ) from None
    return ModelLoadResponse(
        model_id=result.model_id,
        data_objects=result.data_objects,
        dimensions=result.dimensions,
        measures=result.measures,
        metrics=result.metrics,
        warnings=result.warnings,
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
    """Validate OBML YAML within a session context."""
    store = _get_store(session_id, mgr)
    summary = store.validate(body.model_yaml)
    return ValidateResponse(
        valid=summary.valid,
        errors=[ErrorDetail(code=e.code, message=e.message, path=e.path) for e in summary.errors],
        warnings=[
            ErrorDetail(code=w.code, message=w.message, path=w.path) for w in summary.warnings
        ],
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
        result = store.compile_query(body.model_id, body.query, body.dialect)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None
    except UnsupportedDialectError:
        raise HTTPException(
            status_code=400, detail=f"Unsupported dialect: '{body.dialect}'"
        ) from None
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
        sql=result.sql,
        dialect=result.dialect,
        resolved=ResolvedInfoResponse(
            fact_tables=result.resolved.fact_tables,
            dimensions=result.resolved.dimensions,
            measures=result.resolved.measures,
        ),
        warnings=result.warnings,
        sql_valid=result.sql_valid,
        explain=explain_resp,
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


@router.post("/{session_id}/query/execute", response_model=QueryExecuteResponse)
async def execute_query(
    session_id: str,
    body: SessionQueryExecuteRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> QueryExecuteResponse:
    """Compile and execute a query against the configured database.

    Requires QUERY_EXECUTE=true (or FLIGHT_ENABLED=true) and DB_VENDOR + credentials.
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
        result = store.compile_query(body.model_id, query, body.dialect)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None
    except UnsupportedDialectError:
        raise HTTPException(
            status_code=400, detail=f"Unsupported dialect: '{body.dialect}'"
        ) from None
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

    try:
        exec_result = await asyncio.to_thread(execute_sql, result.sql, dialect=body.dialect)
    except ExecutionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ExecutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    return QueryExecuteResponse(
        sql=result.sql,
        dialect=result.dialect,
        columns=[ColumnMetadata(name=c.name, type=c.type_hint) for c in exec_result.columns],
        rows=exec_result.rows,
        row_count=exec_result.row_count,
        execution_time_ms=exec_result.execution_time_ms,
        resolved=ResolvedInfoResponse(
            fact_tables=result.resolved.fact_tables,
            dimensions=result.resolved.dimensions,
            measures=result.resolved.measures,
        ),
        warnings=result.warnings,
        sql_valid=result.sql_valid,
        explain=_build_explain_response(result),
    )
