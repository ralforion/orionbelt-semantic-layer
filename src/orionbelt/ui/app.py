"""Gradio demo UI — thin HTTP client for the OrionBelt REST API."""

from __future__ import annotations

import contextlib
import hashlib
from typing import Any

import httpx
import sqlparse
import yaml

_DEFAULT_API_URL = "http://localhost:8000"
_FALLBACK_DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "postgres",
    "snowflake",
]
_API_HEADERS = {"User-Agent": "OrionBelt-UI/1.0"}


def _format_api_errors(detail: Any) -> str:
    """Format API error detail into readable lines."""
    if isinstance(detail, dict):
        lines: list[str] = []
        if detail.get("error"):
            lines.append(detail["error"])
        for err in detail.get("errors", []):
            code = err.get("code", "ERROR")
            msg = err.get("message", "")
            path = err.get("path", "")
            line = f"  [{code}] {msg}"
            if path:
                line += f"  (at {path})"
            lines.append(line)
        for warn in detail.get("warnings", []):
            if isinstance(warn, dict):
                lines.append(f"  [WARNING] {warn.get('message', warn)}")
            else:
                lines.append(f"  [WARNING] {warn}")
        return "\n".join(lines) if lines else str(detail)
    return str(detail)


_DEFAULT_QUERY = """\
select:
  dimensions:
    - Product Name
    - Client Name
  measures:
    - Total Sales
    - Total Returns
    - Return Rate
where:
  - field: Country Name
    op: in
    value: [Germany, France, Italy]
order_by:
  - field: Total Sales
    direction: desc
limit: 100
"""

_CSS = """\
/* ── Layout: full-width, fit viewport ── */
.gradio-container {
  max-width: 100% !important;
  padding: 4px 16px !important;
}
/* compact header */
.header-row { min-height: 0 !important; padding: 0 !important; }
.header-row h2 { margin: 0 !important; }
/* compact settings row */
.settings-row { min-height: 0 !important; }

/* Code editors + SQL output: viewport-percentage heights */
.code-editor .cm-editor { max-height: 45dvh !important; }
.sql-output .cm-editor { max-height: 20dvh !important; }

/* purple primary button — compact */
.purple-btn {
  background: linear-gradient(135deg, #7c3aed, #9333ea) !important;
  border: none !important;
  color: white !important;
  padding-top: 6px !important;
  padding-bottom: 6px !important;
  margin: 0 !important;
}
.purple-btn:hover {
  background: linear-gradient(135deg, #6d28d9, #7c3aed) !important;
}

/* Custom upload button: match Gradio's native toolbar button style */
.ob-upload-btn {
  background: none !important;
  border: none !important;
  padding: 2px !important;
  margin: 0 !important;
  cursor: pointer;
  color: var(--body-text-color) !important;
  opacity: 0.7;
  display: flex;
  align-items: center;
}
.ob-upload-btn:hover { opacity: 1; }
.ob-upload-btn svg {
  width: 16px;
  height: 16px;
  stroke: currentColor !important;
}

/* ── YAML / SQL syntax highlighting (dark-mode optimised) ── */
.cm-editor .cm-atom     { color: #7dcfff !important; }
.cm-editor .cm-string   { color: #ce9178 !important; }
.cm-editor .cm-comment  { color: #6a9955 !important; font-style: italic; }
.cm-editor .cm-number   { color: #b5cea8 !important; }
.cm-editor .cm-keyword  { color: #c586c0 !important; }
.cm-editor .cm-meta     { color: #858585 !important; }
.cm-editor .cm-def      { color: #9cdcfe !important; }
.cm-editor .cm-variable { color: #4ec9b0 !important; }
.sql-output .cm-editor .cm-keyword { color: #569cd6 !important; }
.sql-output .cm-editor .cm-builtin { color: #4ec9b0 !important; }

/* ── Upload icon button ── */
.ob-upload-btn {
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--body-text-color, #fff);
  padding: 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: opacity 0.15s ease;
}
.ob-upload-btn:hover { opacity: 0.7; }

/* Bridge textboxes: rendered but removed from layout flow */
.ob-bridge {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  overflow: hidden !important;
  clip: rect(0,0,0,0) !important;
  padding: 0 !important;
  margin: -1px !important;
  border: 0 !important;
}

/* ── ER Diagram tab ── */
#er-diagram {
  overflow: auto;
  max-height: calc(100dvh - 220px);
  border: 1px solid var(--border-color-primary);
  border-radius: 8px;
  padding: 8px;
}
#er-diagram svg {
  transform-origin: top left;
  transition: transform 0.15s ease;
}
"""

_DARK_MODE_INIT_JS = """
() => {
    if (!window.location.search.includes('__theme=')) {
        const url = new URL(window.location);
        url.searchParams.set('__theme', 'dark');
        window.location.replace(url.href);
    }
}
"""

# Simple redirect — used as .then() after saving state.
_THEME_REDIRECT_JS = """
() => {
    setTimeout(() => {
        // Signal that a theme toggle is in progress so the restore step
        // knows it should re-select the saved tab.
        sessionStorage.setItem('ob_theme_toggled', '1');

        const url = new URL(window.location);
        const current = url.searchParams.get('__theme');
        url.searchParams.set('__theme', current === 'dark' ? 'light' : 'dark');
        window.location.replace(url.href);
    }, 50);
}
"""


# JS pre-processor: detect the active Gradio colour scheme from the URL
# and inject the matching Mermaid theme into the last argument slot.
_DETECT_THEME_JS = """
(...args) => {
    const p = new URLSearchParams(window.location.search);
    const paramTheme = p.get('__theme');
    const isDark = paramTheme
        ? paramTheme === 'dark'
        : document.documentElement.classList.contains('dark')
          || document.body.classList.contains('dark');
    args[args.length - 1] = isDark ? 'dark' : 'default';
    return args;
}
"""

# JS: download the raw Mermaid text as a .md file
_DOWNLOAD_MD_JS = """(raw) => {
    if (!raw) { alert('No diagram available. Generate the ER diagram first.'); return; }
    var content = '```mermaid\\n' + raw + '\\n```\\n';
    var blob = new Blob([content], {type: 'text/markdown'});
    var a = document.createElement('a');
    a.download = 'mermaid.md';
    a.href = URL.createObjectURL(blob);
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
}"""

# JS: render the Mermaid SVG to a PNG and trigger download
_DOWNLOAD_PNG_JS = """() => {
    var svgEl = document.querySelector('#er-diagram svg');
    if (!svgEl) { alert('No diagram available. Generate the ER diagram first.'); return; }
    var clone = svgEl.cloneNode(true);
    clone.style.transform = 'none';
    var vb = clone.getAttribute('viewBox');
    var w, h;
    if (vb) {
        var parts = vb.split(/[\\s,]+/);
        w = parseFloat(parts[2]);
        h = parseFloat(parts[3]);
    } else {
        w = parseFloat(clone.getAttribute('width')) || svgEl.getBoundingClientRect().width;
        h = parseFloat(clone.getAttribute('height')) || svgEl.getBoundingClientRect().height;
    }
    clone.setAttribute('width', w);
    clone.setAttribute('height', h);
    var xml = new XMLSerializer().serializeToString(clone);
    var dataUrl = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
    var img = new Image();
    img.onload = function() {
        var dpr = 2;
        var canvas = document.createElement('canvas');
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        var ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        ctx.drawImage(img, 0, 0, w, h);
        canvas.toBlob(function(blob) {
            var a = document.createElement('a');
            a.download = 'mermaid.png';
            a.href = URL.createObjectURL(blob);
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(a.href);
        }, 'image/png');
    };
    img.onerror = function() { alert('Failed to render diagram as PNG.'); };
    img.src = dataUrl;
}"""

# SVG icon: upload (Lucide style, matches Gradio's 16x16 toolbar icons)
_UPLOAD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"'
    ' viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    '<polyline points="17 8 12 3 7 8"/>'
    '<line x1="12" y1="3" x2="12" y2="15"/></svg>'
)

_INJECT_UPLOAD_JS = (
    """
() => {
    const SVG = '"""
    + _UPLOAD_SVG.replace("'", "\\'")
    + """';
    function setBridge(bridgeId, content) {
        var el = document.getElementById(bridgeId);
        if (!el) return;
        var ta = el.querySelector('textarea') || el.querySelector('input');
        if (!ta) return;
        /* Clear first so Gradio always sees a state change,
         * even if the same file is loaded twice. */
        ta.value = '';
        ta.dispatchEvent(new Event('input', {bubbles: true}));
        ta.dispatchEvent(new Event('change', {bubbles: true}));
        setTimeout(function() {
            ta.value = content;
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
        }, 50);
    }

    function addUploadBtn(codeId, bridgeId) {
        const root = document.getElementById(codeId);
        if (!root || root.querySelector('.ob-upload-btn')) return;

        /* Find the toolbar: locate an SVG-icon button (download/copy) */
        /* and use its parent as the toolbar container.               */
        var svgInBtn = root.querySelector('button svg');
        if (!svgInBtn) return;
        var toolbar = svgInBtn.closest('button').parentElement;

        const btn = document.createElement('button');
        btn.className = 'ob-upload-btn';
        btn.title = 'Load YAML file';
        btn.innerHTML = SVG;

        btn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            var fi = document.createElement('input');
            fi.type = 'file';
            fi.accept = '.yaml,.yml';
            fi.addEventListener('change', function() {
                var f = fi.files[0];
                if (!f) return;
                var reader = new FileReader();
                reader.addEventListener('load', function() {
                    setBridge(bridgeId, reader.result);
                });
                reader.readAsText(f);
            });
            fi.click();
        });

        /* Prepend to toolbar — places it left of download/copy */
        toolbar.style.display = 'flex';
        toolbar.style.flexWrap = 'nowrap';
        toolbar.style.alignItems = 'center';
        toolbar.insertBefore(btn, toolbar.firstChild);
    }

    /*
     * Rename download files for each Code component.
     * Gradio Code renders a persistent <a download="file.EXT" href="blob:...">
     * inside the component DOM.  We simply find it and change the download attr.
     * For ob-sql we also watch for OSI export content and rename to osi.yml.
     */
    function patchDownloads(codeId, filename) {
        var root = document.getElementById(codeId);
        if (!root) return;
        var anchors = root.querySelectorAll('a[download]');
        anchors.forEach(function(a) { a.download = filename; });

        /* For SQL output: dynamically switch filename based on content */
        if (codeId === 'ob-sql' && !root._ob_dl_observer) {
            root._ob_dl_observer = true;
            /* Re-check filename before each click */
            root.addEventListener('click', function(e) {
                var a = e.target.closest('a[download]');
                if (!a) return;
                var cm = root.querySelector('.cm-content');
                var txt = cm ? cm.textContent || '' : '';
                if (txt.indexOf('OBML') >= 0 && txt.indexOf('OSI') >= 0) {
                    a.download = 'osi.yml';
                } else {
                    a.download = filename;
                }
            }, true);
        }

        /* Gradio may re-render and reset the download attr.
         * Use MutationObserver to keep our filename. */
        if (!root._ob_dl_mo) {
            root._ob_dl_mo = true;
            var mo = new MutationObserver(function() {
                var aa = root.querySelectorAll('a[download]');
                aa.forEach(function(a) {
                    var desired = filename;
                    if (codeId === 'ob-sql') {
                        var cm = root.querySelector('.cm-content');
                        var txt = cm ? cm.textContent || '' : '';
                        if (txt.indexOf('OBML') >= 0 && txt.indexOf('OSI') >= 0)
                            desired = 'osi.yml';
                    }
                    if (a.download !== desired) a.download = desired;
                });
            });
            mo.observe(root, {childList: true, subtree: true,
                attributes: true, attributeFilter: ['download']});
        }
    }

    /*
     * Fix clipboard for non-HTTPS contexts (e.g. http://35.187.174.102).
     * navigator.clipboard.writeText() requires a secure context (HTTPS/localhost).
     * Polyfill it globally so Gradio's own copy buttons use the fallback.
     */
    if (!window.isSecureContext) {
        if (!navigator.clipboard) {
            navigator.clipboard = {};
        }
        navigator.clipboard.writeText = function(text) {
            return new Promise(function(resolve, reject) {
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.left = '-9999px';
                ta.style.top = '-9999px';
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                try {
                    document.execCommand('copy');
                    resolve();
                } catch (err) {
                    reject(err);
                } finally {
                    document.body.removeChild(ta);
                }
            });
        };
    }

    /* Retry — components render asynchronously. */
    var attempts = 0;
    var iv = setInterval(function() {
        addUploadBtn('ob-model', 'ob-model-bridge');
        addUploadBtn('ob-query', 'ob-query-bridge');
        patchDownloads('ob-model', 'obml.yml');
        patchDownloads('ob-query', 'query.yml');
        patchDownloads('ob-sql', 'query.sql');
        patchDownloads('ob-explain', 'explain-query.yml');
        if (++attempts >= 10) clearInterval(iv);
    }, 300);

    /* ── Tab persistence across theme toggle ── */
    var tabBtns = document.querySelectorAll('button[role="tab"]');
    tabBtns.forEach(function(btn, idx) {
        btn.addEventListener('click', function() {
            sessionStorage.setItem('ob_active_tab', String(idx));
        });
    });
    var toggled = sessionStorage.getItem('ob_theme_toggled');
    if (toggled) {
        sessionStorage.removeItem('ob_theme_toggled');
        var savedIdx = parseInt(
            sessionStorage.getItem('ob_active_tab') || '0', 10
        );
        if (savedIdx > 0 && tabBtns[savedIdx]) tabBtns[savedIdx].click();
    }
}
"""
)


_IMPORT_OSI_JS = """
() => {
    const fi = document.createElement('input');
    fi.type = 'file';
    fi.accept = '.yaml,.yml';
    fi.addEventListener('change', function() {
        const f = fi.files[0];
        if (!f) return;
        const reader = new FileReader();
        reader.addEventListener('load', function() {
            const el = document.getElementById('ob-osi-bridge');
            if (!el) return;
            const ta = el.querySelector('textarea') || el.querySelector('input');
            if (!ta) return;
            ta.value = reader.result;
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
        });
        reader.readAsText(f);
    });
    fi.click();
}
"""


def _format_convert_status(
    direction: str,
    warnings: list[str],
    validation: dict[str, Any],
) -> str:
    """Build status lines from a /convert API response."""
    lines: list[str] = [direction]
    for w in warnings:
        lines.append(f"WARNING: {w}")
    schema_ok = (
        "✓"
        if validation.get("schema_valid", True)
        else (f"{len(validation.get('schema_errors', []))} error(s)")
    )
    sem_ok = (
        "✓"
        if validation.get("semantic_valid", True)
        else (f"{len(validation.get('semantic_errors', []))} error(s)")
    )
    lines.append(f"Validation: JSON Schema {schema_ok} | Semantic {sem_ok}")
    for e in validation.get("schema_errors", []):
        lines.append(f"Schema error: {e}")
    for e in validation.get("semantic_errors", []):
        lines.append(f"Semantic error: {e}")
    for w in validation.get("semantic_warnings", []):
        lines.append(f"Validation warning: {w}")
    return "\n".join(lines)


def _import_osi(osi_yaml: str, api_base: str) -> tuple[str, str, str]:
    """Convert OSI YAML to OBML via the API. Returns ``(obml_yaml, status, explain)``."""
    if not osi_yaml or not osi_yaml.strip():
        return "", "Error: No OSI YAML content provided", ""

    try:
        resp = httpx.post(
            f"{api_base}/v1/convert/osi-to-obml",
            json={"input_yaml": osi_yaml},
            headers=_API_HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            detail = resp.json().get("detail", resp.text)
            return "", f"Error: {detail}", ""
        data = resp.json()
    except Exception as exc:
        return "", f"Error: OSI → OBML conversion failed\n{exc}", ""

    status = _format_convert_status(
        "OSI → OBML Import", data.get("warnings", []), data.get("validation", {})
    )
    return data.get("output_yaml", ""), status, ""


def _export_to_osi(obml_yaml: str, api_base: str) -> tuple[str, str]:
    """Convert OBML YAML to OSI via the API. Returns ``(status, explain)``."""
    if not obml_yaml or not obml_yaml.strip():
        return "Error: No OBML model YAML to export", ""

    try:
        resp = httpx.post(
            f"{api_base}/v1/convert/obml-to-osi",
            json={"input_yaml": obml_yaml},
            headers=_API_HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            detail = resp.json().get("detail", resp.text)
            return f"Error: {detail}", ""
        data = resp.json()
    except Exception as exc:
        return f"Error: OBML → OSI conversion failed\n{exc}", ""

    status = _format_convert_status(
        "OBML → OSI Export", data.get("warnings", []), data.get("validation", {})
    )
    output: str = data.get("output_yaml", "")
    return status + "\nCopy the OSI YAML output below.\n\n" + output, ""


def _format_sql(sql: str) -> str:
    """Pretty-print SQL with keyword-per-line formatting."""
    import re

    formatted = sqlparse.format(
        sql,
        reindent=True,
        keyword_case="upper",
        indent_width=2,
        wrap_after=80,
    )
    # sqlparse doesn't break after UNION ALL — ensure newline before next SELECT
    # Capture leading indentation so the new SELECT line keeps alignment
    formatted = re.sub(
        r"^(\s*)(UNION ALL(?:\s+BY NAME)?)\s+(SELECT\b)",
        r"\1\2\n\1\3",
        formatted,
        flags=re.MULTILINE,
    )
    return formatted


def _fetch_diagram_er(
    model_yaml: str,
    show_columns: bool,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
    theme: str = "dark",
) -> tuple[str, str, dict[str, str] | None, dict[str, str] | None]:
    """Fetch a Mermaid ER diagram via the REST API.

    Falls back to local generation (using ``service.diagram``) when the API
    is not reachable.  *theme* is the Mermaid theme name (``"dark"`` or
    ``"default"``), injected by JS based on the active Gradio colour scheme.
    Returns ``(mermaid_md, raw_mermaid, session_state, model_state)``.
    """
    if not model_yaml or not model_yaml.strip():
        return "*No model YAML provided.*", "", session_state, model_state

    try:
        client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
            model_yaml, api_url, session_state, model_state
        )

        # Fetch ER diagram
        resp = client.get(
            f"/v1/sessions/{session_id}/models/{model_id}/diagram/er",
            params={"show_columns": show_columns, "theme": theme},
        )
        # Auto-recover from expired session (404)
        if resp.status_code == 404:
            client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
                model_yaml, api_url, None, None
            )
            resp = client.get(
                f"/v1/sessions/{session_id}/models/{model_id}/diagram/er",
                params={"show_columns": show_columns, "theme": theme},
            )
        resp.raise_for_status()
        mermaid: str = resp.json()["mermaid"]
        return f"```mermaid\n{mermaid}\n```", mermaid, session_state, model_state

    except _ModelValidationError as exc:
        return f"**Model validation failed:** {exc}", "", session_state, model_state
    except httpx.ConnectError:
        # API not available — fall back to local generation
        md, raw = _generate_mermaid_er_local(model_yaml, show_columns, theme=theme)
        return md, raw, session_state, model_state
    except httpx.HTTPStatusError as exc:
        return (
            f"**Error:** HTTP {exc.response.status_code} — {exc.response.text}",
            "",
            session_state,
            model_state,
        )
    except Exception as exc:
        return f"**Error:** {exc}", "", session_state, model_state


def _generate_mermaid_er_local(
    model_yaml: str, show_columns: bool = True, *, theme: str = "dark"
) -> tuple[str, str]:
    """Generate a Mermaid ER diagram locally from raw OBML YAML (no API).

    Returns ``(markdown, raw_mermaid)``."""
    from orionbelt.parser.loader import TrackedLoader
    from orionbelt.parser.resolver import ReferenceResolver
    from orionbelt.service.diagram import generate_mermaid_er

    try:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(model_yaml)
        resolver = ReferenceResolver()
        model, result = resolver.resolve(raw, source_map)
        if not result.valid:
            msgs = "; ".join(e.message for e in result.errors)
            return f"**Model validation failed:** {msgs}", ""
        mermaid = generate_mermaid_er(model, show_columns=show_columns, theme=theme)
        return f"```mermaid\n{mermaid}\n```", mermaid
    except Exception as exc:
        return f"**Error:** {exc}", ""


def _load_example_model() -> str:
    """Load the bundled example OBML model, or return a placeholder."""
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parents[3] / "examples" / "sem-layer.obml.yml",
        Path.cwd() / "examples" / "sem-layer.obml.yml",
    ]
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return "# Place your OBML model YAML here\n"


_cached_dialects: dict[str, list[str]] = {}
_cached_settings: dict[str, dict[str, Any]] = {}


def _fetch_dialects(api_url: str) -> list[str]:
    """Fetch dialect names from the API, falling back to hardcoded list (cached)."""
    url = api_url.rstrip("/")
    if url in _cached_dialects:
        return _cached_dialects[url]
    try:
        resp = httpx.get(f"{url}/v1/dialects", timeout=5, headers=_API_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        names = [d["name"] for d in data.get("dialects", [])]
        result = names if names else _FALLBACK_DIALECTS
    except Exception:
        result = _FALLBACK_DIALECTS
    _cached_dialects[url] = result
    return result


def _fetch_settings(api_url: str) -> dict[str, Any]:
    """Fetch public settings from the API. Returns empty dict on failure (cached)."""
    url = api_url.rstrip("/")
    if url in _cached_settings:
        return _cached_settings[url]
    try:
        resp = httpx.get(f"{url}/v1/settings", timeout=5, headers=_API_HEADERS)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
    except Exception:
        result = {}
    _cached_settings[url] = result
    return result


def _ensure_session_and_model(
    model_yaml: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
) -> tuple[httpx.Client, str, str, dict[str, str], dict[str, str]]:
    """Ensure a session and model exist, creating/uploading as needed.

    Returns ``(client, session_id, model_id, session_state, model_state)``.
    Creates a new session when *session_state* is ``None`` or the API URL
    changed.  Uploads the model when *model_state* is ``None`` or the model
    YAML changed (detected via MD5 hash).  Auto-recovers from expired
    sessions (HTTP 404).
    """
    api_url = api_url.rstrip("/") if api_url else _DEFAULT_API_URL
    model_hash = hashlib.md5(model_yaml.encode()).hexdigest()

    need_session = session_state is None or session_state.get("api_url") != api_url
    client = httpx.Client(base_url=api_url, timeout=30, headers=_API_HEADERS)

    # Create session if needed
    preloaded_model_count = 0
    if need_session:
        resp = client.post("/v1/sessions")
        resp.raise_for_status()
        sess_data = resp.json()
        session_id: str = sess_data["session_id"]
        preloaded_model_count = sess_data.get("model_count", 0)
        session_state = {"session_id": session_id, "api_url": api_url}
        model_state = None  # force model re-upload on new session
    else:
        assert session_state is not None  # for type narrowing
        session_id = session_state["session_id"]

    # Single-model mode: session already has a pre-loaded model
    if preloaded_model_count > 0 and model_state is None:
        resp = client.get(f"/v1/sessions/{session_id}/models")
        resp.raise_for_status()
        models = resp.json()
        if models:
            model_id = models[0]["model_id"]
            model_state = {"model_id": model_id, "model_hash": model_hash}
            return client, session_id, model_id, session_state, model_state

    # Upload model if needed
    need_model = model_state is None or model_state.get("model_hash") != model_hash

    if need_model:
        resp = client.post(
            f"/v1/sessions/{session_id}/models",
            json={"model_yaml": model_yaml},
        )
        # Auto-recover from expired session (404)
        if resp.status_code == 404:
            resp = client.post("/v1/sessions")
            resp.raise_for_status()
            session_id = resp.json()["session_id"]
            session_state = {"session_id": session_id, "api_url": api_url}
            resp = client.post(
                f"/v1/sessions/{session_id}/models",
                json={"model_yaml": model_yaml},
            )
        if resp.status_code == 422:
            raise _ModelValidationError(resp.json().get("detail", resp.text))
        resp.raise_for_status()
        model_id = resp.json()["model_id"]
        model_state = {"model_id": model_id, "model_hash": model_hash}
    else:
        assert model_state is not None  # for type narrowing
        model_id = model_state["model_id"]

    return client, session_id, model_id, session_state, model_state


class _ModelValidationError(Exception):
    """Raised when the API rejects a model with HTTP 422."""

    def __init__(self, detail: Any) -> None:
        self.detail = detail
        super().__init__(str(detail))


def _cleanup_session(session_state: dict[str, str] | None) -> None:
    """Delete the API session on browser tab close."""
    if session_state:
        with contextlib.suppress(Exception):
            httpx.Client(base_url=session_state["api_url"], timeout=5, headers=_API_HEADERS).delete(
                f"/v1/sessions/{session_state['session_id']}"
            )


def _build_explain_yaml(data: dict[str, Any]) -> str:
    """Build a human-readable YAML string from the compile response."""
    explain: dict[str, Any] = {}

    # Resolved info
    resolved = data.get("resolved")
    if resolved:
        explain["resolved"] = {}
        if resolved.get("fact_tables"):
            explain["resolved"]["fact_tables"] = resolved["fact_tables"]
        if resolved.get("dimensions"):
            explain["resolved"]["dimensions"] = resolved["dimensions"]
        if resolved.get("measures"):
            explain["resolved"]["measures"] = resolved["measures"]

    # Query plan explanation
    plan = data.get("explain")
    if plan:
        explain["plan"] = {}
        explain["plan"]["planner"] = plan.get("planner", "")
        explain["plan"]["planner_reason"] = plan.get("planner_reason", "")
        explain["plan"]["base_object"] = plan.get("base_object", "")
        explain["plan"]["base_object_reason"] = plan.get("base_object_reason", "")
        if plan.get("joins"):
            explain["plan"]["joins"] = [
                {
                    "from": j["from_object"],
                    "to": j["to_object"],
                    "columns": j.get("join_columns", []),
                    "reason": j.get("reason", ""),
                }
                for j in plan["joins"]
            ]
        if plan.get("where_filter_count"):
            explain["plan"]["where_filters"] = plan["where_filter_count"]
        if plan.get("having_filter_count"):
            explain["plan"]["having_filters"] = plan["having_filter_count"]
        if plan.get("has_totals"):
            explain["plan"]["has_totals"] = True
        if plan.get("cfl_legs"):
            explain["plan"]["cfl_legs"] = [
                {
                    "measure_source": leg["measure_source"],
                    "common_root": leg["common_root"],
                    "reason": leg.get("reason", ""),
                    "measures": leg.get("measures", []),
                    "joins": leg.get("joins", []),
                }
                for leg in plan["cfl_legs"]
            ]

    # Validation
    validation: dict[str, Any] = {}
    if not data.get("sql_valid", True):
        validation["sql_valid"] = False
    warnings = data.get("warnings", [])
    if warnings:
        validation["warnings"] = warnings
    if validation:
        explain["validation"] = validation

    if not explain:
        return ""
    return yaml.dump(explain, default_flow_style=False, sort_keys=False, allow_unicode=True)


def compile_sql(
    model_yaml: str,
    query_yaml: str,
    dialect: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
) -> tuple[str, str, dict[str, str] | None, dict[str, str] | None]:
    """Compile SQL by calling the OrionBelt REST API.

    Returns ``(sql_output, explain_yaml, updated_session_state, updated_model_state)``.
    """
    try:
        client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
            model_yaml, api_url, session_state, model_state
        )

        # Parse query YAML
        try:
            query_dict = yaml.safe_load(query_yaml)
        except yaml.YAMLError as exc:
            return f"Error: Invalid query YAML\n{exc}", "", session_state, model_state

        if not isinstance(query_dict, dict):
            return (
                "Error: Query YAML must be a mapping (dict), not a scalar or list",
                "",
                session_state,
                model_state,
            )

        # Auto-unwrap if user included a top-level "query:" key
        if "query" in query_dict and "select" not in query_dict:
            query_dict = query_dict["query"]

        # Compile query
        resp = client.post(
            f"/v1/sessions/{session_id}/query/sql",
            json={"model_id": model_id, "query": query_dict, "dialect": dialect},
        )
        # Auto-recover from expired session on compile (404)
        if resp.status_code == 404:
            client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
                model_yaml, api_url, None, None
            )
            resp = client.post(
                f"/v1/sessions/{session_id}/query/sql",
                json={"model_id": model_id, "query": query_dict, "dialect": dialect},
            )
        if resp.status_code in (400, 422):
            detail = resp.json().get("detail", resp.text)
            return (
                f"Error: Query compilation failed\n{_format_api_errors(detail)}",
                "",
                session_state,
                model_state,
            )
        resp.raise_for_status()
        data = resp.json()
        sql: str = data["sql"]
        formatted = _format_sql(sql)
        explain_yaml = _build_explain_yaml(data)

        # Surface validation state and warnings above the SQL output
        warnings: list[str] = data.get("warnings", [])
        sql_valid: bool = data.get("sql_valid", True)
        header_lines: list[str] = []
        if not sql_valid:
            header_lines.append("-- WARNING: SQL validation failed")
        for w in warnings:
            header_lines.append(f"-- WARNING: {w}")
        if header_lines:
            header_lines.append("")  # blank line before SQL
            return (
                "\n".join(header_lines) + "\n" + formatted,
                explain_yaml,
                session_state,
                model_state,
            )
        return formatted, explain_yaml, session_state, model_state

    except _ModelValidationError as exc:
        return (
            f"Error: Model validation failed\n{_format_api_errors(exc.detail)}",
            "",
            session_state,
            model_state,
        )
    except httpx.ConnectError:
        api = api_url.rstrip("/") if api_url else _DEFAULT_API_URL
        return (
            f"Error: Cannot connect to API at {api}\n"
            "Make sure the server is running: uv run orionbelt-api",
            "",
            session_state,
            model_state,
        )
    except httpx.HTTPStatusError as exc:
        return (
            f"Error: HTTP {exc.response.status_code}\n{exc.response.text}",
            "",
            session_state,
            model_state,
        )
    except Exception as exc:
        return f"Error: {exc}", "", session_state, model_state


def validate_model(
    model_yaml: str,
    api_url: str,
) -> tuple[str, str]:
    """Validate OBML YAML by calling the REST API.

    Returns ``(validation_output, detail_yaml)`` shown in the SQL and explain panels.
    """
    if not model_yaml or not model_yaml.strip():
        return "Error: No model YAML provided", ""

    api_url = api_url.rstrip("/") if api_url else _DEFAULT_API_URL
    try:
        resp = httpx.post(
            f"{api_url}/v1/validate",
            json={"model_yaml": model_yaml},
            timeout=30,
            headers=_API_HEADERS,
        )
        if resp.status_code in (400, 422):
            detail = resp.json().get("detail", resp.text)
            return f"Error: {_format_api_errors(detail)}", ""
        resp.raise_for_status()
        data = resp.json()

        errors: list[dict[str, str]] = data.get("errors", [])
        warnings: list[dict[str, str]] = data.get("warnings", [])
        valid: bool = data.get("valid", False)

        # Build detail YAML for explain panel
        detail_info: dict[str, Any] = {"valid": valid}
        if errors:
            detail_info["errors"] = [{k: v for k, v in e.items() if v} for e in errors]
        if warnings:
            detail_info["warnings"] = [{k: v for k, v in w.items() if v} for w in warnings]
        detail_yaml = yaml.dump(detail_info, default_flow_style=False, sort_keys=False)

        # Summary for SQL output panel (plain text, not SQL comments)
        if valid:
            summary = "Model is valid"
            if warnings:
                summary += f" ({len(warnings)} warning(s))"
        else:
            summary = f"Model validation FAILED — {len(errors)} error(s)"
            if warnings:
                summary += f", {len(warnings)} warning(s)"

        return summary, detail_yaml

    except httpx.ConnectError:
        return (
            f"Error: Cannot connect to API at {api_url}\n"
            "Make sure the server is running: uv run orionbelt-api",
            "",
        )
    except Exception as exc:
        return f"Error: {exc}", ""


def create_blocks(default_api_url: str | None = None) -> Any:
    """Build and return a ``gr.Blocks`` instance (without launching).

    Parameters
    ----------
    default_api_url:
        Override the default API URL shown in the UI.  When the UI is
        co-hosted inside FastAPI (mounted at ``/ui``), this is set to the
        local server address so the UI talks to the same process.
    """
    import gradio as gr

    from orionbelt import __version__

    cohosted = default_api_url is not None
    api_base = default_api_url or _DEFAULT_API_URL
    dialects = _fetch_dialects(api_base)
    default_dialect = (
        "postgres" if "postgres" in dialects else (dialects[0] if dialects else "postgres")
    )

    # Detect single-model mode from the API /settings endpoint
    api_settings = _fetch_settings(api_base)
    single_model = api_settings.get("single_model_mode", False)
    if single_model and api_settings.get("model_yaml"):
        example_model = api_settings["model_yaml"]
    else:
        example_model = _load_example_model()

    with gr.Blocks(
        title="OrionBelt Semantic Layer",
        css=_CSS,
        js=_DARK_MODE_INIT_JS,
    ) as demo:
        # ── Browser-persisted state (localStorage via Gradio BrowserState) ──
        saved_model = gr.BrowserState("", storage_key="ob_model_yaml")
        saved_query = gr.BrowserState("", storage_key="ob_query_yaml")
        saved_api = gr.BrowserState(api_base, storage_key="ob_api_url")
        saved_dialect = gr.BrowserState(default_dialect, storage_key="ob_dialect")
        saved_zoom = gr.BrowserState(100, storage_key="ob_zoom")
        saved_sql = gr.BrowserState("", storage_key="ob_sql_output")

        # ── Stateful API session (avoids re-creating per compile) ──
        session_state = gr.State(None)  # {"session_id": str, "api_url": str}
        model_state = gr.State(None)  # {"model_id": str, "model_hash": str}

        with gr.Row(elem_classes=["header-row"]):
            gr.Markdown(
                f"## OrionBelt Semantic Layer <small>v{__version__}</small>"
                " &nbsp; [Docs](https://ralforion.com/orionbelt-semantic-layer/)"
            )
            dark_btn = gr.Button("Light / Dark", size="sm", scale=0, min_width=120)

        with gr.Tabs():
            with gr.Tab("SQL Compiler", id=0):
                with gr.Row(elem_classes=["settings-row"]):
                    dialect = gr.Dropdown(
                        choices=dialects,
                        value=default_dialect,
                        label="SQL Dialect",
                        scale=1,
                    )
                    api_url = gr.Textbox(
                        value=api_base,
                        label="API Base URL",
                        scale=2,
                        interactive=not cohosted,
                    )
                    import_osi_btn = gr.Button(
                        "Import OSI",
                        size="sm",
                        scale=0,
                        min_width=100,
                        visible=not single_model,
                    )
                    export_osi_btn = gr.Button("Export to OSI", size="sm", scale=0, min_width=120)

                with gr.Row(equal_height=True):
                    model_label = (
                        "OBML Model (YAML) \u2014 read-only (single-model mode)"
                        if single_model
                        else "OBML Model (YAML) \u2014 schema/obml-schema.json"
                    )
                    model_input = gr.Code(
                        value=example_model,
                        language="yaml",
                        label=model_label,
                        lines=11,
                        scale=3,
                        interactive=not single_model,
                        elem_classes=["code-editor"],
                        elem_id="ob-model",
                    )
                    query_input = gr.Code(
                        value=_DEFAULT_QUERY,
                        language="yaml",
                        label="Query (YAML) \u2014 schema/query-schema.json",
                        lines=11,
                        scale=2,
                        interactive=True,
                        elem_classes=["code-editor"],
                        elem_id="ob-query",
                    )

                # Hidden textboxes: JS writes file content here → Python
                # forwards to Code editors (bridges JS↔Gradio state).
                model_bridge = gr.Textbox(
                    elem_id="ob-model-bridge",
                    container=False,
                    elem_classes=["ob-bridge"],
                )
                query_bridge = gr.Textbox(
                    elem_id="ob-query-bridge",
                    container=False,
                    elem_classes=["ob-bridge"],
                )
                model_bridge.change(
                    fn=lambda x: x,
                    inputs=[model_bridge],
                    outputs=[model_input],
                )
                query_bridge.change(
                    fn=lambda x: x,
                    inputs=[query_bridge],
                    outputs=[query_input],
                )

                # OSI import bridge: JS file picker → bridge → Python converter
                osi_bridge = gr.Textbox(
                    elem_id="ob-osi-bridge",
                    container=False,
                    elem_classes=["ob-bridge"],
                )
                import_osi_btn.click(fn=None, js=_IMPORT_OSI_JS)

                with gr.Row():
                    compile_btn = gr.Button(
                        "Compile SQL", variant="primary", elem_classes=["purple-btn"]
                    )
                    validate_btn = gr.Button(
                        "Validate Model", variant="secondary", scale=0, min_width=140
                    )

                with gr.Row():
                    sql_output = gr.Code(
                        language="sql",
                        label="Generated SQL",
                        interactive=False,
                        lines=3,
                        elem_classes=["sql-output"],
                        elem_id="ob-sql",
                    )
                    explain_output = gr.Code(
                        language="yaml",
                        label="Query Explain",
                        interactive=False,
                        lines=3,
                        elem_classes=["sql-output"],
                        elem_id="ob-explain",
                    )

                compile_btn.click(
                    fn=compile_sql,
                    inputs=[
                        model_input,
                        query_input,
                        dialect,
                        api_url,
                        session_state,
                        model_state,
                    ],
                    outputs=[sql_output, explain_output, session_state, model_state],
                )
                validate_btn.click(
                    fn=validate_model,
                    inputs=[model_input, api_url],
                    outputs=[sql_output, explain_output],
                )

                # Wire OSI bridge + export after sql_output exists
                osi_bridge.change(
                    fn=_import_osi,
                    inputs=[osi_bridge, api_url],
                    outputs=[model_input, sql_output, explain_output],
                )
                export_osi_btn.click(
                    fn=_export_to_osi,
                    inputs=[model_input, api_url],
                    outputs=[sql_output, explain_output],
                )

            with gr.Tab("ER Diagram", id=1) as er_tab:
                with gr.Row():
                    show_columns_cb = gr.Checkbox(value=True, label="Show columns")
                    zoom_slider = gr.Slider(
                        minimum=10,
                        maximum=200,
                        value=100,
                        step=10,
                        label="Zoom %",
                        scale=1,
                    )
                    er_btn = gr.Button(
                        "Refresh Diagram",
                        variant="primary",
                        elem_classes=["purple-btn"],
                    )
                    dl_md_btn = gr.Button("↓ .md", scale=0, min_width=60, size="sm")
                    dl_png_btn = gr.Button("↓ .png", scale=0, min_width=60, size="sm")

                # Hidden inputs — JS injects the Mermaid theme at call time;
                # mermaid_raw stores the raw Mermaid text for downloads.
                theme_input = gr.Textbox(value="dark", visible=False)
                mermaid_raw = gr.Textbox(value="", visible=False)

                mermaid_output = gr.Markdown(
                    value="*Click 'Refresh Diagram' to generate the ER diagram "
                    "from the model YAML.*",
                    elem_id="er-diagram",
                )

                _apply_zoom_js = """(zoom) => {
                    const el = document.querySelector('#er-diagram svg');
                    if (el) el.style.transform = 'scale(' + (zoom / 100) + ')';
                }"""

                # After diagram generation, Mermaid renders the SVG asynchronously.
                # Poll until the SVG appears, then apply the zoom transform.
                _apply_zoom_deferred_js = """(zoom) => {
                    let tries = 0;
                    const t = setInterval(() => {
                        const el = document.querySelector('#er-diagram svg');
                        if (el) {
                            el.style.transform = 'scale(' + (zoom / 100) + ')';
                            clearInterval(t);
                        }
                        if (++tries > 30) clearInterval(t);
                    }, 100);
                }"""

                er_btn.click(
                    fn=_fetch_diagram_er,
                    inputs=[
                        model_input,
                        show_columns_cb,
                        api_url,
                        session_state,
                        model_state,
                        theme_input,
                    ],
                    outputs=[mermaid_output, mermaid_raw, session_state, model_state],
                    js=_DETECT_THEME_JS,
                ).then(
                    fn=None,
                    inputs=[zoom_slider],
                    js=_apply_zoom_deferred_js,
                )

                er_tab.select(
                    fn=_fetch_diagram_er,
                    inputs=[
                        model_input,
                        show_columns_cb,
                        api_url,
                        session_state,
                        model_state,
                        theme_input,
                    ],
                    outputs=[mermaid_output, mermaid_raw, session_state, model_state],
                    js=_DETECT_THEME_JS,
                ).then(
                    fn=None,
                    inputs=[zoom_slider],
                    js=_apply_zoom_deferred_js,
                )

                zoom_slider.change(
                    fn=None,
                    inputs=[zoom_slider],
                    js=_apply_zoom_js,
                )

                dl_md_btn.click(
                    fn=None,
                    inputs=[mermaid_raw],
                    js=_DOWNLOAD_MD_JS,
                )
                dl_png_btn.click(
                    fn=None,
                    js=_DOWNLOAD_PNG_JS,
                )

            with gr.Tab("Settings", id=2) as settings_tab:
                settings_output = gr.Code(
                    language="yaml",
                    label="API Settings",
                    interactive=False,
                    lines=10,
                )

                def _fetch_settings_yaml(api_url_val: str) -> str:
                    url = api_url_val.rstrip("/") if api_url_val else _DEFAULT_API_URL
                    try:
                        resp = httpx.get(f"{url}/v1/settings", timeout=5, headers=_API_HEADERS)
                        resp.raise_for_status()
                        data = resp.json()
                        # Remove model_yaml from display (too large)
                        data.pop("model_yaml", None)
                        return yaml.dump(data, default_flow_style=False, sort_keys=False)
                    except httpx.ConnectError:
                        return f"# Error: Cannot connect to API at {url}"
                    except Exception as exc:
                        return f"# Error: {exc}"

                settings_tab.select(
                    fn=_fetch_settings_yaml,
                    inputs=[api_url],
                    outputs=[settings_output],
                )

        # ── Toggle: Python saves inputs → BrowserState, then JS redirects ──
        dark_btn.click(
            fn=lambda m, q, a, d, z, s: (m, q, a, d, z, s),
            inputs=[model_input, query_input, api_url, dialect, zoom_slider, sql_output],
            outputs=[
                saved_model,
                saved_query,
                saved_api,
                saved_dialect,
                saved_zoom,
                saved_sql,
            ],
        ).then(
            fn=None,
            js=_THEME_REDIRECT_JS,
        )

        # ── On page load: restore from BrowserState → visible components ──
        def _restore(sm, sq, sa, sd, sz, ss):  # type: ignore[no-untyped-def]
            return (
                example_model if single_model else (sm if sm else example_model),
                sq if sq else _DEFAULT_QUERY,
                sa if sa else api_base,
                sd if sd else default_dialect,
                sz if sz else 100,
                ss if ss else "",
            )

        # In single-model mode, skip injecting the file upload button for the
        # model editor (it's read-only).  The query upload button still applies.
        inject_js = _INJECT_UPLOAD_JS
        if single_model:
            inject_js = inject_js.replace(
                "addUploadBtn('ob-model', 'ob-model-bridge');",
                "/* single-model mode: model upload disabled */",
            )

        demo.load(
            fn=_restore,
            inputs=[saved_model, saved_query, saved_api, saved_dialect, saved_zoom, saved_sql],
            outputs=[model_input, query_input, api_url, dialect, zoom_slider, sql_output],
        ).then(fn=None, js=inject_js)

        # Session cleanup: API sessions expire automatically via SESSION_TTL_SECONDS.
        # Gradio's demo.unload() cannot access gr.State, so we rely on TTL expiry
        # and auto-recovery in _ensure_session_and_model() for stale sessions.

    return demo


def create_ui() -> None:
    """Build and launch the Gradio interface (standalone mode).

    When ``ROOT_PATH`` is set (e.g. ``/ui``), Gradio is mounted inside a
    FastAPI wrapper at that path so the load balancer can forward
    ``/ui/*`` without stripping the prefix.
    """
    import os

    import uvicorn

    api_url = os.environ.get("API_BASE_URL") or None
    port = int(os.environ.get("PORT", "7860"))
    root_path = os.environ.get("ROOT_PATH", "")
    demo = create_blocks(default_api_url=api_url)

    if root_path:
        import gradio as gr
        from fastapi import FastAPI

        app = FastAPI()
        app = gr.mount_gradio_app(app, demo, path=root_path)
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            proxy_headers=True,
            forwarded_allow_ips="*",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
    else:
        demo.launch(
            server_name="0.0.0.0",
            server_port=port,
        )


def main() -> None:
    """Entry point for ``orionbelt-ui`` console script."""
    create_ui()
