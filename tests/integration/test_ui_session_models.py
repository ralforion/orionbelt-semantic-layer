"""The UI keeps working however often the model is edited, and its actions
never take a model from each other.

Each edit in the model editor uploads the YAML as a new model into the UI's
API session. Once the session held ``MAX_MODELS_PER_SESSION`` models, every
further upload was refused with 429 and the UI was stuck until a page reload.
These tests drive the UI's real client helper against the API in process.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from orionbelt.ui import api_client
from tests.conftest import SAMPLE_MODEL_YAML

_API_URL = "http://testserver"


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[int], FastAPI]]:
    """Start the API with a given per-session model cap; route the UI to it."""

    def start(cap: int) -> FastAPI:
        app = create_app(settings=Settings(session_cleanup_interval=9999))
        init_session_manager(SessionManager(max_models_per_session=cap, cleanup_interval=9999))

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
