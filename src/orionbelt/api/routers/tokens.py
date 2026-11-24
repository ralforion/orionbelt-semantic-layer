"""Tokens for BI tools: create, list and revoke them (``/v1/auth/tokens``).

A signed-in user creates a token for one BI tool; the tool sends it as its
password, and it authenticates as that user with the token's scopes. Only
under ``AUTH_MODE=oidc`` with a token store: otherwise every endpoint answers
404. See docs/guide/bi-tokens.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Response

from orionbelt.api.schemas import (
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
    get_token_store,
)
from orionbelt.auth.bi_tokens import BiToken, TokenStore
from orionbelt.auth.oidc import with_implied

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
    )


@router.post("", status_code=201, response_model=BiTokenCreated)
def create_token(body: BiTokenCreateRequest) -> BiTokenCreated:
    """Create a BI token for yourself. The response is the only time it is shown.

    Only a user signed in with the identity provider creates one: an API key
    is no person, and a BI token cannot make another.
    """
    store = _store()
    caller = _caller()
    if caller.kind != "user" or caller.token_id is not None or not caller.sub:
        raise _refused(
            403,
            "BI_TOKEN_NEEDS_SIGN_IN",
            "A BI token is created by a user signed in with the identity provider, "
            "not with an API key or another BI token.",
        )
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
    token, record = store.create(
        owner_sub=caller.sub,
        owner_email=caller.email,
        label=body.label,
        scopes=with_implied(requested),
        days=days,
    )
    return BiTokenCreated(token=token, **_info(record).model_dump())


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
