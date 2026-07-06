"""Query / model handlers for the Gradio UI.

These are the Gradio callback bodies: compile/execute/validate against the
REST API, the explain-YAML builder, ACR composable highlighting, and the
query-editor insertion helpers. They depend on the HTTP client helpers in
``orionbelt.ui.api_client`` and the rendering helpers in
``orionbelt.ui.rendering`` (imported directly to avoid importing ``app``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import gradio as gr
import httpx
import yaml

# Number / locale formatting lives in service.value_formatting so the API
# can apply identical rules when ``format_values`` is requested.
from orionbelt.service.value_formatting import format_number as _format_number
from orionbelt.ui.api_client import (
    _API_HEADERS,
    _DEFAULT_API_URL,
    _ensure_session_and_model,
    _fetch_settings,
    _format_api_errors,
    _ModelValidationError,
)
from orionbelt.ui.rendering import _format_sql


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


# Interactive results: clicking a dimension value injects an equality filter into
# the query YAML (reflected in the editor, recompiled to SQL on the follow-on
# execute). Loop-safe: rewriting ``query_input`` fires no execute (it has only a
# ``.blur`` handler); the entry points (cell select, chip click) are user-only.
_FILTER_EQ_OP = "equals"
_FILTER_NULL_OP = "notset"  # OBML "IS NULL" — used when clicking a null dimension cell
# Ops the click-to-filter UI owns: equality on a value, or IS NULL on a null cell.
# Clear/toggle only touch these, leaving query-defined filters (other ops) intact.
_CLICK_FILTER_OPS = frozenset({_FILTER_EQ_OP, _FILTER_NULL_OP})


def _query_node(root: object) -> dict[str, Any] | None:
    """Return the QueryObject mapping from a parsed editor doc (unwrapping a
    top-level ``query:`` wrapper), or ``None`` if it isn't a mapping."""
    if not isinstance(root, dict):
        return None
    node = root["query"] if ("query" in root and "select" not in root) else root
    return node if isinstance(node, dict) else None


def apply_cell_filter(evt: gr.EventData, query_yaml: str, table: object) -> str:
    """Click a cell -> inject an equality filter into the query YAML.

    A **dimension** cell adds to ``where``; a **measure/metric** cell adds to
    ``having`` (it references the aggregate by alias). A **null** dimension cell
    adds an ``IS NULL`` filter (``op: notset``); a null measure/metric cell is
    inert (filtering a null aggregate is meaningless). Additive across columns,
    click the same value to toggle it off, and query-defined filters (ops other
    than ``equals``/``notset``) are preserved. Returns the rewritten YAML so the
    editor reflects it and it recompiles to SQL on the follow-on execute. The
    ``#`` column is inert.

    Takes the base ``gr.EventData`` (not ``gr.SelectData``) on purpose: Gradio's
    ``SelectData.__init__`` does ``data["value"]`` and raises ``KeyError`` on the
    value-less select events the Dataframe emits (first-click "arm" / deselect /
    re-render), which crashed the whole click and made filtering need a 2nd click.
    We read the raw payload defensively and ignore events without a value.
    """
    import html as html_mod
    from io import StringIO

    import pandas as pd
    from ruamel.yaml import YAML

    data = getattr(evt, "_data", None) or {}
    idx = data.get("index")
    value = data.get("value")
    col_i = idx[1] if isinstance(idx, (list, tuple)) and len(idx) == 2 else None
    # A real cell click carries the ``value`` key (even for a null cell, where it
    # is ``None``); Gradio's value-less "arm"/deselect/lasso events omit the key
    # entirely. Gate on presence so a genuine null-cell click acts on the first
    # click without reviving the old 2nd-click bug.
    if (
        "value" not in data
        or col_i is None
        or not isinstance(table, pd.DataFrame)
        or not (0 <= col_i < len(table.columns))
    ):
        return query_yaml
    col_name = str(table.columns[col_i])
    if col_name == "#":
        return query_yaml

    yaml_rt = YAML()
    try:
        root = yaml_rt.load(query_yaml)
    except Exception:
        return query_yaml
    node = _query_node(root)
    if node is None:
        return query_yaml
    select = node.get("select") or {}
    dims = list(select.get("dimensions") or [])
    aggs = list(select.get("measures") or []) + list(select.get("metrics") or [])
    # A dimension filters via WHERE; a measure/metric filters via HAVING (it
    # references the aggregate by alias). Anything else (#, raw fields) is inert.
    if col_name in dims:
        clause = "where"
    elif col_name in aggs:
        clause = "having"
    else:
        return query_yaml

    entries = node.get(clause) or []
    if value is None:
        # Null cell. Filtering a null aggregate (HAVING) is meaningless, so only
        # dimensions (WHERE) get an IS NULL filter; measures/metrics are inert.
        if clause != "where":
            return query_yaml
        entry: dict[str, object] = {"field": col_name, "op": _FILTER_NULL_OP}
        already = any(
            isinstance(w, dict) and w.get("op") == _FILTER_NULL_OP and w.get("field") == col_name
            for w in entries
        )
    else:
        if isinstance(value, str):
            value = html_mod.unescape(value).strip()
        entry = {"field": col_name, "op": _FILTER_EQ_OP, "value": value}
        already = any(
            isinstance(w, dict)
            and w.get("op") == _FILTER_EQ_OP
            and w.get("field") == col_name
            and str(w.get("value")) == str(value)
            for w in entries
        )
    # Additive across columns: drop only *this column's* click-filter (equals or
    # IS NULL), keeping every other filter (incl. query-defined ones with other
    # ops). Then toggle — the already-active filter clears it; a new one replaces.
    kept = [
        w
        for w in entries
        if not (
            isinstance(w, dict) and w.get("op") in _CLICK_FILTER_OPS and w.get("field") == col_name
        )
    ]
    if not already:
        kept.append(entry)
    if kept:
        node[clause] = kept
    elif clause in node:
        del node[clause]

    buf = StringIO()
    yaml_rt.dump(root, buf)
    return buf.getvalue()


def remove_cell_filter(query_yaml: str) -> str:
    """Drop all click-added equality filters from ``where`` AND ``having`` (Clear).

    Only ``op: equals`` entries (the click-added ones) are removed; query-defined
    filters with other ops stay enforced.
    """
    from io import StringIO

    from ruamel.yaml import YAML

    yaml_rt = YAML()
    try:
        root = yaml_rt.load(query_yaml)
    except Exception:
        return query_yaml
    node = _query_node(root)
    if node is None:
        return query_yaml
    for clause in ("where", "having"):
        if clause not in node:
            continue
        kept = [
            w
            for w in (node.get(clause) or [])
            if not (isinstance(w, dict) and w.get("op") in _CLICK_FILTER_OPS)
        ]
        if kept:
            node[clause] = kept
        else:
            del node[clause]
    buf = StringIO()
    yaml_rt.dump(root, buf)
    return buf.getvalue()


def filter_chip_update(query_yaml: str) -> object:
    """Show a "Clear filters" button listing the active click-filters.

    Lists every click-added equality filter (``op: equals``); the button clears
    all of them at once via :func:`remove_cell_filter`, leaving the query's own
    filters (e.g. an ``in`` list) untouched. Removing a single filter is done by
    clicking that value again (toggle).
    """
    from ruamel.yaml import YAML

    try:
        node = _query_node(YAML().load(query_yaml))
    except Exception:
        node = None
    active: list[str] = []
    if isinstance(node, dict):
        for clause in ("where", "having"):
            for w in node.get(clause) or []:
                if not isinstance(w, dict):
                    continue
                if w.get("op") == _FILTER_EQ_OP:
                    active.append(f"{w.get('field')} = {w.get('value')}")
                elif w.get("op") == _FILTER_NULL_OP:
                    active.append(f"{w.get('field')} IS NULL")
    if active:
        return gr.update(value="✕  Clear filters  ·  " + "   ·   ".join(active), visible=True)
    return gr.update(visible=False)


_ExecTuple = tuple[
    str,
    str,
    dict[str, str] | None,
    dict[str, str] | None,
    object,
    object,
    str,
    str | None,
    str,
    str,
]


def filter_and_execute(
    evt: gr.EventData,
    query_yaml: str,
    table: object,
    model_yaml: str,
    dialect: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
    request: gr.Request | None = None,
) -> tuple[str, *_ExecTuple, object, str]:
    """Apply the clicked filter to the query AND execute — in one call.

    Doing the rewrite + execute together means the execute runs the *just
    rewritten* query directly. If instead ``query_input`` were an output of the
    rewrite and a chained input of the execute, Gradio would feed the execute the
    pre-update value — the bug where a click only took effect on the *next* click.
    The Clear-filters chip and sort-state are computed here too (from the new
    query, in-hand) so they don't suffer the same lag. Returns: new query YAML,
    the 10 execute outputs, the filter-chip update, then the sort-state string.
    """
    new_query = apply_cell_filter(evt, query_yaml, table)
    result = execute_query(
        model_yaml, new_query, dialect, api_url, session_state, model_state, request
    )
    return (new_query, *result, filter_chip_update(new_query), sort_state_str(new_query))


def clear_filters_and_execute(
    query_yaml: str,
    model_yaml: str,
    dialect: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
    request: gr.Request | None = None,
) -> tuple[str, *_ExecTuple, object, str]:
    """Drop all click-added filters AND execute in one call (see :func:`filter_and_execute`)."""
    new_query = remove_cell_filter(query_yaml)
    result = execute_query(
        model_yaml, new_query, dialect, api_url, session_state, model_state, request
    )
    return (new_query, *result, filter_chip_update(new_query), sort_state_str(new_query))


def _apply_sort_signal(query_yaml: str, signal: str | None) -> str:
    """Rewrite the query's ``orderBy`` from a per-header sort click.

    ``signal`` is ``"<column>|<action>|<nonce>"`` where action is
    ``asc`` / ``desc`` / ``clear``. Additive: ▲/▼ add-or-replace that column's
    entry (keeping other columns' ordering); ✕ removes just that column. When no
    ordering remains, ``orderBy`` is dropped entirely (order not enforced).
    """
    from io import StringIO

    from ruamel.yaml import YAML

    parts = (signal or "").split("|")
    if len(parts) < 2:
        return query_yaml
    col, action = parts[0], parts[1]
    if not col:
        return query_yaml

    yaml_rt = YAML()
    try:
        root = yaml_rt.load(query_yaml)
    except Exception:
        return query_yaml
    node = _query_node(root)
    if node is None:
        return query_yaml

    # Drop this column's existing entry, then (for asc/desc) re-add it at the end.
    order = [
        o
        for o in (node.get("orderBy") or [])
        if not (isinstance(o, dict) and o.get("field") == col)
    ]
    if action in ("asc", "desc"):
        order.append({"field": col, "direction": action})
    if order:
        node["orderBy"] = order
    elif "orderBy" in node:
        del node["orderBy"]
    buf = StringIO()
    yaml_rt.dump(root, buf)
    return buf.getvalue()


def sort_state_str(query_yaml: str) -> str:
    """Compact ``field|direction`` newline-list of the active ``orderBy``.

    Fed to the header JS so it can colour the active ▲/▼ per column (and gray the
    rest). Newline-separated so a value can't collide with the delimiter.
    """
    from ruamel.yaml import YAML

    try:
        node = _query_node(YAML().load(query_yaml))
    except Exception:
        node = None
    parts: list[str] = []
    if isinstance(node, dict):
        for o in node.get("orderBy") or []:
            if isinstance(o, dict) and o.get("field"):
                parts.append(f"{o.get('field')}|{o.get('direction', 'desc')}")
    return "\n".join(parts)


def sort_and_execute(
    signal: str,
    query_yaml: str,
    model_yaml: str,
    dialect: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
    request: gr.Request | None = None,
) -> tuple[str, *_ExecTuple, object, str]:
    """Apply a per-header sort click to ``orderBy`` AND execute in one call.

    One handler (rewrite + execute) so the execute runs the just-rewritten query
    directly — the same fix that made click-to-filter apply on the first click.
    Returns the shared shape (query, execute outputs, filter-chip, sort-state).
    """
    new_query = _apply_sort_signal(query_yaml, signal)
    result = execute_query(
        model_yaml, new_query, dialect, api_url, session_state, model_state, request
    )
    return (new_query, *result, filter_chip_update(new_query), sort_state_str(new_query))


def model_jump_targets(model_yaml: str) -> object:
    """Build the model-editor "Jump to" choices as ``(label, line#)`` pairs.

    Two-level labels: each top-level section (``settings`` / ``dataObjects`` /
    ``dimensions`` / ``measures`` / ``metrics`` / ``filters``) plus its named
    children as ``"section / name"``. The value is the 1-based line number so the
    editor JS can scroll there via ``.cm-scroller`` scrollTop.
    """
    import re

    top = ("version", "settings", "dataObjects", "dimensions", "measures", "metrics", "filters")
    named = {"dataObjects", "dimensions", "measures", "metrics", "filters"}
    choices: list[tuple[str, str]] = []
    section: str | None = None
    for i, line in enumerate((model_yaml or "").split("\n"), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_]\w*):", line)
        if m:
            key = m.group(1)
            if key in top:
                choices.append((key, str(i)))
                section = key if key in named else None
            continue
        if section:
            m2 = re.match(r"^  (\S[^:]*):", line)  # exactly-2-space indented name
            if m2:
                choices.append((f"{section} / {m2.group(1).strip()}", str(i)))
    return gr.update(choices=choices, value=None)


def _resolve_execution_dialect(api_url: str, current: str) -> str:
    """Snap the SQL Dialect dropdown to the API's effective execution dialect.

    The dropdown is useful for previewing compiled SQL in different
    dialects, but ``Execute Query`` actually runs against the API's
    backing database — executing the wrong dialect (e.g. Postgres SQL
    against the bundled DuckDB) just fails. Force the dropdown to the
    API's reported ``dialect.effective`` so the user sees what will
    actually be executed. Falls back silently to the current value if
    settings cannot be fetched.
    """
    try:
        settings = _fetch_settings(api_url)
        eff = (settings.get("dialect") or {}).get("effective")
        if isinstance(eff, str) and eff:
            return eff
    except Exception:  # noqa: BLE001 — best-effort UX hint, never block exec
        pass
    return current


def _decode_arrow_execute_response(resp: Any) -> Any:
    """Decode a ``format=arrow`` execute response into the same dict shape the
    JSON path yields, so downstream rendering is identical.

    The response is a length-prefixed frame separating the freshly-assembled
    metadata from the cached row data::

        [u32 big-endian json_len][JSON envelope utf-8][gzip'd Arrow IPC data]

    The metadata (sql, columns, timing, ``cached`` flag, …) is parsed from the
    JSON prefix; rows come from the gzip'd Arrow data sub-part. Because the
    envelope is rebuilt per request, ``execution_time_ms`` / ``cached`` are
    correct even on a cache hit.
    """
    import gzip
    import json

    import pyarrow as pa

    body = resp.content
    meta_len = int.from_bytes(body[:4], "big")
    data: dict[str, Any] = json.loads(body[4 : 4 + meta_len].decode("utf-8"))
    table = pa.ipc.open_stream(gzip.decompress(body[4 + meta_len :])).read_all()
    names = table.column_names
    data["rows"] = [[row.get(n) for n in names] for row in table.to_pylist()]
    if not data.get("columns"):
        data["columns"] = [{"name": n} for n in names]
    return data


def execute_query(
    model_yaml: str,
    query_yaml: str,
    dialect: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
    request: gr.Request | None = None,
) -> tuple[
    str,
    str,
    dict[str, str] | None,
    dict[str, str] | None,
    object,
    object,
    str,
    str | None,
    str,
    str,
]:
    """Execute query via the REST API and return results as a table.

    Returns ``(sql_output, explain_yaml, session_state, model_state,
    display_df, export_df, result_info, tsv_path, num_col_indices,
    meta_yaml)``.
    """
    import pandas as pd

    empty_df = pd.DataFrame()
    try:
        client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
            model_yaml, api_url, session_state, model_state
        )

        try:
            query_dict = yaml.safe_load(query_yaml)
        except yaml.YAMLError as exc:
            return (
                f"Error: Invalid query YAML\n{exc}",
                "",
                session_state,
                model_state,
                empty_df,
                empty_df,
                "",
                None,
                "",
                "",
            )

        if not isinstance(query_dict, dict):
            return (
                "Error: Query YAML must be a mapping (dict), not a scalar or list",
                "",
                session_state,
                model_state,
                empty_df,
                empty_df,
                "",
                None,
                "",
                "",
            )

        if "query" in query_dict and "select" not in query_dict:
            query_dict = query_dict["query"]

        # The UI always uses the self-contained Arrow IPC transport (dogfoods the
        # format=arrow endpoint). The decoded dict is identical to the JSON shape,
        # so all rendering below is unchanged.
        exec_params = {"format": "arrow"}
        exec_headers = {"Accept-Encoding": "gzip"}

        resp = client.post(
            f"/v1/sessions/{session_id}/query/execute",
            json={"model_id": model_id, "query": query_dict, "dialect": dialect},
            params=exec_params,
            headers=exec_headers,
            timeout=120,
        )
        if resp.status_code == 404:
            client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
                model_yaml, api_url, None, None
            )
            resp = client.post(
                f"/v1/sessions/{session_id}/query/execute",
                json={"model_id": model_id, "query": query_dict, "dialect": dialect},
                params=exec_params,
                headers=exec_headers,
                timeout=120,
            )
        if resp.status_code == 503:
            detail = resp.json().get("detail", resp.text)
            return (
                f"Error: {detail}",
                "",
                session_state,
                model_state,
                empty_df,
                empty_df,
                "",
                None,
                "",
                "",
            )
        if resp.status_code in (400, 422):
            detail = resp.json().get("detail", resp.text)
            return (
                f"Error: Query execution failed\n{_format_api_errors(detail)}",
                "",
                session_state,
                model_state,
                empty_df,
                empty_df,
                "",
                None,
                "",
                "",
            )
        resp.raise_for_status()
        data = _decode_arrow_execute_response(resp)

        sql: str = data["sql"]
        formatted = _format_sql(sql)
        explain_yaml = _build_explain_yaml(data)

        columns = data.get("columns", [])
        rows = data.get("rows", [])
        row_count = data.get("row_count", 0)
        # The metadata envelope is rebuilt per request, so on a cache hit this is
        # the cache fetch time (not the stale original DB time).
        exec_time = data.get("execution_time_ms", 0.0)

        col_names = [c["name"] for c in columns]
        col_type_map = {c["name"]: c.get("type", "string") for c in columns}
        col_fmt_map = {c["name"]: c.get("format") for c in columns}
        df = pd.DataFrame(rows, columns=col_names) if col_names else pd.DataFrame(rows)
        num_cols: set[str] = set()
        for cname in col_names:
            if cname not in df.columns:
                continue
            if (
                col_type_map.get(cname) == "number"
                or df[cname].apply(lambda v: isinstance(v, (int, float))).any()
            ):
                is_num = True
            else:
                coerced = pd.to_numeric(df[cname], errors="coerce")
                is_num = coerced.notna().any()
            if is_num:
                df[cname] = pd.to_numeric(df[cname], errors="coerce")
                num_cols.add(cname)
        df.insert(0, "#", range(1, len(df) + 1))

        export_df = df.copy()
        accept_lang = ""
        if request and hasattr(request, "headers"):
            accept_lang = request.headers.get("accept-language", "")
        loc = accept_lang.split(",")[0].strip() if accept_lang else ""
        for cname in num_cols:
            fmt = col_fmt_map.get(cname)
            export_df[cname] = export_df[cname].apply(
                lambda v, f=fmt, lc=loc: _format_number(float(v), f, lc) if pd.notna(v) else v
            )

        import html as _html

        _rtag = '<span style="display:block;text-align:right;width:100%">'
        all_cols = list(df.columns)
        data_rows: list[list[object]] = []
        display_rows: list[list[str]] = []
        for _, row in df.iterrows():
            d_row: list[object] = []
            disp_row: list[str] = []
            for cname in all_cols:
                v = row[cname]
                if cname in num_cols:
                    d_row.append(v if pd.notna(v) else None)
                    if pd.notna(v):
                        disp = _format_number(float(v), col_fmt_map.get(cname), loc)
                        disp_row.append(f"{_rtag}{_html.escape(disp)}</span>")
                    else:
                        disp_row.append(f"{_rtag}-</span>")
                else:
                    d_row.append(v if pd.notna(v) else None)
                    disp_row.append(_html.escape(str(v)) if pd.notna(v) else "-")
            data_rows.append(d_row)
            display_rows.append(disp_row)

        display_df: object = {
            "headers": all_cols,
            "data": data_rows,
            "metadata": {
                "display_value": display_rows,
                "styling": [[""] * len(all_cols) for _ in data_rows],
            },
        }

        warnings: list[str] = data.get("warnings", [])
        sql_valid: bool = data.get("sql_valid", True)
        header_lines: list[str] = []
        if not sql_valid:
            header_lines.append("-- WARNING: SQL validation failed")
        for w in warnings:
            header_lines.append(f"-- WARNING: {w}")
        if header_lines:
            header_lines.append("")
            formatted = "\n".join(header_lines) + "\n" + formatted

        source = "cache" if data.get("cached") else "database"
        info = f"{row_count} rows in {exec_time:.0f} ms ({source})"
        tz_name = data.get("timezone")
        if tz_name:
            info += f" · TZ: {tz_name}"
        if loc:
            info += f" · Locale: {loc}"

        import tempfile

        # Write into a temp dir with a fixed basename so the browser downloads
        # it as "query_results.tsv" (DownloadButton uses the file's basename;
        # mkstemp's random suffix produced names like "query_results_p_jyptv2").
        tsv_dir = tempfile.mkdtemp(prefix="obsl_tsv_")
        tsv_path = f"{tsv_dir}/query_results.tsv"
        export_df.drop(columns=["#"], errors="ignore").to_csv(tsv_path, sep="\t", index=False)

        num_indices = [str(i + 1) for i, c in enumerate(all_cols) if c in num_cols]
        num_col_str = ",".join(num_indices)

        meta: dict[str, Any] = {}
        meta["dialect"] = data.get("dialect", "")
        meta["row_count"] = row_count
        meta["execution_time_ms"] = round(exec_time, 2)
        if tz_name:
            meta["timezone"] = tz_name
        meta["sql_valid"] = sql_valid
        if warnings:
            meta["warnings"] = warnings
        col_meta = []
        for c in columns:
            entry: dict[str, Any] = {"name": c["name"], "type": c.get("type", "string")}
            if c.get("format"):
                entry["format"] = c["format"]
            col_meta.append(entry)
        meta["columns"] = col_meta
        resolved = data.get("resolved", {})
        if resolved:
            meta["resolved"] = resolved
        meta_yaml = yaml.dump(meta, default_flow_style=False, sort_keys=False, allow_unicode=True)

        return (
            formatted,
            explain_yaml,
            session_state,
            model_state,
            display_df,
            export_df,
            info,
            tsv_path,
            num_col_str,
            meta_yaml,
        )

    except _ModelValidationError as exc:
        return (
            f"Error: Model validation failed\n{_format_api_errors(exc.detail)}",
            "",
            session_state,
            model_state,
            empty_df,
            empty_df,
            "",
            None,
            "",
            "",
        )
    except httpx.ConnectError:
        api = api_url.rstrip("/") if api_url else _DEFAULT_API_URL
        return (
            f"Error: Cannot connect to API at {api}\n"
            "Make sure the server is running: uv run orionbelt-api",
            "",
            session_state,
            model_state,
            empty_df,
            empty_df,
            "",
            None,
            "",
            "",
        )
    except httpx.HTTPStatusError as exc:
        return (
            f"Error: HTTP {exc.response.status_code}\n{exc.response.text}",
            "",
            session_state,
            model_state,
            empty_df,
            empty_df,
            "",
            None,
            "",
            "",
        )
    except Exception as exc:
        return (
            f"Error: {exc}",
            "",
            session_state,
            model_state,
            empty_df,
            empty_df,
            "",
            None,
            "",
            "",
        )


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


def _extract_model_items(
    model_yaml: str,
) -> tuple[list[str | tuple[str, str]], list[str], list[str]]:
    """Extract dimension names, measure/metric names, and field names from model YAML.

    Returns ``(dimensions, measures_metrics, fields)``.
    """
    try:
        raw = yaml.safe_load(model_yaml) or {}
    except Exception:
        return [], [], []
    raw_dims = raw.get("dimensions", {})
    dims: list[str | tuple[str, str]] = []
    if isinstance(raw_dims, dict):
        for name, dobj in sorted(raw_dims.items()):
            via = dobj.get("via") if isinstance(dobj, dict) else None
            if via:
                dims.append((f"{name} (via {via})", name))
            else:
                dims.append(name)
    raw_meas = raw.get("measures", {})
    measures = list(raw_meas.keys()) if isinstance(raw_meas, dict) else []
    raw_mets = raw.get("metrics", {})
    metrics = list(raw_mets.keys()) if isinstance(raw_mets, dict) else []
    meas_met = sorted(measures + metrics)
    fields: list[str] = []
    data_objects = raw.get("dataObjects", {})
    if isinstance(data_objects, dict):
        for obj_name, obj in data_objects.items():
            if isinstance(obj, dict):
                for col_name in obj.get("columns", {}):
                    fields.append(f"{obj_name}.{col_name}")
    fields.sort()
    return dims, meas_met, fields


def _composable_sets(model_yaml: str, query_yaml: str) -> dict[str, set[str]] | None:
    """Resolve ACR composable sets in-process for the current query.

    Returns ``{"direct": {...}, "cfl": {...}}`` of artefact names, or ``None``
    when the model or query can't be resolved (caller then leaves the pickers
    un-highlighted rather than erroring). See ``design/PLAN_graph_reasoning.md``.
    """
    try:
        from orionbelt.compiler.composability import resolve_composables_for_query
        from orionbelt.models.query import QueryObject
        from orionbelt.parser.loader import TrackedLoader
        from orionbelt.parser.resolver import ReferenceResolver

        raw, source_map = TrackedLoader().load_string(model_yaml)
        model, result = ReferenceResolver().resolve(raw, source_map)
        if not result.valid:
            return None
        qraw = yaml.safe_load(query_yaml) or {}
        if not isinstance(qraw, dict):
            qraw = {}
        query = QueryObject.model_validate(qraw)
        resolved = resolve_composables_for_query(model, query)
        return {
            "direct": set(resolved.dimensions) | set(resolved.measures) | set(resolved.metrics),
            "cfl": set(resolved.cfl_measures) | set(resolved.cfl_metrics),
        }
    except Exception:  # noqa: BLE001 — highlighting is best-effort
        return None


def _decorate_choices(
    items: Sequence[str | tuple[str, str]],
    sets: dict[str, set[str]] | None,
) -> list[tuple[str, str]]:
    """Mark composable artefacts in picker labels (highlight, never hard-filter)."""
    out: list[tuple[str, str]] = []
    for item in items:
        label, value = item if isinstance(item, tuple) else (item, item)
        if sets is None:
            out.append((label, value))
        elif value in sets["direct"]:
            out.append((f"✓ {label}", value))
        elif value in sets["cfl"]:
            out.append((f"✓ {label} (via CFL)", value))
        else:
            out.append((label, value))
    return out


def _insert_into_query(query: str, value: str, section: str) -> str:
    """Insert *value* into the correct *section* of query YAML.

    *section* is one of ``"dimensions"``, ``"measures"``, or ``"where"``.
    """
    lines = query.rstrip("\n").split("\n")

    if section in ("dimensions", "measures"):
        target = f"  {section}:"
        idx = None
        for i, ln in enumerate(lines):
            if ln.rstrip() == target:
                idx = i
                break

        if idx is not None:
            last = idx
            for i in range(idx + 1, len(lines)):
                if lines[i].startswith("    - "):
                    last = i
                elif lines[i].strip() and not lines[i].startswith("      "):
                    break
            lines.insert(last + 1, f"    - {value}")
        else:
            sel_idx = None
            for i, ln in enumerate(lines):
                if ln.rstrip() == "select:":
                    sel_idx = i
                    break
            if sel_idx is not None:
                end = sel_idx
                for i in range(sel_idx + 1, len(lines)):
                    if lines[i] and not lines[i].startswith(" "):
                        break
                    end = i
                lines.insert(end + 1, target)
                lines.insert(end + 2, f"    - {value}")
            else:
                lines.insert(0, "select:")
                lines.insert(1, target)
                lines.insert(2, f"    - {value}")

    elif section == "where":
        tpl = [
            f"  - field: {value}",
            "    op: equals",
            "    value: ",
        ]
        idx = None
        for i, ln in enumerate(lines):
            if ln.rstrip() == "where:":
                idx = i
                break
        if idx is not None:
            end = idx
            for i in range(idx + 1, len(lines)):
                if lines[i] and not lines[i].startswith(" "):
                    break
                if lines[i].strip():
                    end = i
            for j, t in enumerate(tpl):
                lines.insert(end + 1 + j, t)
        else:
            pos = len(lines)
            for i, ln in enumerate(lines):
                s = ln.strip()
                if s.startswith("orderBy:") or s.startswith("limit:"):
                    pos = i
                    break
            lines.insert(pos, "where:")
            for j, t in enumerate(tpl):
                lines.insert(pos + 1 + j, t)

    return "\n".join(lines) + "\n"


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
