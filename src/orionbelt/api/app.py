"""FastAPI application factory for OrionBelt Semantic Layer."""

from __future__ import annotations

import asyncio
import base64
import functools
import importlib.util
import logging
import os
import shutil
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from orionbelt import __version__
from orionbelt.api import token_page
from orionbelt.api.auth import require_auth
from orionbelt.api.deps import (
    AppRuntime,
    CacheRuntimeConfig,
    OneshotBatchConfig,
    bind_request_runtime,
    init_session_manager,
    reset_request_runtime,
    reset_session_manager,
)
from orionbelt.api.logging_config import configure_logging
from orionbelt.api.middleware import (
    MetricsMiddleware,
    RequestBodyLimitMiddleware,
    RequestIdMiddleware,
    RequestTimingMiddleware,
    SecurityHeadersMiddleware,
    SessionRateLimitMiddleware,
)
from orionbelt.api.openapi_security import (
    docs_connect_sources,
    idp_connect_sources,
    install_openapi_security,
    swagger_init_oauth,
)
from orionbelt.api.routers import (
    auth_status,
    cache_stats,
    composables,
    convert,
    datasources,
    dialects,
    graph,
    heartbeat,
    model_api,
    oneshot,
    reference,
    sessions,
    shortcuts,
    tokens,
)
from orionbelt.api.routers import lineage as lineage_router
from orionbelt.api.routers import (
    models as models_router,
)
from orionbelt.api.routers import (
    rules as rules_router,
)
from orionbelt.api.routers import settings as settings_router
from orionbelt.api.schemas import HealthResponse
from orionbelt.auth import (
    MODE_OIDC,
    AuthConfigError,
    OidcClient,
    OidcConfig,
    get_authorization_endpoint,
    get_mode,
    get_oidc_client,
    get_token_endpoint,
    get_token_store,
    parse_scope_claims,
    parse_scope_map,
    resolve_mode,
    warehouse_sign_in_missing,
)
from orionbelt.auth.sealing import parse_keys as parse_store_keys
from orionbelt.cache.factory import build_cache
from orionbelt.service import db_executor, metrics
from orionbelt.service.compilation_cache import CompilationCache
from orionbelt.service.federated_executor import FederationLimits, spill_directory
from orionbelt.service.federated_executor import configure as configure_federation
from orionbelt.service.session_manager import SessionManager
from orionbelt.service.user_login import (
    DataSourceSignInError,
    SignInInteractionRequiredError,
    describe_login,
    user_login_sources,
)
from orionbelt.service.value_search import ValueSearchConfig
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
            "the direct equivalent). See the v2.7.0 entry in CHANGELOG-archive.md."
        )


def oidc_config(settings: Settings) -> OidcConfig | None:
    """The OIDC settings when ``AUTH_MODE=oidc``, else None.

    Raises ``AuthConfigError`` (refuse to start) when they are incomplete or
    malformed.
    """
    if resolve_mode(settings.auth_mode) != MODE_OIDC:
        return None
    return OidcConfig(
        issuer=settings.oidc_issuer.strip(),
        audience=settings.oidc_audience.strip(),
        jwks_uri=settings.oidc_jwks_uri.strip(),
        jwks_file=settings.oidc_jwks_file.strip(),
        jwks_cache_ttl=settings.oidc_jwks_cache_ttl,
        jwks_min_refetch_seconds=settings.oidc_jwks_min_refetch_seconds,
        scope_claims=parse_scope_claims(settings.oidc_scope_claims),
        scope_map=parse_scope_map(settings.oidc_scope_map),
        clock_skew_seconds=settings.oidc_clock_skew_seconds,
        token_store_path=settings.token_store_path.strip(),
        token_default_days=settings.token_default_days,
        token_max_days=settings.token_max_days,
        authorization_endpoint=settings.oidc_authorization_endpoint.strip(),
        token_endpoint=settings.oidc_token_endpoint.strip(),
        revocation_endpoint=settings.oidc_revocation_endpoint.strip(),
        client=_oidc_client(settings),
        token_store_keys=_token_store_keys(settings),
        consent_scopes=_consent_scopes(settings),
        consent_redirect_uri=settings.oidc_consent_redirect_uri.strip(),
    )


def _token_store_keys(settings: Settings) -> tuple[bytes, ...]:
    """The keys BI tokens' warehouse sign-ins are encrypted with (``TOKEN_STORE_KEY``).

    Raises ``AuthConfigError`` when both settings are set, the file cannot be
    read, or a key is not 32 bytes of base64.
    """
    raw = settings.token_store_key.get_secret_value().strip()
    key_file = settings.token_store_key_file.strip()
    if raw and key_file:
        raise AuthConfigError("Set TOKEN_STORE_KEY or TOKEN_STORE_KEY_FILE, not both.")
    if key_file:
        try:
            raw = Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthConfigError(
                f"TOKEN_STORE_KEY_FILE {key_file!r} cannot be read ({exc.strerror})."
            ) from None
        if not raw:
            raise AuthConfigError(f"TOKEN_STORE_KEY_FILE {key_file!r} is empty.")
        return parse_store_keys(raw, setting="TOKEN_STORE_KEY_FILE")
    return parse_store_keys(raw)


def _consent_scopes(settings: Settings) -> str:
    """The scopes of a warehouse sign-in: ``OIDC_CONSENT_SCOPES``, else the page's and
    ``offline_access``."""
    explicit = settings.oidc_consent_scopes.split()
    if explicit:
        return " ".join(explicit)
    scopes = settings.oidc_tokens_scopes.split() or ["openid"]
    if "offline_access" not in scopes:
        scopes.append("offline_access")
    return " ".join(scopes)


def _oidc_client(settings: Settings) -> OidcClient | None:
    """OBSL's own client at the IdP (``OIDC_CLIENT_ID`` and its secret), if configured.

    Raises ``AuthConfigError`` when only half of it is set, both secret
    settings are, or the secret file cannot be read.
    """
    client_id = settings.oidc_client_id.strip()
    secret = settings.oidc_client_secret.get_secret_value().strip()
    secret_file = settings.oidc_client_secret_file.strip()
    if secret and secret_file:
        raise AuthConfigError("Set OIDC_CLIENT_SECRET or OIDC_CLIENT_SECRET_FILE, not both.")
    if secret_file:
        try:
            secret = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthConfigError(
                f"OIDC_CLIENT_SECRET_FILE {secret_file!r} cannot be read ({exc.strerror})."
            ) from None
        if not secret:
            raise AuthConfigError(f"OIDC_CLIENT_SECRET_FILE {secret_file!r} is empty.")
    if not client_id and not secret:
        return None
    if not client_id:
        raise AuthConfigError("OIDC_CLIENT_SECRET is set without OIDC_CLIENT_ID.")
    if not secret:
        raise AuthConfigError(
            "OIDC_CLIENT_ID is set without OIDC_CLIENT_SECRET or OIDC_CLIENT_SECRET_FILE."
        )
    return OidcClient(client_id=client_id, client_secret=secret)


def _sign_in_challenge(error: DataSourceSignInError) -> str:
    """``WWW-Authenticate`` for a 401 from a data source that logs in as the user.

    When the identity provider wants the user to sign in again (MFA,
    conditional access, consent), it says where, in Microsoft's claims
    challenge format: ``authorization_uri`` (``OIDC_AUTHORIZATION_ENDPOINT``,
    else the discovered one, so the tenant's own) and, when the identity
    provider sent one, its claims challenge, base64-encoded as Microsoft's
    clients read it. The realm is OBSL's, as on every challenge it sends.
    """
    if not isinstance(error, SignInInteractionRequiredError):
        return 'Bearer realm="obsl"'
    params = ['realm="obsl"']
    sign_in_at = get_authorization_endpoint()
    if sign_in_at:
        params.append(f'authorization_uri="{sign_in_at}"')
    if error.claims:
        encoded = base64.b64encode(error.claims.encode("utf-8")).decode("ascii")
        params += ['error="insufficient_claims"', f'claims="{encoded}"']
    return "Bearer " + ", ".join(params)


def refuse_removed_auth_settings(settings: Settings) -> None:
    """Refuse to start when a removed setting is still set to turn auth on.

    ``AUTH_ENABLED``, ``FLIGHT_AUTH_MODE=token`` and ``FLIGHT_API_TOKEN`` are
    gone. Ignoring one would start the server without the auth it asked for,
    so its presence stops the start with what to set instead.
    """
    removed = []
    if settings.auth_enabled:
        removed.append("AUTH_ENABLED (set AUTH_MODE=api_key)")
    if settings.flight_auth_mode.strip().lower() not in ("", "none"):
        removed.append("FLIGHT_AUTH_MODE (set AUTH_MODE=api_key)")
    if settings.flight_api_token:
        removed.append("FLIGHT_API_TOKEN (add the token to API_KEYS)")
    if removed:
        raise AuthConfigError(
            "These settings were removed and would no longer turn authentication on: "
            + "; ".join(removed)
            + ". Remove them. See docs/guide/authentication.md."
        )


def _refuse_unusable_user_login() -> None:
    """Refuse to start when a data source logs in as the user and cannot.

    ``DB_<NAME>_AUTH=user`` needs the caller's identity provider token, which
    only ``AUTH_MODE=oidc`` carries. A source whose user token the identity
    provider issues (Snowflake) also needs OBSL's own client there and its
    token endpoint. A data-source configuration error is left to surface
    where the source is used, as before.
    """
    try:
        from ob_flight.datasources import (
            USER_LOGIN,
            DataSourceConfigError,
            configured_datasources,
        )
    except ImportError:  # no ob-flight-extension: no named data sources
        return
    try:
        sources = configured_datasources()
    except DataSourceConfigError as exc:
        logger.warning("Data source configuration: %s", exc)
        return
    user_login = [s.name for s in sources if s.login == USER_LOGIN]
    if user_login and get_mode() != MODE_OIDC:
        raise AuthConfigError(
            f"Data source(s) {', '.join(user_login)} log in as the user (DB_<NAME>_AUTH=user), "
            "which needs AUTH_MODE=oidc: only a signed-in user's token can be exchanged."
        )
    idp_issued = ", ".join(s.name for s in sources if s.idp_issues_token)
    if idp_issued and get_oidc_client() is None:
        raise AuthConfigError(
            f"Data source(s) {idp_issued} log in as the user with a token the identity "
            "provider issues, which OBSL asks for as its own client there: set "
            "OIDC_CLIENT_ID and OIDC_CLIENT_SECRET (or OIDC_CLIENT_SECRET_FILE)."
        )
    if idp_issued and get_token_endpoint() is None:
        raise AuthConfigError(
            f"Data source(s) {idp_issued} log in as the user, and the identity provider's "
            "token endpoint is unknown: set OIDC_TOKEN_ENDPOINT (OIDC_JWKS_URI and "
            "OIDC_JWKS_FILE skip the discovery document that names it)."
        )


def _report_bi_token_sign_in() -> None:
    """Name the data sources that log in as the user, and whether BI tokens reach them.

    Not a reason to refuse the start: without the pieces, those sources
    refuse BI tokens, as they did before warehouse sign-ins existed.
    """
    sources = user_login_sources()
    if get_mode() != MODE_OIDC or not sources:
        return
    logger.info(
        "Auth: data sources that log in as the user: %s",
        ", ".join(f"{source.name} ({describe_login(source)})" for source in sources),
    )
    if get_token_store() is None:
        return
    missing = warehouse_sign_in_missing()
    if missing:
        logger.warning(
            "Data sources that log in as the user refuse BI tokens: a BI token's warehouse "
            "sign-in needs %s.",
            ", ".join(missing),
        )
    else:
        logger.info("BI tokens created on /tokens take a warehouse sign-in along")


def _start_duckdb_workers() -> None:
    """Check and start the DuckDB sources on Hardcharger workers.

    One that cannot run (its settings, the driver) refuses the start, by the
    source's name: found now rather than at its first query. The others' pools
    start, so their workers' health is known before the first query. Data
    sources that are not usable as written are reported where they are used,
    as before.
    """
    from orionbelt.service.datasource_selection import configured_datasource_list
    from orionbelt.service.db_executor import ExecutionUnavailableError
    from orionbelt.service.duckdb_pool import PoolConfigError, check_sources, start_pools

    try:
        sources = configured_datasource_list()
    except ExecutionUnavailableError:
        return
    try:
        check_sources(sources)
        start_pools(sources)
    except PoolConfigError as exc:
        raise RuntimeError(str(exc)) from None


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
    _start_duckdb_workers()

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
        compilation_cache=CompilationCache(
            max_entries=settings.compile_cache_max_entries,
            max_bytes=settings.compile_cache_max_bytes,
            max_entry_bytes=settings.compile_cache_max_entry_bytes,
        ),
    )
    mgr.start()

    # Build Flight info dict if enabled (exposed via GET /v1/settings)
    flight_info: dict[str, object] | None = None
    if settings.flight_enabled:
        flight_info = {
            "enabled": True,
            "port": settings.flight_port,
            "auth_mode": resolve_mode(settings.auth_mode),
            "db_vendor": settings.db_vendor,
        }

    # query/execute is available when explicitly enabled OR when Flight is enabled
    query_execute_enabled = settings.query_execute or settings.flight_enabled
    configure_federation(
        FederationLimits(
            max_concurrent=settings.federation_max_concurrent,
            queue_timeout_seconds=settings.federation_queue_timeout_seconds,
            leg_threads=settings.federation_leg_threads,
            max_leg_rows=settings.federation_max_leg_rows,
            max_leg_bytes=settings.federation_max_leg_mb * 1024 * 1024,
            combine_threads=settings.federation_combine_threads,
            combine_memory_limit=settings.federation_combine_memory_limit,
            spill_dir=spill_directory(settings.federation_spill_dir),
            spill_threshold_bytes=settings.federation_spill_threshold_mb * 1024 * 1024,
            spill_max_bytes=settings.federation_spill_max_mb * 1024 * 1024,
            spill_budget_bytes=settings.federation_spill_budget_mb * 1024 * 1024,
        )
    )

    # Each MODEL_FILES entry goes into its own protected named session.
    # JSON Schema validation already happened in ``_read_and_validate_model_file``
    # (via ``store.validate``, which is schema-aware), so an invalid file has
    # already failed startup by this point.
    for name, yaml_str in named_preloads:
        store = mgr.get_or_create_named(name)
        # Where queries execute, a data source the model names must exist.
        store.load_model(
            yaml_str,
            execution_dialect=settings.db_vendor if query_execute_enabled else None,
        )
        logger.info("Loaded model '%s' into protected session", name)
    if named_preloads:
        logger.info(
            "Multi-model mode active: %d model(s) loaded — %s",
            len(named_preloads),
            ", ".join(n for n, _ in named_preloads),
        )

    # Wipe persisted cache state on startup. Shared models now use a stable
    # content-derived ``model_id`` (see service/model_cache.py), so cache keys
    # from a previous run are no longer orphaned by construction — but we still
    # wipe conservatively: a change to the compiler or model semantics between
    # runs could otherwise let a stale row survive under a matching key.
    # Starting empty avoids that risk (a future compiler-version stamp could
    # enable cross-restart reuse). See PLAN_freshness_driven_cache.md §7.
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
    # Load pyarrow before serving, whatever the cache backend. The executor's
    # Arrow fetch paths refuse to run unless it is already imported, so this is
    # what decides whether a result carries the driver's schema — and that
    # schema is what types a cached or ``format=arrow`` column from the
    # declaration rather than from the values one result happened to contain
    # (#410). It used to sit inside the ``backend_name == "file"`` branch
    # below, so on the default ``cache_backend=noop`` it never ran and every
    # column fell back to inference. Startup is also the only safe moment:
    # importing it inside a live uvicorn loop on macOS drags in gRPC
    # initialisation mid-request, which is why the executor guards rather than
    # imports.
    if not db_executor.ensure_arrow():
        logger.warning(
            "pyarrow is not installed — result columns will be typed by "
            "inference rather than from the driver schema"
        )

    if cache.backend_name == "file":
        # Pay the remaining lazy-import taxes at startup so the first
        # user-visible cache hit doesn't include first-call codec setup.
        # DuckDB lazy-imports ``pytz`` on the first TIMESTAMPTZ bind. We
        # exercise these with a full encode → decode round-trip on a one-row
        # payload.
        try:
            from orionbelt.cache import result_codec

            # Run the encode → decode round-trip on the default threadpool so
            # the same worker ``try_cache_get`` uses for its off-loop decode is
            # warmed too, not just the (process-global) pyarrow import.
            await asyncio.to_thread(result_codec.warm)
            await cache.stats()  # exercises DuckDB meta read + pytz bind
            # A hit's fetch path is a DuckDB metadata SELECT plus an off-loop
            # blob read, neither of which ``stats()`` (nor a plain miss) touches.
            # ``warmup`` drives the full set -> read-back -> evict round-trip so
            # those statements and the off-loop read are warm before the first
            # real hit, without recording a synthetic miss or leaving an entry.
            await cache.warmup()
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

    # Keep each MODEL_FILES entry's YAML by name: GET /v1/models/{name} serves
    # it to the UI's read-only model editor (model upload is blocked with 403
    # in admin-curated mode, so the UI cannot fall back to uploading). With
    # one entry it is also /v1/settings.model_yaml, and POST /v1/sessions seeds
    # each new user session with it.
    app.state.runtime = init_session_manager(
        mgr,
        disable_session_list=settings.disable_session_list,
        admin_curated=is_admin_curated,
        curated_model_yamls=dict(named_preloads),
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
        value_search_config=ValueSearchConfig(
            enabled=settings.value_search_enabled,
            max_limit=settings.value_search_max_limit,
            fuzzy_domain_max=settings.value_search_fuzzy_domain_max,
            timeout_ms=settings.value_search_timeout_ms,
        ),
        cache=cache,
        cache_config=cache_config,
        auth_mode=settings.auth_mode,
        api_keys=settings.api_keys,
        api_key_header=settings.api_key_header,
        oidc=oidc_config(settings),
    )
    _refuse_unusable_user_login()
    _report_bi_token_sign_in()

    # Start Arrow Flight SQL server only when FLIGHT_ENABLED=true. It is an
    # extra network surface (binds 0.0.0.0), so it must be opted into
    # explicitly rather than auto-started merely because ob-flight-extension is
    # installed (which would silently expose a SQL surface). When the package is
    # present but the flag is off, log a hint.
    flight_thread = None
    if not settings.flight_enabled and importlib.util.find_spec("ob_flight") is not None:
        logger.info(
            "ob-flight-extension is installed but FLIGHT_ENABLED is not set; "
            "Arrow Flight SQL is disabled. Set FLIGHT_ENABLED=true to enable it."
        )
    if settings.flight_enabled:
        try:
            from ob_flight.startup import start_flight_background
            from ob_flight.tls import FlightTLSConfigError, load_flight_tls

            # Read the TLS material before anything else: a misconfiguration
            # here stops startup rather than quietly serving plaintext, which
            # would leave the deployment believing it was encrypted.
            try:
                flight_tls = load_flight_tls(
                    settings.flight_tls_cert,
                    settings.flight_tls_key,
                    settings.flight_tls_client_ca,
                )
            except FlightTLSConfigError as exc:
                raise RuntimeError(f"Flight TLS configuration is unusable: {exc}") from None

            # Decide the Flight auth handler here, from Settings, and always
            # pass it explicitly so the handler matches what we report and so
            # ob_flight never re-reads the environment (which could diverge from
            # Settings / .env). Auth on (api_key / oidc) -> OBSL's shared auth:
            # an API key, or under oidc also the IdP's token, and each call runs
            # as its caller. Otherwise no auth (NoopAuthHandler) + warning.
            from orionbelt.auth import MODE_NONE, get_mode

            flight_auth_middleware: dict[str, Any] = {}
            if get_mode() != MODE_NONE:
                from ob_flight.auth import build_shared_auth

                # Built as a pair, and installed as a pair. The handler alone
                # cannot authenticate a current client - it implements Flight's
                # legacy Handshake, while ADBC uses AuthenticateBasicToken,
                # which carries the credential in a header - and the middleware
                # alone would refuse the legacy handshake. Each covers what the
                # other cannot.
                flight_auth_handler, flight_auth_middleware = build_shared_auth()
            else:
                # No shared auth -> the Flight listener binds 0.0.0.0 and
                # accepts every client.
                from ob_flight.auth import NoopAuthHandler

                flight_auth_handler = NoopAuthHandler()
                logger.warning(
                    "Flight SQL is starting WITHOUT authentication on 0.0.0.0:%d. "
                    "Anyone who can reach this port can query. Set AUTH_MODE=api_key "
                    "to require a key, or restrict network access.",
                    settings.flight_port,
                )

            flight_thread = start_flight_background(
                session_manager=mgr,
                port=settings.flight_port,
                auth_handler=flight_auth_handler,
                auth_middleware=flight_auth_middleware,
                tls=flight_tls,
                default_dialect=settings.db_vendor,
                cache=cache,
                cache_config=cache_config,
            )
            # Imported here rather than reused from the branch above, which
            # only runs in the no-auth case - exactly the case this warning
            # does not fire for.
            from ob_flight.auth import NoopAuthHandler as _NoopAuthHandler

            if flight_tls is None and not isinstance(flight_auth_handler, _NoopAuthHandler):
                logger.warning(
                    "Flight SQL is authenticating over plaintext gRPC on port %d: "
                    "credentials cross the wire in the clear. Set FLIGHT_TLS_CERT "
                    "and FLIGHT_TLS_KEY, or terminate TLS in front of it.",
                    settings.flight_port,
                )

            settings.flight_enabled = True
            logger.info(
                "Flight SQL server started on port %d (vendor=%s, AUTH_MODE=%s)",
                settings.flight_port,
                settings.db_vendor,
                get_mode(),
            )
            # Refresh cached deps so /v1/settings and query gating
            # reflect the auto-detected Flight state.
            from orionbelt.api.deps import update_flight_state

            update_flight_state(
                flight_info={
                    "enabled": True,
                    "port": settings.flight_port,
                    "auth_mode": resolve_mode(settings.auth_mode),
                    "db_vendor": settings.db_vendor,
                },
                query_execute_enabled=True,
            )
        except ImportError:
            logger.warning(
                "FLIGHT_ENABLED=true but ob-flight-extension is not installed. "
                "Install with: uv sync --extra flight"
            )
        except TypeError as e:
            # Signature drift between OBSL and the installed
            # ob-flight-extension. The published PyPI release has
            # historically lagged behind the kwargs OBSL passes
            # (issue #96: PyPI ob-flight-extension 2.1.0 lacks the
            # ``cache=`` kwarg that OBSL has been passing since v2.4.0).
            # Catch the kwarg-mismatch TypeError, log both versions so
            # the user knows what to upgrade, and continue without
            # Flight SQL rather than crashing the whole API lifespan.
            try:
                import ob_flight as _ob_flight

                installed_version = getattr(_ob_flight, "__version__", "unknown")
            except Exception:
                installed_version = "unknown"
            logger.warning(
                "Flight SQL startup skipped: ob-flight-extension "
                "(installed: %s) does not accept the kwargs this OBSL "
                "version passes (%s). REST and pgwire continue to work. "
                "Upgrade ob-flight-extension to a version that matches "
                "this OBSL release, or unset FLIGHT_ENABLED to silence "
                "this warning.",
                installed_version,
                e,
            )

    # Start Postgres wire surface if PGWIRE_ENABLED=true. The startup
    # helper builds a SemanticRouter bound to the live SessionManager,
    # so SELECT statements over the wire run through the same
    # translate→compile→execute pipeline as REST /query/semantic-ql.
    # See design/PLAN_postgres_wire.md.
    pgwire_runtime = None
    if settings.pgwire_enabled:
        from orionbelt.pgwire.startup import start_pgwire

        pgwire_runtime = await start_pgwire(
            settings,
            session_manager=mgr,
            cache=cache,
            cache_config=cache_config,
        )

    metrics.start(
        enabled=settings.metrics_enabled,
        host=settings.metrics_host,
        port=settings.metrics_port,
        sessions=mgr,
        cache=cache,
    )
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
        from orionbelt.service.duckdb_pool import close_pools
        from orionbelt.service.duckdb_source import close_instances

        metrics.stop()
        close_pools()
        close_instances()
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
    refuse_removed_auth_settings(settings)

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
        swagger_ui_init_oauth=swagger_init_oauth(settings),
    )
    app.state.settings = settings
    install_openapi_security(app, settings)

    # Pydantic ``extra='forbid'`` on QueryObject / OBML models surfaces as a
    # generic 422 "Extra inputs are not permitted" by default. Translate those
    # entries into the OBSL ``UNKNOWN_PROPERTY`` error shape so clients can
    # branch on the code instead of pattern-matching English. Non-extra errors
    # are passed through unchanged so other validators (e.g. missing fields)
    # keep their default form.
    @app.exception_handler(DataSourceSignInError)
    async def _data_source_sign_in_error(request: Request, exc: Exception) -> Response:
        # A data source that logs in as the user could not: no signed-in user
        # (401), the warehouse refused them (403), or its sign-in service was
        # unreachable (503). Not a warehouse failure.
        if not isinstance(exc, DataSourceSignInError):
            raise exc
        headers = {"WWW-Authenticate": _sign_in_challenge(exc)} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            content={"detail": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(request: Request, exc: RequestValidationError) -> Response:
        unknown_property_errors: list[dict[str, object]] = []
        for err in exc.errors():
            if err.get("type") != "extra_forbidden":
                continue
            loc = err.get("loc", ())
            # Drop the leading "body" frame for a cleaner path
            # (e.g. ``where[0].feild`` instead of ``body.query.where.0.feild``).
            trimmed = [str(p) for p in loc if p != "body"]
            key = trimmed[-1] if trimmed else "?"
            path = ".".join(trimmed[:-1]) if len(trimmed) > 1 else ""
            unknown_property_errors.append(
                {
                    "code": "UNKNOWN_PROPERTY",
                    "message": f"Unknown property '{key}'" + (f" at {path}" if path else ""),
                    "path": path or None,
                }
            )
        if unknown_property_errors:
            return JSONResponse(
                status_code=422,
                content={
                    "message": "Request contains unknown properties",
                    "errors": unknown_property_errors,
                    "warnings": [],
                },
            )
        # No extra_forbidden in this batch — defer to FastAPI's default
        # handler, which safely serialises ctx values (e.g. ValueError).
        return await request_validation_exception_handler(request, exc)

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
    app.add_middleware(
        SecurityHeadersMiddleware,
        docs_connect_src=functools.partial(docs_connect_sources, settings),
        tokens_connect_src=functools.partial(idp_connect_sources, settings),
    )
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        SessionRateLimitMiddleware,
        max_requests=settings.session_rate_limit,
        window_seconds=60,
        trusted_proxy_count=settings.trusted_proxy_count,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware)

    @app.middleware("http")
    async def _bind_runtime(request: Request, call_next: Any) -> Any:
        """Bind this app's runtime for the request so deps providers read it.

        Makes the no-argument providers request-scoped: with multiple app
        instances in one process, each request reads its own app's runtime
        instead of the shared process-level fallback.
        """
        runtime = getattr(request.app.state, "runtime", None)
        if not isinstance(runtime, AppRuntime):
            return await call_next(request)
        token = bind_request_runtime(runtime)
        try:
            return await call_next(request)
        finally:
            reset_request_runtime(token)

    # Versioned API routes under /v1. The auth dependency is applied at the
    # router level so every /v1/* endpoint is protected when AUTH_MODE != none;
    # root-level endpoints (/health, /robots.txt, /docs, /openapi.json, /ui)
    # stay exempt by virtue of living outside this router.
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])
    # Routers whose prefix lives on their APIRouter() constructor (so their
    # root routes keep an empty path and resolve with no trailing slash under
    # FastAPI 0.137+) are included without a prefix here: sessions, dialects,
    # reference, models, settings. model_api/graph share the /sessions space
    # but have no empty-path routes, so they keep the include-time prefix.
    v1.include_router(sessions.router, tags=["sessions"])
    v1.include_router(model_api.router, prefix="/sessions", tags=["model-discovery"])
    v1.include_router(composables.router, prefix="/sessions", tags=["model-discovery"])
    v1.include_router(rules_router.router, prefix="/sessions", tags=["rules"])
    v1.include_router(lineage_router.router, prefix="/sessions", tags=["lineage"])
    v1.include_router(graph.router, prefix="/sessions", tags=["graph"])
    v1.include_router(shortcuts.router, tags=["model-discovery"])
    v1.include_router(convert.router, prefix="/convert", tags=["convert"])
    v1.include_router(dialects.router, tags=["dialects"])
    v1.include_router(datasources.router, tags=["datasources"])
    v1.include_router(oneshot.router, prefix="/oneshot", tags=["oneshot"])
    v1.include_router(reference.router, tags=["reference"])
    v1.include_router(models_router.router, tags=["models"])
    v1.include_router(settings_router.router, tags=["settings"])
    v1.include_router(cache_stats.router, prefix="/cache", tags=["cache"])
    v1.include_router(tokens.router, prefix="/auth/tokens", tags=["auth"])
    v1.include_router(auth_status.router, prefix="/auth", tags=["auth"])
    app.include_router(token_page.build_router(settings))
    app.include_router(v1)

    # Heartbeat is included OUTSIDE the auth-bearing v1 router: it carries its
    # own auth (Authorization: Bearer <HEARTBEAT_AUTH_TOKEN>, and 404 when the
    # token is unset). Routing it through global API-key auth would collide,
    # because that auth also treats `Authorization: Bearer` as an API key, so a
    # valid heartbeat token would be rejected as an invalid API key before the
    # handler runs. It keeps the same /v1/heartbeat path.
    app.include_router(heartbeat.router, prefix="/v1", tags=["cache"])

    # Root-level endpoints (no version prefix — used by load balancers, crawlers)
    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        # auth_mode is exposed here (unauthenticated) so clients can detect
        # whether a credential is required before hitting any /v1 endpoint.
        from orionbelt.auth import get_mode

        return HealthResponse(status="ok", version=__version__, auth_mode=get_mode())

    @app.get("/robots.txt", include_in_schema=False)
    async def robots_txt() -> Response:
        # This host serves the REST API and the interactive Gradio UI (under
        # /ui) - there is nothing here meant for search indexing (the docs site
        # lives on a separate host). Disallow all crawling so compliant bots
        # (Googlebot/GoogleOther/bingbot) stop probing app/internal paths.
        return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")

    # Mount Gradio UI at /ui when the 'ui' extra is installed
    try:
        import gradio as gr

        from orionbelt.auth import MODE_NONE
        from orionbelt.ui.app import create_blocks, set_api_credentials

        # The embedded UI is a server-side proxy that holds an API key and can
        # act on /v1 (create sessions, load models, run queries, clear cache).
        # /ui is intentionally NOT behind require_auth (browsers can't send the
        # key on navigation), so injecting the server's key here would turn /ui
        # into an open, privileged proxy: anyone who can reach /ui acts as the
        # key holder. We therefore do NOT auto-pull a key from API_KEYS. The
        # operator must opt in explicitly by setting OBSL_API_KEY, which signals
        # they accept /ui as a key-holding surface and will network-protect it.
        # Under AUTH_MODE=oidc the key must be one of API_KEYS (a service key);
        # signing in to the UI with the IdP comes later.
        ui_auth_mode = resolve_mode(settings.auth_mode)
        if ui_auth_mode != MODE_NONE:
            ui_key = os.environ.get("OBSL_API_KEY") or None
            if ui_key:
                set_api_credentials(ui_key, settings.api_key_header)
                logger.warning(
                    "Embedded UI is configured with OBSL_API_KEY and acts as an "
                    "authenticated proxy to /v1. /ui is NOT itself behind API-key "
                    "auth - restrict network access to it (reverse proxy / firewall)."
                )
            else:
                logger.warning(
                    "AUTH_MODE=%s but OBSL_API_KEY is not set: the embedded UI "
                    "cannot call /v1 and will surface auth errors. Set OBSL_API_KEY to "
                    "one of API_KEYS to enable it (and protect /ui), or run the UI as a "
                    "separate service.",
                    ui_auth_mode,
                )

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
                "auth_mode": resolve_mode(settings.auth_mode),
                "db_vendor": settings.db_vendor,
            }
        demo = create_blocks(default_api_url=api_url, embedded_settings=ui_settings)
        from pathlib import Path

        from starlette.responses import FileResponse

        _favicon_path = Path(__file__).resolve().parents[1] / "ui" / "favicon.png"

        @app.get("/favicon.ico", include_in_schema=False)
        async def _favicon() -> FileResponse:
            return FileResponse(_favicon_path, media_type="image/png")

        app = gr.mount_gradio_app(app, demo, path="/ui", **demo.ob_frontend)
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
    #
    # Exactly ``./.env``, which is what pydantic-settings reads for
    # ``env_file=".env"`` (see ``settings.Settings``). Both of the obvious
    # alternatives search, and searching is the whole problem: bare
    # ``load_dotenv()`` walks up from *this file*, so a source checkout's .env
    # was loaded whatever directory the server started in;
    # ``find_dotenv(usecwd=True)`` walks up from the working directory, so a
    # subdirectory with no .env of its own still inherits a parent's - and
    # ``Settings`` inherits neither. Either way the two disagree, and the
    # disagreement injects a *different deployment's* database credentials and
    # ``MODEL_FILES`` into ``os.environ`` while ``Settings`` reads the intended
    # ones. A missing file makes this a no-op, so a deployment configured
    # purely by environment variables is unaffected.
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)

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
        **_rest_tls_kwargs(settings),
    )


def _rest_tls_kwargs(settings: Any) -> dict[str, Any]:
    """uvicorn's TLS arguments, or an empty dict when TLS is not configured.

    Loaded before the socket binds, through the same loader the Flight and
    pgwire listeners use, so a misconfiguration stops startup with the
    operator's message instead of serving plaintext on a deployment whose
    configuration reads as encrypted. That failure mode is the whole reason
    both other surfaces refuse to start on a half-configured pair.

    uvicorn takes paths rather than PEM bytes, which is why ``ListenerTLS``
    carries both.
    """
    from orionbelt.service.tls import TLSConfigError, load_listener_tls

    try:
        tls = load_listener_tls(
            settings.api_tls_cert,
            settings.api_tls_key,
            settings.api_tls_client_ca,
            prefix="API",
        )
    except TLSConfigError as exc:
        raise RuntimeError(f"REST TLS configuration is unusable: {exc}") from None
    if tls is None:
        logger.info(
            "REST API serving plaintext HTTP on %s:%s",
            settings.api_server_host,
            settings.effective_port,
        )
        return {}

    kwargs: dict[str, Any] = {
        "ssl_certfile": tls.cert_path,
        "ssl_keyfile": tls.key_path,
    }
    if tls.client_ca_path is not None:
        import ssl

        kwargs["ssl_ca_certs"] = tls.client_ca_path
        kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED
    logger.info(
        "REST API serving HTTPS on %s:%s (client certificates %s)",
        settings.api_server_host,
        settings.effective_port,
        "required" if tls.client_ca_path else "not required",
    )
    return kwargs
