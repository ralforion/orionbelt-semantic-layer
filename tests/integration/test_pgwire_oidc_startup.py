"""pgwire refuses to start under AUTH_MODE=oidc when no client could log in."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from orionbelt.auth import AuthConfigError, OidcConfig, init_auth, reset_auth
from orionbelt.auth.jwt_verify import JwtVerifier
from orionbelt.pgwire.startup import start_pgwire
from orionbelt.settings import Settings
from tests.integration.test_pgwire_tls import _pki
from tests.oidc_tokens import AUDIENCE, ISSUER, CountingFetch, FakeIssuer


@pytest.fixture(autouse=True)
def oidc_without_keys() -> Iterator[None]:
    config = OidcConfig(issuer=ISSUER, audience=AUDIENCE)
    init_auth(
        auth_mode="oidc",
        oidc=config,
        verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
    )
    yield
    reset_auth()


def _settings(**overrides: object) -> Settings:
    return Settings(pgwire_enabled=True, pgwire_host="127.0.0.1", pgwire_port=0, **overrides)  # type: ignore[arg-type]


async def test_no_api_keys_and_no_tls_refuses_to_start() -> None:
    with pytest.raises(AuthConfigError, match="needs API_KEYS, PGWIRE_TLS_CERT or TOKEN_STORE"):
        await start_pgwire(_settings())


async def test_tls_alone_is_enough(tmp_path: Path) -> None:
    pki = _pki(tmp_path)
    runtime = await start_pgwire(_settings(pgwire_tls_cert=pki["cert"], pgwire_tls_key=pki["key"]))
    assert runtime is not None
    await runtime.shutdown()


async def test_a_token_store_alone_is_enough(tmp_path: Path) -> None:
    """BI tokens log in by SCRAM, with the token's id as the user name: no TLS needed."""
    config = OidcConfig(
        issuer=ISSUER, audience=AUDIENCE, token_store_path=str(tmp_path / "bi.sqlite")
    )
    init_auth(
        auth_mode="oidc",
        oidc=config,
        verifier=JwtVerifier(config, fetch=CountingFetch(FakeIssuer())),
    )
    runtime = await start_pgwire(_settings())
    assert runtime is not None
    await runtime.shutdown()
