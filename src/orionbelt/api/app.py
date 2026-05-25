"""FastAPI application factory for OrionBelt Semantic Layer."""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from orionbelt import __version__
from orionbelt.api.deps import (
    CacheRuntimeConfig,
    OneshotBatchConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.api.logging_config import configure_logging
from orionbelt.api.middleware import (
    RequestBodyLimitMiddleware,
    RequestIdMiddleware,
    RequestTimingMiddleware,
    SecurityHeadersMiddleware,
    SessionRateLimitMiddleware,
)
from orionbelt.api.routers import (
    cache_stats,
    convert,
    dialects,
    graph,
    heartbeat,
    model_api,
    oneshot,
    reference,
    sessions,
    shortcuts,
)
from orionbelt.api.routers import (
    models as models_router,
)
from orionbelt.api.routers import settings as settings_router
from orionbelt.api.schemas import HealthResponse
from orionbelt.cache.factory import build_cache
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

logger = logging.getLogger("orionbelt.api")


def _wipe_file_cache_state(backend: str, cache_dir: str) -> None:
    """Delete persisted FileCache artifacts (``meta.duckdb`` + ``results/``).

    No-op when ``backend != "file"`` or the directory doesn't exist. Touches
    only the known cache files; sibling files (other tools sharing the same
    parent dir) are left alone.
    """
    if (backend or "noop").strip().lower() != "file":
        return
    if not os.path.isdir(cache_dir):
        return
    removed_files = 0
    try:
        for entry in os.listdir(cache_dir):
            if entry == "meta.duckdb" or entry.startswith("meta.duckdb."):
                with __import__("contextlib").suppress(Exception):
                    os.remove(os.path.join(cache_dir, entry))
                    removed_files += 1
        results_dir = os.path.join(cache_dir, "results")
        if os.path.isdir(results_dir):
            shutil.rmtree(results_dir, ignore_errors=True)
            removed_files += 1
    except Exception:
        logger.exception("Failed wiping cache state at %s", cache_dir)
        return
    if removed_files:
        logger.info("Cache wiped on startup: %s", cache_dir)


def _read_model_file(path_str: str, model_dir: str | None = None) -> tuple[str, Path]:
    """Read and validate one OBML YAML at startup. Raises on error.

    If the YAML contains ``extends`` or ``inherits`` keys, the referenced
    files are resolved relative to the model file's directory and merged
    before validation. Returns ``(yaml_string, resolved_path)`` so callers
    can use the resolved path for filename-based addressing.
    """
    import yaml as pyyaml

    path = Path(path_str)
    if not path.is_absolute() and model_dir:
        path = Path(model_dir) / path
    if not path.is_file():
        raise FileNotFoundError(f"model file not found: {path}")
    yaml_str = path.read_text(encoding="utf-8")
    if not yaml_str.strip():
        raise ValueError(f"model file is empty: {path}")

    raw = pyyaml.safe_load(yaml_str) or {}
    if raw.get("extends") or raw.get("inherits"):
        from orionbelt.parser.merger import ExtendsMerger

        merger = ExtendsMerger()
        merged, _warnings = merger.merge_from_files(raw, path.parent)
        yaml_str = pyyaml.dump(merged, default_flow_style=False, allow_unicode=True)

    # Validate the model can be parsed (fail fast at startup)
    from orionbelt.service.model_store import ModelStore

    store = ModelStore()
    summary = store.validate(yaml_str)
    if not summary.valid:
        msgs = "; ".join(e.message for e in summary.errors)
        raise ValueError(f"model file validation failed ({path}): {msgs}")
    return yaml_str, path


def _resolve_model_name(yaml_str: str, path: Path) -> str:
    """Derive a model's addressing name from its YAML.

    Preference order: top-level OBML ``name:`` field → filename stem.
    Both paths go through ``normalize_model_name`` so the result is
    guaranteed to be a valid identifier or a precise ``ModelNameError``
    is raised.
    """
    import yaml as pyyaml

    from orionbelt.models.identifiers import normalize_model_name

    raw = pyyaml.safe_load(yaml_str) or {}
    obml_name = raw.get("name")
    if obml_name:
        return normalize_model_name(obml_name, source=f"OBML `name:` in {path}")
    return normalize_model_name(path.stem, source=f"filename '{path.name}'")


def _parse_model_files_env(model_files: str) -> list[str]:
    """Split the ``MODEL_FILES`` env var into individual paths.

    Comma-separated. Empty entries skipped. Whitespace trimmed.
    """
    return [p.strip() for p in model_files.split(",") if p.strip()]


def _warn_if_legacy_model_file_set() -> None:
    """Log a deprecation warning if the removed ``MODEL_FILE`` env var is still
    set. ``pydantic-settings`` silently ignores unknown env vars, so a
    deployment that didn't migrate to ``MODEL_FILES`` would otherwise boot
    with no preloaded model and the admin lock disabled."""
    if os.environ.get("MODEL_FILE"):
        logger.warning(
            "MODEL_FILE is set but was removed in v2.7.0 and is now ignored. "
            "Replace with MODEL_FILES=<same-path> (single-entry MODEL_FILES is "
            "the direct equivalent). See CHANGELOG for the v2.7.0 entry."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start/stop the SessionManager alongside the application.

    Two startup-time model-loading modes:

    * ``MODEL_FILES=a.yaml,b.yaml`` — admin-curated. Each YAML is loaded
      into its own internal session, addressable by the resolved name
      (OBML ``name:`` → filename stem). BI tools select via the Flight
      ``database`` catalog or pgwire ``database=`` URL parameter. A
      single path is fine — it just means one named protected session.
      REST ``POST /sessions/{id}/models`` returns 403 in this mode.
    * Neither set — dynamic mode. Sessions and models are created at
      runtime via REST. The Flight surface has no preloaded model and
      will return ``NO_MODEL_AVAILABLE`` on connect.
    """
    settings: Settings = app.state.settings

    _warn_if_legacy_model_file_set()

    # Read + validate every YAML before constructing the SessionManager —
    # fail fast at startup rather than emitting half-broken state.
    preloads: list[tuple[str, Path]] = []  # (yaml_str, path)
    if settings.model_files:
        for path_str in _parse_model_files_env(settings.model_files):
            yaml_str, resolved = _read_model_file(path_str, settings.model_dir)
            preloads.append((yaml_str, resolved))

    # Resolve every model's addressing name and check uniqueness BEFORE
    # we start the SessionManager — surface collisions as one clean error
    # rather than partial state.
    named_preloads: list[tuple[str, str]] = []  # (model_name, yaml_str)
    seen: dict[str, Path] = {}
    for yaml_str, path in preloads:
        name = _resolve_model_name(yaml_str, path)
        if name in seen:
            raise RuntimeError(
                f"Model name '{name}' is used by both "
                f"{seen[name]} and {path}. Each MODEL_FILES entry "
                "must resolve to a unique addressing name. Override "
                "via OBML `name:` field if needed."
            )
        seen[name] = path
        named_preloads.append((name, yaml_str))

    is_admin_curated = bool(preloads)  # disables POST /models

    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        max_age_seconds=settings.session_max_age_seconds,
        max_sessions=settings.max_sessions,
        max_models_per_session=settings.max_models_per_session,
        cleanup_interval=settings.session_cleanup_interval,
        is_single_model_mode=is_admin_curated,
    )
    mgr.start()

    # Build Flight info dict if enabled (exposed via GET /v1/settings)
    flight_info: dict[str, object] | None = None
    if settings.flight_enabled:
        flight_info = {
            "enabled": True,
            "port": settings.flight_port,
            "auth_mode": settings.flight_auth_mode,
            "db_vendor": settings.db_vendor,
        }

    # query/execute is available when explicitly enabled OR when Flight is enabled
    query_execute_enabled = settings.query_execute or settings.flight_enabled

    # Each MODEL_FILES entry goes into its own protected named session.
    for name, yaml_str in named_preloads:
        store = mgr.get_or_create_named(name)
        store.load_model(yaml_str)
        logger.info("Loaded model '%s' into protected session", name)
    if named_preloads:
        logger.info(
            "Multi-model mode active: %d model(s) loaded — %s",
            len(named_preloads),
            ", ".join(n for n, _ in named_preloads),
        )

    # Wipe persisted cache state on startup. ``model_id`` is regenerated as
    # a fresh UUID on every model load, so any entries from a previous
    # process run are orphans by construction (their cache keys reference
    # model_ids that no longer exist). Starting empty avoids accumulating
    # dead state between restarts. See PLAN_freshness_driven_cache.md §7.
    _wipe_file_cache_state(settings.cache_backend, settings.cache_dir)

    cache = build_cache(settings)
    cache_config = CacheRuntimeConfig(
        backend=cache.backend_name,
        min_ttl_seconds=settings.cache_min_ttl_seconds,
        max_ttl_seconds=settings.cache_max_ttl_seconds,
        unknown_policy=settings.cache_unknown_freshness_policy,
        unknown_default_ttl_seconds=settings.cache_unknown_freshness_default_ttl,
        heartbeat_auth_token=settings.heartbeat_auth_token,
    )
    if cache.backend_name == "file":
        # Pay every lazy-import tax at startup so the first user-visible
        # cache hit doesn't include ~100ms of cold imports + first-call
        # codec setup. Top-level ``pyarrow`` alone is ~60MB of native
        # code; ``pyarrow.parquet`` is the columnar reader/writer that
        # ``parquet_codec`` actually uses. DuckDB lazy-imports ``pytz``
        # on the first TIMESTAMPTZ bind. We exercise all three with a
        # full encode → decode round-trip on a one-row payload.
        try:
            import pyarrow  # noqa: F401
            import pyarrow.parquet  # noqa: F401

            from orionbelt.cache import parquet_codec

            warm_payload = parquet_codec.encode(
                columns=[{"name": "warm", "data_type": "string"}],
                rows=[["warm"]],
                sql="SELECT 1",
                dialect=cache.backend_name,
                explain=None,
                warnings=[],
                sql_valid=True,
                execution_time_ms=0.0,
                timezone=None,
                resolved={},
                physical_tables=[],
            )
            parquet_codec.decode(warm_payload)
            await cache.stats()  # exercises DuckDB meta read + pytz bind
        except Exception:
            logger.exception("Cache warm-up failed (non-fatal)")

        try:
            cache.start_sweep_task()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Failed to start cache sweep task")
        logger.info("Cache enabled: backend=%s", cache.backend_name)
        logger.info("Cache dir=%s", settings.cache_dir)
        logger.info("Cache min_ttl=%ds", settings.cache_min_ttl_seconds)
        logger.info("Cache max_ttl=%ds", settings.cache_max_ttl_seconds)
        logger.info("Cache max_value=%dB", settings.cache_max_value_bytes)
        logger.info("Cache max_disk=%dB", settings.cache_max_disk_bytes)
        logger.info("Cache sweep=%ds", settings.cache_sweep_interval_seconds)
        logger.info("Cache unknown_policy=%s", settings.cache_unknown_freshness_policy)
        logger.info(
            "Cache unknown_default_ttl=%ds",
            settings.cache_unknown_freshness_default_ttl,
        )
        logger.info(
            "Cache heartbeat_auth=%s",
            "configured" if settings.heartbeat_auth_token else "disabled (404)",
        )
    elif cache.backend_name != "noop":
        # Unknown backend selected via env — still useful to log
        logger.info("Cache enabled: backend=%s", cache.backend_name)

    init_session_manager(
        mgr,
        disable_session_list=settings.disable_session_list,
        admin_curated=is_admin_curated,
        flight_info=flight_info,
        query_execute_enabled=query_execute_enabled,
        db_vendor=settings.db_vendor,
        query_default_limit=settings.query_default_limit,
        default_locale=settings.default_locale,
        oneshot_batch_config=OneshotBatchConfig(
            max_queries=settings.oneshot_batch_max_queries,
            max_parallelism=settings.oneshot_batch_max_parallelism,
            default_timeout_ms=settings.oneshot_batch_default_timeout_ms,
            batch_timeout_ms=settings.oneshot_batch_batch_timeout_ms,
        ),
        cache=cache,
        cache_config=cache_config,
    )

    # Start Arrow Flight SQL server if ob-flight-extension is installed
    # (auto-detected) or FLIGHT_ENABLED=true is set explicitly.
    flight_thread = None
    flight_available = importlib.util.find_spec("ob_flight") is not None
    if settings.flight_enabled or flight_available:
        try:
            from ob_flight.startup import start_flight_background

            flight_thread = start_flight_background(
                session_manager=mgr,
                port=settings.flight_port,
                default_dialect=settings.db_vendor,
                cache=cache,
                cache_config=cache_config,
            )
            settings.flight_enabled = True
            logger.info(
                "Flight SQL server started on port %d (vendor=%s)",
                settings.flight_port,
                settings.db_vendor,
            )
            # Refresh cached deps so /v1/settings and query gating
            # reflect the auto-detected Flight state.
            from orionbelt.api.deps import update_flight_state

            update_flight_state(
                flight_info={
                    "enabled": True,
                    "port": settings.flight_port,
                    "auth_mode": settings.flight_auth_mode,
                    "db_vendor": settings.db_vendor,
                },
                query_execute_enabled=True,
            )
        except ImportError:
            logger.warning(
                "FLIGHT_ENABLED=true but ob-flight-extension is not installed. "
                "Install with: uv sync --extra flight"
            )

    # Start Postgres wire surface if PGWIRE_ENABLED=true. The startup
    # helper builds a SemanticRouter bound to the live SessionManager,
    # so SELECT statements over the wire run through the same
    # translate→compile→execute pipeline as REST /query/semantic-ql.
    # See design/PLAN_postgres_wire.md.
    pgwire_runtime = None
    if settings.pgwire_enabled:
        from orionbelt.pgwire.startup import start_pgwire

        pgwire_runtime = await start_pgwire(settings, session_manager=mgr)

    try:
        yield
    finally:
        if pgwire_runtime is not None:
            await pgwire_runtime.shutdown()
        if flight_thread is not None:
            from ob_flight.startup import stop_flight_server

            stop_flight_server()
        # Drain connection pools before stopping sessions
        try:
            from ob_flight.db_router import close_all_pools

            close_all_pools()
        except ImportError:
            pass
        try:
            await cache.shutdown()
        except Exception:
            logger.exception("Cache shutdown failed")
        mgr.stop()
        reset_session_manager()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = Settings()

    app = FastAPI(
        title="OrionBelt Semantic Layer",
        description=(
            "Compiles and executes YAML semantic models as analytical SQL across multiple dialects."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.expose_api_docs else None,
        redoc_url="/redoc" if settings.expose_api_docs else None,
        openapi_url="/openapi.json" if settings.expose_openapi_schema else None,
    )
    app.state.settings = settings

    # Global exception handler — prevents stack trace leaks
    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # Middleware (order matters: last added = first to execute)
    app.add_middleware(RequestTimingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        SessionRateLimitMiddleware,
        max_requests=settings.session_rate_limit,
        window_seconds=60,
        trusted_proxy_count=settings.trusted_proxy_count,
    )
    app.add_middleware(RequestIdMiddleware)

    # Versioned API routes under /v1
    v1 = APIRouter(prefix="/v1")
    v1.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
    v1.include_router(model_api.router, prefix="/sessions", tags=["model-discovery"])
    v1.include_router(graph.router, prefix="/sessions", tags=["graph"])
    v1.include_router(shortcuts.router, tags=["model-discovery"])
    v1.include_router(convert.router, prefix="/convert", tags=["convert"])
    v1.include_router(dialects.router, prefix="/dialects", tags=["dialects"])
    v1.include_router(oneshot.router, prefix="/oneshot", tags=["oneshot"])
    v1.include_router(reference.router, prefix="/reference", tags=["reference"])
    v1.include_router(models_router.router, prefix="/models", tags=["models"])
    v1.include_router(settings_router.router, prefix="/settings", tags=["settings"])
    v1.include_router(cache_stats.router, prefix="/cache", tags=["cache"])
    v1.include_router(heartbeat.router, tags=["cache"])
    app.include_router(v1)

    # Root-level endpoints (no version prefix — used by load balancers, crawlers)
    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    @app.get("/robots.txt", include_in_schema=False)
    async def robots_txt() -> Response:
        return Response("User-agent: *\nAllow: /\n", media_type="text/plain")

    # Mount Gradio UI at /ui when the 'ui' extra is installed
    try:
        import gradio as gr

        from orionbelt.ui.app import create_blocks

        api_url = f"http://localhost:{settings.effective_port}"
        # Resolve from Settings directly: the UI is mounted in create_app(), which
        # runs before the lifespan hook initialises deps.py globals, so calling
        # is_query_execute_enabled()/is_admin_curated_mode()/get_flight_info() here
        # would always read defaults. Mirror the same logic used in lifespan().
        query_execute_enabled = settings.query_execute or settings.flight_enabled
        # Admin-curated mode: MODEL_FILES is set, so POST /models is locked down.
        admin_curated = bool(settings.model_files)
        ui_settings: dict[str, object] = {
            "single_model_mode": admin_curated,
            "query_execute": query_execute_enabled,
            "session_ttl_seconds": settings.session_ttl_seconds,
        }
        if settings.flight_enabled:
            ui_settings["flight"] = {
                "enabled": True,
                "port": settings.flight_port,
                "auth_mode": settings.flight_auth_mode,
                "db_vendor": settings.db_vendor,
            }
        demo = create_blocks(default_api_url=api_url, embedded_settings=ui_settings)
        from pathlib import Path

        from starlette.responses import FileResponse

        _favicon_path = Path(__file__).resolve().parents[1] / "ui" / "favicon.png"

        @app.get("/favicon.ico", include_in_schema=False)
        async def _favicon() -> FileResponse:
            return FileResponse(_favicon_path, media_type="image/png")

        app = gr.mount_gradio_app(app, demo, path="/ui")
        logger.info("Gradio UI mounted at %s/ui", api_url)
    except Exception:
        pass  # gradio not installed or mount failed — skip UI mount

    return app


class _StaticAssetLogFilter(logging.Filter):
    """Suppress access log noise from Gradio static assets and heartbeats."""

    _SKIP = ("/ui/assets/", "/ui/static/", "/ui/theme.css", "/ui/gradio_api/heartbeat", "/favicon")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(path in msg for path in self._SKIP)


class _ShutdownLogFilter(logging.Filter):
    """Suppress noisy uvicorn errors during graceful shutdown.

    When Gradio keeps WebSocket connections open, uvicorn's graceful shutdown
    timeout force-cancels them, producing ERROR-level messages that are
    harmless but alarming.  This filter silences those specific messages.
    """

    _SUPPRESSED = (
        "Cancel",
        "ASGI callable returned without completing response",
        "Exception in ASGI application",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(msg.startswith(prefix) for prefix in self._SUPPRESSED)


def main() -> None:
    """Run the REST API server using settings from environment / .env file."""
    # Load .env into os.environ so all env vars (DB credentials, POSTGRES_SCHEMA,
    # etc.) are visible to os.getenv() — not just to pydantic Settings.
    from dotenv import load_dotenv

    load_dotenv(override=False)

    settings = Settings()

    configure_logging(log_level=settings.log_level, log_format=settings.log_format)
    logger.info(
        "OrionBelt API Server v%s starting (host=%s, port=%d)",
        __version__,
        settings.api_server_host,
        settings.effective_port,
    )

    # Filter noisy uvicorn logs: static asset access lines and shutdown errors
    logging.getLogger("uvicorn.access").addFilter(_StaticAssetLogFilter())
    logging.getLogger("uvicorn.error").addFilter(_ShutdownLogFilter())

    # "cloudrun" log format uses JSON but disables uvicorn access logs
    # since Cloud Run generates its own request logs (with trace ID, LB latency, etc.).
    access_log = settings.log_format != "cloudrun"

    uvicorn.run(
        "orionbelt.api.app:create_app",
        factory=True,
        host=settings.api_server_host,
        port=settings.effective_port,
        log_level=settings.log_level.lower(),
        log_config=None,
        access_log=access_log,
        timeout_graceful_shutdown=3,
    )
