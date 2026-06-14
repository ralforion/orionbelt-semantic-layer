"""Dependency injection for FastAPI — SessionManager singleton."""

from __future__ import annotations

from dataclasses import dataclass

from orionbelt.auth import init_auth, reset_auth
from orionbelt.cache.noop import NoopCache
from orionbelt.cache.protocol import Cache
from orionbelt.service.session_manager import SessionManager


@dataclass(frozen=True)
class OneshotBatchConfig:
    """Server-side config for POST /v1/oneshot/batch."""

    max_queries: int = 50
    max_parallelism: int = 8
    default_timeout_ms: int = 30000
    batch_timeout_ms: int = 120000


_session_manager: SessionManager | None = None
_disable_session_list: bool = False
_single_model_mode: bool = False
_preload_model_yaml: str | None = None
_flight_info: dict[str, object] | None = None
_query_execute_enabled: bool = False
_db_vendor: str = "duckdb"
_query_default_limit: int = 1000
_default_locale: str = ""
_oneshot_batch_config: OneshotBatchConfig = OneshotBatchConfig()
_cache: Cache = NoopCache()


@dataclass(frozen=True)
class CacheRuntimeConfig:
    """Cache-related settings made available to request handlers."""

    backend: str = "noop"
    min_ttl_seconds: int = 5
    max_ttl_seconds: int = 86400
    unknown_policy: str = "no_cache"
    unknown_default_ttl_seconds: int = 300
    heartbeat_auth_token: str | None = None


_cache_config: CacheRuntimeConfig = CacheRuntimeConfig()


def init_session_manager(
    manager: SessionManager,
    *,
    disable_session_list: bool = False,
    admin_curated: bool = False,
    preload_model_yaml: str | None = None,
    flight_info: dict[str, object] | None = None,
    query_execute_enabled: bool = False,
    db_vendor: str = "duckdb",
    query_default_limit: int = 1000,
    default_locale: str = "",
    oneshot_batch_config: OneshotBatchConfig | None = None,
    cache: Cache | None = None,
    cache_config: CacheRuntimeConfig | None = None,
    auth_mode: str = "none",
    api_keys: str = "",
    api_key_header: str = "X-API-Key",
    auth_enabled: bool = False,
) -> None:
    """Set the global SessionManager (called at app startup).

    ``preload_model_yaml`` is the original YAML of the single protected
    MODEL_FILES model — passed in only when admin-curated mode loaded
    exactly one file. The string is surfaced via ``/v1/settings.model_yaml``
    so UI clients can render the read-only model editor, and re-used by
    ``POST /v1/sessions`` to seed each new user session with the protected
    model (since session-scoped model upload is blocked with 403 in
    admin-curated mode).
    """
    global _session_manager, _disable_session_list  # noqa: PLW0603
    global _single_model_mode, _preload_model_yaml, _flight_info  # noqa: PLW0603
    global _query_execute_enabled, _db_vendor, _query_default_limit  # noqa: PLW0603
    global _default_locale, _oneshot_batch_config  # noqa: PLW0603
    global _cache, _cache_config  # noqa: PLW0603
    _session_manager = manager
    _disable_session_list = disable_session_list
    _single_model_mode = admin_curated
    _preload_model_yaml = preload_model_yaml
    _flight_info = flight_info
    _query_execute_enabled = query_execute_enabled
    _db_vendor = db_vendor
    _query_default_limit = query_default_limit
    _default_locale = default_locale
    if oneshot_batch_config is not None:
        _oneshot_batch_config = oneshot_batch_config
    if cache is not None:
        _cache = cache
    if cache_config is not None:
        _cache_config = cache_config
    # Initialise the shared auth subsystem here (not only in the ASGI
    # lifespan) so test fixtures that call init_session_manager() directly
    # get a consistent, validated auth config. Raises AuthConfigError on
    # bad config — fail loudly at startup.
    init_auth(
        auth_mode=auth_mode,
        api_keys=api_keys,
        header_name=api_key_header,
        auth_enabled=auth_enabled,
    )


def get_session_manager() -> SessionManager:
    """FastAPI ``Depends`` provider for SessionManager."""
    if _session_manager is None:
        raise RuntimeError("SessionManager not initialised — call init_session_manager() first")
    return _session_manager


def is_session_list_disabled() -> bool:
    """Return True when the GET /sessions endpoint is suppressed."""
    return _disable_session_list


def get_preload_model_yaml() -> str | None:
    """Return the YAML of the single MODEL_FILES protected model, if any.

    Populated at startup only when MODEL_FILES has exactly one entry — the
    single-model-mode UX expects exactly one model to render in the
    read-only editor. Multi-model deployments return ``None``; clients use
    ``GET /v1/models`` for discovery instead.
    """
    return _preload_model_yaml


def is_single_model_mode() -> bool:
    """Return True when admin-curated mode is active (MODEL_FILES is set).

    Name is kept for backwards compatibility with the public ``/v1/settings``
    response field ``single_model_mode`` — the actual semantics today are
    "any admin-curated preload is active", which gates POST/DELETE on
    ``/v1/sessions/{id}/models`` (returns 403) and several shortcut routes.
    """
    return _single_model_mode


def get_flight_info() -> dict[str, object] | None:
    """Return Flight SQL settings dict, or None if Flight is not enabled."""
    return _flight_info


def update_flight_state(
    *,
    flight_info: dict[str, object] | None,
    query_execute_enabled: bool,
) -> None:
    """Refresh cached Flight state after auto-detection at startup."""
    global _flight_info, _query_execute_enabled  # noqa: PLW0603
    _flight_info = flight_info
    _query_execute_enabled = query_execute_enabled


def is_query_execute_enabled() -> bool:
    """Return True when POST /query/execute is available."""
    return _query_execute_enabled


def get_db_vendor() -> str:
    """Return the configured default database vendor."""
    return _db_vendor


def get_query_default_limit() -> int:
    """Return the default row limit for query execution."""
    return _query_default_limit


def get_default_locale() -> str:
    """Return the configured default locale for value formatting."""
    return _default_locale


def get_oneshot_batch_config() -> OneshotBatchConfig:
    """Return the configured one-shot batch limits."""
    return _oneshot_batch_config


def get_cache() -> Cache:
    """FastAPI ``Depends`` provider for the result cache."""
    return _cache


def get_cache_config() -> CacheRuntimeConfig:
    """Return the cache runtime config (TTL bounds, heartbeat token, etc.)."""
    return _cache_config


def reset_session_manager() -> None:
    """Clear the global SessionManager (for tests)."""
    global _session_manager, _disable_session_list  # noqa: PLW0603
    global _single_model_mode, _preload_model_yaml, _flight_info  # noqa: PLW0603
    global _query_execute_enabled, _db_vendor, _query_default_limit  # noqa: PLW0603
    global _default_locale, _oneshot_batch_config  # noqa: PLW0603
    global _cache, _cache_config  # noqa: PLW0603
    _session_manager = None
    _disable_session_list = False
    _single_model_mode = False
    _preload_model_yaml = None
    _flight_info = None
    _query_execute_enabled = False
    _db_vendor = "duckdb"
    _query_default_limit = 1000
    _default_locale = ""
    _oneshot_batch_config = OneshotBatchConfig()
    _cache = NoopCache()
    _cache_config = CacheRuntimeConfig()
    reset_auth()
