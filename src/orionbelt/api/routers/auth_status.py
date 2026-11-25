"""How authentication is set up: ``GET /v1/auth/status`` (admin).

Everything an admin would otherwise piece together from settings and startup
logs: the mode, the API keys by id and when each was last used since the
process started, what OIDC verifies against, the BI token store, whether a
BI token can take a warehouse sign-in along, the data sources that log in as
the user, and how Flight SQL and the Postgres wire protocol log clients in.
Never a secret: keys appear by their id only. See
design/PLAN_auth_phase6.md and docs/guide/authentication.md.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from orionbelt.api.schemas import (
    AuthBiTokensStatus,
    AuthKeyStatus,
    AuthOidcStatus,
    AuthSigningKeys,
    AuthStatus,
    AuthSurfaces,
    AuthSurfaceStatus,
    AuthUserLoginSource,
    AuthWarehouseSignIn,
)
from orionbelt.api.token_page import token_page_enabled
from orionbelt.auth import (
    MODE_OIDC,
    get_authorization_endpoint,
    get_header_name,
    get_key_last_used,
    get_mode,
    get_oidc_client,
    get_oidc_config,
    get_revocation_endpoint,
    get_signing_keys_status,
    get_token_endpoint,
    get_token_store,
    warehouse_sign_in_missing,
)
from orionbelt.auth.pgwire_login import login_summary
from orionbelt.service.user_login import describe_login, user_login_sources
from orionbelt.settings import Settings

router = APIRouter()

_NO_USER_LOGIN_SOURCE = "a data source with DB_<NAME>_AUTH=user"


def _when(seconds: float | None) -> datetime | None:
    return datetime.fromtimestamp(seconds, tz=UTC) if seconds is not None else None


def _oidc() -> AuthOidcStatus | None:
    config = get_oidc_config()
    if config is None:
        return None
    keys = get_signing_keys_status() or {"source": "", "count": 0, "loaded_at": None}
    client = get_oidc_client()
    return AuthOidcStatus(
        issuer=config.issuer,
        audience=config.audience,
        authorization_endpoint=get_authorization_endpoint(),
        token_endpoint=get_token_endpoint(),
        revocation_endpoint=get_revocation_endpoint(),
        signing_keys=AuthSigningKeys(
            source=str(keys["source"]), count=int(keys["count"]), loaded_at=_when(keys["loaded_at"])
        ),
        scope_claims=list(config.scope_claims),
        scope_map={name: sorted(scopes) for name, scopes in config.scope_map.items()},
        client_id=client.client_id if client is not None else None,
    )


def _bi_tokens() -> AuthBiTokensStatus:
    store = get_token_store()
    if store is None:
        return AuthBiTokensStatus(enabled=False)
    try:
        active, signed_in = store.counts()
    except sqlite3.Error:
        active = signed_in = 0
    return AuthBiTokensStatus(
        enabled=True,
        store=str(store.path),
        active=active,
        with_warehouse_sign_in=signed_in,
        default_days=store.default_days,
        max_days=store.max_days,
    )


def _surfaces(settings: Settings) -> AuthSurfaces:
    pgwire_tls = bool(settings.pgwire_tls_cert)
    return AuthSurfaces(
        flight=AuthSurfaceStatus(
            enabled=settings.flight_enabled,
            tls=bool(settings.flight_tls_cert),
            login=f"AUTH_MODE={get_mode()}" if settings.flight_enabled else None,
        ),
        pgwire=AuthSurfaceStatus(
            enabled=settings.pgwire_enabled,
            tls=pgwire_tls,
            login=(
                login_summary(settings.pgwire_auth_mode, tls=pgwire_tls)
                if settings.pgwire_enabled
                else None
            ),
        ),
        tokens_page=token_page_enabled(settings) and get_token_store() is not None,
        swagger_sign_in=get_mode() == MODE_OIDC and bool(settings.oidc_swagger_client_id.strip()),
    )


@router.get("/status", response_model=AuthStatus)
def get_auth_status(request: Request) -> AuthStatus:
    """How authentication is set up on this server (admin). Never a secret.

    API keys appear by their id (eight hex characters of a hash) with when
    each last authenticated a caller since this process started.
    """
    settings: Settings = request.app.state.settings
    sources = user_login_sources() if get_mode() == MODE_OIDC else []
    missing = warehouse_sign_in_missing() + ([] if sources else [_NO_USER_LOGIN_SOURCE])
    return AuthStatus(
        mode=get_mode(),
        api_key_header=get_header_name(),
        service_keys=[
            AuthKeyStatus(id=identifier, last_used_since_start=_when(used))
            for identifier, used in get_key_last_used().items()
        ],
        oidc=_oidc(),
        bi_tokens=_bi_tokens(),
        warehouse_sign_in=AuthWarehouseSignIn(ready=not missing, missing=missing),
        user_login_sources=[
            AuthUserLoginSource(name=source.name, login=describe_login(source))
            for source in sources
        ],
        surfaces=_surfaces(settings),
    )
