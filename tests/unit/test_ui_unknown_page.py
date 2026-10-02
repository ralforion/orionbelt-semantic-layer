"""Unknown ``?page=`` names get a 404, not a 500 with a traceback.

Gradio's root route looks the ``page`` query parameter up in its page table
without a guard, so ``/ui?page=gravitysmtp-settings`` (a WordPress plugin
probe seen several times a day on Cloud Run) raised ``KeyError`` and logged a
full traceback as an error.
"""

from __future__ import annotations

import pytest

gr = pytest.importorskip("gradio", reason="gradio required by the UI")
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from orionbelt.ui.app import _reject_unknown_pages  # noqa: E402


def _app(guarded: bool) -> FastAPI:
    with gr.Blocks() as demo:
        gr.Markdown("hello")
    app = FastAPI()
    if guarded:
        _reject_unknown_pages(app, "/ui", set(demo.config["page"]))
    return gr.mount_gradio_app(app, demo, path="/ui")


async def _status(app: FastAPI, path: str) -> int:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return (await client.get(path)).status_code


@pytest.mark.parametrize(
    "path", ["/ui?page=gravitysmtp-settings", "/ui/?page=gravitysmtp-connections"]
)
async def test_unknown_page_is_404(path: str) -> None:
    assert await _status(_app(guarded=True), path) == 404


async def test_the_ui_itself_still_loads() -> None:
    assert await _status(_app(guarded=True), "/ui/") == 200


async def test_page_param_on_other_routes_is_left_alone() -> None:
    """Only the Gradio root reads ``page``; other routes keep their own handling."""
    assert await _status(_app(guarded=True), "/ui/config?page=anything") == 200


async def test_without_the_guard_gradio_fails() -> None:
    """Documents the bug the guard exists for."""
    assert await _status(_app(guarded=False), "/ui/?page=gravitysmtp-settings") == 500
