"""Session-scoped endpoints for model management, validation, and query.

The heavy helper and core-logic functions live in ``orionbelt.api.services``;
this module keeps the thin FastAPI handlers (dependency resolution + domain →
HTTP exception translation) and re-exports the moved helpers under their
historical names so existing cross-module importers (``oneshot``,
``shortcuts``, ``model_api``) keep resolving unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Literal, cast

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from orionbelt.api.deps import (
    CacheRuntimeConfig,
    get_cache,
    get_cache_config,
    get_preload_model_yaml,
    get_query_default_limit,
    get_session_manager,
    is_query_execute_enabled,
    is_session_list_disabled,
    is_single_model_mode,
)
from orionbelt.api.osi_support import get_converter_module, parse_yaml, run_validation
from orionbelt.api.query_cache import (
    build_explain_response,
    build_format_map,
    build_type_map,
)
from orionbelt.api.schema_guards import validate_model_body, validate_query_body
from orionbelt.api.schemas import (
    ConvertResponse,
    DatabaseExplain,
    DiagramResponse,
    ModelLoadRequest,
    ModelLoadResponse,
    ModelSummaryResponse,
    OSIModelLoadRequest,
    OSIModelLoadResponse,
    QueryCompileResponse,
    QueryExecuteResponse,
    QueryPlanRequest,
    QueryPlanResponse,
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
from orionbelt.api.services.model_loading import _load_obml, _model_load_fields
from orionbelt.api.services.query_compilation import (
    _join_path_steps,
    _obsql_translation_errors,
    _resolve_dialect,
    build_compile_response,
    build_semantic_ql_compile_response,
    compile_query_for_plan,
    compile_query_or_raise,
)
from orionbelt.api.services.query_execution import (
    _apply_no_cache_metadata,
    _apply_ttl_metadata,
    _build_execute_response,
    _physical_tables_for,
    _run_with_cache,
    negotiate_execute_format,
)
from orionbelt.api.services.session_lifecycle import _get_store, _session_response
from orionbelt.api.warnings_adapter import (
    error_info_to_detail,
    semantic_error_to_warning,
)
from orionbelt.cache.protocol import Cache
from orionbelt.compiler.sql_translator import SQLTranslationError, translate_sql_to_query
from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionUnavailableError,
    explain_sql,
)
from orionbelt.service.diagram import generate_mermaid_er
from orionbelt.service.session_manager import (
    SessionCapacityError,
    SessionExpiredError,
    SessionManager,
    SessionNotFoundError,
)

logger = logging.getLogger("orionbelt.api.sessions")

# Re-exports: these moved into orionbelt.api.services but external modules
# (oneshot, shortcuts, model_api, value_formatting) still import them FROM this
# module by their historical names. Keep them in this namespace.
_build_type_map = build_type_map
_build_format_map = build_format_map
_build_explain_response = build_explain_response

__all__ = [
    "router",
    # re-exported helpers (imported by other modules from this namespace)
    "_resolve_dialect",
    "_run_with_cache",
    "_build_execute_response",
    "_build_explain_response",
    "_build_type_map",
    "_build_format_map",
    "_load_obml",
    "_model_load_fields",
    "_session_response",
    "_get_store",
    "_obsql_translation_errors",
    "_join_path_steps",
    "_physical_tables_for",
    "_apply_ttl_metadata",
    "_apply_no_cache_metadata",
]

# Prefix lives on the constructor (not the include_router call) so the root
# routes can keep an empty path ("") and still resolve to /v1/sessions with no
# trailing slash. FastAPI 0.137+ rejects an empty path supplied via an
# include_router(prefix=...) call. See design/PLAN_authentication.md note.
router = APIRouter(prefix="/sessions")


# -- session CRUD ------------------------------------------------------------


@router.post(
    "",
    response_model=SessionResponse,
    status_code=201,
    dependencies=[Depends(validate_model_body)],
)
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
    """Close a session and release its resources.

    The result cache is intentionally NOT purged here: as of the per-datasource
    cache scope (see ``orionbelt.cache.key``) entries are shared across sessions
    that resolve to the same data source, so they outlive any single session.
    Cache lifetime is governed by TTL / freshness, capacity eviction and table
    invalidation -- not session lifetime.
    """
    try:
        mgr.close_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None


# -- model management -------------------------------------------------------


@router.post(
    "/{session_id}/models",
    response_model=ModelLoadResponse,
    status_code=201,
    dependencies=[Depends(validate_model_body)],
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


@router.post(
    "/{session_id}/query/sql",
    response_model=QueryCompileResponse,
    dependencies=[Depends(validate_query_body)],
)
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
    result = compile_query_or_raise(
        store=store, model_id=body.model_id, query=body.query, dialect=dialect
    )
    logger.info("Compiled SQL:\n%s", result.sql)
    return build_compile_response(result)


@router.post(
    "/{session_id}/query/plan",
    response_model=QueryPlanResponse,
    dependencies=[Depends(validate_query_body)],
)
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

    result, error_response = compile_query_for_plan(
        store=store, model_id=body.model_id, query=body.query, dialect=dialect
    )
    if error_response is not None:
        return error_response

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


@router.post(
    "/{session_id}/query/execute",
    response_model=QueryExecuteResponse,
    dependencies=[Depends(validate_query_body)],
)
async def execute_query(
    session_id: str,
    body: SessionQueryExecuteRequest,
    request: Request,
    format: Literal["json", "tsv", "arrow"] = "json",  # noqa: A002 — public query parameter
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
    * ``format`` — ``json`` (default), ``tsv``, or ``arrow``. ``tsv`` returns a
      tab-separated body; cells with tab/newline/CR/double-quote are RFC 4180
      quoted. ``tsv`` implies ``format_values=true``. ``arrow`` returns a
      length-prefixed result frame (``application/vnd.orionbelt.result+arrow``):
      ``[u32 big-endian json_len][JSON envelope][gzip'd Arrow IPC data]``. The
      JSON envelope (sql, columns, timing, ``cached`` flag, …) is assembled
      fresh per request; the Arrow sub-part holds the typed, locale-neutral row
      data (its own gzip, so a cache hit ships the stored data verbatim). The
      Arrow frame is also selectable via the ``Accept`` header.
    * ``format_values`` — when true, numeric cells are rendered as
      locale-aware display strings using each column's ``format`` pattern
      (matches the Gradio UI). Applies to both ``json`` and ``arrow`` (the
      display strings are encoded into the Arrow data); raw ``arrow`` (the
      default, as used by the UI round trip) ships typed values and formats
      client-side. Default false.
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
    result = compile_query_or_raise(
        store=store, model_id=body.model_id, query=query, dialect=dialect
    )

    logger.info("Compiled SQL:\n%s", result.sql)

    effective_format = negotiate_execute_format(format, request.headers.get("accept"))
    return await _run_with_cache(
        store=store,
        model=model,
        compile_result=result,
        session_id=session_id,
        model_id=body.model_id,
        dialect=dialect,
        cache=cache,
        cache_config=cache_config,
        response_format=effective_format,
        format_values=format_values,
        locale=locale,
        timezone_override=timezone,
        accept_encoding=request.headers.get("accept-encoding"),
    )


# -- OrionBelt Semantic QL (OBSQL) ------------------------------------------


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
    result = compile_query_or_raise(
        store=store, model_id=body.model_id, query=query, dialect=dialect
    )

    logger.info("Compiled SQL:\n%s", result.sql)

    return build_semantic_ql_compile_response(result, query)


@router.post(
    "/{session_id}/query/semantic-ql",
    response_model=QueryExecuteResponse,
    tags=["query"],
)
async def execute_semantic_ql(
    session_id: str,
    body: SemanticQLRequest,
    request: Request,
    format: Literal["json", "tsv", "arrow"] = "json",  # noqa: A002 — public query parameter
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
    ``?format=arrow`` (or the Arrow ``Accept`` header), ``?format_values=true``,
    ``?locale=``, ``?timezone=``.

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
    result = compile_query_or_raise(
        store=store, model_id=body.model_id, query=query, dialect=dialect
    )

    logger.info("Compiled SQL:\n%s", result.sql)

    effective_format = negotiate_execute_format(format, request.headers.get("accept"))
    return await _run_with_cache(
        store=store,
        model=model,
        compile_result=result,
        session_id=session_id,
        model_id=body.model_id,
        dialect=dialect,
        cache=cache,
        cache_config=cache_config,
        response_format=effective_format,
        format_values=format_values,
        locale=locale,
        timezone_override=timezone,
        accept_encoding=request.headers.get("accept-encoding"),
    )
