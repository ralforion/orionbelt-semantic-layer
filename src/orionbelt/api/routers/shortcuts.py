"""Top-level shortcut endpoints that auto-resolve session/model when unambiguous.

These endpoints mirror the session-scoped model discovery routes but without
requiring session_id and model_id path parameters. They work when:
- Single-model mode is active (exactly one model pre-loaded), or
- Exactly one session exists with exactly one model loaded.

Returns 409 Conflict if resolution is ambiguous.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from orionbelt.api.deps import (
    CacheRuntimeConfig,
    get_cache,
    get_cache_config,
    get_db_vendor,
    get_session_manager,
)
from orionbelt.api.routers.composables import build_composables
from orionbelt.api.routers.lineage import (
    LINEAGE_RESPONSES,
    LineageFormat,
    artefact_lineage,
    query_lineage,
)
from orionbelt.api.routers.model_api import (
    _build_explain,
    _build_join_graph,
    _build_schema,
    _build_search_response,
)
from orionbelt.api.schema_guards import validate_query_body
from orionbelt.api.schemas import (
    ComposablesResponse,
    ConceptMappingListResponse,
    ConceptNamespacesResponse,
    DiagramResponse,
    DimensionDetail,
    ExampleDetail,
    ExampleListResponse,
    ExplainCflLegResponse,
    ExplainJoinResponse,
    ExplainPlanResponse,
    ExplainResponse,
    JoinGraphResponse,
    LineageResponse,
    MeasureDetail,
    MetricDetail,
    QueryCompileResponse,
    QueryExecuteResponse,
    QueryPlanRequest,
    QueryPlanResponse,
    ResolvedInfoResponse,
    RuleCompileAllResponse,
    RuleCompileRequest,
    RuleCompileResponse,
    RuleDetail,
    RuleEvaluateRequest,
    RuleEvaluateResponse,
    RuleListResponse,
    RuleReportRequest,
    RuleReportResponse,
    SchemaResponse,
    SearchRequest,
    SearchResponse,
    SemanticQLCompileResponse,
    SemanticQLRequest,
    SPARQLRequest,
    SPARQLResponse,
    UnmappedObjectsResponse,
    ValidateRequest,
    ValidateResponse,
)
from orionbelt.api.warnings_adapter import error_info_to_detail, semantic_error_to_warning
from orionbelt.cache import Cache
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.validator import format_sql
from orionbelt.dialect.base import (
    AmbiguousTableReferenceError,
    UnsupportedAggregationError,
    UnsupportedFunctionError,
    UnsupportedGroupingError,
    UnsupportedNestedAccessError,
)
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import SessionManager

router = APIRouter()


# -- helpers -----------------------------------------------------------------


def _candidate_session_ids(mgr: SessionManager) -> list[str]:
    """Ordered, deduplicated list of session ids the shortcut routes consult.

    In admin-curated mode (``MODEL_FILES``) the shortcut resolves to the
    curated **protected** session(s) *only* — transient ``__default__`` and
    user-created sessions are ignored. Otherwise a short-lived extra model in a
    scratch session would make the unique-model shortcut 409 even though the
    curated model is unambiguous (observed on the demo deployment, where the
    MCP audit's single-model shortcut intermittently 409'd against transient
    sessions). Outside admin-curated mode, order is ``__default__`` (legacy
    single-model), then protected, then user-created sessions.
    """
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(session_id: str) -> None:
        if session_id in seen:
            return
        seen.add(session_id)
        ordered.append(session_id)

    if mgr.is_single_model_mode:
        for sid in mgr.list_protected_session_ids():
            _add(sid)
        return ordered

    _add("__default__")
    for sid in mgr.list_protected_session_ids():
        _add(sid)
    for sess in mgr.list_sessions():
        _add(sess.session_id)
    return ordered


def _resolve_single_model(mgr: SessionManager) -> tuple[str, str, SemanticModel]:
    """Resolve to a unique (session_id, model_id, model).

    Scans ``__default__``, every admin-curated protected session
    (``MODEL_FILES``), and every user-created session. Raises 409 if more
    than one model is found, 404 if none.
    """
    candidates: list[tuple[str, str, SemanticModel]] = []

    for sid in _candidate_session_ids(mgr):
        try:
            store = mgr.get_store(sid)
        except Exception:
            continue
        for ms in store.list_models():
            model = store.get_model(ms.model_id)
            candidates.append((sid, ms.model_id, model))

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

    Scans ``__default__``, every admin-curated protected session, and every
    user-created session. Raises 409 if more than one model is found.
    """
    candidates: list[tuple[ModelStore, str]] = []

    for sid in _candidate_session_ids(mgr):
        try:
            store = mgr.get_store(sid)
        except Exception:
            continue
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


def _session_id_for_store(mgr: SessionManager, store: ModelStore) -> str:
    """Find the session_id whose store matches ``store``.

    Used by shortcut endpoints so the cache key stays session-scoped (the
    same session_id the caller would have supplied via the session-scoped
    endpoint). Falls back to ``__default__`` if no match is found.
    """
    for sid in _candidate_session_ids(mgr):
        try:
            if mgr.get_store(sid) is store:
                return sid
        except Exception:
            continue
    return "__default__"


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
    from orionbelt.api.routers.model_api import get_dimension

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await get_dimension(session_id, model_id, name, mgr)


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
    from orionbelt.api.routers.model_api import get_measure

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await get_measure(session_id, model_id, name, mgr)


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
    from orionbelt.api.routers.model_api import get_metric

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await get_metric(session_id, model_id, name, mgr)


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
    return _build_search_response(model, body.query, body.types)


@router.get("/join-graph", response_model=JoinGraphResponse, tags=["model-discovery"])
async def shortcut_join_graph(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> JoinGraphResponse:
    """Return the join graph (auto-resolves session/model)."""
    _, _, model = _resolve_single_model(mgr)
    return _build_join_graph(model)


@router.post(
    "/composables",
    response_model=ComposablesResponse,
    tags=["model-discovery"],
    dependencies=[Depends(validate_query_body)],
)
async def shortcut_composables_for_query(
    query: QueryObject,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ComposablesResponse:
    """Resolve composables for an in-progress query (auto-resolves session/model)."""
    _, _, model = _resolve_single_model(mgr)
    return build_composables(model, query=query)


@router.get("/composables", response_model=ComposablesResponse, tags=["model-discovery"])
async def shortcut_composables_for_anchors(
    anchor: Annotated[list[str], Query()] = [],  # noqa: B006 — FastAPI query list
    anchor_type: Annotated[str | None, Query(alias="anchorType")] = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ComposablesResponse:
    """Resolve composables for named anchors (auto-resolves session/model)."""
    _, _, model = _resolve_single_model(mgr)
    return build_composables(model, anchors=anchor, anchor_type=anchor_type)


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
    online: bool = False,
    dialect: str | None = None,
) -> ValidateResponse:
    """Validate an OBML model (stateless — no session required).

    With ``online=true`` the model is additionally checked against the
    configured datasource: each data object is probed for its table and its
    declared columns, and drift is reported as an error. ``dialect`` selects
    the datasource to probe and defaults to ``DB_VENDOR`` — it names a
    connection to open, not SQL to generate, so unlike the ``dialect`` on the
    query endpoints it does not fall back to the model's
    ``settings.defaultDialect``.
    """
    from orionbelt.api.routers.sessions import _probe_dialect

    store = ModelStore()
    raw = cast("dict[str, object] | None", body.model_json)
    summary = store.validate(
        body.model_yaml, raw_dict=raw, datasource_dialect=_probe_dialect(online, dialect)
    )
    return ValidateResponse(
        valid=summary.valid,
        errors=[error_info_to_detail(e) for e in summary.errors],
        warnings=[error_info_to_detail(w) for w in summary.warnings],
    )


class ShortcutQueryRequest(QueryObject):
    """Query request for top-level shortcut (query body only, dialect as param)."""

    pass


@router.post(
    "/query/sql",
    response_model=QueryCompileResponse,
    tags=["query"],
    dependencies=[Depends(validate_query_body)],
)
async def shortcut_compile_query(
    body: ShortcutQueryRequest,
    dialect: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> QueryCompileResponse:
    """Compile a query (auto-resolves session/model). When ``dialect`` is
    omitted, falls back to ``model.settings.defaultDialect`` then ``DB_VENDOR``."""
    from orionbelt.api.routers.sessions import _resolve_dialect

    store, model_id = _resolve_store_and_model(mgr)
    model = store.get_model(model_id)
    dialect = _resolve_dialect(request_dialect=dialect, model=model, fallback=get_db_vendor())
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
    except UnsupportedNestedAccessError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported nested access",
                "message": str(exc),
                "dialect": exc.dialect,
                "dataObject": exc.alias,
            },
        ) from None
    except UnsupportedFunctionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported function",
                "message": str(exc),
                "dialect": exc.dialect,
                "function": exc.function,
            },
        ) from None
    except AmbiguousTableReferenceError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Ambiguous table reference",
                "message": str(exc),
                "dialect": exc.dialect,
                "database": exc.database,
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
        warnings=[semantic_error_to_warning(w) for w in result.warnings],
        sql_valid=result.sql_valid,
        explain=explain_resp,
        physical_tables=list(result.physical_tables),
    )


class ShortcutQueryExecuteRequest(QueryObject):
    """Query request body for the execute shortcut (query body only, dialect as param)."""

    pass


@router.post(
    "/query/execute",
    response_model=QueryExecuteResponse,
    tags=["query"],
    dependencies=[Depends(validate_query_body)],
)
async def shortcut_execute_query(
    body: ShortcutQueryExecuteRequest,
    request: Request,
    dialect: str | None = None,
    format: Literal["json", "tsv", "arrow"] = "json",  # noqa: A002 — public query parameter
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
    * ``format`` — ``json`` (default), ``tsv``, or ``arrow``. ``tsv`` returns a
      tab-separated body; cells with tab/newline/CR/double-quote are RFC 4180
      quoted. ``tsv`` implies ``format_values=true``. ``arrow`` returns an
      Arrow IPC stream (also selectable via the ``Accept`` header).
    * ``format_values`` — when true, numeric cells are rendered as
      locale-aware display strings using each column's ``format`` pattern
      (matches the Gradio UI). Applies to both ``json`` and ``arrow`` (baked
      into the IPC blob); raw ``arrow`` (the default) ships typed values and
      formats client-side. Default false.
    * ``locale`` — BCP-47 locale tag (e.g. ``de``, ``en-US``). Falls back
      to ``DEFAULT_LOCALE`` env when omitted.
    * ``timezone`` — IANA TZ name (e.g. ``Europe/Berlin``). Overrides the
      model's ``default_timezone`` for naive timestamp coercion.
    """
    from orionbelt.api.deps import (
        get_query_default_limit,
        is_query_execute_enabled,
    )
    from orionbelt.api.routers.sessions import _resolve_dialect

    if not is_query_execute_enabled():
        raise HTTPException(
            status_code=503,
            detail="Query execution is not available. Set QUERY_EXECUTE=true "
            "and configure DB_VENDOR + credentials.",
        )

    store, model_id = _resolve_store_and_model(mgr)
    model = store.get_model(model_id)

    # Resolve dialect: explicit param → model.settings.defaultDialect → DB_VENDOR.
    dialect = _resolve_dialect(request_dialect=dialect, model=model, fallback=get_db_vendor())

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
    except UnsupportedNestedAccessError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported nested access",
                "message": str(exc),
                "dialect": exc.dialect,
                "dataObject": exc.alias,
            },
        ) from None
    except UnsupportedFunctionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported function",
                "message": str(exc),
                "dialect": exc.dialect,
                "function": exc.function,
            },
        ) from None
    except AmbiguousTableReferenceError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Ambiguous table reference",
                "message": str(exc),
                "dialect": exc.dialect,
                "database": exc.database,
            },
        ) from None

    # Resolve the session_id that owns the resolved store so the cache key
    # remains scoped per session (matching the session-scoped endpoint).
    from orionbelt.api.deps import get_cache, get_cache_config
    from orionbelt.api.routers.sessions import _run_with_cache
    from orionbelt.api.services.query_execution import negotiate_execute_format

    session_id = _session_id_for_store(mgr, store)
    cache = get_cache()
    cache_config = get_cache_config()
    effective_format = negotiate_execute_format(format, request.headers.get("accept"))
    return await _run_with_cache(
        query=query,
        store=store,
        model=model,
        compile_result=result,
        session_id=session_id,
        model_id=model_id,
        dialect=dialect,
        cache=cache,
        cache_config=cache_config,
        response_format=effective_format,
        format_values=format_values,
        locale=locale,
        timezone_override=timezone,
        accept_encoding=request.headers.get("accept-encoding"),
    )


class ShortcutSemanticQLRequest(SemanticQLRequest):
    """Semantic-SQL shortcut: model_id auto-resolved when single-model mode."""

    model_id: str = ""


@router.post(
    "/query/semantic-ql/compile",
    response_model=SemanticQLCompileResponse,
    tags=["query"],
)
async def shortcut_compile_semantic_ql(
    body: ShortcutSemanticQLRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SemanticQLCompileResponse:
    """Translate Semantic QL → QueryObject and compile (auto-resolves session/model)."""
    from orionbelt.api.routers.sessions import compile_semantic_ql

    store, model_id = _resolve_store_and_model(mgr)
    session_id = _session_id_for_store(mgr, store)
    body.model_id = model_id
    return await compile_semantic_ql(session_id, body, mgr)


@router.post(
    "/query/semantic-ql",
    response_model=QueryExecuteResponse,
    tags=["query"],
)
async def shortcut_execute_semantic_ql(
    body: ShortcutSemanticQLRequest,
    request: Request,
    format: Literal["json", "tsv", "arrow"] = "json",  # noqa: A002 — public query parameter
    format_values: bool = False,
    locale: str | None = None,
    timezone: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> QueryExecuteResponse | Response:
    """Translate Semantic QL → QueryObject and execute (auto-resolves session/model)."""
    from orionbelt.api.deps import get_cache, get_cache_config
    from orionbelt.api.routers.sessions import execute_semantic_ql

    store, model_id = _resolve_store_and_model(mgr)
    session_id = _session_id_for_store(mgr, store)
    body.model_id = model_id
    return await execute_semantic_ql(
        session_id,
        body,
        request,
        format,
        format_values,
        locale,
        timezone,
        mgr,
        get_cache(),
        get_cache_config(),
    )


@router.post(
    "/query/plan",
    response_model=QueryPlanResponse,
    tags=["query"],
    dependencies=[Depends(validate_query_body)],
)
async def shortcut_plan_query(
    body: QueryPlanRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> QueryPlanResponse:
    """Return the planner's understanding of a query (auto-resolves session/model).

    The session-scoped form is ``POST /v1/sessions/{sid}/query/plan``. This
    shortcut auto-resolves when exactly one model is loaded — typical of
    single-model deployments. The request body's ``model_id`` is ignored
    here in favor of the resolved one (kept in the schema for shape parity).
    """
    from orionbelt.api.routers.sessions import plan_query

    _, model_id, _ = _resolve_single_model(mgr)
    body.model_id = model_id
    session_id = _session_id_for_store(mgr, _resolve_store_and_model(mgr)[0])
    return await plan_query(session_id, body, mgr)


@router.get("/rules", response_model=RuleListResponse, tags=["rules"])
async def shortcut_rules(
    dialect: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
) -> RuleListResponse:
    """List business rules with statistics (auto-resolves session/model)."""
    from orionbelt.api.routers.rules import list_rules

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await list_rules(session_id, model_id, dialect, mgr, db_vendor)


@router.post("/rules/compile", response_model=RuleCompileAllResponse, tags=["rules"])
async def shortcut_compile_all_rules(
    body: RuleCompileRequest | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
) -> RuleCompileAllResponse:
    """Compile every rule (auto-resolves session/model)."""
    from orionbelt.api.routers.rules import compile_all_rules

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await compile_all_rules(session_id, model_id, body, mgr, db_vendor)


@router.post("/rules/evaluate", response_model=RuleReportResponse, tags=["rules"])
async def shortcut_evaluate_all_rules(
    body: RuleReportRequest | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
    cache: Cache = Depends(get_cache),  # noqa: B008
    cache_config: CacheRuntimeConfig = Depends(get_cache_config),  # noqa: B008
) -> RuleReportResponse:
    """Evaluate every rule into a report (auto-resolves session/model)."""
    from orionbelt.api.routers.rules import evaluate_all_rules

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await evaluate_all_rules(session_id, model_id, body, mgr, db_vendor, cache, cache_config)


@router.post("/rules/{name}/evaluate", response_model=RuleEvaluateResponse, tags=["rules"])
async def shortcut_evaluate_rule(
    name: str,
    body: RuleEvaluateRequest | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
    cache: Cache = Depends(get_cache),  # noqa: B008
    cache_config: CacheRuntimeConfig = Depends(get_cache_config),  # noqa: B008
) -> RuleEvaluateResponse:
    """Run one rule and return its findings (auto-resolves session/model)."""
    from orionbelt.api.routers.rules import evaluate_rule

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await evaluate_rule(
        session_id, model_id, name, body, mgr, db_vendor, cache, cache_config
    )


@router.get("/rules/{name}", response_model=RuleDetail, tags=["rules"])
async def shortcut_rule(
    name: str,
    dialect: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
) -> RuleDetail:
    """Describe one business rule (auto-resolves session/model)."""
    from orionbelt.api.routers.rules import get_rule

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await get_rule(session_id, model_id, name, dialect, mgr, db_vendor)


@router.post("/rules/{name}/compile", response_model=RuleCompileResponse, tags=["rules"])
async def shortcut_compile_rule(
    name: str,
    body: RuleCompileRequest | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
) -> RuleCompileResponse:
    """Compile one business rule (auto-resolves session/model)."""
    from orionbelt.api.routers.rules import compile_rule

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await compile_rule(session_id, model_id, name, body, mgr, db_vendor)


def _shortcut_lineage(kind: str, plural: str) -> None:
    @router.get(
        f"/{plural}/{{name}}/lineage",
        response_model=LineageResponse,
        responses=LINEAGE_RESPONSES,
        tags=["lineage"],
        name=f"shortcut_{kind}_lineage",
        summary=f"Lineage of a {kind}",
    )
    async def _lineage(
        name: str,
        format: LineageFormat = LineageFormat.json,  # noqa: A002 - public query parameter
        mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    ) -> LineageResponse | Response:
        session_id, model_id, _ = _resolve_single_model(mgr)
        return artefact_lineage(session_id, model_id, kind, name, mgr, format)

    _lineage.__doc__ = (
        f"What the {kind} is built from, down to the tables it reads (auto-resolves session/model)."
    )


for _kind, _plural in (
    ("dimension", "dimensions"),
    ("measure", "measures"),
    ("metric", "metrics"),
    ("rule", "rules"),
):
    _shortcut_lineage(_kind, _plural)


@router.post(
    "/query/lineage",
    response_model=LineageResponse,
    responses=LINEAGE_RESPONSES,
    tags=["lineage"],
    dependencies=[Depends(validate_query_body)],
)
async def shortcut_query_lineage(
    body: ShortcutQueryRequest,
    dialect: str | None = None,
    format: LineageFormat = LineageFormat.json,  # noqa: A002 - public query parameter
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> LineageResponse | Response:
    """What a query is built from, including the planner's joins (auto-resolves)."""
    store, model_id = _resolve_store_and_model(mgr)
    return query_lineage(store, model_id, body, dialect, format)


@router.get(
    "/concept-mappings",
    response_model=ConceptMappingListResponse,
    tags=["model-discovery"],
)
async def shortcut_concept_mappings(
    concept: str | None = None,
    namespace: str | None = None,
    relation: str | None = None,
    types: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ConceptMappingListResponse:
    """List external concept mappings (auto-resolves session/model)."""
    from orionbelt.api.routers.model_api import list_concept_mappings

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await list_concept_mappings(
        session_id, model_id, concept, namespace, relation, types, mgr
    )


@router.get(
    "/concept-mappings/namespaces",
    response_model=ConceptNamespacesResponse,
    tags=["model-discovery"],
)
async def shortcut_concept_namespaces(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ConceptNamespacesResponse:
    """External namespaces the model links into (auto-resolves session/model)."""
    from orionbelt.api.routers.model_api import list_concept_namespaces

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await list_concept_namespaces(session_id, model_id, mgr)


@router.get(
    "/concept-mappings/unmapped",
    response_model=UnmappedObjectsResponse,
    tags=["model-discovery"],
)
async def shortcut_unmapped_objects(
    types: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> UnmappedObjectsResponse:
    """Artefacts without an external concept mapping (auto-resolves session/model)."""
    from orionbelt.api.routers.model_api import list_unmapped_objects

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await list_unmapped_objects(session_id, model_id, types, mgr)


@router.get(
    "/examples",
    response_model=ExampleListResponse,
    tags=["model-discovery"],
)
async def shortcut_list_examples(
    intent: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ExampleListResponse:
    """List canonical example queries (auto-resolves session/model)."""
    from orionbelt.api.routers.model_api import list_examples

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await list_examples(session_id, model_id, intent, mgr)


@router.get(
    "/examples/{example_name}",
    response_model=ExampleDetail,
    tags=["model-discovery"],
)
async def shortcut_get_example(
    example_name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ExampleDetail:
    """Get a single example by name (auto-resolves session/model)."""
    from orionbelt.api.routers.model_api import get_example

    session_id, model_id, _ = _resolve_single_model(mgr)
    return await get_example(session_id, model_id, example_name, mgr)
