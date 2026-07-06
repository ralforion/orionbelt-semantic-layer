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
from datetime import datetime
from typing import Any

from orionbelt.api.schemas import (
    ColumnMetadata,
    ExplainCflLegResponse,
    ExplainJoinResponse,
    ExplainPlanResponse,
)
from orionbelt.cache import (
    build_cache_key,
    build_datasource_key,
    compute_effective_ttl,
    is_nondeterministic_sql,
)
from orionbelt.cache.protocol import Cache
from orionbelt.cache.result_codec import decode_data, encode_data, table_to_rows
from orionbelt.cache.ttl import NoCacheReason, TtlResult
from orionbelt.compiler.validator import format_sql
from orionbelt.service.db_executor import (
    ColumnMeta,
    ExecutionResult,
    ExecutionUnavailableError,
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


async def try_cache_get(cache: Cache, key: str) -> tuple[Any, str] | None:
    """Best-effort cache lookup; failures degrade to a miss.

    Returns ``(data_table, cached_at_iso)`` — the decoded pyarrow data table and
    the store timestamp — or ``None``. The blob holds only row data; the
    response envelope (sql, timing, ``cached`` flag, …) is rebuilt fresh by the
    caller, so a hit's metadata is always correct.
    """
    try:
        result = await cache.get(key)
    except Exception:
        logger.debug("cache.get error", exc_info=True)
        return None
    if result is None:
        return None
    try:
        # Decode (gzip + Arrow IPC) runs off the event loop.
        table = await asyncio.to_thread(decode_data, result.payload)
    except Exception:
        logger.debug("cache decode failed", exc_info=True)
        return None
    return table, result.cached_at.isoformat().replace("+00:00", "Z")


async def try_cache_get_raw(cache: Cache, key: str) -> tuple[bytes, str] | None:
    """Fetch a cached data blob WITHOUT decoding it, for arrow byte-passthrough.

    Returns ``(raw_gzip_blob, cached_at_iso)`` or ``None``. Skips the gzip +
    Arrow IPC decode that :func:`try_cache_get` does, so a ``format=arrow`` hit
    ships the stored *data* blob verbatim (data stays zero-copy); the fresh JSON
    envelope carrying timing + the ``cached`` flag is prepended by the caller.
    """
    try:
        result = await cache.get(key)
    except Exception:
        logger.debug("cache.get error", exc_info=True)
        return None
    if result is None:
        return None
    return result.payload, result.cached_at.isoformat().replace("+00:00", "Z")


async def try_cache_set(
    *,
    cache: Cache,
    key: str,
    columns: list[ColumnMetadata],
    rows: list[list[Any]],
    sql: str,
    dialect: str,
    physical_tables: list[str],
    row_count: int,
    ttl_seconds: int,
    datasource: str,
    model_id: str,
) -> None:
    """Encode and store the row data. Failures are logged and ignored.

    Only the row data is cached; the response envelope (sql, explain, timing,
    ``cached`` flag, …) is rebuilt fresh on every read, so it is never stored.
    ``sql`` is used for the query-hash bookkeeping, not persisted in the blob.
    """
    from orionbelt.cache import key as cache_key_mod

    try:
        payload = encode_data([c.name for c in columns], rows)
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
    """Neutral result of a cache-aware execution, adapted per surface.

    Only row data is cached; each surface rebuilds the response envelope fresh
    from ``compile_result`` + ``model``, so a hit's timing and ``cached`` flag
    are always correct. A hit carries the decoded ``data_table`` (JSON/TSV and
    pgwire) and/or the raw ``payload_blob`` (arrow data zero-copy), plus the
    cache-read ``fetch_elapsed_ms`` and the entry's ``cached_at_iso``.
    """

    cached: bool
    cache_key: str | None
    ttl_outcome: TtlResult | None
    # Fresh (miss) path:
    exec_result: ExecutionResult | None
    columns: list[ColumnMetadata] | None
    # Cached (hit) path:
    fetch_elapsed_ms: float | None
    data_table: Any | None = None
    cached_at_iso: str | None = None
    # Raw stored data blob (gzip'd Arrow IPC) for byte-passthrough on arrow hits.
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
                    hit = await try_cache_get(cache, cache_key)
                    if hit is not None:
                        data_table, cached_at_iso = hit
                        return CachedExecution(
                            cached=True,
                            cache_key=cache_key,
                            ttl_outcome=ttl_outcome,
                            exec_result=None,
                            columns=None,
                            fetch_elapsed_ms=round((time.monotonic() - t0) * 1000, 2),
                            data_table=data_table,
                            cached_at_iso=cached_at_iso,
                        )
                else:
                    # Arrow data passthrough: fetch the blob without decoding it.
                    raw = await try_cache_get_raw(cache, cache_key)
                    if raw is not None:
                        payload_blob, cached_at_iso = raw
                        return CachedExecution(
                            cached=True,
                            cache_key=cache_key,
                            ttl_outcome=ttl_outcome,
                            exec_result=None,
                            columns=None,
                            fetch_elapsed_ms=round((time.monotonic() - t0) * 1000, 2),
                            payload_blob=payload_blob,
                            cached_at_iso=cached_at_iso,
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
        fetch_elapsed_ms=None,
    )


# --- data-table adapters (cache-hit reconstruction) -------------------------


def exec_columns_from_table(data_table: Any) -> list[ColumnMeta]:
    """Reconstruct executor ``ColumnMeta`` from a cached data table's schema.

    Mirrors the executor's own column build from an Arrow result
    (``db_executor`` uses the same ``_arrow_type_to_hint`` /
    ``_default_format_for_arrow_type`` on live results), so a cache hit derives
    identical type hints + default formats to a fresh execution — no column
    metadata needs to be cached.
    """
    from orionbelt.service.db_executor import (
        _arrow_type_to_hint,
        _default_format_for_arrow_type,
    )

    return [
        ColumnMeta(
            name=field.name,
            type_hint=_arrow_type_to_hint(field.type),
            default_format=_default_format_for_arrow_type(field.type),
        )
        for field in data_table.schema
    ]


def execution_result_from_data(
    data_table: Any, *, execution_time_ms: float, tz: Any = None
) -> ExecutionResult:
    """Rebuild an ExecutionResult from a cached data table for wire encoding.

    The blob holds only row data; columns are recovered from the Arrow schema
    via :func:`exec_columns_from_table`. ``execution_time_ms`` is the per-request
    value (cache fetch time on a hit), never a stored one. ``tz`` is the resolved
    fallback timezone used to label the response (the stored rows are already
    materialized, so it is a label only).
    """
    return ExecutionResult(
        columns=exec_columns_from_table(data_table),
        raw_rows=table_to_rows(data_table),
        row_count=data_table.num_rows,
        execution_time_ms=execution_time_ms,
        tz=tz,
    )


__all__ = [
    "CachedExecution",
    "ExecutionUnavailableError",
    "build_explain_response",
    "build_format_map",
    "build_result_columns",
    "build_type_map",
    "exec_columns_from_table",
    "execute_query_with_cache",
    "execution_result_from_data",
    "RESULT_TYPE_TO_HINT",
    "resolve_effective_ttl",
    "try_cache_get",
    "try_cache_get_raw",
    "try_cache_set",
]
