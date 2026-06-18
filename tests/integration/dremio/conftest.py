"""Fixtures for the Dremio-as-pgwire-client integration suite.

Stage 1 only: Dremio reaches into OBSL's pgwire surface as if OBSL were
a vanilla Postgres database. The compose file owns lifecycle for both
containers; these fixtures discover the running endpoints, bootstrap
Dremio's first admin user, and register an OBSL Postgres source via
Dremio's REST API.

Tests are opt-in via the ``dremio`` marker. To run::

    docker compose -f tests/integration/dremio/docker-compose.yml up -d --build
    uv run pytest -m dremio
    docker compose -f tests/integration/dremio/docker-compose.yml down -v

Or, in one shot::

    tests/integration/dremio/run.sh
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx
import pytest

# Endpoints — overridable via env for CI or non-default ports.
DREMIO_REST_URL = os.environ.get("DREMIO_REST_URL", "http://localhost:19047")
OBSL_PGWIRE_HOST = os.environ.get("OBSL_PGWIRE_HOST", "obsl")  # docker network alias
OBSL_PGWIRE_PORT = int(os.environ.get("OBSL_PGWIRE_PORT", "5432"))
# Defaults to ``commerce`` (the model name served by the demo stack in
# ``demo/dremio/``, the usual local target). The dedicated test stack in
# this directory bakes the ``orionbelt_1_commerce`` example, so ``run.sh``
# exports ``OBSL_MODEL_NAME=orionbelt_1_commerce`` to match it.
OBSL_MODEL_NAME = os.environ.get("OBSL_MODEL_NAME", "commerce")
# Stage-2: Dremio-backed model. OBSL compiles to Dremio SQL and executes
# back against the same Dremio container via the ob-dremio Flight driver.
OBSL_STAGE2_MODEL_NAME = os.environ.get("OBSL_STAGE2_MODEL_NAME", "dremio_info_schema")

# Dremio first-user bootstrap. The container has no preexisting admin
# until the very first PUT /apiv2/bootstrap/firstuser succeeds.
DREMIO_ADMIN_USER = "obsl_admin"
DREMIO_ADMIN_PASS = "obsl_admin_pw_123!"  # noqa: S105 — test container only
DREMIO_SOURCE_NAME = "obsl_pg"

_BOOTSTRAP_TIMEOUT_SECONDS = 180
_BOOTSTRAP_POLL_INTERVAL = 2.0


@dataclass(frozen=True)
class DremioSession:
    """Authenticated handle on a Dremio container."""

    base_url: str
    token: str
    source_name: str

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"_dremio{self.token}"}


def _server_ready(client: httpx.Client) -> bool:
    """Return True once Dremio reports a usable HTTP layer."""

    try:
        resp = client.get("/apiv2/server_status", timeout=3.0)
    except httpx.RequestError:
        return False
    return resp.status_code == 200


def _bootstrap_first_user(client: httpx.Client) -> str:
    """Create the first admin user (idempotent) and return a login token.

    ``PUT /apiv2/bootstrap/firstuser`` returns a User object **without**
    a token — the token always comes from a subsequent
    ``POST /apiv2/login``. On a re-run the firstuser call fails with an
    error message; that's expected and we proceed straight to login.
    """

    payload = {
        "userName": DREMIO_ADMIN_USER,
        "firstName": "OBSL",
        "lastName": "Tester",
        "email": "obsl@example.invalid",
        "createdAt": int(time.time() * 1000),
        "password": DREMIO_ADMIN_PASS,
    }
    client.put("/apiv2/bootstrap/firstuser", json=payload, timeout=10.0)

    login = client.post(
        "/apiv2/login",
        json={"userName": DREMIO_ADMIN_USER, "password": DREMIO_ADMIN_PASS},
        timeout=10.0,
    )
    if login.status_code != 200:
        raise RuntimeError(
            f"Dremio login failed after bootstrap: {login.status_code} {login.text!r}"
        )
    return str(login.json()["token"])


def _ensure_postgres_source(client: httpx.Client, token: str) -> None:
    """Create the OBSL Postgres source if it doesn't already exist."""

    headers = {"Authorization": f"_dremio{token}"}
    existing = client.get(
        f"/api/v3/catalog/by-path/{DREMIO_SOURCE_NAME}",
        headers=headers,
        timeout=10.0,
    )
    if existing.status_code == 200:
        return

    body = {
        "entityType": "source",
        "name": DREMIO_SOURCE_NAME,
        "type": "POSTGRES",
        "config": {
            "hostname": OBSL_PGWIRE_HOST,
            "port": str(OBSL_PGWIRE_PORT),
            "databaseName": OBSL_MODEL_NAME,
            "authenticationType": "MASTER",
            "username": "obsl",
            "password": "obsl",  # trust-auth: any password accepted
            "useSsl": False,
            "fetchSize": 200,
        },
        "metadataPolicy": {
            "authTTLMs": 86_400_000,
            "namesRefreshMs": 3_600_000,
            "datasetRefreshAfterMs": 3_600_000,
            "datasetExpireAfterMs": 10_800_000,
            "datasetUpdateMode": "PREFETCH_QUERIED",
        },
    }
    created = client.post(
        "/api/v3/catalog",
        json=body,
        headers=headers,
        timeout=30.0,
    )
    if created.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to register OBSL as a Postgres source in Dremio: "
            f"{created.status_code} {created.text!r}"
        )


@pytest.fixture(scope="session")
def dremio_session() -> Iterator[DremioSession]:
    """Wait for Dremio + OBSL, bootstrap the admin user, register the source."""

    with httpx.Client(base_url=DREMIO_REST_URL) as client:
        # Fast probe first so a default ``pytest`` run with no stack up
        # skips immediately instead of polling for three minutes.
        if not _server_ready(client):
            pytest.skip(
                f"Dremio at {DREMIO_REST_URL} not reachable — bring the stack up with "
                "`tests/integration/dremio/run.sh` or `docker compose -f "
                "tests/integration/dremio/docker-compose.yml up -d --build`."
            )

        # Even when the HTTP layer is up, the server may still be
        # initialising metadata for ~10–30s after a cold start. Tolerate
        # a short ramp before bootstrap.
        deadline = time.monotonic() + _BOOTSTRAP_TIMEOUT_SECONDS
        while not _server_ready(client):
            if time.monotonic() > deadline:
                pytest.fail(f"Dremio at {DREMIO_REST_URL} stopped responding mid-bootstrap")
            time.sleep(_BOOTSTRAP_POLL_INTERVAL)

        token = _bootstrap_first_user(client)
        _ensure_postgres_source(client, token)

        yield DremioSession(
            base_url=DREMIO_REST_URL,
            token=token,
            source_name=DREMIO_SOURCE_NAME,
        )


def _run_sql(session: DremioSession, sql: str, timeout: float = 60.0) -> list[list[object]]:
    """Run SQL via Dremio's /api/v3/sql and return result rows."""

    with httpx.Client(base_url=session.base_url) as client:
        submit = client.post(
            "/api/v3/sql",
            json={"sql": sql},
            headers=session.headers(),
            timeout=timeout,
        )
        submit.raise_for_status()
        job_id = submit.json()["id"]

        deadline = time.monotonic() + timeout
        while True:
            status = client.get(
                f"/api/v3/job/{job_id}",
                headers=session.headers(),
                timeout=10.0,
            )
            status.raise_for_status()
            state = status.json().get("jobState")
            if state == "COMPLETED":
                break
            if state in {"FAILED", "CANCELED"}:
                raise RuntimeError(f"Dremio job {job_id} ended in {state}: {status.json()!r}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"Dremio job {job_id} did not complete in {timeout}s")
            time.sleep(0.5)

        results = client.get(
            f"/api/v3/job/{job_id}/results",
            headers=session.headers(),
            timeout=30.0,
        )
        results.raise_for_status()
        payload = results.json()
        cols = [c["name"] for c in payload.get("schema", [])]
        return [[row.get(c) for c in cols] for row in payload.get("rows", [])]


RunSql = Callable[..., list[list[object]]]


@pytest.fixture(scope="session")
def run_dremio_sql(dremio_session: DremioSession) -> RunSql:
    """Return a helper bound to the active Dremio session."""

    def _exec(sql: str, *, timeout: float = 60.0) -> list[list[object]]:
        return _run_sql(dremio_session, sql, timeout=timeout)

    return _exec
