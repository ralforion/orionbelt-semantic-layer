"""Tokens for BI tools: the store, how a token authenticates, and where it may not.

See design/PLAN_bi_tokens.md (decisions T1-T9) and docs/guide/bi-tokens.md.
"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from orionbelt.auth import (
    OidcConfig,
    Principal,
    TokenInvalidError,
    authenticate,
    bi_tokens,
    check_still_valid,
    init_auth,
    reset_auth,
)
from orionbelt.auth.bi_tokens import TokenStore, is_bi_token, scram_keys, token_id_of_user
from orionbelt.auth.errors import AuthInvalidError
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.pgwire.auth import authenticate as pgwire_authenticate
from orionbelt.pgwire.protocol import StartupMessage
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

STARTUP = StartupMessage(protocol_version=196608, parameters={"user": "obsl"})
READ = frozenset({"obsl:read"})


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(str(tmp_path / "tokens" / "bi.sqlite"), default_days=90, max_days=365)


def _create(store: TokenStore, owner: str = "alice", days: int = 90) -> tuple[str, str]:
    token, record = store.create(
        owner_sub=owner, owner_email=f"{owner}@example.com", label="Tableau", scopes=READ, days=days
    )
    return token, record.id


class TestTheStore:
    def test_a_token_authenticates_as_its_owner(self, store: TokenStore) -> None:
        token, token_id = _create(store)
        assert is_bi_token(token)
        assert store.authenticate(token) == Principal(
            kind="user",
            sub="alice",
            email="alice@example.com",
            scopes=READ,
            token_id=token_id,
        )

    def test_a_wrong_secret_and_an_unknown_id_look_the_same(self, store: TokenStore) -> None:
        token, token_id = _create(store)
        wrong_secret = f"obsl_tok_{token_id}_{'0' * 40}"
        unknown_id = f"obsl_tok_{'0' * 12}_{token[-40:]}"
        for attempt in (wrong_secret, unknown_id, "obsl_tok_short"):
            with pytest.raises(TokenInvalidError, match="^BI token is not valid$"):
                store.authenticate(attempt)

    def test_a_revoked_token_stops_at_once(self, store: TokenStore) -> None:
        token, token_id = _create(store)
        assert not store.revoke(token_id, owner_sub="bob")  # not bob's
        assert store.revoke(token_id, owner_sub="alice")
        with pytest.raises(TokenInvalidError, match="revoked"):
            store.authenticate(token)
        assert not store.revoke(token_id, owner_sub=None)  # already revoked

    def test_an_expired_token_is_refused(
        self, store: TokenStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token, _ = _create(store, days=1)
        now = bi_tokens.time.time()
        monkeypatch.setattr(bi_tokens.time, "time", lambda: now + 86401)
        with pytest.raises(TokenInvalidError, match="expired"):
            store.authenticate(token)

    def test_its_lifetime_is_bounded(self, store: TokenStore) -> None:
        for days in (0, 366):
            with pytest.raises(ValueError, match="between 1 and 365"):
                _create(store, days=days)

    def test_listing_and_revoking_by_owner(self, store: TokenStore) -> None:
        _, first = _create(store, "alice")
        _, second = _create(store, "alice")
        _, bobs = _create(store, "bob")
        assert {t.id for t in store.list_tokens("alice")} == {first, second}
        assert {t.id for t in store.list_tokens(None)} == {first, second, bobs}
        assert store.revoke_owner("alice") == 2
        assert [t.id for t in store.list_tokens(None)] == [bobs]

    def test_ten_failures_lock_an_id_for_a_minute(
        self, store: TokenStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token, token_id = _create(store)
        clock = [1000.0]
        monkeypatch.setattr(bi_tokens.time, "monotonic", lambda: clock[0])
        for _ in range(10):
            with pytest.raises(TokenInvalidError):
                store.authenticate(f"obsl_tok_{token_id}_{'0' * 40}")
        with pytest.raises(TokenInvalidError, match="not valid"):
            store.authenticate(token)  # the right secret, refused for now
        clock[0] += 60
        assert store.authenticate(token).token_id == token_id

    def test_counting_failures_keeps_a_bounded_number_of_ids(
        self, store: TokenStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flood of made-up ids forgets the oldest; it does not grow, nor get slower."""
        monkeypatch.setattr(bi_tokens, "_MAX_TRACKED_IDS", 3)
        token, token_id = _create(store)
        for _ in range(10):
            with pytest.raises(TokenInvalidError):
                store.authenticate(f"obsl_tok_{token_id}_{'0' * 40}")
        with pytest.raises(TokenInvalidError):
            store.authenticate(token)  # locked
        made_up = [f"{n:012x}" for n in range(3)]
        for other in made_up:
            with pytest.raises(TokenInvalidError):
                store.authenticate(f"obsl_tok_{other}_{'0' * 40}")
        assert list(store._failures) == made_up  # the newest three, oldest first
        assert store.authenticate(token).token_id == token_id

    def test_an_open_connection_can_check_its_token_again(
        self, store: TokenStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(TokenInvalidError, match="not valid"):
            store.check_active("aaaaaaaaaaaa")  # no store file yet
        _, token_id = _create(store, days=1)
        store.check_active(token_id)
        with pytest.raises(TokenInvalidError, match="not valid"):
            store.check_active("aaaaaaaaaaaa")
        now = bi_tokens.time.time()
        monkeypatch.setattr(bi_tokens.time, "time", lambda: now + 86401)
        with pytest.raises(TokenInvalidError, match="expired"):
            store.check_active(token_id)
        store.revoke(token_id, owner_sub=None)
        with pytest.raises(TokenInvalidError, match="revoked"):
            store.check_active(token_id)

    def test_last_used_is_written_at_most_once_a_minute(
        self, store: TokenStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token, _ = _create(store)
        now = [bi_tokens.time.time()]
        monkeypatch.setattr(bi_tokens.time, "time", lambda: now[0])
        store.authenticate(token)
        first = store.list_tokens("alice")[0].last_used_at
        now[0] += 30
        store.authenticate(token)
        assert store.list_tokens("alice")[0].last_used_at == first
        now[0] += 31
        store.authenticate(token)
        assert store.list_tokens("alice")[0].last_used_at == now[0]

    def test_the_token_itself_is_never_stored(self, store: TokenStore) -> None:
        token, _ = _create(store)
        stored = store.path.read_bytes()
        for wal in store.path.parent.glob("*-wal"):
            stored += wal.read_bytes()
        assert token[-40:].encode() not in stored
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    def test_the_scram_verifier_is_the_token_s(self, store: TokenStore) -> None:
        """Computed at creation, the only time the token is known (for pgwire SCRAM)."""
        token, token_id = _create(store)
        with sqlite3.connect(store.path) as conn:
            salt, iterations, stored_key, server_key = conn.execute(
                "SELECT scram_salt, scram_iterations, scram_stored_key, scram_server_key "
                "FROM bi_tokens WHERE id = ?",
                (token_id,),
            ).fetchone()
        assert iterations == 4096
        assert (stored_key, server_key) == scram_keys(token, salt, iterations)

    def test_a_postgres_user_name_names_a_token_by_its_id(self) -> None:
        assert token_id_of_user("3f2a9c41b7e0") == "3f2a9c41b7e0"
        assert token_id_of_user("tok_3f2a9c41b7e0") == "3f2a9c41b7e0"
        for user in ("obsl", "", "3F2A9C41B7E0", "3f2a9c41b7e", "obsl_tok_3f2a9c41b7e0"):
            assert token_id_of_user(user) is None, user

    def test_a_scram_login_uses_the_verifier_stored_with_the_token(self, store: TokenStore) -> None:
        token, token_id = _create(store)
        verifier = store.scram_verifier(token_id)
        assert (verifier.stored_key, verifier.server_key) == scram_keys(
            token, verifier.salt, verifier.iterations
        )
        assert store.scram_login(token_id, proven=True).sub == "alice"

    def test_an_unknown_id_gets_a_decoy_no_password_matches(self, store: TokenStore) -> None:
        """Its salt stays the same, as a real id's does, so the id's existence does not show."""
        _create(store)
        first, again = store.scram_verifier("aaaaaaaaaaaa"), store.scram_verifier("aaaaaaaaaaaa")
        assert first.salt == again.salt and first.iterations == 4096
        assert first.stored_key != again.stored_key  # nothing a proof could be made for
        with pytest.raises(TokenInvalidError, match="not valid"):
            store.scram_login("aaaaaaaaaaaa", proven=True)

    def test_failed_scram_proofs_lock_the_id_and_revoking_stops_it(
        self, store: TokenStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, token_id = _create(store)
        clock = [1000.0]
        monkeypatch.setattr(bi_tokens.time, "monotonic", lambda: clock[0])
        for _ in range(10):
            with pytest.raises(TokenInvalidError, match="not valid"):
                store.scram_login(token_id, proven=False)
        with pytest.raises(TokenInvalidError, match="not valid"):
            store.scram_login(token_id, proven=True)  # locked for now
        clock[0] += 60
        assert store.scram_login(token_id, proven=True).token_id == token_id
        store.revoke(token_id, owner_sub=None)
        with pytest.raises(TokenInvalidError, match="revoked"):
            store.scram_login(token_id, proven=True)

    def test_no_file_until_the_first_token(self, store: TokenStore) -> None:
        with pytest.raises(TokenInvalidError):
            store.authenticate(f"obsl_tok_{'a' * 12}_{'b' * 40}")
        assert store.list_tokens(None) == []
        assert not store.revoke("aaaaaaaaaaaa", owner_sub=None)
        store.scram_verifier("aaaaaaaaaaaa")
        assert not store.path.exists()


@pytest.fixture
def oidc_with_tokens(tmp_path: Path) -> Iterator[TokenStore]:
    config = OidcConfig(
        issuer=ISSUER, audience=AUDIENCE, token_store_path=str(tmp_path / "bi.sqlite")
    )
    init_auth(
        auth_mode="oidc",
        oidc=config,
        verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
    )
    from orionbelt.auth import get_token_store

    store = get_token_store()
    assert store is not None
    yield store
    reset_auth()


class TestAuthenticate:
    def test_only_a_bi_token_is_checked_again(self, oidc_with_tokens: TokenStore) -> None:
        check_still_valid(None)
        check_still_valid(Principal(kind="user", sub="alice", scopes=READ))  # no BI token
        _, token_id = _create(oidc_with_tokens)
        signed_in = Principal(kind="user", sub="alice", scopes=READ, token_id=token_id)
        check_still_valid(signed_in)
        oidc_with_tokens.revoke(token_id, owner_sub="alice")
        with pytest.raises(TokenInvalidError, match="revoked"):
            check_still_valid(signed_in)

    def test_under_oidc_a_bi_token_is_its_owner(self, oidc_with_tokens: TokenStore) -> None:
        token, token_id = _create(oidc_with_tokens)
        assert authenticate(token).token_id == token_id

    def test_without_a_store_it_is_refused(self) -> None:
        config = OidcConfig(issuer=ISSUER, audience=AUDIENCE)  # no TOKEN_STORE_PATH
        init_auth(
            auth_mode="oidc",
            oidc=config,
            verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
        )
        try:
            with pytest.raises(TokenInvalidError, match="not enabled"):
                authenticate(f"obsl_tok_{'a' * 12}_{'b' * 40}")
        finally:
            reset_auth()

    def test_under_api_key_it_is_no_key(self, store: TokenStore) -> None:
        token, _ = _create(store)
        init_auth(auth_mode="api_key", api_keys="obsl_pat_" + "0123456789abcdef" * 3)
        try:
            with pytest.raises(AuthInvalidError):
                authenticate(token)
        finally:
            reset_auth()


class TestNotExchangedForAWarehouseToken:
    """T5: a BI token is no identity provider token, so a source that logs in as
    the user gets nothing to exchange (and answers SIGN_IN_REQUIRED)."""

    def test_pgwire_accepts_it_over_tls_and_keeps_no_credential(
        self, oidc_with_tokens: TokenStore
    ) -> None:
        token, token_id = _create(oidc_with_tokens)
        result = pgwire_authenticate(startup=STARTUP, password=token, tls=True)
        assert result.ok
        assert result.principal is not None and result.principal.token_id == token_id
        assert result.credential is None

    def test_pgwire_refuses_it_without_tls(self, oidc_with_tokens: TokenStore) -> None:
        token, _ = _create(oidc_with_tokens)
        result = pgwire_authenticate(startup=STARTUP, password=token, tls=False)
        assert not result.ok
        assert "only over TLS" in (result.error_message or "")

    def test_flight_hands_no_credential_on(self) -> None:
        pytest.importorskip("ob_flight")
        from ob_flight.auth import AUTH_MIDDLEWARE_KEY, _AuthMiddleware, user_credential_of

        def context(caller: Principal, credential: str) -> SimpleNamespace:
            middleware = _AuthMiddleware(credential, caller=caller)
            return SimpleNamespace(
                get_middleware=lambda key: middleware if key == AUTH_MIDDLEWARE_KEY else None
            )

        signed_in = Principal(kind="user", sub="alice", scopes=READ)
        by_token = Principal(kind="user", sub="alice", scopes=READ, token_id="a" * 12)
        assert user_credential_of(context(signed_in, "the-idp-token")) == "the-idp-token"
        assert user_credential_of(context(by_token, "obsl_tok_...")) is None
