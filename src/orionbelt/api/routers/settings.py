"""Public settings endpoint: GET /settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from orionbelt.api.deps import (
    get_flight_info,
    get_preload_model_yaml,
    get_session_manager,
    is_query_execute_enabled,
    is_single_model_mode,
)
from orionbelt.api.schemas import FlightSettingsInfo, SettingsResponse
from orionbelt.service.session_manager import SessionManager

router = APIRouter()


@router.get("", response_model=SettingsResponse, response_model_exclude_none=True)
async def get_settings(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SettingsResponse:
    """Return public configuration for API clients (UI, MCP, etc.)."""
    fi = get_flight_info()
    return SettingsResponse(
        single_model_mode=is_single_model_mode(),
        model_yaml=get_preload_model_yaml() if is_single_model_mode() else None,
        session_ttl_seconds=mgr.ttl,
        session_max_age_seconds=mgr.max_age,
        max_sessions=mgr.max_sessions,
        max_models_per_session=mgr.max_models_per_session,
        query_execute=is_query_execute_enabled(),
        flight=FlightSettingsInfo(**fi) if fi else None,
    )
