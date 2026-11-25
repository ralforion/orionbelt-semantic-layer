"""Who did what: the audit log on REST, the Postgres wire protocol and Flight SQL.

See design/PLAN_auth_phase6.md (A5-A7): off by default; one event per REST
``/v1`` request, per pgwire login and semantic query, per Flight sign-in and
refusal; never a credential or a value of the query.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app, oidc_config
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.auth import AuthConfigError, Principal, init_auth, reset_auth
from orionbelt.auth import audit as audit_module
from orionbelt.auth.audit import FileSink, configure_audit, record, reset_audit
from orionbelt.auth.keys import key_id
from orionbelt.pgwire.router import SemanticRouter
from orionbelt.pgwire.server import PgWireServer
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML
from tests.integration.test_pgwire_oidc import _serve

KEY = "obsl_pat_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    reset_audit()
    reset_auth()


@pytest.fixture
def audit_file(tmp_path: Path) -> Path:
    path = tmp_path / "audit" / "events.jsonl"
    configure_audit("file", str(path))
    return path


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestConfiguration:
    def test_off_by_default_and_on_request(self, tmp_path: Path) -> None:
        assert not audit_module.audit_enabled()
        configure_audit("log")
        assert audit_module.audit_enabled()
        configure_audit("off")
        assert not audit_module.audit_enabled()

    @pytest.mark.parametrize(
        ("mode", "path", "match"),
        [
            ("loud", "", "AUDIT_LOG='loud'"),
            ("file", "", "needs AUDIT_LOG_PATH"),
            ("file", "/proc/no/such/dir/events.jsonl", "cannot be written"),
        ],
    )
    def test_a_wrong_setting_refuses_to_start(self, mode: str, path: str, match: str) -> None:
        with pytest.raises(AuthConfigError, match=match):
            configure_audit(mode, path)

    def test_a_failing_sink_never_breaks_the_request(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = FileSink(str(tmp_path / "events.jsonl"))
        audit_module._sink = sink
        sink.path = tmp_path / "missing-dir" / "x" / "events.jsonl"  # cannot be opened now
        record(surface="rest", action="GET /v1/sessions", outcome="200")
        assert "Audit event lost" in caplog.text

    def test_the_log_keeps_events_when_the_application_logs_warnings_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from orionbelt.api.logging_config import configure_logging

        configure_logging("WARNING", "json")
        try:
            configure_audit("log")
            record(surface="rest", action="GET /v1/sessions", outcome="200")
            out = capsys.readouterr().out
        finally:
            configure_logging("INFO", "console")
        (line,) = [json.loads(x) for x in out.splitlines() if '"audit"' in x]
        assert line["event"] == "audit" and line["action"] == "GET /v1/sessions"

    def test_the_log_sink_writes_the_event_s_fields(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        sink = audit_module.LogSink()
        sink._log = type("Stub", (), {"info": lambda self, e, **kw: calls.append((e, kw))})()
        audit_module._sink = sink
        record(
            surface="rest",
            action="GET /v1/sessions",
            outcome="200",
            principal=Principal(kind="user", sub="alice", token_id="3f2a9c41b7e0"),
        )
        ((event, fields),) = calls
        assert event == "audit"
        assert fields["who"] == {"kind": "user", "sub": "alice", "token_id": "3f2a9c41b7e0"}


@asynccontextmanager
async def _rest(settings: Settings, *, raise_errors: bool = True) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=settings)
    init_session_manager(
        SessionManager(ttl_seconds=3600, cleanup_interval=9999),
        auth_mode=settings.auth_mode,
        api_keys=settings.api_keys,
        oidc=oidc_config(settings),
    )
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=raise_errors)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        reset_session_manager()


class TestRest:
    async def test_each_v1_request_who_what_on_what_and_the_outcome(self, audit_file: Path) -> None:
        async with _rest(Settings(auth_mode="api_key", api_keys=KEY)) as client:
            key = {"X-API-Key": KEY, "X-Request-ID": "req-1"}
            session = (await client.post("/v1/sessions", headers=key)).json()["session_id"]
            await client.get(f"/v1/sessions/{session}", headers=key)
            await client.get(
                "/v1/sessions", headers={"X-API-Key": "obsl_pat_wrong_key_value_0000000"}
            )
            await client.get("/health")
        created, read, refused = _events(audit_file)
        assert created["surface"] == "rest"
        assert created["action"] == "POST /v1/sessions"
        assert created["outcome"] == "201"
        assert created["who"] == {"kind": "api_key", "key_id": key_id(KEY)}
        assert created["request_id"] == "req-1"
        assert created["client"]
        assert read["action"] == "GET /v1/sessions/{session_id}"
        assert read["target"] == {"session_id": session}
        assert refused["outcome"] == "403"
        assert refused["reason"] == "AUTH_INVALID"
        assert refused["who"] == {"kind": "anonymous"}
        assert KEY not in audit_file.read_text()  # /health is not audited either

    async def test_a_missing_scope_names_who_was_refused(
        self, audit_file: Path, tmp_path: Path
    ) -> None:
        from tests.oidc_tokens import AUDIENCE, ISSUER, FakeIssuer

        issuer = FakeIssuer()
        jwks = tmp_path / "jwks.json"
        jwks.write_text(json.dumps(issuer.jwks()))
        settings = Settings(
            auth_mode="oidc", oidc_issuer=ISSUER, oidc_audience=AUDIENCE, oidc_jwks_file=str(jwks)
        )
        async with _rest(settings) as client:
            token = issuer.token("alice", scope="obsl:read")
            await client.get("/v1/auth/status", headers={"Authorization": f"Bearer {token}"})
        (event,) = _events(audit_file)
        assert event["outcome"] == "403"
        assert event["reason"] == "AUTH_INSUFFICIENT_SCOPE"
        assert event["who"] == {"kind": "user", "sub": "alice"}
        assert token not in audit_file.read_text()

    async def test_an_unhandled_failure_is_still_on_record(
        self, audit_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the session store failed")

        monkeypatch.setattr(SessionManager, "list_sessions", broken)
        async with _rest(Settings(auth_mode="api_key", api_keys=KEY), raise_errors=False) as client:
            answer = await client.get("/v1/sessions", headers={"X-API-Key": KEY})
        assert answer.status_code == 500
        (event,) = _events(audit_file)
        assert (event["action"], event["outcome"]) == ("GET /v1/sessions", "500")
        assert event["reason"] == "RuntimeError"
        assert event["who"]["key_id"] == key_id(KEY)
        assert "the session store failed" not in audit_file.read_text()

    async def test_nothing_is_recorded_when_off(self, tmp_path: Path) -> None:
        async with _rest(Settings(auth_mode="api_key", api_keys=KEY)) as client:
            await client.get("/v1/sessions", headers={"X-API-Key": KEY})
        assert not audit_module.audit_enabled()


@pytest.fixture
def pgwire(audit_file: Path) -> Iterator[tuple[int, str]]:
    init_auth(auth_mode="api_key", api_keys=KEY)
    manager = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    session = manager.create_session().session_id
    manager.get_store(session).load_model(SAMPLE_MODEL_YAML, dedup=False)
    server = PgWireServer(
        host="127.0.0.1",
        port=0,
        query_handler=SemanticRouter(session_manager=manager, default_dialect="duckdb").handle,
    )
    for port in _serve(server):
        yield port, session


class TestPgwire:
    def test_logins_and_semantic_queries_not_catalog_probes(
        self, pgwire: tuple[int, str], audit_file: Path
    ) -> None:
        port, session = pgwire
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(
                host="127.0.0.1", port=port, user="obsl", password="wrong", dbname=session
            )
        with psycopg.connect(
            host="127.0.0.1", port=port, user="obsl", password=KEY, dbname=session, autocommit=True
        ) as conn:
            conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
            conn.execute("SELECT 1").fetchall()
            with pytest.raises(psycopg.Error):  # no data behind the sample model
                conn.execute('SELECT "Customer Country" FROM model').fetchall()
        refused, login, query = _events(audit_file)
        assert (refused["action"], refused["outcome"]) == ("login", "refused")
        assert refused["reason"] == "SCRAM authentication failed"
        assert refused["target"] == {"database": session, "user": "obsl"}
        assert (login["action"], login["outcome"]) == ("login", "ok")
        assert login["who"] == {"kind": "api_key", "key_id": key_id(KEY)}
        assert login["client"] == "127.0.0.1"
        assert (query["action"], query["surface"]) == ("query", "pgwire")
        assert query["outcome"] == "error"
        assert query["who"]["key_id"] == key_id(KEY)
        assert "Customer Country" not in audit_file.read_text()  # never the query


class TestFlight:
    @pytest.fixture(autouse=True)
    def _flight(self) -> None:
        pytest.importorskip("ob_flight")

    def test_a_handshake_is_a_sign_in_and_other_calls_record_only_refusals(
        self, audit_file: Path
    ) -> None:
        from ob_flight.auth import CredentialRejected, _check
        from pyarrow import flight

        caller = Principal(kind="user", sub="alice")

        def ok(credential: str) -> Principal:
            return caller

        def refuse(credential: str) -> Principal:
            raise CredentialRejected("Invalid API key")

        assert _check(ok, "token", sign_in=True) is caller
        assert _check(ok, "token") is caller  # an ordinary call: not recorded
        with pytest.raises(flight.FlightUnauthenticatedError):
            _check(refuse, "token")
        sign_in, refused = _events(audit_file)
        assert (sign_in["action"], sign_in["outcome"]) == ("sign-in", "ok")
        assert sign_in["who"] == {"kind": "user", "sub": "alice"}
        assert (refused["action"], refused["outcome"]) == ("call", "refused")
        assert refused["reason"] == "invalid_credential"

    def test_a_query_names_its_model_and_source(self, audit_file: Path) -> None:
        from types import SimpleNamespace

        from ob_flight.server import _audit_query

        _audit_query({"model_id": "m1", "source": SimpleNamespace(name="crm")}, outcome="ok")
        (event,) = _events(audit_file)
        assert (event["surface"], event["action"], event["outcome"]) == ("flight", "query", "ok")
        assert event["target"] == {"model_id": "m1", "data_source": "crm"}
