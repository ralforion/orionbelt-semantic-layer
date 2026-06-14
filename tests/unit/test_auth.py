"""Unit tests for the shared auth subsystem (design/PLAN_authentication.md Phase 1)."""

from __future__ import annotations

import pytest

from orionbelt.auth import (
    AuthConfigError,
    AuthInvalidError,
    AuthRequiredError,
    authenticate,
    get_header_name,
    get_mode,
    init_auth,
    reset_auth,
    resolve_mode,
)
from orionbelt.auth.keys import (
    KeyStore,
    find_low_strength_keys,
    find_weak_keys,
    parse_keys,
)

VALID_KEY = "obsl_pat_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # 49 chars, high entropy


@pytest.fixture(autouse=True)
def _reset() -> None:
    """Ensure each test starts and ends with auth reset to defaults."""
    reset_auth()
    yield
    reset_auth()


# --- key store helpers ---


class TestKeyStore:
    def test_parse_keys_strips_and_dedupes(self) -> None:
        keys = parse_keys(" a , b ,a, ,b ")
        assert keys == frozenset({"a", "b"})

    def test_parse_keys_empty(self) -> None:
        assert parse_keys("") == frozenset()

    def test_find_weak_keys_masks_prefix(self) -> None:
        weak = find_weak_keys(frozenset({"short", VALID_KEY}))
        assert weak == ["shor..."]  # only the <16 char key, masked

    def test_find_low_strength_flags_short(self) -> None:
        # 20 chars: passes the 16 hard floor but below the 32 recommendation.
        strong = "obsl_pat_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # 49 chars, diverse
        low = find_low_strength_keys(frozenset({"0123456789abcdef0123", strong}))
        assert low == ["0123..."]  # only the short key flagged; the 49-char one passes

    def test_find_low_strength_flags_low_entropy(self) -> None:
        # Long but only one distinct character -> low entropy.
        low = find_low_strength_keys(frozenset({"a" * 40}))
        assert low == ["aaaa..."]

    def test_validate_matches(self) -> None:
        store = KeyStore(frozenset({VALID_KEY, "another_long_key_here"}))
        assert store.validate(VALID_KEY) is True
        assert store.validate("wrong") is False
        assert store.validate("") is False


# --- mode resolution + legacy alias ---


class TestResolveMode:
    def test_default_none(self) -> None:
        assert resolve_mode("none", False) == "none"

    def test_explicit_api_key(self) -> None:
        assert resolve_mode("api_key", False) == "api_key"

    def test_auth_enabled_alias(self) -> None:
        assert resolve_mode("none", True) == "api_key"

    def test_explicit_mode_wins_over_alias(self) -> None:
        # AUTH_MODE explicitly set is honoured even with the legacy flag on.
        assert resolve_mode("oidc", True) == "oidc"

    def test_case_insensitive(self) -> None:
        assert resolve_mode("API_KEY", False) == "api_key"


# --- init_auth validation ---


class TestInitAuth:
    def test_none_mode(self) -> None:
        init_auth(auth_mode="none")
        assert get_mode() == "none"

    def test_api_key_mode(self) -> None:
        init_auth(auth_mode="api_key", api_keys=VALID_KEY)
        assert get_mode() == "api_key"

    def test_custom_header(self) -> None:
        init_auth(auth_mode="api_key", api_keys=VALID_KEY, header_name="X-Custom-Key")
        assert get_header_name() == "X-Custom-Key"

    def test_api_key_empty_keys_raises(self) -> None:
        with pytest.raises(AuthConfigError, match="API_KEYS is empty"):
            init_auth(auth_mode="api_key", api_keys="")

    def test_api_key_weak_key_raises(self) -> None:
        with pytest.raises(AuthConfigError, match="at least 16 characters"):
            init_auth(auth_mode="api_key", api_keys="tooshort")

    def test_api_key_low_strength_rejected(self) -> None:
        # Passes the 16-char floor but < 32 chars -> hard error, not just a warning.
        with pytest.raises(AuthConfigError, match="too weak"):
            init_auth(auth_mode="api_key", api_keys="0123456789abcdef0123")  # 20 chars

    def test_api_key_low_entropy_rejected(self) -> None:
        with pytest.raises(AuthConfigError, match="too weak"):
            init_auth(auth_mode="api_key", api_keys="a" * 40)  # long but 1 distinct char

    def test_weak_key_error_does_not_leak_full_key(self) -> None:
        secret = "supersecretbutshort"[:10]  # 10 chars
        with pytest.raises(AuthConfigError) as exc:
            init_auth(auth_mode="api_key", api_keys=secret)
        assert secret not in str(exc.value)

    def test_oidc_mode_rejected_in_phase1(self) -> None:
        with pytest.raises(AuthConfigError, match="oidc is not implemented"):
            init_auth(auth_mode="oidc")

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(AuthConfigError, match="Invalid AUTH_MODE"):
            init_auth(auth_mode="bogus")

    def test_legacy_auth_enabled_alias(self) -> None:
        init_auth(auth_mode="none", api_keys=VALID_KEY, auth_enabled=True)
        assert get_mode() == "api_key"


# --- authenticate dispatch ---


class TestAuthenticate:
    def test_none_returns_anonymous(self) -> None:
        init_auth(auth_mode="none")
        principal = authenticate(None)
        assert principal.kind == "anonymous"
        # full scopes so existing routes keep working
        assert principal.has_scope("obsl:write")

    def test_none_ignores_credential(self) -> None:
        init_auth(auth_mode="none")
        assert authenticate("anything").kind == "anonymous"

    def test_api_key_valid(self) -> None:
        init_auth(auth_mode="api_key", api_keys=VALID_KEY)
        principal = authenticate(VALID_KEY)
        assert principal.kind == "api_key"
        assert principal.has_scope("obsl:read")

    def test_api_key_missing_raises_required(self) -> None:
        init_auth(auth_mode="api_key", api_keys=VALID_KEY)
        with pytest.raises(AuthRequiredError):
            authenticate(None)

    def test_api_key_wrong_raises_invalid(self) -> None:
        init_auth(auth_mode="api_key", api_keys=VALID_KEY)
        with pytest.raises(AuthInvalidError):
            authenticate("wrong-key-1234567890")

    def test_multiple_keys_each_valid(self) -> None:
        k2 = "obsl_pat_f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d"  # 49 chars, strong
        init_auth(auth_mode="api_key", api_keys=f"{VALID_KEY},{k2}")
        assert authenticate(VALID_KEY).kind == "api_key"
        assert authenticate(k2).kind == "api_key"
