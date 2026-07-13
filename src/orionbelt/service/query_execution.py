"""Surface-agnostic cache planning for query execution.

Every query surface (REST, pgwire, Flight) must resolve the *same* result-cache
key and freshness TTL for a given compiled query, or the cache fractures: the
same query would be keyed or TTL'd differently per surface. Historically that
derivation was duplicated: REST/pgwire in ``api/query_cache.py`` and Flight in
``ob_flight/server_execution.py``, so a change to key construction or freshness
policy in one place silently did not apply to the other (issue #126, follow-up
to #117).

This module owns that derivation once. It depends only on ``orionbelt.cache``
(not ``orionbelt.api``), so the Flight driver package -- which deliberately
avoids depending back on the main app package -- can import it without a
layering violation. Each surface still runs its own execution engine (REST/
pgwire via ``service.db_executor``, Flight via its Arrow ``db_router``); only
the shared cache *plan* (key + TTL) lives here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orionbelt.cache import (
    build_cache_key,
    build_datasource_key,
    compute_effective_ttl,
    is_nondeterministic_sql,
)
from orionbelt.cache.protocol import Cache
from orionbelt.cache.ttl import NoCacheReason, TtlResult

logger = logging.getLogger(__name__)


def resolve_effective_ttl(
    *,
    store: Any,
    model_id: str,
    cache: Cache,
    cache_config: Any,
    physical_tables: list[str],
) -> TtlResult:
    """Compose the effective TTL for a query, merging contracts + heartbeats.

    ``store`` supplies per-model refresh contracts; ``cache`` supplies the
    heartbeat snapshot (best-effort -- both degrade to empty on error). Both are
    duck-typed so any surface (REST session store, Flight session manager store)
    can pass its own equivalents.
    """
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


@dataclass
class CachePlan:
    """The cache key + freshness TTL derived for one compiled query.

    ``cacheable`` is True only when the SQL is deterministic *and* the resolved
    TTL is a positive freshness window. When False, ``cache_key`` is None and
    the caller must execute against the warehouse without caching;
    ``ttl_outcome.no_cache_reason`` documents why (non-deterministic SQL,
    unknown freshness, below-min TTL, ...).

    ``datasource`` is the cache scope actually used (the dialect by default),
    echoed back so callers persist entries under the same scope the key was
    built with.
    """

    cacheable: bool
    cache_key: str | None
    ttl_outcome: TtlResult
    datasource: str


def resolve_cache_plan(
    *,
    store: Any,
    model_id: str,
    dialect: str,
    sql: str,
    physical_tables: list[str],
    cache: Cache,
    cache_config: Any,
    datasource: str | None = None,
) -> CachePlan:
    """Derive the shared cache key + TTL for a compiled query.

    Single source of truth for cache-key construction and freshness TTL across
    REST, pgwire, and Flight, so the same compiled query keys and TTLs
    identically on every surface. Non-deterministic SQL (RAND/NOW/CURRENT_DATE/
    TABLESAMPLE/...) is never cached: the SQL hash *is* the cache key, so caching
    would freeze one stale clock/random slice forever.

    The cache is scoped to ``datasource`` (defaults to the dialect, since
    connections are global per dialect today), NOT the session, so any session
    resolving to the same data source, model, dialect and compiled SQL shares
    entries.
    """
    ds = datasource or build_datasource_key(dialect)

    nondet, name = is_nondeterministic_sql(sql)
    if nondet:
        logger.info("cache skipped for %s/%s: non-deterministic SQL (%s)", ds, model_id, name)
        return CachePlan(
            cacheable=False,
            cache_key=None,
            ttl_outcome=TtlResult(ttl=None, no_cache_reason=NoCacheReason.NON_DETERMINISTIC_SQL),
            datasource=ds,
        )

    cache_key = build_cache_key(
        datasource=ds,
        model_id=model_id,
        dialect=dialect,
        sql=sql,
    )
    ttl_outcome = resolve_effective_ttl(
        store=store,
        model_id=model_id,
        cache=cache,
        cache_config=cache_config,
        physical_tables=physical_tables,
    )
    return CachePlan(
        cacheable=ttl_outcome.ttl is not None,
        cache_key=cache_key,
        ttl_outcome=ttl_outcome,
        datasource=ds,
    )


__all__ = [
    "CachePlan",
    "resolve_cache_plan",
    "resolve_effective_ttl",
]
