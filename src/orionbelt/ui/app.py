"""Gradio demo UI — thin HTTP client for the OrionBelt REST API.

This module owns the Gradio assembly (``create_blocks``/``create_ui``/``main``)
plus the large CSS/JS string constants. The HTTP/auth client, rendering, and
query/model handlers live in sibling modules and are re-exported here so that
``create_blocks`` (which references them by bare name) and the test suite
(which monkeypatches ``ui_app.<name>``) keep working unchanged.
"""

from __future__ import annotations

# ``time`` is kept imported so tests can patch ``ui_app.time.sleep`` for the
# re-exported ``_fetch_settings`` retry loop.
import time  # noqa: F401
from typing import Any

import gradio as gr
import httpx
import yaml

# Re-exported HTTP/auth client surface (see orionbelt.ui.api_client). These
# names are imported into this module's namespace so create_blocks/create_ui
# can reference them by bare name and tests can monkeypatch ``ui_app.<name>``.
from orionbelt.ui.api_client import (
    _API_HEADERS,
    _DEFAULT_API_KEY_HEADER,
    _DEFAULT_API_URL,
    _FALLBACK_DIALECTS,
    _ensure_session_and_model,
    _export_to_osi,
    _fetch_diagram_er,
    _fetch_dialects,
    _fetch_obsl_turtle,
    _fetch_settings,
    _format_api_errors,
    _import_osi,
    _ModelValidationError,
    _warn_if_auth_required_without_key,
    set_api_credentials,
)
from orionbelt.ui.handlers import (
    _build_explain_yaml,
    _composable_sets,
    _decorate_choices,
    _extract_model_items,
    _insert_into_query,
    _load_example_model,
    _resolve_execution_dialect,
    clear_filters_and_execute,
    compile_sql,
    execute_query,
    filter_and_execute,
    filter_chip_update,
    model_jump_targets,
    sort_and_execute,
    sort_state_str,
    validate_model,
)
from orionbelt.ui.rendering import (
    _format_convert_status,
    _format_sql,
    _generate_mermaid_er_local,
    _generate_ontology_graph_html,
    _get_vis_network_b64,
    _render_ontology_graph,
)

__all__ = [
    "_API_HEADERS",
    "_DEFAULT_API_KEY_HEADER",
    "_DEFAULT_API_URL",
    "_FALLBACK_DIALECTS",
    "_ModelValidationError",
    "_build_explain_yaml",
    "_composable_sets",
    "_decorate_choices",
    "_ensure_session_and_model",
    "_export_to_osi",
    "_extract_model_items",
    "_fetch_diagram_er",
    "_fetch_dialects",
    "_fetch_obsl_turtle",
    "_fetch_settings",
    "_format_api_errors",
    "_format_convert_status",
    "_format_sql",
    "_generate_mermaid_er_local",
    "_generate_ontology_graph_html",
    "_get_vis_network_b64",
    "_import_osi",
    "_insert_into_query",
    "_load_example_model",
    "_render_ontology_graph",
    "_resolve_execution_dialect",
    "_warn_if_auth_required_without_key",
    "compile_sql",
    "create_blocks",
    "create_ui",
    "execute_query",
    "main",
    "set_api_credentials",
    "validate_model",
]


# GitHub SVG icon (Octicon mark-github)
_GITHUB_SVG = (  # noqa: E501
    '<svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>'  # noqa: E501
)

# Logo images as base64 data URIs (dark text for light mode, white text for dark mode)
_LOGO_DARK_URI = "data:image/png;base64,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"  # noqa: E501
_LOGO_LIGHT_URI = "data:image/png;base64,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"  # noqa: E501


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
orderBy:
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
.header-row { min-height: 0 !important; padding: 0 4px !important; align-items: center !important; }
.header-row h2 { margin: 0 !important; }
.header-bar {
  display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; width: 100%;
}
.header-bar .header-brand {
  display: flex; align-items: flex-end; gap: 10px;
}
.header-bar img.logo-dark { display: inline-block; }
.header-bar img.logo-light { display: none; }
.dark .header-bar img.logo-dark { display: none; }
.dark .header-bar img.logo-light { display: inline-block; }
.header-bar .header-title {
  font-size: 22px; font-weight: 600; white-space: nowrap; line-height: 1; padding-bottom: 2px;
}
.header-bar .header-version {
  font-size: 16px; opacity: 0.6; white-space: nowrap;
}
.header-bar .header-links {
  display: flex; align-items: flex-end; gap: 14px; margin-left: auto; white-space: nowrap;
  line-height: 1; padding-bottom: 2px;
}
.header-bar .header-links a {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 15px; text-decoration: none; opacity: 0.8;
  color: var(--body-text-color) !important;
}
.header-bar .header-links a:hover { opacity: 1; }
.header-bar .header-links a svg { width: 18px; height: 18px; fill: currentColor; }
/* compact settings row — label inline with input via .settings-pair */
.settings-row {
  min-height: 0 !important;
  align-items: center !important;
  flex-wrap: nowrap !important;
}
.settings-pair {
  flex-wrap: nowrap !important;
  align-items: center !important;
  justify-content: flex-start !important;
  gap: 6px !important;
  min-height: 0 !important;
  flex: 0 0 auto !important;
  width: auto !important;
  padding: 0 !important;
}
.settings-pair > * {
  align-self: center !important;
  flex: 0 0 auto !important;
  width: auto !important;
  min-width: 0 !important;
}
/* Gradio wraps gr.HTML in a div — collapse it to its content width */
.settings-pair > div:has(> .settings-label),
.settings-pair > .prose,
.settings-pair > .html-container {
  flex: 0 0 auto !important;
  width: auto !important;
  min-width: 0 !important;
  padding: 0 !important;
}
/* Constrain dropdown/textbox wrappers so they don't fill the row */
.settings-pair .wrap,
.settings-pair .form,
.settings-pair .block {
  width: auto !important;
  min-width: 0 !important;
}
.settings-pair input,
.settings-pair textarea {
  min-width: 360px !important;
  white-space: nowrap !important;
  overflow-x: auto !important;
}
.settings-pair textarea {
  resize: none !important;
  overflow-y: hidden !important;
  height: 32px !important;
  line-height: 1.4 !important;
}
.settings-pair .secondary-wrap,
.settings-pair .options { min-width: 160px !important; }
.settings-label {
  font-size: 0.85rem;
  font-weight: 500;
  white-space: nowrap;
  padding: 0 !important;
  margin: 0 !important;
  opacity: 0.85;
  line-height: 32px;
  display: inline-block !important;
}
.settings-spacer {
  flex: 1 1 auto !important;
  min-width: 0 !important;
  width: auto !important;
  padding: 0 !important;
  background: transparent !important;
}

/* ── Vertically responsive editors (viewport-relative) ──
   Constrain ROW height; make .code-editor a flex column so CodeMirror's
   wrapper (last child) absorbs all remaining space inside its block. */
.editor-row {
  height: 42dvh !important;
  max-height: 42dvh !important;
  min-height: 240px !important;
}
.output-row {
  height: 32dvh !important;
  max-height: 32dvh !important;
  min-height: 140px !important;
}

/* gr.Code blocks become flex columns; their CodeMirror child fills remaining space */
.editor-row .code-editor,
.output-row .code-editor {
  height: 100% !important;
  max-height: 100% !important;
  display: flex !important;
  flex-direction: column !important;
}
.editor-row .code-editor > *,
.output-row .code-editor > * {
  min-height: 0 !important;
}
.editor-row .code-editor > *:last-child,
.output-row .code-editor > *:last-child {
  flex: 1 1 0 !important;
}
.editor-row .cm-editor,
.output-row .cm-editor {
  height: 100% !important;
  max-height: 100% !important;
  min-height: 0 !important;
}
.editor-row .cm-scroller,
.output-row .cm-scroller { max-height: none !important; }

/* Picker col: dropdowns auto-height on top, query editor fills rest */
.picker-col {
  height: 100% !important;
  display: flex !important;
  flex-direction: column !important;
  overflow: hidden;
}
.picker-col > .picker-row { flex: 0 0 auto !important; }
.picker-col > .code-editor {
  flex: 1 1 0 !important;
  height: auto !important;
  min-height: 0 !important;
}

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
.orange-btn {
  background: linear-gradient(135deg, #ea580c, #f97316) !important;
  border: none !important;
  color: white !important;
  padding-top: 6px !important;
  padding-bottom: 6px !important;
  margin: 0 !important;
}
.orange-btn:hover {
  background: linear-gradient(135deg, #c2410c, #ea580c) !important;
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

/* ── Model picker dropdowns ── */
.picker-col {
  gap: 0 !important; padding: 0 !important;
}
.picker-row {
  min-height: 0 !important; padding: 0 !important;
  margin: 0 !important; flex: 0 0 auto !important;
  flex-wrap: nowrap !important;
}
.picker-row > div { gap: 4px !important; flex-wrap: nowrap !important; }
.picker-row label span {
  font-size: 0.75rem !important;
}
/* Magnifier icon hint inside the 3 query-input dropdowns
   (Dimensions / Measures-Metrics / Columns) so users discover the
   type-to-search affordance. Gradio's Dropdown renders <input> as
   the search field; the icon is drawn as a non-interactive
   background image and the input gets matching left-padding so the
   glyph doesn't collide with typed text. */
.picker-dropdown input {
  background-image: url("__SEARCH_GLASS_SVG__");
  background-repeat: no-repeat;
  background-position: 8px center;
  background-size: 14px 14px;
  padding-left: 28px !important;
}

/* ── ER Diagram tab ── */
#er-diagram {
  overflow: auto;
  border: 1px solid var(--border-color-primary);
  border-radius: 8px;
  padding: 8px;
  height: calc(100dvh - 220px);
  min-height: 400px;
}
#er-diagram svg {
  transform-origin: top left;
  transition: transform 0.15s ease;
  /* Mermaid's ER renderer auto-sizes columns from getBBox(). Gradio's
     default Markdown CSS sets max-width:100% on inline SVGs, which
     causes the SVG to scale down and the renderer's pre-measured
     column widths to clip attribute text. Allow the natural width and
     let the parent's overflow:auto provide horizontal scroll. */
  max-width: none !important;
  width: auto;
  height: auto;
}
/* Don't override font-size on ER text: Mermaid measures column widths
   with its own font and clips at the measured width. Inflating the
   rendered font past the measured width was the cause of the per-row
   right-edge clipping. */
/* Force the same font family Mermaid measures column widths with (pinned
   in the diagram's %%{init}%% themeVariables.fontFamily) so the host page
   CSS can't cascade a wider font onto the painted text and re-introduce
   the per-cell right-edge clipping ("string" → "strin"). */
#er-diagram svg text,
#er-diagram svg .er .entityLabel,
#er-diagram svg .er.relationshipLabel {
  font-family: Helvetica, Arial, sans-serif !important;
}
/* ── Ontology Graph tab ── */
#ob-ontology-graph-container {
  overflow: auto;
  border-radius: 8px;
}
.ob-cb-do label span::before { content: '● '; color: #9E9E9E; }
.ob-cb-dim label span::before { content: '● '; color: #4CAF50; }
.ob-cb-meas label span::before { content: '● '; color: #2196F3; }
.ob-cb-met label span::before { content: '● '; color: #9C27B0; }
.ob-cb-joins label span::before { content: '◆ '; color: #9E9E9E; }

/* ── Results table: scroll wide tables horizontally instead of overflowing
   (Gradio's .table-wrap defaults to overflow-x:visible, so the extra columns
   spilled off and got clipped by the container on small screens). ── */
.result-table .table-wrap { overflow-x: auto !important; }
/* Disable Gradio's native client-side column sort + its "3-dot" cell menu.
   It's a browser-only reorder (Gradio: "sorting in the browser will not affect
   the values") and it's *sticky* — it re-applies to every new result, silently
   overriding the server-side ORDER BY. Sorting is done via the query instead. */
.result-table thead th,
.result-table .header-button { pointer-events: none !important; cursor: default !important; }
.result-table .cell-menu,
.result-table .cell-menu-button,
.result-table .menu-button,
.result-table .sort-arrow { display: none !important; }
/* Custom per-header sort controls — re-enable pointer events over the disabled th. */
.result-table .ob-sort { pointer-events: auto; margin-left: 10px; white-space: nowrap;
  font-size: 18px; opacity: 0.6; letter-spacing: 3px; vertical-align: middle; }
.result-table .ob-sort-btn { pointer-events: auto; cursor: pointer; padding: 0 3px;
  user-select: none; }
.result-table .ob-sort-btn:hover { opacity: 1; color: #ff7a1a; }
.result-table .ob-sort-btn.ob-active { opacity: 1; color: #ff7a1a; }
/* Clear-filters button: full-width row, one line, text left-aligned. NOTE: Gradio
   puts elem_classes on the <button> itself, so target .filter-chip directly (a
   ".filter-chip button" selector matches nothing). */
.filter-chip { width: 100% !important; white-space: nowrap !important;
  justify-content: flex-start !important; text-align: left !important;
  font-size: 14px !important; }

/* Keep the Copy Data / ↓ TSV actions side by side on one row, even on phones. */
.result-actions {
  flex-wrap: nowrap !important;
  gap: 8px !important;
  flex: 0 0 auto !important;
  min-width: 0 !important;
}

/* ── Responsive: narrower viewports ── */
@media (max-width: 900px) {
  .settings-row { flex-wrap: wrap !important; }
  .picker-row { flex-wrap: wrap !important; }
  .picker-row > div { flex-wrap: wrap !important; }
  .header-bar .header-links { gap: 8px; }
  .header-bar .header-title { font-size: 18px; }
}

/* ── Responsive: phones — stack columns, drop fixed heights/min-widths ──
   On phones the side-by-side editor/picker columns collapse and the fixed
   viewport-relative (dvh) row heights squash the dropdowns to nothing. Stack
   everything vertically with explicit heights, and let inputs fill the row
   instead of overflowing (the desktop 360px min-width pushed fields off-screen). */
@media (max-width: 768px) {
  .gradio-container { padding: 4px 8px !important; overflow-x: hidden !important; }

  /* Header: give the brand/links block the full row so the theme button drops
     to its own line and the links wrap cleanly instead of being clipped. */
  .header-row { flex-wrap: wrap !important; row-gap: 8px !important; }
  .header-row > *:first-child { flex: 1 1 100% !important; width: 100% !important; }
  .header-bar {
    flex-direction: column !important; align-items: flex-start !important; gap: 6px !important;
  }
  .header-bar img.logo-dark,
  .header-bar img.logo-light { height: 26px !important; }
  .header-bar .header-brand { align-items: center !important; }
  .header-bar .header-title { white-space: normal !important; }
  .header-bar .header-links {
    margin-left: 0 !important; flex-wrap: wrap !important; white-space: normal !important;
    gap: 12px !important;
  }

  /* Settings: one control per row, full width (no 360px overflow) */
  .settings-row { flex-wrap: wrap !important; }
  .settings-pair { width: 100% !important; flex-wrap: wrap !important; }
  .settings-pair input,
  .settings-pair textarea { min-width: 0 !important; width: 100% !important; }
  .settings-pair .secondary-wrap,
  .settings-pair .options { min-width: 0 !important; width: 100% !important; }
  .settings-spacer { display: none !important; }

  /* Main editors + pickers: stack vertically with real heights so CodeMirror
     and the Dimensions/Measures/Columns dropdowns get usable space. */
  .editor-row, .output-row {
    height: auto !important; max-height: none !important; flex-direction: column !important;
  }
  .editor-row > *, .output-row > * { width: 100% !important; min-width: 0 !important; }
  .editor-row > .code-editor { height: 300px !important; min-height: 300px !important; }
  .output-row .code-editor { height: 240px !important; min-height: 240px !important; }
  .picker-col { height: auto !important; }
  .picker-col > .code-editor {
    height: 240px !important; min-height: 240px !important; flex: 0 0 auto !important;
  }
  .picker-row { flex-wrap: wrap !important; }
  .picker-dropdown { width: 100% !important; min-width: 0 !important; }

  /* Diagram/graph canvases: fit the smaller viewport */
  #er-diagram { height: 60dvh !important; min-height: 300px !important; }

  /* Smaller type on phones to fit more on screen */
  .gradio-container { font-size: 13px !important; }
  .header-bar .header-title { font-size: 16px !important; }
  .header-bar .header-version,
  .header-bar .header-links a { font-size: 12px !important; }
  .cm-editor { font-size: 12px !important; }
  /* Results table is the worst offender — shrink cells + padding */
  .result-table table { font-size: 11px !important; }
  .result-table th button,
  .result-table th span,
  .result-table td { font-size: 11px !important; }
  .result-table th,
  .result-table td { padding: 2px 6px !important; }
}
"""

# Inline SVG search-glass for the .picker-dropdown CSS rule. Defined here
# (not inside _CSS) so the URL doesn't trip ruff E501 — the data: URL is
# unavoidably long.
_SEARCH_GLASS_SVG = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' "
    "fill='none' stroke='%23888' stroke-width='2' stroke-linecap='round' "
    "stroke-linejoin='round'>"
    "<circle cx='11' cy='11' r='7'/>"
    "<line x1='21' y1='21' x2='16.65' y2='16.65'/>"
    "</svg>"
)
_CSS = _CSS.replace("__SEARCH_GLASS_SVG__", _SEARCH_GLASS_SVG)

_JUMP_TO_JS = """
(line_str) => {
    var line = parseInt(line_str, 10);
    if (!line) return;
    var scroller = document.querySelector('#ob-model .cm-scroller');
    if (!scroller) return;
    var lineEl = scroller.querySelector('.cm-line');
    var lh = lineEl ? lineEl.getBoundingClientRect().height : 19;
    // Land the target line at the very top of the editor.
    scroller.scrollTo({ top: Math.max(0, (line - 1) * lh), behavior: 'smooth' });
}
"""

_ALIGN_HEADERS_JS = """
(indices_str) => {
    var old = document.getElementById('ob-hdr-align');
    if (old) old.remove();
    if (indices_str) {
        var nums = indices_str.split(',').map(Number);
        var css = '';
        for (var j = 0; j < nums.length; j++) {
            css += '.result-table th:nth-child(' + nums[j] +
                   ') button>span{text-align:right!important}';
        }
        var tag = document.createElement('style');
        tag.id = 'ob-hdr-align';
        tag.textContent = css;
        document.head.appendChild(tag);
    }
    // Inject per-column sort controls (up / down / clear) into each header. They
    // write a hidden signal textbox that triggers a server-side re-query. Value-
    // less selects the injection may cause are now harmless (handlers use EventData).
    var tries = 0;
    var iv = setInterval(function () {
        var root = document.querySelector('.result-table');
        var ths = root ? root.querySelectorAll('th') : [];
        if (!ths.length) { if (++tries > 25) clearInterval(iv); return; }
        clearInterval(iv);
        // Active orderBy ("field|dir" per line) -> colour the matching ▲/▼ orange.
        var sortState = (document.querySelector('#ob-sort-state textarea') || {}).value || '';
        var sortMap = {};
        sortState.split('\\n').forEach(function (s) {
            var kv = s.split('|');
            if (kv[0]) sortMap[kv[0]] = kv[1];
        });
        ths.forEach(function (th) {
            var existing = th.querySelector('.ob-sort');
            if (existing) existing.remove();           // re-inject to refresh active colour
            var btn = th.querySelector('button');
            var label = ((btn || th).textContent || '').trim();
            if (!label || label === '#') return;       // skip the index + empty columns
            var wrap = document.createElement('span');
            wrap.className = 'ob-sort';
            [['\\u25B2', 'asc'], ['\\u25BC', 'desc'], ['\\u2715', 'clear']].forEach(function (p) {
                var b = document.createElement('span');
                b.textContent = p[0];
                b.className = 'ob-sort-btn' + (sortMap[label] === p[1] ? ' ob-active' : '');
                b.title = (p[1] === 'clear' ? 'Clear sort on ' : 'Sort ' + p[1] + ' by ') + label;
                b.addEventListener('click', function (e) {
                    e.stopPropagation();
                    e.preventDefault();
                    var signal = document.querySelector('#ob-sort-signal textarea');
                    if (!signal) return;
                    signal.value = label + '|' + p[1] + '|' + Date.now();
                    signal.dispatchEvent(new Event('input', { bubbles: true }));
                });
                wrap.appendChild(b);
            });
            (btn || th).appendChild(wrap);             // inline, next to the label
        });
    }, 150);
}
"""


_DARK_MODE_INIT_JS = """
() => {
    if (!window.location.search.includes('__theme=')) {
        const url = new URL(window.location);
        url.searchParams.set('__theme', 'dark');
        window.location.replace(url.href);
    }
    // Patch metadata download filename on click (capture phase, before browser acts)
    document.addEventListener('click', function(e) {
        var a = e.target.closest('#ob-meta-code a.download-link');
        if (a) a.download = 'response_metadata.yaml';
    }, true);
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

# JS: download OBSL Turtle as a .ttl file
_DOWNLOAD_TTL_JS = """(turtle) => {
    if (!turtle) { alert('No OBSL graph available. Load a model first.'); return; }
    var blob = new Blob([turtle], {type: 'text/turtle'});
    var a = document.createElement('a');
    a.download = 'obsl-model.ttl';
    a.href = URL.createObjectURL(blob);
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
}"""

# JS: download the exported OSI model as a .osi.yaml file
_DOWNLOAD_OSI_JS = """(osiYaml) => {
    if (!osiYaml) { alert('No OSI model to export. Load a model first.'); return; }
    var blob = new Blob([osiYaml], {type: 'text/yaml'});
    var a = document.createElement('a');
    a.download = 'model.osi.yaml';
    a.href = URL.createObjectURL(blob);
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
}"""

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

    function addClearBtn(codeId, bridgeId) {
        var root = document.getElementById(codeId);
        if (!root || root.querySelector('.ob-clear-btn')) return;
        var svgBtn = root.querySelector('button svg');
        if (!svgBtn) return;
        var toolbar = svgBtn.closest('button').parentElement;
        var btn = document.createElement('button');
        btn.className = 'ob-upload-btn ob-clear-btn';
        btn.title = codeId === 'ob-model' ? 'Clear model' : 'Clear query';
        btn.innerHTML = '\u2715';
        btn.style.fontSize = '14px';
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            var el = document.getElementById(bridgeId);
            if (!el) return;
            var ta = el.querySelector('textarea') || el.querySelector('input');
            if (!ta) return;
            ta.value = ' ';
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            setTimeout(function() {
                ta.value = '';
                ta.dispatchEvent(new Event('input', {bubbles: true}));
            }, 50);
        });
        toolbar.style.display = 'flex';
        toolbar.style.flexWrap = 'nowrap';
        toolbar.style.alignItems = 'center';
        toolbar.insertBefore(btn, toolbar.firstChild);
    }

    var isMac = /Mac/.test(navigator.platform);
    function cmKeyCmd(codeId, key, shift) {
        var root = document.getElementById(codeId);
        if (!root) return;
        var content = root.querySelector('.cm-content');
        if (!content) return;
        content.focus();
        content.dispatchEvent(new KeyboardEvent('keydown', {
            key: key, code: 'Key' + key.toUpperCase(),
            ctrlKey: !isMac, metaKey: isMac,
            shiftKey: !!shift,
            bubbles: true, cancelable: true, composed: true
        }));
    }

    function addUndoRedoBtns(codeId) {
        var root = document.getElementById(codeId);
        if (!root || root.querySelector('.ob-redo-btn')) return;
        var svgBtn = root.querySelector('button svg');
        if (!svgBtn) return;
        var toolbar = svgBtn.closest('button').parentElement;

        var redo = document.createElement('button');
        redo.className = 'ob-upload-btn ob-redo-btn';
        redo.title = 'Redo';
        redo.innerHTML = '\u21b7';
        redo.style.fontSize = '16px';
        redo.addEventListener('click', function(e) {
            e.preventDefault(); e.stopPropagation();
            cmKeyCmd(codeId, 'z', true);
        });

        var undo = document.createElement('button');
        undo.className = 'ob-upload-btn ob-undo-btn';
        undo.title = 'Undo';
        undo.innerHTML = '\u21b6';
        undo.style.fontSize = '16px';
        undo.addEventListener('click', function(e) {
            e.preventDefault(); e.stopPropagation();
            cmKeyCmd(codeId, 'z', false);
        });

        toolbar.insertBefore(redo, toolbar.firstChild);
        toolbar.insertBefore(undo, toolbar.firstChild);
    }

    /* Retry — components render asynchronously. */
    var attempts = 0;
    var iv = setInterval(function() {
        addUploadBtn('ob-model', 'ob-model-bridge');
        addUploadBtn('ob-query', 'ob-query-bridge');
        addClearBtn('ob-model', 'ob-model-bridge');
        addClearBtn('ob-query', 'ob-query-bridge');
        addUndoRedoBtns('ob-model');
        addUndoRedoBtns('ob-query');
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


def create_blocks(
    default_api_url: str | None = None,
    embedded_settings: dict[str, Any] | None = None,
    head_html: str | None = None,
) -> Any:
    """Build and return a ``gr.Blocks`` instance (without launching).

    Parameters
    ----------
    default_api_url:
        Override the default API URL shown in the UI.  When the UI is
        co-hosted inside FastAPI (mounted at ``/ui``), this is set to the
        local server address so the UI talks to the same process.
    embedded_settings:
        Pre-built settings dict passed by the API host process in embedded
        mode.  Avoids an HTTP round-trip to ``/v1/settings`` before the
        server is listening.
    """

    from orionbelt import __version__

    cohosted = default_api_url is not None
    api_base = default_api_url or _DEFAULT_API_URL
    dialects = _fetch_dialects(api_base) if not cohosted else _FALLBACK_DIALECTS

    # In embedded mode use pre-supplied settings; standalone fetches via HTTP
    api_settings = embedded_settings if embedded_settings is not None else _fetch_settings(api_base)

    # Pick the initial dialect from what the API will *actually* use, not
    # an alphabetical fallback: dialect.effective reflects the model's
    # settings.defaultDialect (or DB_VENDOR env). That keeps the dropdown
    # honest before the user touches it; on Compile they can switch to
    # any other registered dialect for SQL preview, and Execute Query
    # snaps back via _resolve_execution_dialect.
    api_effective_dialect = (api_settings.get("dialect") or {}).get("effective")
    if isinstance(api_effective_dialect, str) and api_effective_dialect in dialects:
        default_dialect = api_effective_dialect
    elif "postgres" in dialects:
        default_dialect = "postgres"
    elif dialects:
        default_dialect = dialects[0]
    else:
        default_dialect = "postgres"
    single_model = api_settings.get("single_model_mode", False)
    query_exec_enabled = api_settings.get("query_execute", False)
    api_unreachable = bool(api_settings.get("_unreachable"))
    # Pre-v2.7.6 (issue #89): an empty ``api_settings`` (from a cached
    # transient fetch failure) collapsed into self-service mode and
    # silently loaded the bundled ``sem-layer.obml.yml`` over whatever
    # the API actually had. Now we distinguish three real cases:
    #
    # * single-model API reached → use ``model_yaml`` (the deployed model)
    # * API unreachable          → surface "API unreachable" + placeholder;
    #                              never swap in the bundled starter
    # * self-service API reached → load the bundled starter as the
    #                              authoring template (legitimate use)
    if single_model and api_settings.get("model_yaml"):
        example_model = api_settings["model_yaml"]
    elif api_unreachable:
        err = api_settings.get("_error", "unknown error")
        example_model = (
            f"# API unreachable at {api_base} ({err}).\n"
            "# Refresh this page once the API is healthy — the model\n"
            "# loaded into the API will appear here automatically.\n"
        )
    else:
        # Genuine self-service mode (API up, no MODEL_FILES set) —
        # seed the editor with a starter template.
        example_model = _load_example_model()

    with gr.Blocks(
        title="OrionBelt Semantic Layer",
        css=_CSS,
        js=_DARK_MODE_INIT_JS,
        head=head_html,
    ) as demo:
        # ── Browser-persisted state (localStorage via Gradio BrowserState) ──
        saved_model = gr.BrowserState("", storage_key="ob_model_yaml")
        saved_query = gr.BrowserState("", storage_key="ob_query_yaml")
        saved_api = gr.BrowserState(api_base, storage_key="ob_api_url")
        saved_dialect = gr.BrowserState(default_dialect, storage_key="ob_dialect")
        # 0 = "user has not set a zoom yet" → fall back to a device-aware default
        # (60% on desktop, 20% on phones) computed at load time.
        saved_zoom = gr.BrowserState(0, storage_key="ob_zoom")
        saved_sql = gr.BrowserState("", storage_key="ob_sql_output")
        # Carries viewport width class into _restore (set by the load JS).
        is_mobile = gr.Number(value=0, visible=False)

        # ── Stateful API session (avoids re-creating per compile) ──
        session_state = gr.State(None)  # {"session_id": str, "api_url": str}
        model_state = gr.State(None)  # {"model_id": str, "model_hash": str}

        with gr.Row(elem_classes=["header-row"]):
            gr.HTML(
                f'<div class="header-bar">'
                f'<span class="header-brand">'
                f'<img class="logo-dark" src="{_LOGO_DARK_URI}"'
                f' style="height:34px;width:auto" alt="OrionBelt">'
                f'<img class="logo-light"'
                f' src="{_LOGO_LIGHT_URI}"'
                f' style="height:34px;width:auto" alt="OrionBelt">'
                f'<span class="header-title">'
                f"Semantic Layer and Sidecar</span></span>"
                f'<span class="header-links">'
                f'<span class="header-version">'
                f"v{__version__}</span>"
                f'<a href="https://github.com/ralforion'
                f'/orionbelt-semantic-layer"'
                f' target="_blank">'
                f"{_GITHUB_SVG} GitHub</a>"
                f'<a href="https://github.com/ralforion'
                f'/orionbelt-semantic-layer/issues"'
                f' target="_blank">Report Issue</a>'
                f'<a href="https://ralforion.com'
                f'/orionbelt-semantic-layer/"'
                f' target="_blank">Docs</a>'
                f"</span></div>"
            )
            dark_btn = gr.Button("Light / Dark", size="sm", scale=0, min_width=120)

        export_table = gr.Dataframe(visible=False, elem_classes=["ob-bridge"])
        copy_buf = gr.Textbox(visible=False, elem_id="ob-copy-buf")
        num_cols_box = gr.Textbox(visible=False, elem_classes=["ob-bridge"])

        with gr.Tabs() as tabs:
            with gr.Tab("SQL Compiler", id=0):
                with gr.Row(elem_classes=["settings-row"]):
                    with gr.Row(elem_classes=["settings-pair"]):
                        gr.HTML('<span class="settings-label">Jump to</span>', padding=False)
                        jump_dropdown = gr.Dropdown(
                            choices=[],
                            value=None,
                            label="Jump to",
                            show_label=False,
                            container=False,
                            scale=0,
                            min_width=230,
                            filterable=True,
                            elem_classes=["jump-select"],
                        )
                    with gr.Row(elem_classes=["settings-pair"]):
                        gr.HTML(
                            '<span class="settings-label">SQL Dialect</span>',
                            padding=False,
                        )
                        dialect = gr.Dropdown(
                            choices=dialects,
                            value=default_dialect,
                            label="SQL Dialect",
                            show_label=False,
                            container=False,
                            scale=0,
                            min_width=160,
                        )
                    with gr.Row(elem_classes=["settings-pair"]):
                        gr.HTML(
                            '<span class="settings-label">API Base URL</span>',
                            padding=False,
                        )
                        api_url = gr.Textbox(
                            value=api_base,
                            label="API Base URL",
                            show_label=False,
                            container=False,
                            scale=0,
                            min_width=360,
                            lines=1,
                            max_lines=1,
                            interactive=not cohosted,
                        )
                    gr.HTML("", elem_classes=["settings-spacer"])
                    import_osi_btn = gr.Button(
                        "Import OSI",
                        size="sm",
                        scale=0,
                        min_width=100,
                        visible=not single_model,
                    )
                    export_osi_btn = gr.Button("↓ Export as OSI", size="sm", scale=0, min_width=140)
                    download_obsl_btn = gr.Button("\u2193 OBSL", size="sm", scale=0, min_width=80)

                init_dims, init_meas, init_fields = _extract_model_items(example_model)
                # ACR: decorate the initial pickers against the shipped default
                # query so composability highlighting is visible on first render,
                # not only after the query is edited. ``_composable_sets`` returns
                # None (plain choices) if the query can't resolve against the model.
                _init_sets = _composable_sets(example_model, _DEFAULT_QUERY)
                init_dim_choices = _decorate_choices(init_dims, _init_sets)
                init_meas_choices = _decorate_choices(init_meas, _init_sets)

                with gr.Row(elem_classes=["editor-row"]):
                    model_label = (
                        "OBML Model (YAML) \u2014 read-only (single-model mode)"
                        if single_model
                        else "OBML Model (YAML) \u2014 schema/obml-schema.json"
                    )
                    model_input = gr.Code(
                        value=example_model,
                        language="yaml",
                        label=model_label,
                        lines=8,
                        max_lines=10000,
                        scale=3,
                        interactive=not single_model,
                        elem_classes=["code-editor"],
                        elem_id="ob-model",
                    )
                    with gr.Column(scale=2, elem_classes=["picker-col"]):
                        with gr.Row(elem_classes=["picker-row"]):
                            dim_picker = gr.Dropdown(
                                choices=init_dim_choices,
                                value=None,
                                label="Dimensions",
                                scale=1,
                                interactive=True,
                                elem_classes=["picker-dropdown"],
                            )
                            meas_picker = gr.Dropdown(
                                choices=init_meas_choices,
                                value=None,
                                label="Measures / Metrics",
                                scale=1,
                                interactive=True,
                                elem_classes=["picker-dropdown"],
                            )
                            field_picker = gr.Dropdown(
                                choices=init_fields,
                                value=None,
                                label="Columns",
                                scale=1,
                                interactive=True,
                                elem_classes=["picker-dropdown"],
                            )
                        query_input = gr.Code(
                            value=_DEFAULT_QUERY,
                            language="yaml",
                            label="Query (YAML) \u2014 schema/query-schema.json",
                            lines=6,
                            max_lines=10000,
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

                def _update_pickers(
                    model_yaml: str, current_dialect: str, query_yaml: str
                ) -> tuple[object, ...]:
                    dims, meas_met, fields = _extract_model_items(model_yaml)
                    sets = _composable_sets(model_yaml, query_yaml)
                    import gradio as gr

                    # Auto-pick the dialect from the model's
                    # ``settings.defaultDialect`` if present and registered.
                    # The user can still change it manually after the auto-pick.
                    dialect_update = gr.update()
                    try:
                        import yaml as _pyyaml

                        raw = _pyyaml.safe_load(model_yaml or "") or {}
                        if isinstance(raw, dict):
                            settings_block = raw.get("settings") or {}
                            if isinstance(settings_block, dict):
                                model_dialect = settings_block.get("defaultDialect")
                                if (
                                    isinstance(model_dialect, str)
                                    and model_dialect in dialects
                                    and model_dialect != current_dialect
                                ):
                                    dialect_update = gr.update(value=model_dialect)
                    except Exception:  # noqa: BLE001 — dialect auto-pick is best-effort
                        pass

                    return (
                        gr.update(choices=_decorate_choices(dims, sets), value=None),
                        gr.update(choices=_decorate_choices(meas_met, sets), value=None),
                        gr.update(choices=fields, value=None),
                        dialect_update,
                    )

                def _highlight_pickers(model_yaml: str, query_yaml: str) -> tuple[object, object]:
                    """Re-mark composable artefacts for the current query (ACR).

                    When the query YAML can't be resolved yet (e.g. mid-edit /
                    invalid), leave the pickers unchanged instead of clearing the
                    highlighting — avoids flicker while the user is typing.
                    """
                    import gradio as gr

                    sets = _composable_sets(model_yaml, query_yaml)
                    if sets is None:
                        return gr.update(), gr.update()
                    dims, meas_met, _ = _extract_model_items(model_yaml)
                    return (
                        gr.update(choices=_decorate_choices(dims, sets)),
                        gr.update(choices=_decorate_choices(meas_met, sets)),
                    )

                def _make_inserter(section: str) -> object:  # noqa: E501
                    def _fn(val: str | None, query: str) -> tuple[str, object]:
                        import gradio as gr

                        if not val:
                            return query, gr.update(value=None)
                        return (
                            _insert_into_query(query, val, section),
                            gr.update(value=None),
                        )

                    return _fn

                model_input.change(
                    fn=_update_pickers,
                    inputs=[model_input, dialect, query_input],
                    outputs=[dim_picker, meas_picker, field_picker, dialect],
                )

                # "Jump to" navigator for the (large) model YAML: refill targets when
                # the model changes, and scroll the editor to the picked key on select.
                model_input.change(
                    fn=model_jump_targets, inputs=[model_input], outputs=[jump_dropdown]
                )
                jump_dropdown.input(fn=None, inputs=[jump_dropdown], js=_JUMP_TO_JS)

                for picker, sec in (
                    (dim_picker, "dimensions"),
                    (meas_picker, "measures"),
                    (field_picker, "where"),
                ):
                    picker.change(
                        fn=_make_inserter(sec),
                        inputs=[picker, query_input],
                        outputs=[query_input, picker],
                    ).then(
                        # Re-highlight right after a pick (the insert above
                        # produces a valid query), so selecting an artefact
                        # updates composability immediately.
                        fn=_highlight_pickers,
                        inputs=[model_input, query_input],
                        outputs=[dim_picker, meas_picker],
                    )

                # ACR: re-highlight when the user finishes editing the query
                # (on blur), not on every keystroke. Picks re-highlight via the
                # .then above; this keeps manual edits from resolving the model
                # on every character while still updating when focus leaves.
                query_input.blur(
                    fn=_highlight_pickers,
                    inputs=[model_input, query_input],
                    outputs=[dim_picker, meas_picker],
                )

                with gr.Row(equal_height=True):
                    compile_btn = gr.Button(
                        "Compile SQL", variant="primary", elem_classes=["purple-btn"]
                    )
                    execute_btn = gr.Button(
                        "Execute Query",
                        variant="primary",
                        scale=0,
                        min_width=140,
                        visible=query_exec_enabled,
                        elem_classes=["orange-btn"],
                    )
                    validate_btn = gr.Button(
                        "Validate Model",
                        variant="secondary",
                        scale=0,
                        min_width=140,
                    )

                with gr.Row(elem_classes=["output-row"]):
                    sql_output = gr.Code(
                        language="sql",
                        label="Generated SQL",
                        interactive=False,
                        lines=4,
                        max_lines=10000,
                        elem_classes=["sql-output", "code-editor"],
                        elem_id="ob-sql",
                    )
                    explain_output = gr.Code(
                        language="yaml",
                        label="Query Explain",
                        interactive=False,
                        lines=4,
                        max_lines=10000,
                        elem_classes=["sql-output", "code-editor"],
                        elem_id="ob-explain",
                    )

                # Holds the exported OSI YAML for the browser-download JS hook.
                osi_dl_state = gr.Textbox(visible=False)

                # Reset the preview box label back to "Generated SQL" after any
                # SQL/validate/import action (export relabels it to "OSI YAML").
                def _reset_sql_label() -> Any:
                    return gr.update(label="Generated SQL")

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
                ).then(fn=_reset_sql_label, outputs=[sql_output])
                validate_btn.click(
                    fn=validate_model,
                    inputs=[model_input, api_url],
                    outputs=[sql_output, explain_output],
                ).then(fn=_reset_sql_label, outputs=[sql_output])

                # Wire OSI bridge + export after sql_output exists
                osi_bridge.change(
                    fn=_import_osi,
                    inputs=[osi_bridge, api_url],
                    outputs=[model_input, sql_output, explain_output],
                ).then(fn=_reset_sql_label, outputs=[sql_output])
                # Export → clean OSI YAML in the preview box + validation status,
                # then the browser downloads the YAML as a .osi.yaml file.
                export_osi_btn.click(
                    fn=_export_to_osi,
                    inputs=[model_input, api_url],
                    outputs=[sql_output, explain_output, osi_dl_state],
                ).then(
                    fn=None,
                    inputs=[osi_dl_state],
                    js=_DOWNLOAD_OSI_JS,
                )

                # OBSL graph download: fetch Turtle → JS triggers file download
                obsl_turtle_state = gr.Textbox(visible=False)
                download_obsl_btn.click(
                    fn=_fetch_obsl_turtle,
                    inputs=[model_input, api_url, session_state, model_state],
                    outputs=[obsl_turtle_state, session_state, model_state],
                ).then(
                    fn=None,
                    inputs=[obsl_turtle_state],
                    js=_DOWNLOAD_TTL_JS,
                )

            with gr.Tab("Query Results", id=1, visible=query_exec_enabled) as results_tab:
                # Execution info + actions on one row: a short info box, then the
                # buttons inline to its right (all scale=0 so they pack left).
                with gr.Row(elem_classes=["result-actions"]):
                    result_info = gr.Textbox(
                        label="Execution Info",
                        interactive=False,
                        lines=1,
                        max_lines=1,
                        scale=0,
                        min_width=440,
                    )
                    copy_data_btn = gr.Button(
                        "Copy Data",
                        visible=False,
                        variant="secondary",
                        size="sm",
                        scale=0,
                        min_width=110,
                    )
                    tsv_download = gr.DownloadButton(
                        "↓ TSV",
                        visible=False,
                        variant="secondary",
                        size="sm",
                        scale=0,
                        min_width=90,
                    )
                # "Clear filters" button — full-width row so the filter list never
                # wraps onto multiple lines.
                with gr.Row():
                    filter_chip = gr.Button(
                        visible=False,
                        size="sm",
                        variant="secondary",
                        elem_classes=["filter-chip"],
                    )
                # Hidden bridge: per-header sort icons (injected via JS) write
                # "column|action|nonce" here to trigger a server-side re-sort.
                sort_signal = gr.Textbox(
                    value="",
                    elem_id="ob-sort-signal",
                    elem_classes=["ob-bridge"],
                    container=False,
                    show_label=False,
                )
                # Holds the active orderBy ("field|dir" per line) so the header JS
                # can colour the active ▲/▼ per column.
                sort_state = gr.Textbox(
                    value="",
                    elem_id="ob-sort-state",
                    elem_classes=["ob-bridge"],
                    container=False,
                    show_label=False,
                )
                result_table = gr.Dataframe(
                    label="Query Results",
                    interactive=False,
                    wrap=True,
                    max_height=800,
                    datatype="html",
                    elem_classes=["result-table"],
                    visible=False,
                )
                with gr.Accordion("Response Metadata", open=False, visible=False) as meta_acc:
                    meta_code = gr.Code(
                        language="yaml",
                        interactive=False,
                        lines=8,
                        elem_id="ob-meta-code",
                    )

            # Refresh execute button/tab visibility AND snap the dialect to
            # the API's effective dialect. Runs on api_url blur and on every
            # page load — the startup-time fetch in create_blocks() can fail
            # silently (5s timeout vs Cloud Run cold start), baking a stale
            # visible=False / dialect=postgres into the page. Re-fetching per
            # page load makes the decision per-session instead of per-process.
            def _refresh_query_exec_visibility(
                api_url_val: str,
            ) -> tuple[object, object, object]:
                import gradio as gr

                # _cached_settings was removed in v2.7.6 (#89); the
                # cache-bust call here is now a no-op since every call
                # to _fetch_settings already hits the API.
                s = _fetch_settings(api_url_val)
                enabled = s.get("query_execute", False)
                effective = (s.get("dialect") or {}).get("effective")
                if isinstance(effective, str) and effective in dialects:
                    dialect_update = gr.update(value=effective)
                else:
                    dialect_update = gr.update()
                return (
                    gr.update(visible=enabled),
                    gr.update(visible=enabled),
                    dialect_update,
                )

            api_url.blur(
                fn=_refresh_query_exec_visibility,
                inputs=[api_url],
                outputs=[execute_btn, results_tab, dialect],
            )

            # Wire execute button after result components are defined.
            # Pre-step: snap the dialect dropdown to the API's effective
            # execution dialect so the user sees what will actually run
            # (and execute_query itself reads the snapped value).
            execute_btn.click(
                fn=_resolve_execution_dialect,
                inputs=[api_url, dialect],
                outputs=[dialect],
            ).then(
                fn=execute_query,
                inputs=[
                    model_input,
                    query_input,
                    dialect,
                    api_url,
                    session_state,
                    model_state,
                ],
                outputs=[
                    sql_output,
                    explain_output,
                    session_state,
                    model_state,
                    result_table,
                    export_table,
                    result_info,
                    tsv_download,
                    num_cols_box,
                    meta_code,
                ],
            ).then(
                fn=lambda info: (
                    gr.Tabs(selected=1) if info else gr.Tabs(),
                    gr.update(visible=bool(info)),
                    gr.update(visible=bool(info)),
                    gr.update(visible=bool(info)),
                    gr.update(visible=bool(info)),
                ),
                inputs=[result_info],
                outputs=[tabs, tsv_download, copy_data_btn, meta_acc, result_table],
            ).then(
                fn=filter_chip_update,
                inputs=[query_input],
                outputs=[filter_chip],
            ).then(
                fn=sort_state_str,
                inputs=[query_input],
                outputs=[sort_state],
            ).then(
                # Header JS runs LAST so it can read the freshly-set sort_state.
                fn=None,
                inputs=[num_cols_box],
                js=_ALIGN_HEADERS_JS,
            )

            # -- Interactive results: click-to-filter -----------------------------
            # The rewrite + execute happen in ONE handler (filter_and_execute /
            # clear_filters_and_execute), so the execute runs the just-rewritten
            # query directly — avoiding Gradio's output→input lag that made a
            # filter take effect only on the *next* click. ``query_input`` is the
            # first output (updates the editor). ``_wire_post`` only re-shows the
            # actions, re-aligns headers, and syncs the Clear-filters button.
            # ``filter_chip`` is the LAST output: the handler computes it from the
            # just-rewritten query (in-hand), so the Clear-filters button doesn't
            # suffer the output→input lag a chained read would.
            _filter_outputs = [
                query_input,
                sql_output,
                explain_output,
                session_state,
                model_state,
                result_table,
                export_table,
                result_info,
                tsv_download,
                num_cols_box,
                meta_code,
                filter_chip,
                sort_state,
            ]

            def _wire_post(trigger: Any) -> Any:
                return trigger.then(
                    fn=lambda info: (
                        gr.update(visible=bool(info)),
                        gr.update(visible=bool(info)),
                        gr.update(visible=bool(info)),
                    ),
                    inputs=[result_info],
                    outputs=[tsv_download, copy_data_btn, result_table],
                ).then(fn=None, inputs=[num_cols_box], js=_ALIGN_HEADERS_JS)

            # Click a dimension value -> add/toggle its filter and re-run in one shot.
            _wire_post(
                result_table.select(
                    fn=filter_and_execute,
                    inputs=[
                        query_input,
                        result_table,
                        model_input,
                        dialect,
                        api_url,
                        session_state,
                        model_state,
                    ],
                    outputs=_filter_outputs,
                )
            )

            # "Clear filters" -> drop click-added filters and re-run.
            _wire_post(
                filter_chip.click(
                    fn=clear_filters_and_execute,
                    inputs=[
                        query_input,
                        model_input,
                        dialect,
                        api_url,
                        session_state,
                        model_state,
                    ],
                    outputs=_filter_outputs,
                )
            )

            # Per-header sort icons -> rewrite orderBy + execute in one shot
            # (same output shape as the filter handlers, incl. the chip).
            _wire_post(
                sort_signal.input(
                    fn=sort_and_execute,
                    inputs=[
                        sort_signal,
                        query_input,
                        model_input,
                        dialect,
                        api_url,
                        session_state,
                        model_state,
                    ],
                    outputs=_filter_outputs,
                )
            )

            tsv_download.click(fn=lambda: gr.Info("TSV file downloaded"))

            def _to_tsv(df: object) -> str:
                import pandas as pd

                if not isinstance(df, pd.DataFrame) or df.empty:
                    gr.Info("No data to copy")
                    return ""
                export = df.drop(columns=["#"], errors="ignore")
                tsv: str = export.to_csv(sep="\t", index=False)
                gr.Info("Copied to clipboard")
                return tsv

            copy_data_btn.click(
                fn=_to_tsv,
                inputs=[export_table],
                outputs=[copy_buf],
            ).then(
                fn=None,
                inputs=[copy_buf],
                js="async (tsv) => {if(tsv) await navigator.clipboard.writeText(tsv);}",
            )

            with gr.Tab("ER Diagram", id=2) as er_tab:
                with gr.Row():
                    show_columns_cb = gr.Checkbox(value=True, label="Show columns")
                    zoom_slider = gr.Slider(
                        minimum=10,
                        maximum=200,
                        value=60,
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

                show_columns_cb.change(
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

            with gr.Tab("Ontology Graph", id=3) as ontology_tab:
                with gr.Row():
                    show_do_cb = gr.Checkbox(
                        value=True, label="DataObjects", elem_classes=["ob-cb-do"]
                    )
                    show_dim_cb = gr.Checkbox(
                        value=True, label="Dimensions", elem_classes=["ob-cb-dim"]
                    )
                    show_meas_cb = gr.Checkbox(
                        value=True, label="Measures", elem_classes=["ob-cb-meas"]
                    )
                    show_met_cb = gr.Checkbox(
                        value=True, label="Metrics", elem_classes=["ob-cb-met"]
                    )
                    show_joins_cb = gr.Checkbox(
                        value=True, label="Joins", elem_classes=["ob-cb-joins"]
                    )
                    node_spacing_slider = gr.Slider(
                        minimum=50,
                        maximum=400,
                        value=150,
                        step=10,
                        label="Node Spacing",
                        scale=2,
                        min_width=240,
                    )
                    ontology_btn = gr.Button(
                        "Render Graph",
                        variant="primary",
                        elem_classes=["purple-btn"],
                        scale=1,
                        min_width=160,
                    )

                ontology_output = gr.HTML(
                    value=(
                        "<p style='padding:16px;opacity:0.6'>"
                        "Click 'Render Graph' to generate the ontology graph "
                        "from the model YAML.</p>"
                    ),
                    elem_id="ob-ontology-graph-container",
                )

                _ontology_inputs = [
                    model_input,
                    show_do_cb,
                    show_dim_cb,
                    show_meas_cb,
                    show_met_cb,
                    show_joins_cb,
                    node_spacing_slider,
                ]

                ontology_btn.click(
                    fn=_render_ontology_graph,
                    inputs=_ontology_inputs,
                    outputs=[ontology_output],
                )

                ontology_tab.select(
                    fn=_render_ontology_graph,
                    inputs=_ontology_inputs,
                    outputs=[ontology_output],
                )

                for _cb in [show_do_cb, show_dim_cb, show_meas_cb, show_met_cb, show_joins_cb]:
                    _cb.change(
                        fn=_render_ontology_graph,
                        inputs=_ontology_inputs,
                        outputs=[ontology_output],
                    )

                node_spacing_slider.release(
                    fn=_render_ontology_graph,
                    inputs=_ontology_inputs,
                    outputs=[ontology_output],
                )

            with gr.Tab("Settings", id=4) as settings_tab:
                with gr.Row():
                    with gr.Column():
                        settings_output = gr.Code(
                            language="yaml",
                            label="API Settings",
                            interactive=False,
                            lines=10,
                        )
                    with gr.Column():
                        cache_stats_output = gr.Code(
                            language="yaml",
                            label="Cache Stats",
                            interactive=False,
                            lines=10,
                        )
                        with gr.Row(equal_height=True):
                            cache_stats_refresh = gr.Button(
                                "Refresh Cache Stats",
                                variant="secondary",
                                size="sm",
                                scale=1,
                                min_width=180,
                                elem_classes=["purple-btn"],
                            )
                            cache_sweep_btn = gr.Button(
                                "Sweep Cache now",
                                variant="secondary",
                                size="sm",
                                scale=1,
                                min_width=180,
                                elem_classes=["orange-btn"],
                            )
                            cache_clear_btn = gr.Button(
                                "Clear Cache",
                                variant="stop",
                                size="sm",
                                scale=1,
                                min_width=180,
                            )

                def _fetch_cache_stats(api_url_val: str) -> str:
                    url = api_url_val.rstrip("/") if api_url_val else _DEFAULT_API_URL
                    try:
                        resp = httpx.get(
                            f"{url}/v1/cache/stats",
                            timeout=5,
                            headers=_API_HEADERS,
                        )
                        resp.raise_for_status()
                        data = resp.json()
                    except httpx.ConnectError:
                        return f"# Error: Cannot connect to API at {url}"
                    except Exception as exc:  # noqa: BLE001 — surface any failure
                        return f"# Error: {exc}"
                    return yaml.dump(data, default_flow_style=False, sort_keys=False)

                cache_stats_refresh.click(
                    fn=_fetch_cache_stats,
                    inputs=[api_url],
                    outputs=[cache_stats_output],
                )

                def _trigger_cache_sweep(api_url_val: str) -> str:
                    url = api_url_val.rstrip("/") if api_url_val else _DEFAULT_API_URL
                    try:
                        resp = httpx.post(
                            f"{url}/v1/cache/sweep",
                            timeout=10,
                            headers=_API_HEADERS,
                        )
                        resp.raise_for_status()
                    except httpx.ConnectError:
                        return f"# Error: Cannot connect to API at {url}"
                    except Exception as exc:  # noqa: BLE001 — surface any failure
                        return f"# Error: {exc}"
                    # Re-fetch fresh stats so the user sees the post-sweep state.
                    return _fetch_cache_stats(api_url_val)

                cache_sweep_btn.click(
                    fn=_trigger_cache_sweep,
                    inputs=[api_url],
                    outputs=[cache_stats_output],
                )

                def _clear_cache(api_url_val: str) -> str:
                    url = api_url_val.rstrip("/") if api_url_val else _DEFAULT_API_URL
                    try:
                        resp = httpx.post(
                            f"{url}/v1/cache/clear",
                            timeout=30,
                            headers=_API_HEADERS,
                        )
                        resp.raise_for_status()
                    except httpx.ConnectError:
                        return f"# Error: Cannot connect to API at {url}"
                    except Exception as exc:  # noqa: BLE001 — surface any failure
                        return f"# Error: {exc}"
                    return _fetch_cache_stats(api_url_val)

                cache_clear_btn.click(
                    fn=_clear_cache,
                    inputs=[api_url],
                    outputs=[cache_stats_output],
                )

                def _fetch_settings_yaml(
                    api_url_val: str,
                    sess_state: dict[str, str] | None,
                    mdl_state: dict[str, str] | None,
                    model_yaml_val: str,
                ) -> str:
                    """Fetch /v1/settings, scoped to the active session+model
                    when one has been compiled in this UI session so the
                    returned ``model_settings`` / ``timezone`` blocks reflect
                    the model the user actually loaded.

                    If the user has typed/pasted a model but not compiled
                    yet, the server doesn't know about it. Parse the local
                    YAML's ``settings:`` block and overlay it on the response
                    so the model's TZ/dialect choices are visible without
                    needing a compile round-trip. Server-resolved fields
                    (host TZ, DB session TZ, effective values) still come
                    from the API.
                    """
                    url = api_url_val.rstrip("/") if api_url_val else _DEFAULT_API_URL
                    params: dict[str, str] = {}
                    sid = (sess_state or {}).get("session_id")
                    mid = (mdl_state or {}).get("model_id")
                    if sid:
                        params["session_id"] = sid
                        if mid:
                            params["model_id"] = mid
                    try:
                        resp = httpx.get(
                            f"{url}/v1/settings",
                            params=params or None,
                            timeout=5,
                            headers=_API_HEADERS,
                        )
                        resp.raise_for_status()
                        data = resp.json()
                    except httpx.ConnectError:
                        return f"# Error: Cannot connect to API at {url}"
                    except Exception as exc:
                        return f"# Error: {exc}"

                    # Remove model_yaml from display (too large)
                    data.pop("model_yaml", None)

                    # Overlay the locally-edited model's settings block when
                    # the API response is missing it (no compile yet).
                    local_settings: dict[str, Any] = {}
                    try:
                        raw = yaml.safe_load(model_yaml_val or "") or {}
                        if isinstance(raw, dict):
                            block = raw.get("settings")
                            if isinstance(block, dict):
                                local_settings = block
                    except Exception:  # noqa: BLE001 — best-effort overlay
                        local_settings = {}

                    if local_settings:
                        # If the API didn't return ``model_settings`` the
                        # server has no loaded model — the user has typed
                        # the YAML but not compiled yet, and the API's
                        # ``timezone.effective`` / ``dialect.effective``
                        # are no-model fallbacks (UTC / DB_VENDOR). In
                        # that case the local YAML is the source of
                        # truth: overlay its values onto the response,
                        # including ``effective``, so the Settings tab
                        # mirrors what compiling will produce.
                        #
                        # When the API DOES return ``model_settings``
                        # (compiled session), only fill in the ``model``
                        # fields if the API didn't supply them — the
                        # server is authoritative for everything else.
                        api_has_model = bool(data.get("model_settings"))

                        existing_ms = data.get("model_settings") or {}
                        merged_ms = {**local_settings, **existing_ms}
                        data["model_settings"] = merged_ms

                        tz = data.get("timezone") or {}
                        local_tz = local_settings.get("defaultTimezone")
                        if local_tz and (not api_has_model or "model" not in tz):
                            tz["model"] = local_tz
                        if "override_database_timezone" not in tz:
                            tz["override_database_timezone"] = bool(
                                local_settings.get("overrideDatabaseTimezone", False)
                            )
                        if not api_has_model and local_tz:
                            tz["effective"] = local_tz
                            # The API computed ``now`` server-side against
                            # its own (no-model) effective TZ — UTC. After
                            # overlaying the local TZ as effective, recompute
                            # ``now`` so the wall clock matches.
                            try:
                                from datetime import UTC, datetime
                                from zoneinfo import ZoneInfo

                                now_local = datetime.now(UTC).astimezone(ZoneInfo(local_tz))
                                tz["now"] = now_local.isoformat()
                            except Exception:  # noqa: BLE001 — best-effort
                                pass
                        data["timezone"] = tz

                        dl = data.get("dialect") or {}
                        local_dl = local_settings.get("defaultDialect")
                        if local_dl and (not api_has_model or "model" not in dl):
                            dl["model"] = local_dl
                        if not api_has_model and local_dl:
                            dl["effective"] = local_dl
                        data["dialect"] = dl

                    return yaml.dump(data, default_flow_style=False, sort_keys=False)

                settings_tab.select(
                    fn=_fetch_settings_yaml,
                    inputs=[api_url, session_state, model_state, model_input],
                    outputs=[settings_output],
                ).then(
                    fn=_fetch_cache_stats,
                    inputs=[api_url],
                    outputs=[cache_stats_output],
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
        def _restore(sm, sq, sa, sd, sz, ss, im):  # type: ignore[no-untyped-def]
            # No saved zoom yet → device-aware default: 20% on phones, 60% on desktop.
            zoom = sz if sz else (20 if im else 60)
            return (
                example_model if single_model else (sm if sm else example_model),
                sq if sq else _DEFAULT_QUERY,
                sa if sa else api_base,
                sd if sd else default_dialect,
                zoom,
                ss if ss else "",
            )

        # Frontend shim: pass the BrowserState values straight through, appending
        # a mobile flag from the live viewport width so _restore can pick the
        # device-aware default zoom.
        _restore_js = (
            "(sm, sq, sa, sd, sz, ss, im) => "
            "[sm, sq, sa, sd, sz, ss, (window.innerWidth <= 768 ? 1 : 0)]"
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
            inputs=[
                saved_model,
                saved_query,
                saved_api,
                saved_dialect,
                saved_zoom,
                saved_sql,
                is_mobile,
            ],
            outputs=[model_input, query_input, api_url, dialect, zoom_slider, sql_output],
            js=_restore_js,
        ).then(
            # Re-fetch settings against the restored api_url so the Execute
            # Query button and the dialect dropdown reflect the live API,
            # not the (possibly stale) values from process startup.
            fn=_refresh_query_exec_visibility,
            inputs=[api_url],
            outputs=[execute_btn, results_tab, dialect],
        ).then(
            # ACR: decorate the pickers against the restored query on load.
            # Programmatic value updates above don't fire the .change handlers,
            # so without this the dropdowns ignore the shipped/restored query.
            fn=_highlight_pickers,
            inputs=[model_input, query_input],
            outputs=[dim_picker, meas_picker],
        ).then(
            # Populate the model "Jump to" navigator from the restored model.
            fn=model_jump_targets,
            inputs=[model_input],
            outputs=[jump_dropdown],
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

    from orionbelt import __version__

    print(f"OrionBelt Semantic Layer UI v{__version__}")

    api_url = os.environ.get("API_BASE_URL") or None
    port = int(os.environ.get("PORT", "7860"))
    root_path = os.environ.get("ROOT_PATH", "")

    # Forward an API key on every REST call when the API enforces auth.
    api_key = os.environ.get("OBSL_API_KEY") or None
    api_key_header = os.environ.get("API_KEY_HEADER", _DEFAULT_API_KEY_HEADER)
    set_api_credentials(api_key, api_key_header)
    _warn_if_auth_required_without_key(api_url or _DEFAULT_API_URL, api_key)

    from pathlib import Path

    favicon_file = Path(__file__).resolve().parent / "favicon.png"

    if root_path:
        # Behind a reverse proxy / load balancer mounting Gradio under
        # ``root_path``. Gradio ignores ``favicon_path`` here (its default
        # head template uses absolute "/favicon.ico"), so we serve the
        # favicon ourselves and inject a <link> tag into the head.
        import gradio as gr
        from fastapi import FastAPI
        from fastapi.responses import FileResponse, PlainTextResponse

        favicon_url = f"{root_path.rstrip('/')}/favicon.png"
        head_html = f'<link rel="icon" type="image/png" href="{favicon_url}">'
        demo = create_blocks(default_api_url=api_url, head_html=head_html)

        app = FastAPI()

        @app.get(favicon_url, include_in_schema=False)
        async def _favicon() -> FileResponse:
            return FileResponse(favicon_file, media_type="image/png")

        @app.get("/robots.txt", include_in_schema=False)
        async def _robots() -> PlainTextResponse:
            # The UI is an interactive app, not indexable content. When this
            # service is fronted on its own host (rather than under /ui on the
            # API host), serve a disallow-all so crawlers skip it.
            return PlainTextResponse("User-agent: *\nDisallow: /\n")

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
        # Standalone — Gradio mounts at "/", so favicon_path works as designed.
        demo = create_blocks(default_api_url=api_url)
        demo.launch(
            server_name="0.0.0.0",
            server_port=port,
            favicon_path=str(favicon_file),
        )


def main() -> None:
    """Entry point for ``orionbelt-ui`` console script."""
    create_ui()
