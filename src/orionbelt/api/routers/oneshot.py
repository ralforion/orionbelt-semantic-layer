"""One-shot batch endpoint: POST /v1/oneshot/batch.

Loads (or references) a model and runs N independent queries against it in
a single round trip. See ``design/PLAN_oneshot_batch.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from orionbelt.api.deps import (
    OneshotBatchConfig,
    get_db_vendor,
    get_oneshot_batch_config,
    get_session_manager,
    is_query_execute_enabled,
    is_single_model_mode,
)
from orionbelt.api.query_cache import (
    CachedEntry,
    execution_result_from_data,
    try_cache_get,
    try_cache_set,
)
from orionbelt.api.routers.sessions import (
    _build_execute_response,
    _build_explain_response,
    _resolve_dialect,
)
from orionbelt.api.schema_guards import validate_oneshot_body
from orionbelt.api.schemas import (
    OneshotBatchQueryError,
    OneshotBatchQueryItem,
    OneshotBatchQueryResult,
    OneshotBatchRequest,
    OneshotBatchResponse,
    QueryExecuteResponse,
    StructuredWarning,
)
from orionbelt.api.warnings_adapter import semantic_error_to_warning
from orionbelt.cache import (
    build_cache_key,
    build_datasource_key,
    compute_effective_ttl,
    is_nondeterministic_sql,
)
from orionbelt.cache.protocol import Cache
from orionbelt.cache.ttl import NoCacheReason, TtlResult
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.validator import format_sql
from orionbelt.dialect.base import UnsupportedAggregationError, UnsupportedGroupingError
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionUnavailableError,
    execute_sql,
    resolve_timezone,
)
from orionbelt.service.model_store import (
    ModelCapacityError,
    ModelStore,
    ModelValidationError,
)
from orionbelt.service.session_manager import (
    SessionCapacityError,
    SessionExpiredError,
    SessionManager,
    SessionNotFoundError,
)

logger = logging.getLogger("orionbelt.api.oneshot")

router = APIRouter()


def _resolve_session_and_store(
    body: OneshotBatchRequest, mgr: SessionManager
) -> tuple[str, ModelStore, bool]:
    """Acquire or create a session, return (session_id, store, created).

    ``created`` is True when this call created a new session (so we own its
    lifecycle on failure paths).
    """
    if body.session_id:
        try:
            store = mgr.get_store(body.session_id)
        except SessionExpiredError:
            raise HTTPException(
                status_code=410, detail=f"Session '{body.session_id}' has expired"
            ) from None
        except SessionNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"Session '{body.session_id}' not found"
            ) from None
        return body.session_id, store, False

    try:
        info = mgr.create_session()
    except SessionCapacityError:
        raise HTTPException(
            status_code=429,
            detail="Too many active sessions. Please retry later.",
            headers={"Retry-After": "60"},
        ) from None
    return info.session_id, mgr.get_store(info.session_id), True


def _resolve_model(
    *,
    body: OneshotBatchRequest,
    store: ModelStore,
) -> tuple[str, str]:
    """Resolve the model the batch should run against.

    Returns ``(model_id, model_load)`` where ``model_load`` is one of
    ``"fresh"``, ``"reused"``, or ``"referenced"`` (existing model_id supplied).
    """
    if body.model_id:
        try:
            store.get_model(body.model_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"Model '{body.model_id}' not found"
            ) from None
        return body.model_id, "referenced"

    # body.model_yaml is non-empty (validated in OneshotBatchRequest)
    assert body.model_yaml is not None
    try:
        result = store.load_model(body.model_yaml, dedup=body.dedup)
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
    return result.model_id, result.model_load


def _compile(
    *,
    store: ModelStore,
    model_id: str,
    item: OneshotBatchQueryItem,
    default_dialect: str | None,
) -> tuple[Any, str] | OneshotBatchQueryError:
    """Compile a single query. Return ``(compile_result, dialect)`` or an error."""
    try:
        model = store.get_model(model_id)
    except KeyError:
        return OneshotBatchQueryError(
            code="MODEL_NOT_FOUND",
            message=f"Model '{model_id}' not found",
        )
    dialect = _resolve_dialect(
        request_dialect=item.dialect or default_dialect,
        model=model,
        fallback=get_db_vendor(),
    )
    try:
        compile_result = store.compile_query(model_id, item.query, dialect)
    except UnsupportedDialectError:
        return OneshotBatchQueryError(
            code="UNSUPPORTED_DIALECT",
            message=f"Unsupported dialect: '{dialect}'",
        )
    except ResolutionError as exc:
        first = exc.errors[0] if exc.errors else None
        return OneshotBatchQueryError(
            code=first.code if first else "RESOLUTION_ERROR",
            message=first.message if first else "Query resolution failed",
            path=first.path if first else None,
        )
    except FanoutError as exc:
        return OneshotBatchQueryError(code="FANOUT", message=exc.message)
    except UnsupportedAggregationError as exc:
        return OneshotBatchQueryError(
            code="UNSUPPORTED_AGGREGATION",
            message=str(exc),
        )
    except UnsupportedGroupingError as exc:
        return OneshotBatchQueryError(
            code="UNSUPPORTED_GROUPING",
            message=str(exc),
        )
    return compile_result, dialect


async def _run_query(
    *,
    store: ModelStore,
    session_id: str,
    model_id: str,
    item: OneshotBatchQueryItem,
    default_dialect: str | None,
    execute: bool,
    semaphore: asyncio.Semaphore,
    per_query_timeout_s: float,
    cache: Cache,
    cache_config: Any,
) -> OneshotBatchQueryResult:
    """Compile + optionally execute a single query under a semaphore."""
    async with semaphore:
        outcome = _compile(
            store=store, model_id=model_id, item=item, default_dialect=default_dialect
        )
        if isinstance(outcome, OneshotBatchQueryError):
            return OneshotBatchQueryResult(id=item.id, status="error", error=outcome)
        compile_result, dialect = outcome

        sql_str = format_sql(compile_result.sql, compile_result.dialect)
        explain = _build_explain_response(compile_result)
        warnings = [semantic_error_to_warning(w) for w in compile_result.warnings]
        physical_tables = list(compile_result.physical_tables)

        # Per-query execute decides on the merged flag (explicit override > batch default).
        wants_execute = item.execute if item.execute is not None else execute
        if not wants_execute:
            return OneshotBatchQueryResult(
                id=item.id,
                status="ok",
                sql=sql_str,
                dialect=dialect,
                sql_valid=compile_result.sql_valid,
                explain=explain,
                executed=False,
                warnings=warnings,
                physical_tables=physical_tables,
            )

        if not is_query_execute_enabled():
            return OneshotBatchQueryResult(
                id=item.id,
                status="error",
                error=OneshotBatchQueryError(
                    code="QUERY_EXECUTE_DISABLED",
                    message=(
                        "Query execution is not available. Set QUERY_EXECUTE=true "
                        "and configure DB_VENDOR + credentials."
                    ),
                ),
            )

        model = store.get_model(model_id)
        model_default_tz: str | None = None
        override_db_tz = False
        if model.settings:
            model_default_tz = model.settings.default_timezone
            override_db_tz = model.settings.override_database_timezone
        tz = resolve_timezone(default_timezone=model_default_tz)

        # Freshness-driven cache lookup. Cacheable here is always True
        # because oneshot batch produces canonical JSON only — but non-
        # deterministic SQL (RAND, NOW, CURRENT_DATE, …) still bypasses
        # the cache. Cache key is the compiled SQL hash; caching a clock-
        # reading query would freeze a stale moment forever.
        cache_key: str | None = None
        cached_hit = None
        nondet, nondet_name = is_nondeterministic_sql(compile_result.sql)
        if nondet:
            logger.info(
                "oneshot cache skipped for %s/%s: non-deterministic SQL (%s)",
                session_id,
                model_id,
                nondet_name,
            )
            ttl_outcome = TtlResult(
                ttl=None,
                no_cache_reason=NoCacheReason.NON_DETERMINISTIC_SQL,
            )
        else:
            cache_key = build_cache_key(
                datasource=build_datasource_key(dialect),
                model_id=model_id,
                dialect=dialect,
                sql=compile_result.sql,
            )
            ttl_outcome = _resolve_oneshot_ttl(
                store=store,
                model_id=model_id,
                cache=cache,
                cache_config=cache_config,
                physical_tables=physical_tables,
            )
            # A "no cache" TTL must block reads as well as writes — only serve
            # from cache when the current freshness says the query is cacheable.
            if ttl_outcome.ttl is not None:
                _cache_t0 = time.monotonic()
                cached_hit = await _try_oneshot_cache_get(cache, cache_key)
        if cached_hit is not None:
            fetch_elapsed_ms = round((time.monotonic() - _cache_t0) * 1000, 2)
            cached_at_iso = cached_hit.cached_at_iso
            # Only row data + column schema are cached; rebuild the response from
            # the compile result so per-request timing matches a fresh run.
            hit_exec = execution_result_from_data(
                cached_hit.data_table,
                execution_time_ms=fetch_elapsed_ms,
                tz=tz,
                columns=cached_hit.columns,
            )
            hit_envelope = _build_execute_response(
                compile_result=compile_result,
                exec_result=hit_exec,
                model=model,
                response_format="json",
                format_values=False,
                locale="",
                cached=True,
                cached_at=cached_at_iso,
            )
            assert isinstance(hit_envelope, QueryExecuteResponse)
            return OneshotBatchQueryResult(
                id=item.id,
                status="ok",
                sql=hit_envelope.sql,
                dialect=hit_envelope.dialect,
                sql_valid=hit_envelope.sql_valid,
                explain=hit_envelope.explain,
                columns=hit_envelope.columns,
                rows=hit_envelope.rows,
                row_count=hit_envelope.row_count,
                execution_time_ms=hit_envelope.execution_time_ms,
                executed=True,
                warnings=warnings,
                physical_tables=physical_tables,
                cached=True,
                cached_at=hit_envelope.cached_at,
                ttl_seconds=ttl_outcome.ttl.seconds if ttl_outcome.ttl else None,
                ttl_source=ttl_outcome.ttl.source if ttl_outcome.ttl else None,
                ttl_limiting_table=(ttl_outcome.ttl.limiting_table if ttl_outcome.ttl else None),
            )

        try:
            exec_result = await asyncio.wait_for(
                asyncio.to_thread(
                    execute_sql,
                    compile_result.sql,
                    dialect=dialect,
                    tz=tz,
                    override_db_tz=override_db_tz,
                ),
                timeout=per_query_timeout_s,
            )
        except TimeoutError:
            return OneshotBatchQueryResult(
                id=item.id,
                status="error",
                error=OneshotBatchQueryError(
                    code="QUERY_TIMEOUT",
                    message=f"Query exceeded {per_query_timeout_s}s execution timeout",
                ),
            )
        except ExecutionUnavailableError as exc:
            return OneshotBatchQueryResult(
                id=item.id,
                status="error",
                error=OneshotBatchQueryError(code="EXECUTION_UNAVAILABLE", message=str(exc)),
            )
        except ExecutionError as exc:
            return OneshotBatchQueryResult(
                id=item.id,
                status="error",
                error=OneshotBatchQueryError(code="EXECUTION_ERROR", message=str(exc)),
            )

        # Reuse the standard execute response builder so column metadata,
        # type hints, and format strings stay consistent with /query/execute.
        envelope = _build_execute_response(
            compile_result=compile_result,
            exec_result=exec_result,
            model=model,
            response_format="json",
            format_values=False,
            locale="",
        )
        # _build_execute_response can return a Response when format=tsv; we
        # always pass json above so this is unreachable, but mypy/runtime
        # wants the narrowing.
        assert isinstance(envelope, QueryExecuteResponse)

        # Cache the fresh result when TTL composition succeeded.
        ttl_seconds: int | None = None
        ttl_source: str | None = None
        ttl_limiting_table: str | None = None
        if ttl_outcome.ttl is not None and cache_key is not None:
            # cache_key is only None on the non-det bypass, which also sets
            # ttl=None — so this branch is the strictly-cacheable case.
            ttl_seconds = ttl_outcome.ttl.seconds
            ttl_source = ttl_outcome.ttl.source
            ttl_limiting_table = ttl_outcome.ttl.limiting_table
            await _try_oneshot_cache_set(
                cache=cache,
                key=cache_key,
                envelope=envelope,
                ttl_seconds=ttl_outcome.ttl.seconds,
                datasource=build_datasource_key(dialect),
                model_id=model_id,
                dialect=dialect,
                physical_tables=physical_tables,
            )
        elif ttl_outcome.no_cache_reason is not None:
            ttl_source = (
                "no_cache"
                if ttl_outcome.no_cache_reason.value == "unknown_freshness"
                else f"no_cache:{ttl_outcome.no_cache_reason.value}"
            )
            ttl_limiting_table = ttl_outcome.no_cache_table

        return OneshotBatchQueryResult(
            id=item.id,
            status="ok",
            sql=envelope.sql,
            dialect=envelope.dialect,
            sql_valid=envelope.sql_valid,
            explain=envelope.explain,
            columns=envelope.columns,
            rows=envelope.rows,
            row_count=envelope.row_count,
            execution_time_ms=envelope.execution_time_ms,
            executed=True,
            warnings=warnings,
            physical_tables=physical_tables,
            cached=False,
            ttl_seconds=ttl_seconds,
            ttl_source=ttl_source,
            ttl_limiting_table=ttl_limiting_table,
        )


@router.post(
    "/batch",
    response_model=OneshotBatchResponse,
    dependencies=[Depends(validate_oneshot_body)],
)
async def oneshot_batch(
    body: OneshotBatchRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    cfg: OneshotBatchConfig = Depends(get_oneshot_batch_config),  # noqa: B008
) -> OneshotBatchResponse:
    """Load a model and run multiple queries against it in a single round trip.

    See ``design/PLAN_oneshot_batch.md`` for the full design.
    """
    batch_warnings: list[StructuredWarning] = []

    # Single-model mode disallows model uploads via this endpoint too.
    if body.model_yaml and is_single_model_mode():
        raise HTTPException(
            status_code=403,
            detail="Single-model mode: model upload is disabled",
        )

    if len(body.queries) > cfg.max_queries:
        raise HTTPException(
            status_code=422,
            detail=(f"Batch has {len(body.queries)} queries; server cap is {cfg.max_queries}"),
        )

    # Cap parallelism silently — see PLAN_oneshot_batch.md §10 question 3.
    requested = body.max_parallelism or cfg.max_parallelism
    parallelism = max(1, min(requested, cfg.max_parallelism))
    if body.max_parallelism and body.max_parallelism > cfg.max_parallelism:
        batch_warnings.append(
            StructuredWarning(
                code="MAX_PARALLELISM_CAPPED",
                severity="warning",
                message=(
                    f"max_parallelism reduced from {body.max_parallelism} to "
                    f"{parallelism} (server cap)"
                ),
                context={
                    "requested": body.max_parallelism,
                    "applied": parallelism,
                    "cap": cfg.max_parallelism,
                },
            )
        )

    session_id, store, session_created = _resolve_session_and_store(body, mgr)

    try:
        model_id, model_load = _resolve_model(body=body, store=store)
    except HTTPException:
        # Don't leave a freshly-created session sitting around if model load fails.
        if session_created:
            with contextlib.suppress(SessionNotFoundError):
                mgr.close_session(session_id)
        raise

    semaphore = asyncio.Semaphore(parallelism)
    per_query_timeout_s = cfg.default_timeout_ms / 1000.0
    batch_timeout_s = cfg.batch_timeout_ms / 1000.0

    from orionbelt.api.deps import get_cache, get_cache_config

    cache = get_cache()
    cache_config = get_cache_config()

    # Pre-allocate result slots so we can keep stable ordering by id.
    results: list[OneshotBatchQueryResult | None] = [None] * len(body.queries)

    async def _run_indexed(idx: int, item: OneshotBatchQueryItem) -> None:
        results[idx] = await _run_query(
            store=store,
            session_id=session_id,
            model_id=model_id,
            item=item,
            default_dialect=body.dialect,
            execute=body.execute,
            semaphore=semaphore,
            per_query_timeout_s=per_query_timeout_s,
            cache=cache,
            cache_config=cache_config,
        )

    if body.fail_fast:
        # Sequential supervision: walk through queries in order, abort the rest
        # on the first error. Independent execution still happens via the
        # semaphore but we await in submission order so a failure short-
        # circuits cleanly without leaking tasks.
        for idx, item in enumerate(body.queries):
            await _run_indexed(idx, item)
            r = results[idx]
            if r is not None and r.status == "error":
                for j in range(idx + 1, len(body.queries)):
                    results[j] = OneshotBatchQueryResult(id=body.queries[j].id, status="cancelled")
                break
    else:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(_run_indexed(i, q) for i, q in enumerate(body.queries))),
                timeout=batch_timeout_s,
            )
        except TimeoutError:
            batch_warnings.append(
                StructuredWarning(
                    code="BATCH_TIMEOUT",
                    severity="warning",
                    message=f"Batch exceeded {batch_timeout_s}s — partial results returned",
                    context={"batch_timeout_s": batch_timeout_s},
                )
            )
            for i, q in enumerate(body.queries):
                if results[i] is None:
                    results[i] = OneshotBatchQueryResult(
                        id=q.id,
                        status="error",
                        error=OneshotBatchQueryError(
                            code="BATCH_TIMEOUT",
                            message="Cancelled by whole-batch timeout",
                        ),
                    )

    # Model lifecycle: if we loaded the model for this batch and persist is
    # off, evict it. If model_id was supplied by the caller, never touch it.
    model_was_loaded_here = model_load in ("fresh", "reused") and body.model_yaml is not None
    persisted = True
    if model_was_loaded_here and not body.persist_model:
        try:
            store.remove_model(model_id)
            persisted = False
        except KeyError:
            persisted = False
    elif not model_was_loaded_here:
        # Caller-owned model — we don't decide its lifecycle.
        persisted = True

    final_results = [r for r in results if r is not None]
    return OneshotBatchResponse(
        session_id=session_id,
        model_id=model_id,
        model_persisted=persisted,
        model_load=model_load,
        results=final_results,
        batch_warnings=batch_warnings,
    )


# -- cache helpers ----------------------------------------------------------


def _resolve_oneshot_ttl(
    *,
    store: ModelStore,
    model_id: str,
    cache: Cache,
    cache_config: Any,
    physical_tables: list[str],
) -> Any:
    """Compose the effective TTL for one batch query."""
    contracts: dict[str, Any] = {}
    try:
        contracts = store.refresh_contracts(model_id)
    except Exception:
        logger.debug("oneshot refresh_contracts failed", exc_info=True)
    heartbeats: dict[str, Any] = {}
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


async def _try_oneshot_cache_get(cache: Cache, key: str) -> CachedEntry | None:
    """Best-effort cache get for oneshot. Returns a :class:`CachedEntry` or None.

    Delegates to the shared :func:`try_cache_get`, which offloads the gzip +
    Arrow IPC decode to a worker thread so a oneshot cache hit never blocks the
    event loop (mirrors the REST path). Only row data + column schema are cached;
    the caller rebuilds the response envelope from the compile result.
    """
    return await try_cache_get(cache, key)


async def _try_oneshot_cache_set(
    *,
    cache: Cache,
    key: str,
    envelope: Any,
    ttl_seconds: int,
    datasource: str,
    model_id: str,
    dialect: str,
    physical_tables: list[str],
) -> None:
    """Best-effort cache set for oneshot batch entries (row data only)."""
    await try_cache_set(
        cache=cache,
        key=key,
        columns=list(envelope.columns),
        rows=list(envelope.rows),
        sql=envelope.sql,
        dialect=envelope.dialect,
        physical_tables=physical_tables,
        row_count=envelope.row_count,
        ttl_seconds=ttl_seconds,
        datasource=datasource,
        model_id=model_id,
    )
