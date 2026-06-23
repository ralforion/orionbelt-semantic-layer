"""Smoke test: the Gradio UI assembles without launching.

``create_blocks`` wires together every UI helper (API client, graph
rendering, query handlers). Building it exercises the whole assembly, so
this catches extraction breakage (missing imports, NameErrors, broken
re-exports) that the narrow auth/settings unit tests would miss — the UI
itself is otherwise not behaviourally tested.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("gradio", reason="gradio required to build the UI")
pytest.importorskip("httpx", reason="httpx required by the UI client")

from orionbelt.ui import app as ui_app  # noqa: E402


def test_create_blocks_builds_without_network() -> None:
    # Startup may try to fetch settings/dialects; stub them so the build is
    # offline and deterministic.
    with (
        patch.object(ui_app, "_fetch_settings", return_value={"_unreachable": True}),
        patch.object(ui_app, "_fetch_dialects", return_value=["postgres", "duckdb"]),
    ):
        blocks = ui_app.create_blocks(default_api_url="http://example.invalid")
    assert blocks is not None


def test_create_blocks_with_embedded_settings() -> None:
    embedded = {
        "single_model_mode": True,
        "model_yaml": "version: 1.0\nname: demo\n",
        "dialect": {"effective": "duckdb"},
    }
    with patch.object(ui_app, "_fetch_dialects", return_value=["duckdb"]):
        blocks = ui_app.create_blocks(
            default_api_url="http://example.invalid", embedded_settings=embedded
        )
    assert blocks is not None
