"""Sessions belong to their creator (design/PLAN_authentication.md D3, Phase 4a)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from orionbelt.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    Principal,
    bind_principal,
    current_principal,
    may_use,
    reset_principal,
)
from orionbelt.auth.principal import ANONYMOUS, API_KEY_PRINCIPAL
from orionbelt.service.session_manager import (
    SessionManager,
    SessionNotFoundError,
)

ALICE = Principal(kind="user", sub="alice", scopes=frozenset({SCOPE_READ, SCOPE_WRITE}))
BOB = Principal(kind="user", sub="bob", scopes=frozenset({SCOPE_READ, SCOPE_WRITE}))
ADMIN = Principal(kind="user", sub="root", scopes=frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN}))


@contextmanager
def acting_as(principal: Principal) -> Iterator[None]:
    token = bind_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


@pytest.fixture
def mgr() -> SessionManager:
    return SessionManager(ttl_seconds=3600, cleanup_interval=9999)


class TestMayUse:
    def test_unowned_is_shared(self) -> None:
        assert may_use(BOB, None)

    def test_owner_and_admin(self) -> None:
        assert may_use(ALICE, "alice")
        assert may_use(ADMIN, "alice")
        assert not may_use(BOB, "alice")

    def test_no_bound_caller_sees_everything(self) -> None:
        assert may_use(None, "alice")

    def test_api_key_and_anonymous_callers_see_everything(self) -> None:
        # They hold every scope, admin included: AUTH_MODE=none and api_key
        # behave exactly as before ownership existed.
        assert may_use(ANONYMOUS, "alice")
        assert may_use(API_KEY_PRINCIPAL, "alice")


class TestSessionOwnership:
    def test_creator_is_owner(self, mgr: SessionManager) -> None:
        with acting_as(ALICE):
            info = mgr.create_session()
        assert info.owner == "alice"

    def test_caller_without_sub_creates_shared_session(self, mgr: SessionManager) -> None:
        with acting_as(API_KEY_PRINCIPAL):
            info = mgr.create_session()
        assert info.owner is None
        with acting_as(BOB):
            assert mgr.get_session(info.session_id).session_id == info.session_id

    def test_other_user_gets_not_found(self, mgr: SessionManager) -> None:
        with acting_as(ALICE):
            sid = mgr.create_session().session_id
        with acting_as(BOB):
            with pytest.raises(SessionNotFoundError):
                mgr.get_session(sid)
            with pytest.raises(SessionNotFoundError):
                mgr.get_store(sid)
            with pytest.raises(SessionNotFoundError):
                mgr.close_session(sid)
        with acting_as(ALICE):
            assert mgr.get_store(sid) is not None

    def test_admin_sees_and_closes_any_session(self, mgr: SessionManager) -> None:
        with acting_as(ALICE):
            sid = mgr.create_session().session_id
        with acting_as(ADMIN):
            assert mgr.get_session(sid).owner == "alice"
            mgr.close_session(sid)
        with acting_as(ALICE), pytest.raises(SessionNotFoundError):
            mgr.get_session(sid)

    def test_list_shows_own_sessions_only(self, mgr: SessionManager) -> None:
        with acting_as(ALICE):
            alice_sid = mgr.create_session().session_id
        with acting_as(BOB):
            bob_sid = mgr.create_session().session_id
            assert [s.session_id for s in mgr.list_sessions()] == [bob_sid]
        with acting_as(ADMIN):
            assert {s.session_id for s in mgr.list_sessions()} == {alice_sid, bob_sid}

    def test_curated_and_default_sessions_are_shared(self, mgr: SessionManager) -> None:
        mgr.get_or_create_named("sales")
        mgr.get_or_create_default()
        with acting_as(BOB):
            assert mgr.get_store("sales") is not None
            assert mgr.get_store("__default__") is not None
            assert mgr.list_protected_session_ids() == ["sales"]

    def test_without_a_bound_caller_everything_is_reachable(self, mgr: SessionManager) -> None:
        # Startup, background jobs, and the Flight / pgwire surfaces (until
        # Phase 4b binds their callers) see every session, as before.
        with acting_as(ALICE):
            sid = mgr.create_session().session_id
        assert current_principal() is None
        assert mgr.get_store(sid) is not None
        assert [s.session_id for s in mgr.list_sessions()] == [sid]
