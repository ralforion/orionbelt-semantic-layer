"""Session lifecycle helpers extracted from the session router."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import HTTPException

from orionbelt.api.schemas import SessionResponse
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import (
    SessionExpiredError,
    SessionInfo,
    SessionManager,
    SessionNotFoundError,
)


def _get_store(session_id: str, mgr: SessionManager) -> ModelStore:
    """Resolve session_id to ModelStore, raise 410/404 as appropriate."""
    try:
        return mgr.get_store(session_id)
    except SessionExpiredError:
        raise HTTPException(status_code=410, detail=f"Session '{session_id}' has expired") from None
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None


def _session_response(info: SessionInfo) -> SessionResponse:
    """Convert a SessionInfo dataclass to a Pydantic response."""
    d = asdict(info)
    return SessionResponse(**d)
