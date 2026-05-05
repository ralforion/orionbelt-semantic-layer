"""FastAPI application factory for OrionBelt Semantic Layer."""

from __future__ import annotations

import importlib.util
import logging
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
from orionbelt.api.routers import settings as settings_router
from orionbelt.api.schemas import HealthResponse
from orionbelt.cache.factory import build_cache
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings

logger = logging.getLogger("orionbelt.api")


def _read_model_file(path_str: str, model_dir: str | None = None) -> str:
    """Read and validate the MODEL_FILE at startup. Raises on error.

    If the YAML contains ``extends`` or ``inherits`` keys, the referenced files
    are resolved relative to the model file's directory and merged before
    validation. The returned string is the fully merged YAML.
    """
    import yaml as pyyaml

    path = Path(path_str)
    if not path.is_absolute() and model_dir:
        path = Path(model_dir) / path
    if not path.is_file():
        raise FileNotFoundError(f"MODEL_FILE not found: {path}")
    yaml_str = path.read_text(encoding="utf-8")
    if not yaml_str.strip():
        raise ValueError(f"MODEL_FILE is empty: {path}")

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
        raise ValueError(f"MODEL_FILE validation failed: {msgs}")
    return yaml_str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start/stop the SessionManager alongside the application."""
    settings: Settings = app.state.settings

    # Read and validate MODEL_FILE before starting (fail fast)
    preload_yaml: str | None = None
    if settings.model_file:
        preload_yaml = _read_model_file(settings.model_file, settings.model_dir)
        logger.info("Single-model mode: loaded %s", settings.model_file)

    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        max_age_seconds=settings.session_max_age_seconds,
        max_sessions=settings.max_sessions,
        max_models_per_session=settings.max_models_per_session,
        cleanup_interval=settings.session_cleanup_interval,
        is_single_model_mode=preload_yaml is not None,
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

    # Single-model mode: create __default__ session with the preloaded model
    if preload_yaml is not None:
        default_store = mgr.get_or_create_default()
        default_store.load_model(preload_yaml)
        logger.info("Preloaded model into __default__ session")

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
        try:
            cache.start_sweep_task()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Failed to start cache sweep task")
        logger.info(
            "Cache enabled: backend=%s dir=%s min_ttl=%ds max_ttl=%ds "
            "max_value=%dB max_disk=%dB sweep=%ds unknown_policy=%s "
            "unknown_default_ttl=%ds heartbeat_auth=%s",
            cache.backend_name,
            settings.cache_dir,
            settings.cache_min_ttl_seconds,
            settings.cache_max_ttl_seconds,
            settings.cache_max_value_bytes,
            settings.cache_max_disk_bytes,
            settings.cache_sweep_interval_seconds,
            settings.cache_unknown_freshness_policy,
            settings.cache_unknown_freshness_default_ttl,
            "configured" if settings.heartbeat_auth_token else "disabled (404)",
        )
    elif cache.backend_name != "noop":
        # Unknown backend selected via env — still useful to log
        logger.info("Cache enabled: backend=%s", cache.backend_name)

    init_session_manager(
        mgr,
        disable_session_list=settings.disable_session_list,
        preload_model_yaml=preload_yaml,
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
            from ob_flight.startup import start_flight_background  # type: ignore[import-untyped]

            flight_thread = start_flight_background(
                session_manager=mgr,
                port=settings.flight_port,
                default_dialect=settings.db_vendor,
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

    try:
        yield
    finally:
        if flight_thread is not None:
            from ob_flight.startup import stop_flight_server

            stop_flight_server()
        # Drain connection pools before stopping sessions
        try:
            from ob_flight.db_router import close_all_pools  # type: ignore[import-untyped]

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

        from orionbelt.api.deps import (
            get_flight_info,
            is_query_execute_enabled,
            is_single_model_mode,
        )
        from orionbelt.ui.app import create_blocks

        api_url = f"http://localhost:{settings.effective_port}"
        fi = get_flight_info()
        ui_settings: dict[str, object] = {
            "single_model_mode": is_single_model_mode(),
            "query_execute": is_query_execute_enabled(),
            "session_ttl_seconds": settings.session_ttl_seconds,
        }
        if fi:
            ui_settings["flight"] = fi
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
