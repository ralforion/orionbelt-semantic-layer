"""Surface-agnostic cached query execution.

The freshness-driven result cache used to live inline in the REST query
handlers, so the pgwire and Flight surfaces (which have their own execution
paths) bypassed it entirely. This module owns the compile -> cache key ->
freshness TTL -> get -> on-miss execute -> set pipeline once, so every surface
reuses the same cache. See ``design/PLAN_freshness_driven_cache.md`` and
issue #117.

The REST layer adapts :class:`CachedExecution` into ``QueryExecuteResponse``;
the pgwire layer adapts it into an ``ExecutionResult`` for wire encoding.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from orionbelt.api.schemas import (
    ColumnMetadata,
    ExplainCflLegResponse,
    ExplainJoinResponse,
    ExplainPlanResponse,
    ResolvedInfoResponse,
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
from orionbelt.cache.result_codec import decode as cache_decode
from orionbelt.cache.ttl import NoCacheReason, TtlResult
from orionbelt.compiler.validator import format_sql
from orionbelt.service.db_executor import (
    ColumnMeta,
    ExecutionResult,
    ExecutionUnavailableError,
    coarse_hint_from_type_name,
    execute_sql,
)

logger = logging.getLogger(__name__)


# --- model introspection (column types / formats) -------------------------

RESULT_TYPE_TO_HINT: dict[str, str] = {
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


def build_type_map(model: Any) -> dict[str, str]:
    """Build a column-name -> type map from model definitions.

    Uses ``dataType`` when available (e.g. ``decimal(18, 2)``), then falls back
    to ``settings.defaultNumericDataType`` for numeric measures/metrics,
    otherwise maps ``resultType`` to a simple hint.
    """
    default_num = None
    if model.settings and model.settings.default_numeric_data_type:
        default_num = model.settings.default_numeric_data_type

    types: dict[str, str] = {}
    for label, dim in model.dimensions.items():
        types[label] = RESULT_TYPE_TO_HINT.get(str(dim.result_type), "string")
    for label, measure in model.measures.items():
        if measure.data_type:
            types[label] = measure.data_type
        elif default_num:
            types[label] = default_num
        else:
            types[label] = RESULT_TYPE_TO_HINT.get(str(measure.result_type), "number")
    for label, metric in model.metrics.items():
        if metric.data_type:
            types[label] = metric.data_type
        elif default_num:
            types[label] = default_num
        else:
            types[label] = "number"
    return types


def build_format_map(model: Any) -> dict[str, str | None]:
    """Build a column-name -> format-string map from model dims/measures/metrics."""
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


def build_result_columns(
    model: Any,
    exec_result: ExecutionResult,
    *,
    type_map: dict[str, str] | None = None,
    fmt_map: dict[str, str | None] | None = None,
) -> list[ColumnMetadata]:
    """Decorate executor columns with model-declared types and formats.

    This is the canonical column shape persisted in the cache and surfaced by
    REST, so both cache writers (REST and pgwire) agree on the stored payload.
    Callers that already built the type/format maps (the REST response builder)
    can pass them in to avoid rebuilding.
    """
    model_type_map = type_map if type_map is not None else build_type_map(model)
    fmt_map = fmt_map if fmt_map is not None else build_format_map(model)
    for c in exec_result.columns:
        if fmt_map.get(c.name) is None and getattr(c, "default_format", None):
            fmt_map[c.name] = c.default_format
    return [
        ColumnMetadata(
            name=c.name,
            type=model_type_map.get(c.name, c.type_hint),
            format=fmt_map.get(c.name),
        )
        for c in exec_result.columns
    ]


def build_explain_response(result: Any) -> ExplainPlanResponse | None:
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


# --- TTL + cache get/set ----------------------------------------------------


def resolve_effective_ttl(
    *,
    store: Any,
    model_id: str,
    cache: Cache,
    cache_config: Any,
    physical_tables: list[str],
) -> TtlResult:
    """Compose the effective TTL for a query, merging contracts + heartbeats."""
    contracts: dict[str, Any] = {}
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


async def try_cache_get(cache: Cache, key: str) -> Any:
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
        # Decode (gzip + Arrow IPC + row rebuild) runs off the event loop.
        envelope = await asyncio.to_thread(cache_decode, result.payload)
    except Exception:
        logger.debug("cache decode failed", exc_info=True)
        return None
    envelope.cached_at_iso = result.cached_at.isoformat().replace("+00:00", "Z")
    return envelope


async def try_cache_get_raw(cache: Cache, key: str) -> tuple[bytes, str] | None:
    """Fetch a cached blob WITHOUT decoding it, for raw-arrow byte-passthrough.

    Returns ``(raw_gzip_blob, cached_at_iso)`` or ``None``. Skips the whole
    gzip + Arrow IPC + row-rebuild that :func:`try_cache_get` does, so a raw
    ``format=arrow`` hit is truly zero-copy: the stored blob (stamped
    ``cached=true`` at write time) is returned to the client verbatim.
    """
    try:
        result = await cache.get(key)
    except Exception:
        logger.debug("cache.get error", exc_info=True)
        return None
    if result is None:
        return None
    return result.payload, result.cached_at.isoformat().replace("+00:00", "Z")


def encode_cached_payload(**encode_kwargs: Any) -> bytes:
    """Encode a result blob for cache STORAGE (stamped as a cache entry).

    A stored blob is only ever served on a cache HIT, so it is stamped
    ``cached=true`` + a store timestamp in its Arrow schema metadata. That lets
    the raw-arrow hit path return it byte-for-byte (zero-copy passthrough) while
    still carrying the in-band ``cached`` flag the UI reads for its "cache"
    source label. Every cache writer (REST and oneshot) MUST encode through here
    so the invariant holds regardless of which surface populated the entry.
    """
    from orionbelt.cache import result_codec

    encode_kwargs["cached"] = True
    encode_kwargs["cached_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return result_codec.encode(**encode_kwargs)


async def try_cache_set(
    *,
    cache: Cache,
    key: str,
    columns: list[ColumnMetadata],
    rows: list[list[Any]],
    sql: str,
    dialect: str,
    explain: ExplainPlanResponse | None,
    warnings: list[StructuredWarning],
    sql_valid: bool,
    execution_time_ms: float,
    timezone: str | None,
    resolved: ResolvedInfoResponse,
    physical_tables: list[str],
    row_count: int,
    ttl_seconds: int,
    datasource: str,
    model_id: str,
) -> None:
    """Encode and store a result payload. Failures are logged and ignored."""
    from orionbelt.cache import key as cache_key_mod

    try:
        payload = encode_cached_payload(
            columns=[c.model_dump() for c in columns],
            rows=rows,
            sql=sql,
            dialect=dialect,
            explain=explain.model_dump() if explain else None,
            warnings=[w.model_dump() for w in warnings],
            sql_valid=sql_valid,
            execution_time_ms=execution_time_ms,
            timezone=timezone,
            resolved=resolved.model_dump(),
            physical_tables=list(physical_tables),
        )
    except Exception:
        logger.debug("cache encode failed", exc_info=True)
        return
    try:
        await cache.set(
            key,
            payload,
            ttl_seconds=ttl_seconds,
            physical_tables=list(physical_tables),
            datasource=datasource,
            model_id=model_id,
            query_hash=cache_key_mod.query_hash(sql=sql),
            dialect=dialect,
            row_count=row_count,
        )
    except Exception:
        logger.debug("cache.set error", exc_info=True)


# --- shared cached execution -----------------------------------------------


@dataclass
class CachedExecution:
    """Neutral result of a cache-aware execution, adapted per surface."""

    cached: bool
    cache_key: str | None
    ttl_outcome: TtlResult | None
    # Fresh (miss) path:
    exec_result: ExecutionResult | None
    columns: list[ColumnMetadata] | None
    # Cached (hit) path:
    envelope: Any | None
    fetch_elapsed_ms: float | None
    # Raw stored blob (gzip'd Arrow IPC) for byte-passthrough on raw-arrow hits.
    payload_blob: bytes | None = None


async def execute_query_with_cache(
    *,
    store: Any,
    model: Any,
    compile_result: Any,
    model_id: str,
    dialect: str,
    cache: Cache,
    cache_config: Any,
    datasource: str | None = None,
    tz: Any = None,
    override_db_tz: bool = False,
    cacheable: bool = True,
    decode_payload: bool = True,
) -> CachedExecution:
    """Run a compiled query through the result cache, executing on a miss.

    Caching is skipped when ``cacheable`` is False or the SQL is
    non-deterministic. The actual DB execution runs off the event loop via
    ``asyncio.to_thread``. Shared by the REST, pgwire, and Flight surfaces so
    they hit one cache.

    ``decode_payload=False`` lets a caller that only needs the raw stored blob
    (the REST raw-arrow byte-passthrough) skip the gzip + Arrow decode on a hit:
    the returned :class:`CachedExecution` carries ``payload_blob`` and a ``None``
    ``envelope``. Every other surface leaves it True and gets the decoded rows.

    The cache is scoped to the ``datasource`` (defaults to the dialect, since
    connections are global per dialect today). Any session resolving to the
    same data source, model, dialect and compiled SQL shares entries.
    """
    ds = datasource or build_datasource_key(dialect)
    cache_key: str | None = None
    ttl_outcome: TtlResult | None = None

    if cacheable:
        nondet, name = is_nondeterministic_sql(compile_result.sql)
        if nondet:
            logger.info("cache skipped for %s/%s: non-deterministic SQL (%s)", ds, model_id, name)
            ttl_outcome = TtlResult(ttl=None, no_cache_reason=NoCacheReason.NON_DETERMINISTIC_SQL)
        else:
            cache_key = build_cache_key(
                datasource=ds,
                model_id=model_id,
                dialect=dialect,
                sql=compile_result.sql,
            )
            ttl_outcome = resolve_effective_ttl(
                store=store,
                model_id=model_id,
                cache=cache,
                cache_config=cache_config,
                physical_tables=compile_result.physical_tables,
            )
            # A "no cache" TTL (unknown freshness, missing heartbeat, below-min)
            # must block reads as well as writes: serving an entry written
            # during an earlier cacheable window would defeat the freshness
            # policy. Writes are already gated on ttl below, so only read when
            # the current TTL says the query is cacheable.
            if ttl_outcome.ttl is not None:
                t0 = time.monotonic()
                if decode_payload:
                    envelope = await try_cache_get(cache, cache_key)
                    if envelope is not None:
                        return CachedExecution(
                            cached=True,
                            cache_key=cache_key,
                            ttl_outcome=ttl_outcome,
                            exec_result=None,
                            columns=None,
                            envelope=envelope,
                            fetch_elapsed_ms=round((time.monotonic() - t0) * 1000, 2),
                        )
                else:
                    # Raw-arrow passthrough: fetch the blob without decoding it.
                    raw = await try_cache_get_raw(cache, cache_key)
                    if raw is not None:
                        payload_blob, _cached_at_iso = raw
                        return CachedExecution(
                            cached=True,
                            cache_key=cache_key,
                            ttl_outcome=ttl_outcome,
                            exec_result=None,
                            columns=None,
                            envelope=None,
                            fetch_elapsed_ms=round((time.monotonic() - t0) * 1000, 2),
                            payload_blob=payload_blob,
                        )

    exec_result = await asyncio.to_thread(
        execute_sql,
        compile_result.sql,
        dialect=dialect,
        tz=tz,
        override_db_tz=override_db_tz,
    )
    columns = build_result_columns(model, exec_result)

    if (
        cacheable
        and cache_key is not None
        and ttl_outcome is not None
        and ttl_outcome.ttl is not None
    ):
        await try_cache_set(
            cache=cache,
            key=cache_key,
            columns=columns,
            rows=exec_result.rows,
            sql=format_sql(compile_result.sql, dialect),
            dialect=dialect,
            explain=build_explain_response(compile_result),
            warnings=[semantic_error_to_warning(w) for w in compile_result.warnings],
            sql_valid=compile_result.sql_valid,
            execution_time_ms=exec_result.execution_time_ms,
            timezone=exec_result.timezone,
            resolved=ResolvedInfoResponse(
                fact_tables=compile_result.resolved.fact_tables,
                dimensions=compile_result.resolved.dimensions,
                measures=compile_result.resolved.measures,
            ),
            physical_tables=compile_result.physical_tables,
            row_count=exec_result.row_count,
            ttl_seconds=ttl_outcome.ttl.seconds,
            datasource=ds,
            model_id=model_id,
        )

    return CachedExecution(
        cached=False,
        cache_key=cache_key,
        ttl_outcome=ttl_outcome,
        exec_result=exec_result,
        columns=columns,
        envelope=None,
        fetch_elapsed_ms=None,
    )


# --- pgwire adapter ---------------------------------------------------------


def _obml_type_to_hint(type_str: str) -> str:
    """Map a stored OBML/SQL column type to a pgwire coarse hint.

    Delegates to the single shared classifier so the cache-hit pgwire path and
    the live-execution path agree on number/datetime/binary/string.
    """
    return coarse_hint_from_type_name(type_str)


def execution_result_from_envelope(envelope: Any) -> ExecutionResult:
    """Rebuild an ExecutionResult from a cached envelope for wire encoding."""
    columns = [
        ColumnMeta(
            name=c.get("name", ""),
            type_hint=_obml_type_to_hint(c.get("type", "string")),
            default_format=c.get("format"),
        )
        for c in envelope.columns
    ]
    return ExecutionResult(
        columns=columns,
        raw_rows=list(envelope.rows),
        row_count=envelope.row_count,
        execution_time_ms=envelope.execution_time_ms,
    )


__all__ = [
    "CachedExecution",
    "ExecutionUnavailableError",
    "build_explain_response",
    "build_format_map",
    "build_result_columns",
    "build_type_map",
    "encode_cached_payload",
    "execute_query_with_cache",
    "execution_result_from_envelope",
    "RESULT_TYPE_TO_HINT",
    "resolve_effective_ttl",
    "try_cache_get",
    "try_cache_get_raw",
    "try_cache_set",
]
