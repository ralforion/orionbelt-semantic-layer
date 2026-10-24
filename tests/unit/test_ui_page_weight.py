"""The UI page stays small enough to serve to every visitor.

The deployed ``/ui/`` page was 16 MB of HTML, uncompressed: Gradio inlined a
10 MB client-API schema (every event, with generated code snippets) and the
vendored mermaid/ACE bundles travel in the page head. Scanners replaying
domain lists against the load balancer's IP fetched it in full each time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("gradio", reason="gradio required by the UI")
from httpx import ASGITransport, AsyncClient  # noqa: E402

from orionbelt.ui import app as ui_app  # noqa: E402


@pytest.fixture(scope="module")
def demo() -> object:
    with (
        patch.object(ui_app, "_fetch_settings", return_value={"_unreachable": True}),
        patch.object(ui_app, "_fetch_dialects", return_value=["postgres", "duckdb"]),
    ):
        return ui_app.create_blocks(default_api_url="http://example.invalid")


def test_no_event_is_documented_as_client_api(demo: object) -> None:
    assert demo.get_api_info()["named_endpoints"] == {}  # type: ignore[attr-defined]


def test_events_stay_callable(demo: object) -> None:
    """Undocumented, not private: the browser still has to call every handler."""
    visibilities = {fn.api_visibility for fn in demo.fns.values()}  # type: ignore[attr-defined]
    assert "public" not in visibilities
    # The config the page is served with, as mounting or launching builds it.
    served = demo.get_config_file()  # type: ignore[attr-defined]
    assert any(dep["api_visibility"] == "undocumented" for dep in served["dependencies"])


async def test_proxied_page_is_compressed(demo: object, tmp_path: Path) -> None:
    favicon = tmp_path / "favicon.png"
    favicon.write_bytes(b"\x89PNG")
    app = ui_app._proxied_app(demo, "/ui", favicon)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/ui/", headers={"Accept-Encoding": "gzip"})
        favicon_resp = await client.get("/ui/favicon.png")
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert favicon_resp.status_code == 200


async def test_event_stream_is_not_gzipped(demo: object, tmp_path: Path) -> None:
    """Gzip would buffer Gradio's SSE queue, holding progress messages back."""
    from fastapi.responses import StreamingResponse

    favicon = tmp_path / "favicon.png"
    favicon.write_bytes(b"\x89PNG")
    app = ui_app._proxied_app(demo, "/ui", favicon)

    async def _events() -> AsyncIterator[bytes]:
        yield b"data: " + b"x" * 4096 + b"\n\n"

    @app.get("/sse-probe")
    async def _sse() -> StreamingResponse:
        return StreamingResponse(_events(), media_type="text/event-stream")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/sse-probe", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


def test_vendored_bundles_are_not_inlined() -> None:
    head = ui_app.frontend_assets()["head"]
    assert len(head) < 50_000
    assert "atob(" not in head


@pytest.mark.parametrize("name", ["mermaid.min.js", "ace-sparql.min.js"])
async def test_vendored_bundles_are_served(demo: object, tmp_path: Path, name: str) -> None:
    """The head points at the app's vendor route, under the mount path."""
    app = ui_app._proxied_app(demo, "/ui", tmp_path / "favicon.png")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/ui/{ui_app._vendored_src(name)}")
    assert resp.status_code == 200
    assert len(resp.content) == ui_app._VENDORED_JS[name].stat().st_size
    # The URL names the bundle, not where the package is installed.
    assert str(ui_app._STATIC_DIR) not in ui_app._vendored_src(name)


@pytest.mark.parametrize("name", ["mermaid.min.js", "ace-sparql.min.js"])
def test_bundle_urls_pass_exploit_probe_filters(name: str) -> None:
    """Firewalls deny paths that exploit scanners probe, ``/vendor/`` among
    them (``/vendor/phpunit/...``). Served under ``/ui/vendor/`` the bundles
    were refused by the demo's load balancer: the SPARQL editor showed no
    query and no mermaid diagram rendered."""
    probed = {"vendor", "wp-admin", "wp-content", "wp-includes", "cgi-bin", "admin", "phpunit"}
    segments = ui_app._vendored_src(name).split("/")[:-1]
    assert not probed.intersection(segments)
    assert not any(segment.startswith(".") for segment in segments)


@pytest.mark.parametrize("mode", ["api", "proxied"])
async def test_vendored_bundles_are_compressed(demo: object, tmp_path: Path, mode: str) -> None:
    """Compressed by the route itself, so also where no wrapper compresses (the
    API's /ui), and encoded once under the proxied wrapper's gzip."""
    if mode == "api":
        from orionbelt.api.app import create_app

        app = create_app()
    else:
        app = ui_app._proxied_app(demo, "/ui", tmp_path / "favicon.png")
    name = "mermaid.min.js"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/ui/{ui_app._vendored_src(name)}", headers={"Accept-Encoding": "gzip"}
        )
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.num_bytes_downloaded < ui_app._VENDORED_JS[name].stat().st_size / 2
    # httpx decodes once: a second encoding would not come back as the file.
    assert resp.content == ui_app._VENDORED_JS[name].read_bytes()


@pytest.mark.parametrize("name", ["mermaid.min.js", "ace-sparql.min.js"])
@pytest.mark.parametrize(
    ("validator", "condition"),
    [("etag", "If-None-Match"), ("last-modified", "If-Modified-Since")],
)
async def test_a_revalidated_bundle_is_not_sent_again(
    demo: object, tmp_path: Path, name: str, validator: str, condition: str
) -> None:
    """A reload that revalidates gets 304, not the multi-MB bundle again."""
    app = ui_app._proxied_app(demo, "/ui", tmp_path / "favicon.png")
    url = f"/ui/{ui_app._vendored_src(name)}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(url)
        again = await client.get(url, headers={condition: first.headers[validator]})
    assert again.status_code == 304
    assert again.content == b""
