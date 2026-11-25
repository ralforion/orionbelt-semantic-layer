"""Tokens for BI tools: create, list and revoke them (``/v1/auth/tokens``).

A signed-in user creates a token for one BI tool; the tool sends it as its
password, and it authenticates as that user with the token's scopes. Only
under ``AUTH_MODE=oidc`` with a token store: otherwise every endpoint answers
404. See docs/guide/bi-tokens.md.

Where a data source logs in as the user, a new token also takes a warehouse
sign-in along: the user signs in once more, as OBSL's own client, in a popup
the ``/tokens`` page opens (``consents``), and the token is created with the
finished sign-in's id. See ``auth/consent.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, Response

from orionbelt.api.schemas import (
    BiTokenConsentStarted,
    BiTokenConsentStatus,
    BiTokenCreated,
    BiTokenCreateRequest,
    BiTokenInfo,
    BiTokenList,
    BiTokensRevoked,
)
from orionbelt.auth import (
    FULL_SCOPES,
    SCOPE_ADMIN,
    SCOPE_READ,
    Principal,
    current_principal,
    get_consent_redirect_uri,
    get_token_store,
)
from orionbelt.auth.bi_tokens import BiToken, TokenStore
from orionbelt.auth.consent import LIFETIME_SECONDS, ConsentError, consents
from orionbelt.auth.oidc import with_implied
from orionbelt.service.user_login import bi_token_sign_in_available

router = APIRouter()


def _store() -> TokenStore:
    store = get_token_store()
    if store is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return store


def _caller() -> Principal:
    caller = current_principal()
    if caller is None:  # require_auth binds one on every /v1 request
        raise HTTPException(status_code=404, detail="Not Found")
    return caller


def _refused(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _signed_in_user() -> str:
    """The caller's ``sub``, when they signed in with the identity provider."""
    caller = _caller()
    if caller.kind != "user" or caller.token_id is not None or not caller.sub:
        raise _refused(
            403,
            "BI_TOKEN_NEEDS_SIGN_IN",
            "A BI token is created by a user signed in with the identity provider, "
            "not with an API key or another BI token.",
        )
    return caller.sub


def _consent_refused(exc: ConsentError) -> HTTPException:
    return _refused(exc.status, exc.code, exc.message)


def _when(seconds: float | None) -> datetime | None:
    return datetime.fromtimestamp(seconds, tz=UTC) if seconds is not None else None


def _info(record: BiToken) -> BiTokenInfo:
    return BiTokenInfo(
        id=record.id,
        label=record.label,
        owner=record.owner_sub,
        scopes=sorted(record.scopes),
        created_at=datetime.fromtimestamp(record.created_at, tz=UTC),
        expires_at=datetime.fromtimestamp(record.expires_at, tz=UTC),
        last_used_at=_when(record.last_used_at),
        warehouse_sign_in=record.warehouse_sign_in,
    )


@router.post("", status_code=201, response_model=BiTokenCreated)
def create_token(body: BiTokenCreateRequest) -> BiTokenCreated:
    """Create a BI token for yourself. The response is the only time it is shown.

    Only a user signed in with the identity provider creates one: an API key
    is no person, and a BI token cannot make another. With ``consent``, the
    token holds that finished warehouse sign-in, for data sources that log in
    as the user.
    """
    store = _store()
    sub = _signed_in_user()
    caller = _caller()
    requested = set(body.scopes or [SCOPE_READ])
    unknown = sorted(requested - FULL_SCOPES)
    if unknown:
        raise _refused(
            422,
            "UNKNOWN_SCOPE",
            f"Unknown scope(s): {', '.join(unknown)}. Use obsl:read, obsl:write or obsl:admin.",
        )
    missing = sorted(scope for scope in requested if not caller.has_scope(scope))
    if missing:
        raise _refused(
            403,
            "SCOPE_NOT_HELD",
            f"A token gets only scopes you hold; you do not hold {', '.join(missing)}.",
        )
    days = body.days if body.days is not None else store.default_days
    if days > store.max_days:
        raise _refused(
            422,
            "TOKEN_LIFETIME_TOO_LONG",
            f"A token lasts at most {store.max_days} days (TOKEN_MAX_DAYS).",
        )
    refresh_token = None
    if body.consent is not None:
        if store.sealer is None:
            raise _refused(
                422, "WAREHOUSE_SIGN_IN_OFF", "Warehouse sign-in is not enabled on this server."
            )
        try:
            refresh_token = consents.take(body.consent, sub)
        except ConsentError as exc:
            raise _consent_refused(exc) from None
    token, record = store.create(
        owner_sub=sub,
        owner_email=caller.email,
        label=body.label,
        scopes=with_implied(requested),
        days=days,
        refresh_token=refresh_token,
    )
    return BiTokenCreated(token=token, **_info(record).model_dump())


@router.post("/consents", status_code=201, response_model=BiTokenConsentStarted)
def start_consent(request: Request) -> BiTokenConsentStarted:
    """Start the warehouse sign-in a new BI token takes along.

    Where a data source logs in as the user (``DB_<NAME>_AUTH=user``), a BI
    token needs the user's identity provider token, which it is not. Open
    ``authorize_url`` in a popup: the user signs in as OrionBelt's own client,
    which keeps the identity provider's refresh token for the new token. Poll
    the sign-in's status, then create the token with ``consent``.
    """
    _store()
    sub = _signed_in_user()
    if not bi_token_sign_in_available():
        raise _refused(
            404, "WAREHOUSE_SIGN_IN_OFF", "Warehouse sign-in is not enabled on this server."
        )
    redirect_uri = get_consent_redirect_uri() or str(request.url_for("token_consent"))
    try:
        consent, authorize_url = consents.start(sub, redirect_uri)
    except ConsentError as exc:
        raise _consent_refused(exc) from None
    return BiTokenConsentStarted(
        consent=consent, authorize_url=authorize_url, expires_in=int(LIFETIME_SECONDS)
    )


@router.get("/consents/{consent}", response_model=BiTokenConsentStatus)
def consent_status(consent: str) -> BiTokenConsentStatus:
    """Whether your warehouse sign-in finished at the identity provider, or why it failed."""
    _store()
    sub = _signed_in_user()
    try:
        status, message = consents.status(consent, sub)
    except ConsentError as exc:
        raise _consent_refused(exc) from None
    return BiTokenConsentStatus(consent=consent, status=status, message=message)


@router.get("", response_model=BiTokenList)
def list_tokens(
    everyone: bool = Query(False, alias="all", description="Every user's tokens (admin)"),
) -> BiTokenList:
    """Your tokens that are not revoked, newest first; every user's with ``all=true``."""
    store = _store()
    caller = _caller()
    if everyone:
        if not caller.has_scope(SCOPE_ADMIN):
            raise _refused(
                403, "AUTH_INSUFFICIENT_SCOPE", f"Insufficient scope: requires {SCOPE_ADMIN}"
            )
        return BiTokenList(tokens=[_info(record) for record in store.list_tokens(None)])
    if not caller.sub:  # an API key has no tokens of its own
        return BiTokenList(tokens=[])
    return BiTokenList(tokens=[_info(record) for record in store.list_tokens(caller.sub)])


@router.delete("/{token_id}", status_code=204)
def revoke_token(token_id: str) -> Response:
    """Revoke one of your tokens (an admin: anyone's). It stops working at once."""
    store = _store()
    caller = _caller()
    owner = None if caller.has_scope(SCOPE_ADMIN) else caller.sub
    if (owner is None and not caller.has_scope(SCOPE_ADMIN)) or not store.revoke(
        token_id, owner_sub=owner
    ):
        raise _refused(404, "BI_TOKEN_NOT_FOUND", f"No token '{token_id}' of yours to revoke.")
    return Response(status_code=204)


@router.delete("", response_model=BiTokensRevoked)
def revoke_owner_tokens(
    owner: str = Query(description="The owner's subject (sub), as tokens list it"),
) -> BiTokensRevoked:
    """Revoke every token of one user (admin), e.g. when they leave."""
    return BiTokensRevoked(revoked=_store().revoke_owner(owner))
