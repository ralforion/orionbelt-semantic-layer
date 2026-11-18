"""The UI on an API with several curated models (``MODEL_FILES``).

Model upload is refused in that mode, so the UI used to show the bundled
starter and fail on every action. It now lists the curated models, fetches the
chosen one's YAML, and works on it in the model's own protected session. These
tests drive the UI's client helpers against the API in process.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import get_session_manager
from orionbelt.settings import Settings
from orionbelt.ui import api_client
from tests.conftest import SAMPLE_MODEL_YAML

_API_URL = "http://testserver"
_QUERY = {"select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}}


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """The API with two curated models, ``sales`` and ``returns``, reading
    the same tables from different schemas (``SALES`` and ``RETURNS``)."""
    for name in ("sales", "returns"):
        (tmp_path / f"{name}.yaml").write_text(
            SAMPLE_MODEL_YAML.replace("version: 1.0", f"version: 1.0\nname: {name}", 1).replace(
                "schema: PUBLIC", f"schema: {name.upper()}"
            )
        )
    settings = Settings(
        model_files=f"{tmp_path / 'sales.yaml'},{tmp_path / 'returns.yaml'}",
        session_cleanup_interval=9999,
        flight_enabled=False,
    )
    app = create_app(settings=settings)
    with TestClient(app) as lifespan:  # loads MODEL_FILES
        # The helpers build their own httpx clients; send them into the app.
        def in_process_client(*, base_url: str, headers: Any, **_: Any) -> httpx.Client:
            return TestClient(app, base_url=base_url, headers=headers)

        def in_process_get(url: str, *, headers: Any = None, **_: Any) -> httpx.Response:
            return lifespan.get(url, headers=headers)

        monkeypatch.setattr(api_client.httpx, "Client", in_process_client)
        monkeypatch.setattr(api_client.httpx, "get", in_process_get)
        yield app


def _compile(
    model_yaml: str, session_state: dict[str, str], model_state: dict[str, str]
) -> tuple[Any, str, dict[str, str] | None, dict[str, str] | None]:
    """Compile a query from the model editor showing *model_yaml*. The editor
    (``gr.Code``) strips the text it shows, and sends that back."""
    return api_client._model_request(
        model_yaml.strip(),
        _API_URL,
        session_state,
        model_state,
        "POST",
        "/v1/sessions/{session_id}/query/sql",
        payload={"model_id": None, "query": _QUERY},
    )


def test_the_curated_models_are_listed(app: FastAPI) -> None:
    assert api_client._fetch_curated_models(_API_URL) == ["sales", "returns"]


def test_a_curated_model_is_used_in_its_own_session(app: FastAPI) -> None:
    picked = api_client._curated_model(_API_URL, "returns")
    assert picked is not None
    model_yaml, session_state, model_state = picked
    assert "name: returns" in model_yaml
    assert session_state["session_id"] == "returns"
    listed = TestClient(app).get("/v1/sessions/returns/models").json()
    assert [m["model_id"] for m in listed] == [model_state["model_id"]]


def test_actions_create_no_session_and_upload_nothing(app: FastAPI) -> None:
    """Compiling goes to the curated session as it is, although the editor
    sends the model back without its final newline. An upload would be
    refused (403); a new user session would hold no model."""
    picked = api_client._curated_model(_API_URL, "sales")
    assert picked is not None
    model_yaml, session_state, model_state = picked
    mgr = get_session_manager()
    sessions_before = mgr.active_count

    body, error, session_state, model_state = _compile(model_yaml, session_state, model_state)

    assert error == ""
    assert '"SALES"."CUSTOMERS"' in body["sql"]
    assert session_state["session_id"] == "sales"
    assert mgr.active_count == sessions_before
    assert len(mgr.get_store("sales").list_models()) == 1


def test_a_state_left_by_the_previous_model_follows_the_editor(app: FastAPI) -> None:
    """A compile still running when the user picks another model writes back
    the state it started with. The next action must use the model the editor
    shows, not the one that state names, and leave the state right again."""
    sales = api_client._curated_model(_API_URL, "sales")
    returns = api_client._curated_model(_API_URL, "returns")
    assert sales is not None
    assert returns is not None
    _, stale_session, stale_model = sales
    returns_yaml, _, returns_model = returns

    body, error, session_state, model_state = _compile(returns_yaml, stale_session, stale_model)

    assert error == ""
    assert '"RETURNS"."CUSTOMERS"' in body["sql"]
    assert session_state is not None
    assert session_state["session_id"] == "returns"
    assert model_state == returns_model


def test_a_model_the_api_no_longer_serves_is_reported(app: FastAPI) -> None:
    """The editor holds a curated model the API does not serve any more (it
    was restarted with other MODEL_FILES): say so, never query another one."""
    picked = api_client._curated_model(_API_URL, "sales")
    assert picked is not None
    model_yaml, session_state, model_state = picked

    body, error, _, _ = _compile(model_yaml.replace("SALES", "ARCHIVE"), session_state, model_state)

    assert body is None
    assert error == "The model in the editor is no longer on the API. Reload the page."


def test_an_unknown_model_is_none(app: FastAPI) -> None:
    assert api_client._curated_model(_API_URL, "nothing_here") is None


def test_an_unreachable_api_lists_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(api_client.httpx, "get", refuse)
    assert api_client._fetch_curated_models(_API_URL) == []
    assert api_client._curated_model(_API_URL, "sales") is None
