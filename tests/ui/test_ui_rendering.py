"""What the UI actually renders in a browser.

Every other test in this repo asserts what the server *sends*. That was
enough until a dependency bump changed what the browser *does* with it:
Gradio 6 parses a ```mermaid fence into ``<div class="mermaid">`` and stops,
where Gradio 5 rendered it, so the ER diagram tab showed its own source text
and the zoom control was silently dead alongside it. No server-side assertion
could have seen that - the markup was correct at every layer we test.

So these drive a real browser against a real Gradio app. Two things they
cover, both of which have now broken once:

* the ER diagram renders to an ``svg``, not to text;
* the action buttons stay on one line and do not span the row.

Run with::

    uv run pytest -m ui

Opt-in, like the docker and dremio suites: it needs ``playwright`` plus a
downloaded browser (``uv run playwright install chromium``), which the default
suite should not require.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.ui


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def ui_url() -> Iterator[str]:
    """A real Gradio server, served the way production serves it."""
    pytest.importorskip("gradio", reason="gradio required for the UI suite")
    pytest.importorskip("playwright", reason="playwright required: uv run playwright install")

    from orionbelt.ui.app import create_blocks, frontend_assets

    demo = create_blocks()
    port = _free_port()
    # ``frontend_assets`` is the contract under test as much as the markup is:
    # it is where the mermaid loader has to live for every serving mode to get
    # it. Launching without it would test a page production never serves.
    threading.Thread(
        target=lambda: demo.launch(
            server_name="127.0.0.1",
            server_port=port,
            share=False,
            prevent_thread_lock=True,
            quiet=True,
            **frontend_assets(),
        ),
        daemon=True,
    ).start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    else:
        pytest.fail(f"UI did not start on {url}")

    try:
        yield url
    finally:
        demo.close()


@pytest.fixture(scope="module")
def page(ui_url: str) -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browser binary not installed
            pytest.skip(f"no chromium available: {exc}")
        context = browser.new_page()
        # Not ``networkidle``: Gradio holds an SSE connection open, so the
        # network never goes idle and the wait times out.
        context.goto(ui_url, wait_until="domcontentloaded")
        context.wait_for_selector("role=tab[name='ER Diagram']", timeout=60_000)
        try:
            yield context
        finally:
            browser.close()


@pytest.fixture(scope="module")
def embedded_ui_url() -> Iterator[str]:
    """The UI as the API serves it, at ``/ui``, with the CSP middleware on.

    The standalone fixture above launches bare Gradio, which no middleware
    touches. That is the mode the mermaid loader was first written against,
    and it hid a real problem: ``/ui``'s CSP allows scripts from ``'self'``
    and inline only, so a loader importing from a CDN would have been blocked
    here while passing there. Vendoring removed the CDN, and this makes the
    stricter of the two modes the one under test.
    """
    pytest.importorskip("gradio", reason="gradio required for the UI suite")
    pytest.importorskip("playwright", reason="playwright required")
    uvicorn = pytest.importorskip("uvicorn", reason="uvicorn required")

    from orionbelt.api.app import create_app
    from orionbelt.settings import Settings

    app = create_app(settings=Settings(ui_enabled=True))
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    else:
        pytest.fail("embedded UI did not start")

    try:
        yield f"http://127.0.0.1:{port}/ui"
    finally:
        server.should_exit = True


class TestTheEmbeddedUI:
    """``/ui`` inside the API, where the CSP applies."""

    def test_the_diagram_renders_under_the_csp(self, embedded_ui_url: str) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # noqa: BLE001 - browser binary not installed
                pytest.skip(f"no chromium available: {exc}")
            page = browser.new_page()
            violations: list[str] = []
            # A CSP block surfaces as a console error, not an exception.
            page.on(
                "console",
                lambda m: (
                    violations.append(m.text) if "Content Security Policy" in m.text else None
                ),
            )
            try:
                page.goto(embedded_ui_url, wait_until="domcontentloaded")
                page.wait_for_selector("role=tab[name='ER Diagram']", timeout=60_000)
                page.get_by_role("tab", name="ER Diagram").click()
                page.wait_for_selector("#er-diagram svg", timeout=30_000)
                assert page.evaluate("() => typeof window.__obRenderMermaid") == "function"
                assert not violations, f"CSP blocked something: {violations}"
            finally:
                browser.close()


class TestTheERDiagramRenders:
    """The regression a Gradio bump shipped: markup, no diagram."""

    def test_it_becomes_an_svg_not_text(self, page: Any) -> None:
        # The app ships a default model in the editor, so the tab has
        # something to draw without this test typing one in - the editor is
        # CodeMirror rather than a textarea, and driving it would test the
        # editor rather than the diagram.
        page.get_by_role("tab", name="ER Diagram").click()
        # The diagram is fetched, then rendered by the deferred poll.
        page.wait_for_selector("#er-diagram svg", timeout=30_000)

        state = page.evaluate(
            """() => {
                const el = document.querySelector('#er-diagram');
                return {
                    svgs: el.querySelectorAll('svg').length,
                    processed: el.querySelector('.mermaid')?.getAttribute('data-processed'),
                };
            }"""
        )
        assert state["svgs"] >= 1, "the mermaid source rendered as text, not a diagram"
        assert state["processed"] == "true"

    def test_the_loader_reaches_the_page(self, page: Any) -> None:
        """``frontend_assets`` is where it has to live, not the Blocks call.

        Gradio 6 takes ``head`` on ``launch()`` / ``mount_gradio_app()``, so a
        loader attached to the ``Blocks`` constructor reaches only whichever
        serving mode happens to read it.
        """
        assert page.evaluate("() => typeof window.__obRenderMermaid") == "function"


class TestTheActionButtons:
    """Compact, one line each. They were full-width and wrapping."""

    #: ``Execute Query`` is hidden unless query execution is enabled, so it is
    #: checked when present rather than required.
    LABELS = ("Compile SQL", "Execute Query", "Validate Model")
    ALWAYS_PRESENT = ("Compile SQL", "Validate Model")

    def test_each_label_stays_on_one_line(self, page: Any) -> None:
        page.get_by_role("tab", name="SQL Compiler").click()
        metrics = page.evaluate(
            """(labels) => [...document.querySelectorAll('button')]
                .filter(b => labels.includes(b.textContent.trim()))
                .map(b => {
                    const cs = getComputedStyle(b);
                    // One text line: the button's content box is no taller
                    // than a single line-height plus its vertical padding.
                    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
                    return {
                        label: b.textContent.trim(),
                        width: b.getBoundingClientRect().width,
                        contentHeight: b.getBoundingClientRect().height - pad,
                        lineHeight: parseFloat(cs.lineHeight),
                        whiteSpace: cs.whiteSpace,
                    };
                })""",
            list(self.LABELS),
        )
        assert set(self.ALWAYS_PRESENT) <= {m["label"] for m in metrics}
        for m in metrics:
            assert m["whiteSpace"] == "nowrap", m
            assert m["contentHeight"] <= m["lineHeight"] * 1.5, (
                f"{m['label']} wraps to a second line: {m}"
            )

    def test_none_of_them_spans_the_row(self, page: Any) -> None:
        """``Compile SQL`` had the default scale and absorbed the whole row."""
        widths = page.evaluate(
            """(labels) => {
                const btns = [...document.querySelectorAll('button')]
                    .filter(b => labels.includes(b.textContent.trim()));
                const row = btns[0]?.closest('.form, .gap, div');
                return {
                    buttons: btns.map(b => b.getBoundingClientRect().width),
                    viewport: window.innerWidth,
                };
            }""",
            list(self.LABELS),
        )
        assert widths["buttons"], "no action buttons found"
        for w in widths["buttons"]:
            assert w < widths["viewport"] * 0.5, f"a button spans {w}px of the row"
