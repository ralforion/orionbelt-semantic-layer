"""The UI keeps working however often the model is edited, after its API
session expired, and its actions never take a model from each other.

Each edit in the model editor uploads the YAML as a new model into the UI's
API session. Once the session held ``MAX_MODELS_PER_SESSION`` models, every
further upload was refused with 429 and the UI was stuck until a page reload.
An expired session answers its first request with 410 (and is dropped then),
which every UI action reported as an error instead of continuing in a new
session. These tests drive the UI's real client helpers against the API in
process.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import get_session_manager, init_session_manager, reset_session_manager
from orionbelt.service import session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from orionbelt.ui import api_client, handlers
from tests.conftest import SAMPLE_MODEL_YAML

_API_URL = "http://testserver"
_QUERY_YAML = "select:\n  dimensions: [Customer Country]\n  measures: [Total Revenue]\n"


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., FastAPI]]:
    """Start the API with a given per-session model cap (and query execution
    on an empty in-memory DuckDB, with *execute*); route the UI to it."""

    def start(cap: int, *, execute: bool = False) -> FastAPI:
        app = create_app(settings=Settings(session_cleanup_interval=9999))
        init_session_manager(
            SessionManager(max_models_per_session=cap, cleanup_interval=9999),
            query_execute_enabled=execute,
        )

        # The helper builds its own httpx.Client; send it into the app instead.
        def in_process_client(*, base_url: str, headers: Any, **_: Any) -> httpx.Client:
            return TestClient(app, base_url=base_url, headers=headers)

        monkeypatch.setattr(api_client.httpx, "Client", in_process_client)
        return app

    yield start
    reset_session_manager()


def _edit(n: int) -> str:
    """A distinct version of the model, as an edit in the editor would make."""
    edited = SAMPLE_MODEL_YAML.replace("schema: PUBLIC", f"schema: PUBLIC_{n}", 1)
    assert edited != SAMPLE_MODEL_YAML
    return edited


def _model_status(app: FastAPI, session_id: str, model_id: str) -> int:
    return TestClient(app).get(f"/v1/sessions/{session_id}/models/{model_id}").status_code


def test_edits_past_the_cap_continue_in_a_new_session(
    serve: Callable[[int], FastAPI],
) -> None:
    app = serve(2)
    session_state = model_state = None
    used: list[tuple[str, str]] = []
    for n in range(5):
        _, session_id, model_id, session_state, model_state = api_client._ensure_session_and_model(
            _edit(n), _API_URL, session_state, model_state
        )
        assert _model_status(app, session_id, model_id) == 200
        used.append((session_id, model_id))

    # Two versions per session: edits 0-1, 2-3, 4.
    assert len({sid for sid, _ in used}) == 3
    # A full session is left as it is, so an action still using one of its
    # models keeps working.
    for session_id, model_id in used:
        assert _model_status(app, session_id, model_id) == 200


def test_concurrent_actions_keep_their_models(serve: Callable[[int], FastAPI]) -> None:
    """Two actions start from the same state with different versions of the
    model, as Gradio callbacks in separate concurrency groups do after an edit.
    Both upload before either uses its model; each must still find its own."""
    app = serve(10)
    _, _, start_id, session_state, model_state = api_client._ensure_session_and_model(
        _edit(0), _API_URL, None, None
    )
    both_uploaded = threading.Barrier(2, timeout=30)

    def action(n: int) -> int:
        _, session_id, model_id, _, _ = api_client._ensure_session_and_model(
            _edit(n), _API_URL, session_state, model_state
        )
        both_uploaded.wait()
        return _model_status(app, session_id, model_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(action, [1, 2]))

    assert statuses == [200, 200]
    # An action still holding the state from before the edits finds its model too.
    assert _model_status(app, session_state["session_id"], start_id) == 200


@pytest.mark.parametrize("prior_edits", [0, 2], ids=["new-page", "full-session"])
def test_a_rejected_model_keeps_the_session_it_was_sent_to(
    serve: Callable[[int], FastAPI], prior_edits: int
) -> None:
    """A model the API rejects (422) is retried until the user fixes it. The
    session created for the first attempt (on a new page, or because the
    session was full) must carry every later attempt; otherwise each retry
    creates another, and the session-creation rate limit (10 a minute by
    default) refuses the corrected model."""
    serve(2)
    mgr = get_session_manager()
    session_state = model_state = None
    for n in range(prior_edits):
        _, _, _, session_state, model_state = api_client._ensure_session_and_model(
            _edit(n), _API_URL, session_state, model_state
        )
    sessions_before = mgr.active_count
    path = "/v1/sessions/{session_id}/models/{model_id}"

    broken = _edit(9).replace("column: Country", "column: Nowhere", 1)
    assert broken != _edit(9)
    for _ in range(12):
        body, error, session_state, model_state = api_client._model_request(
            broken, _API_URL, session_state, model_state, "GET", path
        )
        assert body is None
        assert "Nowhere" in error

    body, error, session_state, model_state = api_client._model_request(
        _edit(9), _API_URL, session_state, model_state, "GET", path
    )
    assert error == ""
    assert body["model_id"] == model_state["model_id"]
    assert mgr.active_count == sessions_before + 1


@pytest.fixture
def expire_sessions(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Move the session manager's clock past the idle TTL of every session
    that exists, as a pause of an hour would. Nothing purges them before
    their next request (the cleanup sweep is off), as on Cloud Run, which by
    default gives an instance CPU only while it handles a request."""
    offset = 0.0

    class _Clock:  # the session manager reads only time.monotonic()
        @staticmethod
        def monotonic() -> float:
            return time.monotonic() + offset

    monkeypatch.setattr(session_manager, "time", _Clock)

    def expire() -> None:
        nonlocal offset
        offset += 3600  # the TTL is 1800 seconds, the max age a day

    return expire


_State = dict[str, str] | None


def _compile(model_yaml: str, ss: _State, ms: _State) -> tuple[str, _State]:
    sql, _, ss, ms = handlers.compile_sql(model_yaml, _QUERY_YAML, "postgres", _API_URL, ss, ms)
    return ("" if '"CUSTOMERS"' in sql else sql), ss


def _graph(model_yaml: str, ss: _State, ms: _State) -> tuple[str, _State]:
    turtle, ss, ms = api_client._fetch_obsl_turtle(model_yaml, _API_URL, ss, ms)
    return ("" if "@prefix" in turtle else turtle or "no graph"), ss


def _sparql(model_yaml: str, ss: _State, ms: _State) -> tuple[str, _State]:
    _, error, ss, ms = api_client._run_sparql(model_yaml, _API_URL, "ASK { ?s ?p ?o }", ss, ms)
    return error, ss


def _rules(model_yaml: str, ss: _State, ms: _State) -> tuple[str, _State]:
    _, error, ss, ms = api_client._rules_request(model_yaml, _API_URL, ss, ms, "GET", "")
    return error, ss


def _model(model_yaml: str, ss: _State, ms: _State) -> tuple[str, _State]:
    path = "/v1/sessions/{session_id}/models/{model_id}"
    _, error, ss, ms = api_client._model_request(model_yaml, _API_URL, ss, ms, "GET", path)
    return error, ss


def _diagram(model_yaml: str, ss: _State, ms: _State) -> tuple[str, _State]:
    md, _, ss, ms = api_client._fetch_diagram_er(model_yaml, True, _API_URL, ss, ms)
    return ("" if md.startswith("```mermaid") else md), ss


def _edit_then_act(model_yaml: str, ss: _State, ms: _State) -> tuple[str, _State]:
    """An edit after the pause: the upload is the first request."""
    _, _, _, ss, ms = api_client._ensure_session_and_model(_edit(1), _API_URL, ss, ms)
    return "", ss


@pytest.mark.parametrize(
    "action",
    [_compile, _graph, _sparql, _rules, _model, _diagram, _edit_then_act],
    ids=lambda action: action.__name__.strip("_"),
)
def test_an_expired_session_continues_in_a_new_one(
    serve: Callable[[int], FastAPI],
    expire_sessions: Callable[[], None],
    action: Callable[[str, _State, _State], tuple[str, _State]],
) -> None:
    serve(10)
    model_yaml = _edit(0)
    _, session_id, _, session_state, model_state = api_client._ensure_session_and_model(
        model_yaml, _API_URL, None, None
    )
    expire_sessions()

    error, session_state = action(model_yaml, session_state, model_state)

    assert error == ""
    assert session_state is not None
    assert session_state["session_id"] != session_id


def test_execute_after_a_pause_runs_in_a_new_session(
    serve: Callable[..., FastAPI], expire_sessions: Callable[[], None]
) -> None:
    """The sample model's tables are not in the empty database, so the query
    itself fails; it must fail in a new session, not on the expired one."""
    serve(10, execute=True)
    model_yaml = _edit(0)
    _, session_id, _, session_state, model_state = api_client._ensure_session_and_model(
        model_yaml, _API_URL, None, None
    )
    expire_sessions()

    result = handlers._execute_query(
        model_yaml, _QUERY_YAML, "duckdb", _API_URL, session_state, model_state
    )

    assert "Database execution failed" in result[0]  # it reached the database
    assert result[2] is not None
    assert result[2]["session_id"] != session_id


@pytest.mark.parametrize(
    ("status", "detail", "gone"),
    [
        (410, "Session 'abc' has expired", True),
        (404, "Session 'abc' not found", True),
        (404, "Session not found: abc", True),
        (404, "Model 'abc' not found", True),
        (404, "Rule 'Big Orders' not found", False),
        (404, "'Revenue' not found in model", False),
        (410, "The OSI ontology emit has been removed. Omit 'include_ontology'.", False),
        (500, "Session store failed", False),
    ],
)
def test_only_a_lost_session_or_model_starts_a_new_session(
    status: int, detail: str, gone: bool
) -> None:
    """An unknown rule or artefact is the user's to see, not a reason to
    create a session (which the session-creation rate limit counts)."""
    assert api_client._session_gone(httpx.Response(status, json={"detail": detail})) is gone


def test_a_response_without_json_is_not_a_lost_session() -> None:
    assert not api_client._session_gone(httpx.Response(404, text="Not Found"))
