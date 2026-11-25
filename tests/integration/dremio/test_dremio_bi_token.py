"""Dremio as a sidecar that logs in to OrionBelt with a BI token.

A team puts Dremio in front of OrionBelt for its BI tools, with OrionBelt
under ``AUTH_MODE=oidc``. A user signs in and creates a BI token for
Dremio's Postgres source; Dremio logs in with the token's id as its user name
and the token as its password (SCRAM, no TLS) and queries as that user.
Revoking the token cuts Dremio off at its next query.

Runs against ``obsl-auth`` in this directory's stack: sign-in with the key
``run.sh`` makes (``sign_in.py``), a token store, and neither API keys nor
TLS, so a BI token is the only way in.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

from tests.integration.dremio import sign_in
from tests.integration.dremio.conftest import (
    DREMIO_REST_URL,
    OBSL_MODEL_NAME,
    DremioSession,
    _run_sql,
)

pytestmark = pytest.mark.dremio

OBSL_AUTH_REST_URL = os.environ.get("OBSL_AUTH_REST_URL", "http://localhost:18081")
OBSL_AUTH_PGWIRE_HOST = os.environ.get("OBSL_AUTH_PGWIRE_HOST", "obsl-auth")  # docker alias
SOURCE = "obsl_bi_token"


def _query(limit: int) -> str:
    return (
        f'SELECT "Client Name", "Total Sales" FROM {SOURCE}."{OBSL_MODEL_NAME}".model LIMIT {limit}'
    )


@pytest.fixture(scope="module")
def alice(dremio_admin_token: str) -> dict[str, str]:
    """Alice signed in at ``obsl-auth``, as request headers.

    Skips unless that OrionBelt is up with sign-in (``dremio_admin_token``
    first: with no stack at all, it skips at once).
    """
    trusted = sign_in.issuer()
    if trusted is None:
        pytest.skip("No signing key for obsl-auth: bring the stack up with run.sh")
    deadline = time.monotonic() + 60
    while True:
        try:
            health = httpx.get(f"{OBSL_AUTH_REST_URL}/health", timeout=5.0).json()
            break
        except httpx.HTTPError as exc:
            if time.monotonic() > deadline:
                pytest.skip(f"obsl-auth is not reachable at {OBSL_AUTH_REST_URL} ({exc})")
            time.sleep(2)
    if health.get("auth_mode") != "oidc":
        pytest.skip(f"{OBSL_AUTH_REST_URL} does not run AUTH_MODE=oidc: {health}")
    token = trusted.token("alice", scope="obsl:read", expires_in=900, email="alice@example.test")
    return {"Authorization": f"Bearer {token}"}


def _source(user: str, password: str) -> dict[str, Any]:
    return {
        "entityType": "source",
        "name": SOURCE,
        "type": "POSTGRES",
        "config": {
            "hostname": OBSL_AUTH_PGWIRE_HOST,
            "port": "5432",
            "databaseName": OBSL_MODEL_NAME,
            "authenticationType": "MASTER",
            "username": user,
            "password": password,
            "useSsl": False,
            "fetchSize": 200,
        },
    }


def _drop_source(client: httpx.Client) -> None:
    found = client.get(f"/api/v3/catalog/by-path/{SOURCE}", timeout=10.0)
    if found.status_code == 200:
        entity = found.json()
        client.delete(
            f"/api/v3/catalog/{entity['id']}", params={"tag": entity.get("tag")}, timeout=30.0
        ).raise_for_status()


def test_dremio_queries_as_the_token_s_owner_until_it_is_revoked(
    dremio_admin_token: str, alice: dict[str, str]
) -> None:
    created = httpx.post(
        f"{OBSL_AUTH_REST_URL}/v1/auth/tokens",
        headers=alice,
        json={"label": "Dremio sidecar", "days": 1},
        timeout=10.0,
    )
    assert created.status_code == 201, created.text
    token, token_id = created.json()["token"], created.json()["id"]

    dremio = DremioSession(base_url=DREMIO_REST_URL, token=dremio_admin_token, source_name=SOURCE)
    with httpx.Client(base_url=DREMIO_REST_URL, headers=dremio.headers()) as client:
        _drop_source(client)  # left over from an interrupted run
        registered = client.post("/api/v3/catalog", json=_source(token_id, token), timeout=60.0)
        assert registered.status_code in (200, 201), registered.text
        try:
            rows = _run_sql(dremio, _query(5))
            assert rows, "Dremio got no rows through the BI token's login"
            assert len(rows[0]) == 2

            revoked = httpx.delete(
                f"{OBSL_AUTH_REST_URL}/v1/auth/tokens/{token_id}", headers=alice, timeout=10.0
            )
            assert revoked.status_code == 204, revoked.text
            with pytest.raises(RuntimeError, match="BI token was revoked"):
                _run_sql(dremio, _query(6))  # another query: nothing Dremio could reuse
        finally:
            _drop_source(client)
