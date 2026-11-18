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
def playwright() -> Iterator[Any]:
    """One sync Playwright driver for the module.

    The sync API refuses to start while another sync driver is alive in the
    thread, so every page fixture below shares this one instead of opening
    its own.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        yield p


def _open_page(playwright: Any, url: str, ready_tab: str) -> tuple[Any, Any]:
    try:
        browser = playwright.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - browser binary not installed
        pytest.skip(f"no chromium available: {exc}")
    page = browser.new_page()
    # Not ``networkidle``: Gradio holds an SSE connection open, so the
    # network never goes idle and the wait times out.
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(f"role=tab[name='{ready_tab}']", timeout=60_000)
    return browser, page


@pytest.fixture(scope="module")
def page(playwright: Any, ui_url: str) -> Iterator[Any]:
    browser, page = _open_page(playwright, ui_url, "ER Diagram")
    try:
        yield page
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
    yield from _serve_embedded_ui()


def _serve_embedded_ui(**settings: Any) -> Iterator[str]:
    """Serve the API with its ``/ui`` on a free port; yield the UI's URL."""
    pytest.importorskip("gradio", reason="gradio required for the UI suite")
    pytest.importorskip("playwright", reason="playwright required")
    uvicorn = pytest.importorskip("uvicorn", reason="uvicorn required")

    from orionbelt.api.app import create_app
    from orionbelt.settings import Settings

    # The port first: the embedded UI takes its API URL from the settings'
    # effective port at build time, so an app built before the port is known
    # points its tabs at the default (or the .env) port, where a developer's
    # own server may be answering and this fixture's never is.
    port = _free_port()
    app = create_app(settings=Settings(ui_enabled=True, port=port, **settings))
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


class TestTheVendoredBundlesLoadLate:
    """mermaid and ACE arrive as files after the page, not inlined in it."""

    def test_polls_during_a_slow_download_render_the_diagram_once(
        self, playwright: Any, ui_url: str
    ) -> None:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browser binary not installed
            pytest.skip(f"no chromium available: {exc}")
        try:
            page = browser.new_page()
            # Count mermaid.run calls: wrap it as the bundle installs mermaid.
            page.add_init_script(
                """window.__runs = 0;
                Object.defineProperty(window, 'mermaid', {
                    configurable: true,
                    set(m) {
                        const run = m.run.bind(m);
                        m.run = (opts) => { window.__runs += 1; return run(opts); };
                        Object.defineProperty(window, 'mermaid', {value: m, writable: true});
                    },
                });"""
            )
            # Hold the bundle back until the tab's polls have piled up behind it.
            held: list[Any] = []
            page.route("**/mermaid.min.js", lambda route: held.append(route))
            page.goto(ui_url, wait_until="domcontentloaded")
            page.wait_for_selector("role=tab[name='ER Diagram']", timeout=60_000)
            page.get_by_role("tab", name="ER Diagram").click()
            page.wait_for_selector("#er-diagram .mermaid", state="attached", timeout=30_000)
            page.wait_for_timeout(1_000)
            assert held, "the bundle was not requested"
            for route in held:
                route.continue_()
            page.wait_for_selector("#er-diagram svg", timeout=30_000)
            page.wait_for_timeout(1_000)
            assert page.locator("#er-diagram svg").count() == 1
            assert page.evaluate("() => window.__runs") == 1
        finally:
            browser.close()

    def test_a_missing_renderer_is_explained_once(self, playwright: Any, ui_url: str) -> None:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browser binary not installed
            pytest.skip(f"no chromium available: {exc}")
        try:
            page = browser.new_page()
            errors: list[str] = []
            page.on("console", lambda m: m.type == "error" and errors.append(m.text))
            page.route("**/mermaid.min.js", lambda route: route.abort())
            page.goto(ui_url, wait_until="domcontentloaded")
            page.wait_for_selector("role=tab[name='ER Diagram']", timeout=60_000)
            page.get_by_role("tab", name="ER Diagram").click()
            page.wait_for_selector(
                "#er-diagram .mermaid[data-processed='failed']", state="attached", timeout=30_000
            )
            page.wait_for_timeout(1_500)  # past the next polls
            assert page.get_by_text("The diagram renderer could not be loaded").count() == 1
            assert page.locator("#er-diagram svg").count() == 0
            assert len([e for e in errors if "Diagram renderer" in e]) == 1
            assert not [e for e in errors if "Diagram renderer" not in e and "mermaid" in e]
        finally:
            browser.close()

    def test_the_sparql_editor_waits_for_its_tab(self, playwright: Any, ui_url: str) -> None:
        """ACE measures its container; created in a hidden tab it would be 0x0.

        The tab is opened before the bundle arrives, then left, so the bundle
        lands while the editor's container is rendered but hidden.
        """
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - browser binary not installed
            pytest.skip(f"no chromium available: {exc}")
        try:
            page = browser.new_page()
            held: list[Any] = []
            page.route("**/ace-sparql.min.js", lambda route: held.append(route))
            page.goto(ui_url, wait_until="domcontentloaded")
            page.wait_for_selector("role=tab[name='SPARQL']", timeout=60_000)
            page.get_by_role("tab", name="SPARQL").click()
            page.wait_for_selector("#ob-sparql-ace", state="attached", timeout=30_000)
            page.get_by_role("tab", name="ER Diagram").click()
            page.wait_for_selector("#ob-sparql-ace", state="hidden", timeout=30_000)
            assert held, "the bundle was not requested"
            for route in held:
                route.continue_()
            page.wait_for_function("() => !!window.ace", timeout=30_000)
            page.wait_for_timeout(500)
            assert page.locator("#ob-sparql-ace.ace_editor").count() == 0
            page.get_by_role("tab", name="SPARQL").click()
            page.wait_for_selector("#ob-sparql-ace.ace_editor", timeout=30_000)
            box = page.locator("#ob-sparql-ace .ace_content").bounding_box()
            assert box is not None
            assert box["width"] > 0
            assert box["height"] > 0
        finally:
            browser.close()


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


@pytest.fixture(scope="module")
def embedded_page(playwright: Any, embedded_ui_url: str) -> Iterator[Any]:
    """A page on the embedded UI, sharing the module's driver with ``page``."""
    browser, page = _open_page(playwright, embedded_ui_url, "SPARQL")
    try:
        yield page
    finally:
        browser.close()


class TestTheSparqlTab:
    """The SPARQL tab runs a gallery example and shows its bindings as a table.

    On the embedded UI, because that is the mode with a model in the editor:
    the API is up, so the UI seeds the commerce starter and the tab goes
    through ``POST .../sparql``. The standalone fixture has no API and
    deliberately shows an "API unreachable" placeholder instead of a model.
    """

    def test_an_example_renders_rows(self, embedded_page: Any) -> None:
        page = embedded_page
        page.get_by_role("tab", name="SPARQL").click()
        # The editor is ACE with the SPARQL grammar: keywords are tokenised.
        page.wait_for_selector(".ace_editor .ace_keyword", timeout=30_000)
        page.get_by_role("button", name="Execute Query").click()
        page.wait_for_function(
            "() => document.querySelector('#ob-sparql-status')?.innerText.startsWith('SELECT:')",
            timeout=60_000,
        )
        # Gradio virtualises the Dataframe body, so rows are attached but may
        # report as hidden until scrolled into view: count them, do not wait
        # for visibility.
        page.wait_for_selector(".sparql-table table tbody tr", state="attached", timeout=30_000)
        assert page.locator(".sparql-table table tbody tr").count() > 0
        assert page.locator(".sparql-table").is_visible()

    def test_the_ask_example_shows_a_boolean(self, embedded_page: Any) -> None:
        page = embedded_page
        page.get_by_role("tab", name="SPARQL").click()
        # A plain select (not filterable): open it and pick the option.
        page.get_by_label("Example query").click()
        page.get_by_role("option", name="Does the model link into schema.org? (ASK)").click()
        # The example reaches the editor through a server round trip and then
        # ``__obSparql.pull()``. Execute first flushes the editor back to the
        # query it sends, so clicking before the example has landed re-runs
        # the previous SELECT and the ASK status never appears (CI, #552).
        page.wait_for_function(
            """() => {
                const el = document.getElementById('ob-sparql-ace');
                return !!(window.ace && el && window.ace.edit(el).getValue().includes('ASK {'));
            }""",
            timeout=30_000,
        )
        page.get_by_role("button", name="Execute Query").click()
        page.wait_for_function(
            "() => document.querySelector('#ob-sparql-status')?.innerText.includes('ASK:')",
            timeout=60_000,
        )
        assert "true" in page.locator("#ob-sparql-status").inner_text()


class TestTheBusinessRulesTab:
    """The Business Rules tab lists the demo's rules and reports a test outcome.

    On the embedded UI, like the SPARQL tests. Listing needs only the API;
    testing needs a warehouse, which the embedded fixture does not configure,
    so the status line must carry the endpoint's refusal rather than hang.
    """

    def test_the_demo_rules_are_listed(self, embedded_page: Any) -> None:
        page = embedded_page
        page.get_by_role("tab", name="Business Rules").click()
        # The placeholder text also mentions rules: wait for the loaded
        # statistics line (a count and a dialect) or an error, not the word.
        page.wait_for_function(
            "() => /\\d+ rules on|Error|declares no rules/.test("
            "document.querySelector('#ob-rules-stats')?.innerText || '')",
            timeout=60_000,
        )
        stats = page.locator("#ob-rules-stats").inner_text()
        assert "6 rules" in stats
        # Gradio virtualises the body, so only the rows that fit the viewport
        # are in the DOM: the count is "some", the six is in the statistics.
        page.wait_for_selector(".rules-table table tbody tr", state="attached", timeout=30_000)
        assert page.locator(".rules-table table tbody tr").count() > 0

    def test_testing_a_rule_reports_an_outcome(self, embedded_page: Any) -> None:
        page = embedded_page
        page.get_by_role("tab", name="Business Rules").click()
        # The placeholder text also mentions rules: wait for the loaded
        # statistics line (a count and a dialect) or an error, not the word.
        page.wait_for_function(
            "() => /\\d+ rules on|Error|declares no rules/.test("
            "document.querySelector('#ob-rules-stats')?.innerText || '')",
            timeout=60_000,
        )
        page.get_by_role("button", name="Test Rule").click()
        page.wait_for_function(
            "() => { const t = document.querySelector('#ob-rules-status')?.innerText || '';"
            " return t.includes('Error') || t.includes(' in ') }",
            timeout=60_000,
        )
        status = page.locator("#ob-rules-status").inner_text()
        assert "Error" in status or "Electronics Sale" in status

    def test_show_definition_reveals_the_picked_rules_yaml(self, embedded_page: Any) -> None:
        page = embedded_page
        page.get_by_role("tab", name="Business Rules").click()
        page.wait_for_function(
            "() => /\\d+ rules on|Error|declares no rules/.test("
            "document.querySelector('#ob-rules-stats')?.innerText || '')",
            timeout=60_000,
        )
        page.get_by_label("Show rule definition").check()
        page.wait_for_function(
            "() => (document.querySelector('#ob-rule-definition')?.innerText || '')"
            ".includes('condition')",
            timeout=60_000,
        )
        text = page.locator("#ob-rule-definition").inner_text()
        assert "Electronics Sale" in text and "Product Category" in text

    def test_clicking_a_table_row_selects_that_rule(self, embedded_page: Any) -> None:
        page = embedded_page
        page.get_by_role("tab", name="Business Rules").click()
        page.wait_for_function(
            "() => /\\d+ rules on/.test("
            "document.querySelector('#ob-rules-stats')?.innerText || '')",
            timeout=60_000,
        )
        label = page.locator("#ob-rules-selected-label")
        label.get_by_text("Electronics Sale").wait_for(timeout=30_000)
        page.get_by_label("Show rule definition").check()
        # The visible body is virtualised into div cells; the <td>s are a hidden
        # sizing copy.
        page.locator(
            ".rules-table .virtual-row .body-cell", has_text="High Return Rate"
        ).first.click()
        page.wait_for_function(
            "() => (document.querySelector('#ob-rule-definition')?.innerText || '')"
            ".includes('Return Rate')",
            timeout=60_000,
        )
        assert "High Return Rate" in label.inner_text()


#: The window the playground is laid out for (see the fitter in ``ui/app.py``).
_WINDOW = {"width": 1300, "height": 990}

_ROW_FIT_JS = """() => {
    const row = document.querySelector('.settings-row');
    return {scroll: row.scrollWidth, client: row.clientWidth};
}"""


class TestTheSettingsRow:
    """Every control of the SQL Compiler's settings row stays on the page.

    Every input in the row had a 360px minimum, meant for the API URL, and
    the row did not wrap: at 1300px the API URL was cut off and both export
    buttons were off the page.
    """

    def test_nothing_runs_off_the_row(self, embedded_page: Any) -> None:
        page = embedded_page
        page.set_viewport_size(_WINDOW)
        page.get_by_role("tab", name="SQL Compiler").click()
        fit = page.evaluate(_ROW_FIT_JS)
        assert fit["scroll"] <= fit["client"], fit


@pytest.fixture(scope="module")
def curated_page(playwright: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """The embedded UI on an API with two curated models (``MODEL_FILES``),
    reading the same tables from different schemas (``SALES``, ``RETURNS``)."""
    from tests.conftest import SAMPLE_MODEL_YAML

    folder = tmp_path_factory.mktemp("curated")
    paths = []
    for name in ("sales", "returns"):
        path = folder / f"{name}.yaml"
        path.write_text(
            SAMPLE_MODEL_YAML.replace("version: 1.0", f"version: 1.0\nname: {name}", 1).replace(
                "schema: PUBLIC", f"schema: {name.upper()}"
            )
        )
        paths.append(str(path))
    served = _serve_embedded_ui(model_files=",".join(paths))
    url = next(served)
    browser, page = _open_page(playwright, url, "SQL Compiler")
    page.set_viewport_size(_WINDOW)
    # The page load fetches the model; the dropdown shows once it has.
    page.wait_for_selector("input[aria-label='Model']", state="visible", timeout=30_000)
    try:
        yield page
    finally:
        browser.close()
        served.close()


def _open_list(page: Any, label: str) -> Any:
    """Open the dropdown *label*; return its list (a closed one stays, inert)."""
    page.locator(f"input[aria-label='{label}']").click()
    options = page.locator("ul[role='listbox']:not([inert])")
    options.wait_for()
    return options


def _pick(page: Any, label: str, option: str) -> None:
    """Pick *option* in the dropdown *label*, then leave it closed and unfocused."""
    _open_list(page, label).get_by_role("option", name=option, exact=True).click()
    page.keyboard.press("Escape")
    page.locator(f"input[aria-label='{label}']").blur()


def _wait_for_model(page: Any, name: str) -> None:
    """Wait until the model editor shows the curated model *name*."""
    page.wait_for_function(
        "name => document.querySelector('#ob-model .cm-content')"
        ".innerText.includes('name: ' + name)",
        arg=name,
        timeout=30_000,
    )


def _wait_for_load(page: Any) -> None:
    """Wait until a (re)loaded page has fetched its model."""
    page.wait_for_selector("input[aria-label='Model']", state="visible", timeout=30_000)


class TestTheModelDropdown:
    """Several curated models: pick one; the editor and the query follow.

    Model upload is refused in this mode, and the UI used to show the bundled
    starter and fail on every action.
    """

    def test_it_lists_the_models_and_shows_the_first(self, curated_page: Any) -> None:
        page = curated_page
        assert page.locator("input[aria-label='Model']").input_value() == "sales"
        options = _open_list(page, "Model").get_by_role("option").all_inner_texts()
        page.keyboard.press("Escape")
        page.locator("input[aria-label='Model']").blur()
        assert [o.replace("\u2713", "").strip() for o in options] == ["sales", "returns"]
        assert "name: sales" in page.locator("#ob-model .cm-content").inner_text()

    def test_the_api_url_is_not_offered(self, curated_page: Any) -> None:
        """The UI works on each model in its own session on this API."""
        assert not curated_page.locator(".settings-label", has_text="API Base URL").is_visible()

    def test_picking_a_model_loads_it_and_compiles_against_it(self, curated_page: Any) -> None:
        page = curated_page
        _pick(page, "Model", "returns")
        _wait_for_model(page, "returns")
        # A model without examples starts with an empty select; fill it in.
        _pick(page, "Dimensions", "Customer Country")
        _pick(page, "Measures / Metrics", "Total Revenue")
        page.get_by_role("button", name="Compile SQL").click()
        page.wait_for_function(
            "() => [...document.querySelectorAll('.cm-content')]"
            '.some(e => e.innerText.includes(\'"RETURNS"."CUSTOMERS"\'))',
            timeout=15_000,
        )

    def test_nothing_runs_off_the_row(self, curated_page: Any) -> None:
        fit = curated_page.evaluate(_ROW_FIT_JS)
        assert fit["scroll"] <= fit["client"], fit

    def test_a_reload_brings_back_no_query_of_another_model(self, curated_page: Any) -> None:
        """The theme toggle saves the query for the reload it makes. Picking
        another model afterwards, then reloading, must not restore the query
        written for the first: it names fields the picked model may lack.
        Runs last: it leaves the page in the other theme."""
        from playwright.sync_api import expect

        page = curated_page
        _pick(page, "Model", "sales")
        _wait_for_model(page, "sales")
        _pick(page, "Dimensions", "Customer Country")
        query = page.locator("#ob-query .cm-content")
        expect(query).to_contain_text("Customer Country")
        page.get_by_role("button", name="Light / Dark").click()
        page.wait_for_url(lambda url: "__theme=" in url)
        _wait_for_load(page)
        _wait_for_model(page, "sales")
        expect(query).to_contain_text("Customer Country")  # the toggle kept it

        _pick(page, "Model", "returns")
        _wait_for_model(page, "returns")
        page.reload()
        _wait_for_load(page)
        _wait_for_model(page, "returns")
        page.wait_for_function(
            "() => { const q = document.querySelector('#ob-query .cm-content').innerText;"
            " return q.includes('dimensions:') && !q.includes('Customer Country'); }",
            timeout=10_000,
        )


class TestAnExpiredSession:
    """The UI's API session expires after 30 idle minutes and answers the
    first request after that with 410. By default Cloud Run gives an instance
    CPU only while it handles a request, so the cleanup sweep rarely drops
    the session first: the first click after a pause showed the 410."""

    def test_the_ui_continues_after_a_pause(
        self, playwright: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from playwright.sync_api import expect

        from orionbelt.service import session_manager

        offset = 0.0

        class _Clock:  # the session manager reads only time.monotonic()
            @staticmethod
            def monotonic() -> float:
                return time.monotonic() + offset

        monkeypatch.setattr(session_manager, "time", _Clock)
        served = _serve_embedded_ui(session_cleanup_interval=9999)
        browser, page = _open_page(playwright, next(served), "SQL Compiler")
        try:
            page.set_viewport_size(_WINDOW)
            sql = page.locator("#ob-sql")
            page.get_by_role("button", name="Compile SQL").click()
            expect(sql).to_contain_text("SELECT", timeout=15_000)

            offset += 3600  # an hour's pause; the TTL is 30 minutes
            _pick(page, "SQL Dialect", "bigquery")
            page.get_by_role("button", name="Compile SQL").click()
            expect(sql).to_contain_text("`", timeout=15_000)  # BigQuery quotes with backticks

            offset += 3600  # another pause, then the Settings tab
            page.get_by_role("tab", name="Settings").click()
            settings = page.locator(".block", has_text="API Settings").locator(".cm-content")
            expect(settings).to_contain_text("dialect:", timeout=15_000)
            expect(settings).not_to_contain_text("# Error")
        finally:
            browser.close()
            served.close()
