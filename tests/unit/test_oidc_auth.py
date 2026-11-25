"""AUTH_MODE=oidc: scopes from claims, and API keys next to tokens (D2)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from orionbelt.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    AuthConfigError,
    AuthInvalidError,
    AuthRequiredError,
    OidcConfig,
    TokenInvalidError,
    authenticate,
    init_auth,
    parse_scope_claims,
    parse_scope_map,
    reset_auth,
    validate_api_key,
)
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.auth.keys import key_id
from orionbelt.auth.oidc import scopes_from_claims
from orionbelt.auth.principal import API_KEY_PRINCIPAL
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer

SERVICE_KEY = "obsl_pat_service_key_0123456789abcdef0123"
CONFIG = OidcConfig(issuer=ISSUER, audience=AUDIENCE)


class TestScopesFromClaims:
    def test_space_separated_scope(self) -> None:
        assert scopes_from_claims({"scope": "openid obsl:read"}, CONFIG) == {SCOPE_READ}

    def test_scp_and_roles_lists(self) -> None:
        claims = {"scp": ["obsl:read"], "roles": ["obsl:write"]}
        assert scopes_from_claims(claims, CONFIG) == {SCOPE_READ, SCOPE_WRITE}

    def test_nested_claim_path(self) -> None:
        config = OidcConfig(issuer=ISSUER, audience=AUDIENCE, scope_claims=("realm_access.roles",))
        claims = {"realm_access": {"roles": ["obsl:admin", "offline_access"]}}
        assert scopes_from_claims(claims, config) == {SCOPE_ADMIN, SCOPE_WRITE, SCOPE_READ}

    def test_idp_names_through_the_map(self) -> None:
        config = OidcConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            scope_map=parse_scope_map("analysts=obsl:read, modelers=obsl:write"),
        )
        assert scopes_from_claims({"roles": ["modelers"]}, config) == {SCOPE_WRITE, SCOPE_READ}

    def test_admin_implies_write_implies_read(self) -> None:
        assert scopes_from_claims({"scope": "obsl:admin"}, CONFIG) == {
            SCOPE_ADMIN,
            SCOPE_WRITE,
            SCOPE_READ,
        }

    def test_unknown_values_and_odd_claim_types_grant_nothing(self) -> None:
        claims = {"scope": "openid profile", "scp": 42, "roles": [None, {"x": 1}]}
        assert scopes_from_claims(claims, CONFIG) == frozenset()


class TestParsing:
    def test_scope_map_repeats_a_name_for_several_scopes(self) -> None:
        assert parse_scope_map("ops=obsl:read,ops=obsl:admin") == {
            "ops": frozenset({SCOPE_READ, SCOPE_ADMIN})
        }

    @pytest.mark.parametrize("raw", ["analysts", "=obsl:read", "analysts=", "a=obsl:delete"])
    def test_scope_map_refuses_bad_entries(self, raw: str) -> None:
        with pytest.raises(AuthConfigError):
            parse_scope_map(raw)

    def test_scope_claims(self) -> None:
        assert parse_scope_claims(" scope, realm_access.roles ,") == ("scope", "realm_access.roles")

    def test_config_needs_issuer_and_audience(self) -> None:
        with pytest.raises(AuthConfigError, match="OIDC_ISSUER"):
            OidcConfig(issuer="", audience=AUDIENCE)
        with pytest.raises(AuthConfigError, match="OIDC_AUDIENCE"):
            OidcConfig(issuer=ISSUER, audience=" ")


@pytest.fixture
def idp() -> FakeIssuer:
    return FakeIssuer()


@pytest.fixture
def oidc_with_keys(idp: FakeIssuer) -> Iterator[None]:
    init_auth(
        auth_mode="oidc",
        api_keys=SERVICE_KEY,
        oidc=CONFIG,
        verifier=JwtVerifier(CONFIG, fetch=CountingFetch(idp)),
    )
    yield
    reset_auth()


@pytest.fixture
def oidc_without_keys(idp: FakeIssuer) -> Iterator[None]:
    init_auth(auth_mode="oidc", oidc=CONFIG, verifier=JwtVerifier(CONFIG, fetch=CountingFetch(idp)))
    yield
    reset_auth()


@pytest.mark.usefixtures("oidc_with_keys")
class TestKeysNextToTokens:
    def test_token_is_a_user(self, idp: FakeIssuer) -> None:
        principal = authenticate(idp.token("bob"))
        assert (principal.kind, principal.sub) == ("user", "bob")

    def test_service_key_is_the_api_key_principal(self) -> None:
        caller = authenticate(SERVICE_KEY)
        assert caller == API_KEY_PRINCIPAL
        assert caller.key_id == key_id(SERVICE_KEY)  # which key, for the audit log

    def test_wrong_key_is_an_invalid_key_not_a_bad_token(self) -> None:
        with pytest.raises(AuthInvalidError) as excinfo:
            authenticate(SERVICE_KEY[:-1] + "X")
        assert not isinstance(excinfo.value, TokenInvalidError)

    def test_bad_token_is_a_bad_token(self, idp: FakeIssuer) -> None:
        with pytest.raises(TokenInvalidError):
            authenticate(idp.token(expires_in=-300))

    def test_missing_credential(self) -> None:
        with pytest.raises(AuthRequiredError):
            authenticate(None)

    def test_validate_api_key_checks_keys_only(self, idp: FakeIssuer) -> None:
        # What pgwire uses where a token may not be accepted (no TLS).
        assert validate_api_key(SERVICE_KEY)
        assert not validate_api_key(idp.token())


@pytest.mark.usefixtures("oidc_without_keys")
class TestTokensOnly:
    def test_non_token_credential_is_a_bad_token(self) -> None:
        with pytest.raises(TokenInvalidError, match="malformed"):
            authenticate(SERVICE_KEY)


class TestStartup:
    def test_key_with_a_dot_is_refused(self, idp: FakeIssuer) -> None:
        with pytest.raises(AuthConfigError, match="may not contain '.'"):
            init_auth(
                auth_mode="oidc",
                api_keys="obsl_pat_has.a.dot_0123456789abcdefghijklm",
                oidc=CONFIG,
                verifier=JwtVerifier(CONFIG, fetch=CountingFetch(idp)),
            )

    def test_weak_service_key_is_refused(self, idp: FakeIssuer) -> None:
        with pytest.raises(AuthConfigError, match="too weak"):
            init_auth(
                auth_mode="oidc",
                api_keys="obsl_pat_short_1",
                oidc=CONFIG,
                verifier=JwtVerifier(CONFIG, fetch=CountingFetch(idp)),
            )

    def test_unreachable_idp_leaves_the_previous_config(self, idp: FakeIssuer) -> None:
        reset_auth()
        fetch = CountingFetch(idp)
        fetch.fail = True
        with pytest.raises(AuthConfigError):
            init_auth(auth_mode="oidc", oidc=CONFIG, verifier=JwtVerifier(CONFIG, fetch=fetch))
        # Still the defaults: a failed start does not half-switch the mode.
        assert authenticate(None).kind == "anonymous"
