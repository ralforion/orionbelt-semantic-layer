"""Dependency injection for FastAPI.

Runtime state is consolidated into a single :class:`AppRuntime` object
rather than a scatter of module globals. ``create_app`` builds one per app
and attaches it to ``app.state.runtime``; a middleware binds that runtime
into a :class:`~contextvars.ContextVar` for the duration of each request.

The provider functions (``get_session_manager`` etc.) keep their existing
signatures so call sites are untouched, and read fields off the *active*
runtime: the request-bound one when serving a request, otherwise the
process-level ``_runtime`` (used at startup / MODEL_FILES preload / direct
tests). This gives real per-request isolation — two live app instances in
one process each serve their own config — without threading ``request``
through dozens of call sites. ``reset_session_manager`` remains a test
compatibility helper that resets the process runtime.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from fastapi import Request

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


@dataclass(frozen=True)
class CacheRuntimeConfig:
    """Cache-related settings made available to request handlers."""

    backend: str = "noop"
    min_ttl_seconds: int = 5
    max_ttl_seconds: int = 86400
    unknown_policy: str = "no_cache"
    unknown_default_ttl_seconds: int = 300
    heartbeat_auth_token: str | None = None


@dataclass
class AppRuntime:
    """Explicit runtime state for one API app instance.

    Replaces the former collection of ``deps`` module globals. Mutable so
    late-binding updates (e.g. Flight auto-detection at startup) can refresh
    individual fields via :func:`update_flight_state`.
    """

    session_manager: SessionManager | None = None
    disable_session_list: bool = False
    admin_curated: bool = False
    preload_model_yaml: str | None = None
    flight_info: dict[str, object] | None = None
    query_execute_enabled: bool = False
    db_vendor: str = "duckdb"
    query_default_limit: int = 1000
    default_locale: str = ""
    oneshot_batch_config: OneshotBatchConfig = field(default_factory=OneshotBatchConfig)
    cache: Cache = field(default_factory=NoopCache)
    cache_config: CacheRuntimeConfig = field(default_factory=CacheRuntimeConfig)


# Module-level compatibility runtime. Used outside a request (startup,
# MODEL_FILES preload, direct tests) and as the fallback when no per-request
# runtime is bound. Starts at defaults so providers called before/without
# initialisation return the same values the old globals did.
_runtime: AppRuntime = AppRuntime()

# Per-request runtime, bound by ``bind_request_runtime`` from the app's
# ``state.runtime`` at the start of each request (see the middleware in
# ``app.py``). This is what makes providers request-scoped: with two live app
# instances in one process, each request reads its own app's runtime instead
# of a shared module global. ContextVars are isolated per async task.
_request_runtime: ContextVar[AppRuntime | None] = ContextVar(
    "orionbelt_request_runtime", default=None
)


def _active_runtime() -> AppRuntime:
    """Runtime the providers should read: the request's, else the process one."""
    return _request_runtime.get() or _runtime


def bind_request_runtime(runtime: AppRuntime) -> Token[AppRuntime | None]:
    """Bind ``runtime`` for the current request scope; returns a reset token."""
    return _request_runtime.set(runtime)


def reset_request_runtime(token: Token[AppRuntime | None]) -> None:
    """Undo a :func:`bind_request_runtime` binding."""
    _request_runtime.reset(token)


def current_runtime() -> AppRuntime:
    """Return the process-level runtime (compat accessor; ignores request scope)."""
    return _runtime


def get_runtime(request: Request) -> AppRuntime:
    """Return the runtime owned by the request's app (``app.state.runtime``)."""
    runtime = getattr(request.app.state, "runtime", None)
    return runtime if isinstance(runtime, AppRuntime) else _runtime


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
) -> AppRuntime:
    """Build the process runtime and initialise the auth subsystem.

    Returns the constructed :class:`AppRuntime` so ``create_app`` can attach
    it to ``app.state.runtime``.

    ``preload_model_yaml`` is the original YAML of the single protected
    MODEL_FILES model — passed in only when admin-curated mode loaded exactly
    one file. It is surfaced via ``/v1/settings.model_yaml`` so UI clients can
    render the read-only model editor, and re-used by ``POST /v1/sessions`` to
    seed each new user session with the protected model.
    """
    global _runtime  # noqa: PLW0603 — single process-level runtime handle
    _runtime = AppRuntime(
        session_manager=manager,
        disable_session_list=disable_session_list,
        admin_curated=admin_curated,
        preload_model_yaml=preload_model_yaml,
        flight_info=flight_info,
        query_execute_enabled=query_execute_enabled,
        db_vendor=db_vendor,
        query_default_limit=query_default_limit,
        default_locale=default_locale,
        oneshot_batch_config=oneshot_batch_config or OneshotBatchConfig(),
        cache=cache if cache is not None else NoopCache(),
        cache_config=cache_config or CacheRuntimeConfig(),
    )
    # Initialise the shared auth subsystem here (not only in the ASGI
    # lifespan) so test fixtures that call init_session_manager() directly get
    # a consistent, validated auth config. Raises AuthConfigError on bad
    # config — fail loudly at startup.
    init_auth(
        auth_mode=auth_mode,
        api_keys=api_keys,
        header_name=api_key_header,
        auth_enabled=auth_enabled,
    )
    return _runtime


def get_session_manager() -> SessionManager:
    """FastAPI ``Depends`` provider for SessionManager."""
    manager = _active_runtime().session_manager
    if manager is None:
        raise RuntimeError("SessionManager not initialised — call init_session_manager() first")
    return manager


def is_session_list_disabled() -> bool:
    """Return True when the GET /sessions endpoint is suppressed."""
    return _active_runtime().disable_session_list


def get_preload_model_yaml() -> str | None:
    """Return the YAML of the single MODEL_FILES protected model, if any.

    Populated at startup only when MODEL_FILES has exactly one entry — the
    single-model-mode UX expects exactly one model to render in the read-only
    editor. Multi-model deployments return ``None``; clients use
    ``GET /v1/models`` for discovery instead.
    """
    return _active_runtime().preload_model_yaml


def is_single_model_mode() -> bool:
    """Return True when admin-curated mode is active (MODEL_FILES is set).

    Name is kept for backwards compatibility with the public ``/v1/settings``
    response field ``single_model_mode`` — the actual semantics today are
    "any admin-curated preload is active", which gates POST/DELETE on
    ``/v1/sessions/{id}/models`` (returns 403) and several shortcut routes.
    """
    return _active_runtime().admin_curated


def get_flight_info() -> dict[str, object] | None:
    """Return Flight SQL settings dict, or None if Flight is not enabled."""
    return _active_runtime().flight_info


def update_flight_state(
    *,
    flight_info: dict[str, object] | None,
    query_execute_enabled: bool,
) -> None:
    """Refresh cached Flight state after auto-detection at startup."""
    _runtime.flight_info = flight_info
    _runtime.query_execute_enabled = query_execute_enabled


def is_query_execute_enabled() -> bool:
    """Return True when POST /query/execute is available."""
    return _active_runtime().query_execute_enabled


def get_db_vendor() -> str:
    """Return the configured default database vendor."""
    return _active_runtime().db_vendor


def get_query_default_limit() -> int:
    """Return the default row limit for query execution."""
    return _active_runtime().query_default_limit


def get_default_locale() -> str:
    """Return the configured default locale for value formatting."""
    return _active_runtime().default_locale


def get_oneshot_batch_config() -> OneshotBatchConfig:
    """Return the configured one-shot batch limits."""
    return _active_runtime().oneshot_batch_config


def get_cache() -> Cache:
    """FastAPI ``Depends`` provider for the result cache."""
    return _active_runtime().cache


def get_cache_config() -> CacheRuntimeConfig:
    """Return the cache runtime config (TTL bounds, heartbeat token, etc.)."""
    return _active_runtime().cache_config


def reset_session_manager() -> None:
    """Reset the process runtime to defaults (compatibility helper for tests)."""
    global _runtime  # noqa: PLW0603 — single process-level runtime handle
    _runtime = AppRuntime()
    reset_auth()
