"""Heartbeat endpoint — POST /v1/heartbeat.

See ``design/PLAN_freshness_driven_cache.md`` §9. ETL pings this endpoint
after refreshing a physical table; the cache invalidates every entry whose
dependency set includes that table, across every dataObject and every
session.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException

from orionbelt.api.deps import (
    CacheRuntimeConfig,
    get_cache,
    get_cache_config,
    get_session_manager,
)
from orionbelt.api.schemas import HeartbeatRequest, HeartbeatResponse
from orionbelt.cache.protocol import Cache
from orionbelt.service.session_manager import SessionManager

logger = logging.getLogger(__name__)
router = APIRouter()


def _check_auth(authorization: str | None, expected: str | None) -> None:
    """Reject requests without a valid bearer token."""
    if expected is None:
        # Endpoint is gated to a 404 by the route handler when no token is set.
        raise HTTPException(status_code=404, detail="Not found")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid heartbeat token")


def _parse_timestamp(value: str | None) -> datetime:
    """Accept ISO 8601, clamp future timestamps to now."""
    now = datetime.now(UTC)
    if not value:
        return now
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid timestamp: {value!r}") from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if ts > now:
        logger.warning("Heartbeat timestamp in future (%s); clamping to now", ts.isoformat())
        return now
    return ts


def _affected_data_objects(mgr: SessionManager, table_ref: str) -> list[str]:
    """Find every dataObject label across all loaded models that maps to ``table_ref``."""
    affected: set[str] = set()
    try:
        sessions = list(mgr.list_sessions())
    except Exception:
        sessions = []

    # Always include __default__ (single-model mode), since list_sessions excludes it.
    session_ids = [s.session_id for s in sessions]
    session_ids.append("__default__")

    for sid in session_ids:
        try:
            store = mgr.get_store(sid)
        except Exception:
            continue
        try:
            summaries = store.list_models()
        except Exception:
            continue
        for summary in summaries:
            try:
                model = store.get_model(summary.model_id)
            except Exception:
                continue
            for name, obj in model.data_objects.items():
                parts = [str(p) for p in (obj.database, obj.schema_name, obj.code) if p]
                if parts and ".".join(parts) == table_ref:
                    affected.add(name)
    return sorted(affected)


@router.post("/heartbeat", response_model=HeartbeatResponse, tags=["cache"])
async def post_heartbeat(
    body: HeartbeatRequest,
    authorization: str | None = Header(default=None),
    cache: Cache = Depends(get_cache),  # noqa: B008
    config: CacheRuntimeConfig = Depends(get_cache_config),  # noqa: B008
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> HeartbeatResponse:
    """Record a heartbeat and invalidate every cache entry that depends on the table.

    Authentication: ``Authorization: Bearer <HEARTBEAT_AUTH_TOKEN>`` is
    required. When the env var is unset the endpoint returns 404.
    """
    _check_auth(authorization, config.heartbeat_auth_token)
    ts = _parse_timestamp(body.timestamp)
    table_ref = f"{body.database}.{body.schema_name}.{body.table}"

    invalidated = 0
    try:
        invalidated = await cache.invalidate_table(table_ref)
    except Exception:
        logger.exception("Cache invalidation failed for %s", table_ref)

    # Persist the heartbeat on file backends so future TTL derivation
    # honours the most recent observation. Backends without persistent
    # heartbeat storage just silently skip this step.
    record = getattr(cache, "record_heartbeat", None)
    if callable(record):
        try:
            await record(table_ref, ts)
        except Exception:
            logger.exception("Recording heartbeat failed for %s", table_ref)

    affected = _affected_data_objects(mgr, table_ref)

    return HeartbeatResponse(
        table_ref=table_ref,
        recorded_at=ts.isoformat(),
        invalidated_cache_entries=invalidated,
        affected_data_objects=affected,
    )
