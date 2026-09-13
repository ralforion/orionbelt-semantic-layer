"""The SPARQL tab's callbacks: local fallback, API path, ASK, and refusals."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("gradio", reason="gradio required for the UI handlers")
pytest.importorskip("pandas", reason="pandas required for the results table")

from orionbelt.obsl.sparql_examples import EXAMPLE_TITLES, example_query  # noqa: E402
from orionbelt.ui import api_client  # noqa: E402
from orionbelt.ui.handlers import run_sparql, sparql_example_query  # noqa: E402

_MODEL_YAML = (
    Path(__file__).resolve().parents[2] / "examples" / "orionbelt_1_commerce.yaml"
).read_text()
_UNREACHABLE = "http://127.0.0.1:9"  # nothing listens on the discard port


def _visible(update: object) -> bool:
    assert isinstance(update, dict)
    return bool(update.get("visible"))


class TestLocalFallback:
    """Without an API (standalone UI, the browser test) the tab still answers."""

    def test_select_renders_a_table(self) -> None:
        table, status, _, _ = run_sparql(
            _MODEL_YAML, _UNREACHABLE, example_query("External concept mappings"), None, None
        )
        assert _visible(table)
        frame = table["value"]
        assert list(frame.columns) == ["label", "relation", "concept"]
        assert len(frame) > 0
        assert status.startswith("**SELECT:**")

    def test_ask_renders_a_boolean(self) -> None:
        table, status, _, _ = run_sparql(
            _MODEL_YAML, _UNREACHABLE, example_query(EXAMPLE_TITLES[-1]), None, None
        )
        assert not _visible(table)
        assert status == "**ASK:** `true`"

    def test_unbound_variable_warning_is_shown(self) -> None:
        table, status, _, _ = run_sparql(
            _MODEL_YAML,
            _UNREACHABLE,
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
            "SELECT ?label WHERE { ?x rdfs:label ?label } ORDER BY ?lal",
            None,
            None,
        )
        assert _visible(table)
        assert status.startswith("**SELECT:**")
        assert "**Warning:** ORDER BY ?lal: the variable is never bound" in status

    def test_update_operation_is_refused(self) -> None:
        table, status, _, _ = run_sparql(
            _MODEL_YAML, _UNREACHABLE, "DELETE WHERE { ?s ?p ?o }", None, None
        )
        assert not _visible(table)
        assert status.startswith("**Error:**")

    def test_invalid_model_is_an_error_not_a_crash(self) -> None:
        table, status, _, _ = run_sparql(
            "version: 1.0\ndataObjects:\n  X:\n    code: X\ndimensions:\n  D:\n"
            "    dataObject: Nope\n    column: c\n",
            _UNREACHABLE,
            example_query(EXAMPLE_TITLES[0]),
            None,
            None,
        )
        assert not _visible(table)
        assert status.startswith("**Error:**")

    def test_placeholder_editor_means_no_model(self) -> None:
        """The standalone UI shows a comment-only placeholder when the API is down."""
        table, status, _, _ = run_sparql(
            "# API unreachable at http://localhost:8000\n# Refresh once healthy\n",
            _UNREACHABLE,
            example_query(EXAMPLE_TITLES[0]),
            None,
            None,
        )
        assert not _visible(table)
        assert status == "**Error:** No model loaded."

    def test_empty_query_asks_for_one(self) -> None:
        table, status, _, _ = run_sparql(_MODEL_YAML, _UNREACHABLE, "   ", None, None)
        assert not _visible(table)
        assert "pick an example" in status


class TestApiPath:
    """With an API, the tab posts to /sparql and reports the endpoint's refusals."""

    def _client(self, status_code: int, body: dict) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = body
        resp.text = str(body)
        client = MagicMock()
        client.post.return_value = resp
        return client

    def test_select_through_the_api(self) -> None:
        client = self._client(
            200,
            {"type": "select", "variables": ["label"], "results": [{"label": "Sales"}]},
        )
        with patch.object(
            api_client,
            "_ensure_session_and_model",
            return_value=(client, "s1", "m1", {"session_id": "s1"}, {"model_id": "m1"}),
        ):
            table, status, session, model = run_sparql(
                _MODEL_YAML, "http://api.example", "SELECT ?label WHERE {}", None, None
            )
        client.post.assert_called_once_with(
            "/v1/sessions/s1/models/m1/sparql", json={"query": "SELECT ?label WHERE {}"}
        )
        assert _visible(table) and list(table["value"]["label"]) == ["Sales"]
        assert status == "**SELECT:** 1 row"
        assert session == {"session_id": "s1"} and model == {"model_id": "m1"}

    def test_endpoint_refusal_is_shown(self) -> None:
        client = self._client(400, {"detail": "Update operations are not allowed"})
        with patch.object(
            api_client,
            "_ensure_session_and_model",
            return_value=(client, "s1", "m1", {}, {}),
        ):
            table, status, _, _ = run_sparql(
                _MODEL_YAML, "http://api.example", "DROP ALL", None, None
            )
        assert not _visible(table)
        assert "Update operations are not allowed" in status


def test_dropdown_loads_the_example() -> None:
    assert sparql_example_query(EXAMPLE_TITLES[0]) == example_query(EXAMPLE_TITLES[0])
    assert sparql_example_query("nope") == ""
