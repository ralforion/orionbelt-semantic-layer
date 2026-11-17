"""The UI keeps one model in its API session, however often the model is edited.

Each edit in the model editor uploads the YAML as a new model. Before the fix
the previous versions stayed in the session, so the edit after
``MAX_MODELS_PER_SESSION`` was refused with 429 and the UI was stuck until a
page reload. These tests drive the UI's real client helper against the real
API (in process) with a small cap, so the bug shows on the third edit.
"""

from __future__ import annotations

from collections.abc import Iterator
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

_CAP = 2
_API_URL = "http://testserver"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    app = create_app(settings=Settings(session_cleanup_interval=9999))
    mgr = SessionManager(max_models_per_session=_CAP, cleanup_interval=9999)
    init_session_manager(mgr)

    # The helper builds its own httpx.Client; route it into the app instead.
    def in_process_client(*, base_url: str, headers: Any, **_: Any) -> httpx.Client:
        return TestClient(app, base_url=base_url, headers=headers)

    monkeypatch.setattr(api_client.httpx, "Client", in_process_client)
    yield app
    reset_session_manager()


def _edit(n: int) -> str:
    """A distinct version of the model, as an edit in the editor would make."""
    edited = SAMPLE_MODEL_YAML.replace("schema: PUBLIC", f"schema: PUBLIC_{n}", 1)
    assert edited != SAMPLE_MODEL_YAML
    return edited


def _session_models(app: FastAPI, session_id: str) -> list[str]:
    resp = TestClient(app).get(f"/v1/sessions/{session_id}/models")
    resp.raise_for_status()
    return [m["model_id"] for m in resp.json()]


def test_repeated_edits_stay_under_the_session_model_cap(app: FastAPI) -> None:
    session_state = model_state = None
    for n in range(_CAP + 3):
        _, session_id, model_id, session_state, model_state = api_client._ensure_session_and_model(
            _edit(n), _API_URL, session_state, model_state
        )
        assert _session_models(app, session_id) == [model_id]


def test_returning_to_an_earlier_version_reuploads_it(app: FastAPI) -> None:
    _, session_id, first_id, session_state, model_state = api_client._ensure_session_and_model(
        _edit(0), _API_URL, None, None
    )
    _, _, _, session_state, model_state = api_client._ensure_session_and_model(
        _edit(1), _API_URL, session_state, model_state
    )
    _, same_session, back_id, _, _ = api_client._ensure_session_and_model(
        _edit(0), _API_URL, session_state, model_state
    )
    assert same_session == session_id
    assert _session_models(app, session_id) == [back_id]
    # Model ids are content-derived, so the same YAML maps to the same id.
    assert back_id == first_id
