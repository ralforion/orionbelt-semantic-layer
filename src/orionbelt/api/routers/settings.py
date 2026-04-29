"""Public settings endpoint: GET /settings."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query

from orionbelt import __version__
from orionbelt.api.deps import (
    get_db_vendor,
    get_flight_info,
    get_preload_model_yaml,
    get_session_manager,
    is_query_execute_enabled,
    is_single_model_mode,
)
from orionbelt.api.schemas import (
    DialectResolutionInfo,
    FlightSettingsInfo,
    ModelSettingsInfo,
    SettingsResponse,
    TimezoneResolutionInfo,
)
from orionbelt.models.semantic import ModelSettings, SemanticModel
from orionbelt.service.db_executor import (
    _DB_SESSION_TZ,
    _get_host_timezone,
    resolve_timezone,
)
from orionbelt.service.session_manager import SessionManager

router = APIRouter()


def _model_from_default_session(mgr: SessionManager) -> SemanticModel | None:
    """Return the SemanticModel from the default session (single-model mode), or None."""
    try:
        store = mgr.get_or_create_default()
        models = store.list_models()
        if not models:
            return None
        return store.get_model(models[0].model_id)
    except Exception:
        return None


def _resolve_target_model(
    mgr: SessionManager,
    *,
    session_id: str | None,
    model_id: str | None,
) -> SemanticModel | None:
    """Pick the model whose settings are exposed in ``/v1/settings``.

    Resolution rules:
    - ``model_id`` without ``session_id`` → 400 (model_id needs a session).
    - both → explicit lookup; 404 if either is missing.
    - ``session_id`` only → if that session holds exactly one model use it;
      otherwise return ``None`` (block is omitted, caller can request a
      specific ``model_id``).
    - neither → single-model mode uses the ``__default__`` session;
      otherwise auto-resolve a globally unique model across all sessions
      and degrade to ``None`` on ambiguity / emptiness.
    """
    if model_id and not session_id:
        raise HTTPException(
            status_code=400,
            detail="model_id requires session_id",
        )

    if session_id and model_id:
        try:
            store = mgr.get_store(session_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"Session not found: {session_id}"
            ) from None
        try:
            return store.get_model(model_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"Model not found in session: {model_id}"
            ) from None

    if session_id:
        try:
            store = mgr.get_store(session_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=f"Session not found: {session_id}"
            ) from None
        models = store.list_models()
        if len(models) == 1:
            return store.get_model(models[0].model_id)
        return None

    # No params — single-model mode short-circuit.
    if is_single_model_mode():
        return _model_from_default_session(mgr)

    # Multi-model mode, no params: auto-resolve a globally unique model.
    candidates: list[SemanticModel] = []
    try:
        default_store = mgr.get_store("__default__")
        for ms in default_store.list_models():
            candidates.append(default_store.get_model(ms.model_id))
    except KeyError:
        pass
    for sess in mgr.list_sessions():
        try:
            store = mgr.get_store(sess.session_id)
        except KeyError:
            continue
        for ms in store.list_models():
            candidates.append(store.get_model(ms.model_id))
    if len(candidates) == 1:
        return candidates[0]
    return None


def _settings_from_preload_yaml(yaml_text: str | None) -> ModelSettings | None:
    """Parse only the ``settings:`` block from the preloaded YAML.

    Used as a fallback for callers that initialised the SessionManager with
    ``preload_model_yaml`` but did not eagerly load the model into the
    default session (test fixtures, embedded clients).
    """
    if not yaml_text:
        return None
    try:
        import yaml

        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            return None
        block = data.get("settings")
        if not isinstance(block, dict):
            return None
        return ModelSettings(**block)
    except Exception:
        return None


def _resolve_effective_timezone(model_settings: ModelSettings | None, db_vendor: str) -> str:
    """Mirror the runtime timezone resolution at request time.

    Effective TZ priority (matches ``service/db_executor.py``):
    - if ``overrideDatabaseTimezone`` is true: model TZ → host → UTC
    - else: cached DB session TZ → model TZ → host → UTC

    The DB session TZ is read from the lazy cache only — never probed here.
    """
    model_tz = model_settings.default_timezone if model_settings else None
    override = bool(model_settings and model_settings.override_database_timezone)
    if not override:
        db_tz = _DB_SESSION_TZ.get(db_vendor)
        if db_tz is not None:
            return str(db_tz)
    return str(resolve_timezone(default_timezone=model_tz))


def _resolve_effective_dialect(model_settings: ModelSettings | None, db_vendor: str) -> str:
    """Mirror ``_resolve_dialect`` for the case where the request omits dialect."""
    if model_settings and model_settings.default_dialect:
        return str(model_settings.default_dialect)
    return db_vendor or "postgres"


@router.get("", response_model=SettingsResponse, response_model_exclude_none=True)
async def get_settings(
    session_id: str | None = Query(
        default=None,
        description="Scope `model_settings` / `timezone` / `dialect.model` to this session",
    ),
    model_id: str | None = Query(
        default=None,
        description="Scope to a specific model (requires `session_id`)",
    ),
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SettingsResponse:
    """Return public configuration for API clients (UI, MCP, etc.).

    In single-model mode (or when ``session_id`` / ``session_id`` + ``model_id``
    pin a unique model) the response also includes the loaded model's
    ``settings:`` block plus the runtime timezone and dialect resolution
    chains, so a client can display the effective values without compiling
    a query first.

    In multi-model mode without query parameters, the model-specific blocks
    are populated only when exactly one model is loaded across all sessions;
    otherwise they are omitted (no error) and the caller can request a
    specific session/model via the query parameters.
    """
    fi = get_flight_info()
    db_vendor = get_db_vendor()

    single_mode = is_single_model_mode()
    model = _resolve_target_model(mgr, session_id=session_id, model_id=model_id)
    settings_block = model.settings if model else None

    # Single-model fallback: parse the preloaded YAML if no model was loaded
    # into the default session yet (test fixtures, embedded usage).
    if single_mode and settings_block is None and not session_id and not model_id:
        settings_block = _settings_from_preload_yaml(get_preload_model_yaml())

    expose_model_settings = settings_block is not None or model is not None or single_mode

    model_settings_info: ModelSettingsInfo | None = None
    if expose_model_settings:
        model_settings_info = ModelSettingsInfo(
            **(settings_block.model_dump(by_alias=False) if settings_block else {})
        )

    # `timezone` is always present so clients can show the wall clock /
    # effective TZ even without a loaded model.
    host_tz = _get_host_timezone()
    db_tz = _DB_SESSION_TZ.get(db_vendor)
    effective_tz_name = _resolve_effective_timezone(settings_block, db_vendor)
    now_utc = datetime.now(UTC)
    try:
        local_now = now_utc.astimezone(ZoneInfo(effective_tz_name))
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        local_now = now_utc
    utc_iso = now_utc.isoformat().replace("+00:00", "Z")
    local_iso = local_now.isoformat()
    if local_iso.endswith("+00:00"):
        local_iso = local_iso[:-6] + "Z"
    timezone_info = TimezoneResolutionInfo(
        model=settings_block.default_timezone if settings_block else None,
        host=str(host_tz) if host_tz else None,
        database=str(db_tz) if db_tz else None,
        effective=effective_tz_name,
        override_database_timezone=bool(
            settings_block and settings_block.override_database_timezone
        ),
        now=local_iso,
        utc=utc_iso,
    )

    dialect_info = DialectResolutionInfo(
        model=settings_block.default_dialect if settings_block else None,
        env=db_vendor or None,
        effective=_resolve_effective_dialect(settings_block, db_vendor),
    )

    return SettingsResponse(
        version=__version__,
        api_version="v1",
        single_model_mode=single_mode,
        model_yaml=get_preload_model_yaml() if single_mode else None,
        session_ttl_seconds=mgr.ttl,
        session_max_age_seconds=mgr.max_age,
        max_sessions=mgr.max_sessions,
        max_models_per_session=mgr.max_models_per_session,
        query_execute=is_query_execute_enabled(),
        flight=FlightSettingsInfo(**fi) if fi else None,
        model_settings=model_settings_info,
        timezone=timezone_info,
        dialect=dialect_info,
    )
