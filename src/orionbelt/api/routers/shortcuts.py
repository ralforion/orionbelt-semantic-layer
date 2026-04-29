"""Top-level shortcut endpoints that auto-resolve session/model when unambiguous.

These endpoints mirror the session-scoped model discovery routes but without
requiring session_id and model_id path parameters. They work when:
- Single-model mode is active (exactly one model pre-loaded), or
- Exactly one session exists with exactly one model loaded.

Returns 409 Conflict if resolution is ambiguous.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from orionbelt.api.deps import get_session_manager, is_single_model_mode
from orionbelt.api.routers.model_api import (
    _build_explain,
    _build_join_graph,
    _build_schema,
    _search_model,
)
from orionbelt.api.schemas import (
    DiagramResponse,
    DimensionDetail,
    ErrorDetail,
    ExplainCflLegResponse,
    ExplainJoinResponse,
    ExplainPlanResponse,
    ExplainResponse,
    JoinGraphResponse,
    MeasureDetail,
    MetricDetail,
    QueryCompileResponse,
    QueryExecuteResponse,
    ResolvedInfoResponse,
    SchemaResponse,
    SearchRequest,
    SearchResponse,
    SPARQLRequest,
    SPARQLResponse,
    ValidateRequest,
    ValidateResponse,
)
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.validator import format_sql
from orionbelt.dialect.base import UnsupportedAggregationError
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionUnavailableError,
    execute_sql,
    resolve_timezone,
)
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import SessionManager

router = APIRouter()


# -- helpers -----------------------------------------------------------------


def _resolve_single_model(mgr: SessionManager) -> tuple[str, str, SemanticModel]:
    """Resolve to a unique (session_id, model_id, model).

    In single-model mode every session holds the same preloaded model, so we
    always return the __default__ session's copy — no ambiguity check needed.
    Otherwise falls back to scanning all sessions. Raises 409 if ambiguous,
    404 if nothing loaded.
    """
    candidates: list[tuple[str, str, SemanticModel]] = []

    # Check __default__ session first (auto-created in single-model mode)
    try:
        default_store = mgr.get_store("__default__")
        for ms in default_store.list_models():
            model = default_store.get_model(ms.model_id)
            candidates.append(("__default__", ms.model_id, model))
    except Exception:
        pass

    # In single-model mode the same model is pre-loaded into every session;
    # return the __default__ copy directly to avoid counting duplicates.
    if candidates and is_single_model_mode():
        return candidates[0]

    # Also scan user-created sessions
    for sess in mgr.list_sessions():
        try:
            store = mgr.get_store(sess.session_id)
        except Exception:
            continue
        for ms in store.list_models():
            model = store.get_model(ms.model_id)
            candidates.append((sess.session_id, ms.model_id, model))

    if not candidates:
        raise HTTPException(status_code=404, detail="No models loaded in any session")
    if len(candidates) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "Multiple models loaded across sessions — use session-scoped endpoints instead"
            ),
        )
    return candidates[0]


def _resolve_store_and_model(
    mgr: SessionManager,
) -> tuple[ModelStore, str]:
    """Resolve to a unique (store, model_id) for query compilation.

    In single-model mode returns the __default__ session's store directly.
    Otherwise checks __default__ then scans user-created sessions.
    """
    candidates: list[tuple[ModelStore, str]] = []

    # Check __default__ session first
    try:
        default_store = mgr.get_store("__default__")
        for ms in default_store.list_models():
            candidates.append((default_store, ms.model_id))
    except Exception:
        pass

    # In single-model mode, return immediately — no ambiguity possible.
    if candidates and is_single_model_mode():
        return candidates[0]

    # Also scan user-created sessions
    for sess in mgr.list_sessions():
        store = mgr.get_store(sess.session_id)
        for ms in store.list_models():
            candidates.append((store, ms.model_id))

    if not candidates:
        raise HTTPException(status_code=404, detail="No models loaded")
    if len(candidates) > 1:
        raise HTTPException(
            status_code=409,
            detail="Multiple models loaded — use session-scoped endpoints instead",
        )
    return candidates[0]


# -- top-level endpoints ----------------------------------------------------


@router.get("/schema", response_model=SchemaResponse, tags=["model-discovery"])
async def shortcut_schema(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SchemaResponse:
    """Full model structure (auto-resolves session/model)."""
    _, model_id, model = _resolve_single_model(mgr)
    return _build_schema(model_id, model)


@router.get("/dimensions", response_model=list[DimensionDetail], tags=["model-discovery"])
async def shortcut_dimensions(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> list[DimensionDetail]:
    """List all dimensions (auto-resolves session/model)."""
    _, model_id, model = _resolve_single_model(mgr)
    return _build_schema(model_id, model).dimensions


@router.get("/dimensions/{name}", response_model=DimensionDetail, tags=["model-discovery"])
async def shortcut_dimension(
    name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> DimensionDetail:
    """Get a dimension by name (auto-resolves session/model)."""
    _, _, model = _resolve_single_model(mgr)
    dim = model.dimensions.get(name)
    if not dim:
        raise HTTPException(status_code=404, detail=f"Dimension '{name}' not found")
    return DimensionDetail(
        name=name,
        data_object=dim.view,
        column=dim.column,
        result_type=dim.result_type.value,
        time_grain=dim.time_grain.value if dim.time_grain else None,
        via=dim.via,
        format=dim.format,
        owner=dim.owner,
        synonyms=dim.synonyms,
    )


@router.get("/measures", response_model=list[MeasureDetail], tags=["model-discovery"])
async def shortcut_measures(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> list[MeasureDetail]:
    """List all measures (auto-resolves session/model)."""
    _, model_id, model = _resolve_single_model(mgr)
    return _build_schema(model_id, model).measures


@router.get("/measures/{name}", response_model=MeasureDetail, tags=["model-discovery"])
async def shortcut_measure(
    name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> MeasureDetail:
    """Get a measure by name (auto-resolves session/model)."""
    _, _, model = _resolve_single_model(mgr)
    m = model.measures.get(name)
    if not m:
        raise HTTPException(status_code=404, detail=f"Measure '{name}' not found")
    return MeasureDetail(
        name=name,
        result_type=m.result_type.value,
        aggregation=m.aggregation,
        expression=m.expression,
        columns=[{"dataObject": c.view or "", "column": c.column or ""} for c in m.columns],
        distinct=m.distinct,
        total=m.total,
        description=m.description,
        format=m.format,
        data_type=m.data_type,
        owner=m.owner,
        synonyms=m.synonyms,
    )


@router.get("/metrics", response_model=list[MetricDetail], tags=["model-discovery"])
async def shortcut_metrics(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> list[MetricDetail]:
    """List all metrics (auto-resolves session/model)."""
    _, model_id, model = _resolve_single_model(mgr)
    return _build_schema(model_id, model).metrics


@router.get("/metrics/{name}", response_model=MetricDetail, tags=["model-discovery"])
async def shortcut_metric(
    name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> MetricDetail:
    """Get a metric by name (auto-resolves session/model)."""
    _, _, model = _resolve_single_model(mgr)
    met = model.metrics.get(name)
    if not met:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")
    component_names = re.findall(r"\{\[([^\]]+)\]\}", met.expression or "")
    return MetricDetail(
        name=name,
        type=met.type.value,
        expression=met.expression,
        measure=met.measure,
        time_dimension=met.time_dimension,
        component_measures=component_names,
        description=met.description,
        format=met.format,
        data_type=met.data_type,
        owner=met.owner,
        synonyms=met.synonyms,
    )


@router.get("/explain/{name}", response_model=ExplainResponse, tags=["model-discovery"])
async def shortcut_explain(
    name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ExplainResponse:
    """Explain lineage of a dimension, measure, or metric (auto-resolves)."""
    _, _, model = _resolve_single_model(mgr)
    return _build_explain(name, model)


@router.post("/find", response_model=SearchResponse, tags=["model-discovery"])
async def shortcut_find(
    body: SearchRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SearchResponse:
    """Search model artefacts (auto-resolves session/model)."""
    _, _, model = _resolve_single_model(mgr)
    results = _search_model(model, body.query, body.types)
    return SearchResponse(results=results)


@router.get("/join-graph", response_model=JoinGraphResponse, tags=["model-discovery"])
async def shortcut_join_graph(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> JoinGraphResponse:
    """Return the join graph (auto-resolves session/model)."""
    _, _, model = _resolve_single_model(mgr)
    return _build_join_graph(model)


@router.get("/diagram/er", response_model=DiagramResponse, tags=["model-discovery"])
async def shortcut_diagram_er(
    show_columns: bool = True,
    theme: str = "default",
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> DiagramResponse:
    """Generate a Mermaid ER diagram (auto-resolves session/model)."""
    from orionbelt.service.diagram import generate_mermaid_er

    _, _, model = _resolve_single_model(mgr)
    mermaid = generate_mermaid_er(model, show_columns=show_columns, theme=theme)
    return DiagramResponse(mermaid=mermaid)


@router.get(
    "/graph",
    tags=["graph"],
    response_class=Response,
    responses={200: {"content": {"text/turtle": {}}}},
)
async def shortcut_graph(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> Response:
    """Return the OBSL-Core RDF graph as Turtle (auto-resolves session/model)."""
    store, model_id = _resolve_store_and_model(mgr)
    try:
        artifact = store.get_graph(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
    return Response(content=artifact.turtle, media_type="text/turtle")


@router.post("/sparql", response_model=SPARQLResponse, tags=["graph"])
async def shortcut_sparql(
    body: SPARQLRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SPARQLResponse:
    """Execute a read-only SPARQL query (auto-resolves session/model)."""
    from orionbelt.obsl.sparql import SPARQLUpdateError

    store, model_id = _resolve_store_and_model(mgr)
    try:
        result = store.query_graph(model_id, body.query)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
    except SPARQLUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"SPARQL error: {exc}") from None
    return SPARQLResponse(
        type=result.type,
        variables=result.variables,
        results=result.results,
        boolean=result.boolean,
    )


@router.post("/validate", response_model=ValidateResponse, tags=["validation"])
async def shortcut_validate(
    body: ValidateRequest,
) -> ValidateResponse:
    """Validate an OBML model (stateless — no session required)."""
    store = ModelStore()
    raw = cast("dict[str, object] | None", body.model_json)
    summary = store.validate(body.model_yaml, raw_dict=raw)
    return ValidateResponse(
        valid=summary.valid,
        errors=[ErrorDetail(code=e.code, message=e.message, path=e.path) for e in summary.errors],
        warnings=[
            ErrorDetail(code=w.code, message=w.message, path=w.path) for w in summary.warnings
        ],
    )


class ShortcutQueryRequest(QueryObject):
    """Query request for top-level shortcut (query body only, dialect as param)."""

    pass


@router.post("/query/sql", response_model=QueryCompileResponse, tags=["query"])
async def shortcut_compile_query(
    body: ShortcutQueryRequest,
    dialect: str = "postgres",
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> QueryCompileResponse:
    """Compile a query (auto-resolves session/model)."""
    store, model_id = _resolve_store_and_model(mgr)
    try:
        result = store.compile_query(model_id, body, dialect)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
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
        warnings=result.warnings,
        sql_valid=result.sql_valid,
        explain=explain_resp,
    )


class ShortcutQueryExecuteRequest(QueryObject):
    """Query request body for the execute shortcut (query body only, dialect as param)."""

    pass


@router.post("/query/execute", response_model=QueryExecuteResponse, tags=["query"])
async def shortcut_execute_query(
    body: ShortcutQueryExecuteRequest,
    dialect: str | None = None,
    format: Literal["json", "tsv"] = "json",  # noqa: A002 — public query parameter
    format_values: bool = False,
    locale: str | None = None,
    timezone: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> QueryExecuteResponse | Response:
    """Compile and execute a query (auto-resolves session/model).

    Requires ``QUERY_EXECUTE=true`` (or ``FLIGHT_ENABLED=true``). If ``dialect``
    is omitted, uses ``DB_VENDOR``. Enforces a configurable default row limit
    if the query has no explicit limit.

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
    from orionbelt.api.deps import (
        get_db_vendor,
        get_default_locale,
        get_query_default_limit,
        is_query_execute_enabled,
    )

    if not is_query_execute_enabled():
        raise HTTPException(
            status_code=503,
            detail="Query execution is not available. Set QUERY_EXECUTE=true "
            "and configure DB_VENDOR + credentials.",
        )

    store, model_id = _resolve_store_and_model(mgr)

    # Auto-detect dialect from DB_VENDOR when not provided
    if dialect is None:
        dialect = get_db_vendor()

    # Enforce a configurable default limit if the query has none
    query: QueryObject = body
    if query.limit is None:
        query = query.model_copy(update={"limit": get_query_default_limit()})

    try:
        result = store.compile_query(model_id, query, dialect)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
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

    # Resolve timezone — request override → model default → host TZ → UTC.
    model = store.get_model(model_id)
    model_default_tz: str | None = None
    override_db_tz = False
    if model.settings:
        model_default_tz = model.settings.default_timezone
        override_db_tz = model.settings.override_database_timezone
    tz = resolve_timezone(default_timezone=timezone or model_default_tz)

    try:
        exec_result = await asyncio.to_thread(
            execute_sql,
            result.sql,
            dialect=dialect,
            tz=tz,
            override_db_tz=override_db_tz,
        )
    except ExecutionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ExecutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    from orionbelt.api.routers.sessions import _build_execute_response

    effective_locale = locale if locale is not None else get_default_locale()
    return _build_execute_response(
        compile_result=result,
        exec_result=exec_result,
        model=model,
        response_format=format,
        format_values=format_values,
        locale=effective_locale,
    )
