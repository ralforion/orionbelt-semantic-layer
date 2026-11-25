"""Which Content-Security-Policy a path gets, also under a root path and through a mount."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from orionbelt.api.middleware import SecurityHeadersMiddleware


async def _ui(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("ui")(scope, receive, send)


def _app() -> FastAPI:
    """An app with a mounted sub-app at /ui, as the Gradio UI is mounted."""
    app = FastAPI(routes=[Mount("/ui", app=_ui)])

    @app.get("/tokens")
    async def tokens(request: Request) -> PlainTextResponse:
        return PlainTextResponse("tokens")

    app.add_middleware(
        SecurityHeadersMiddleware,
        tokens_connect_src=lambda: ["https://idp.example.test"],
    )
    return app


@pytest.mark.parametrize("root", ["", "/obsl"])
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/ui/", "'unsafe-inline'"),  # the mount moves root_path to /ui while routing
        ("/tokens", "connect-src 'self' https://idp.example.test"),
        ("/v1/anything", "default-src 'self'; frame-ancestors 'none'"),
    ],
)
async def test_the_policy_follows_the_app_s_own_path(root: str, path: str, expected: str) -> None:
    transport = ASGITransport(app=_app(), root_path=root)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(root + path)
    assert expected in response.headers["content-security-policy"]
