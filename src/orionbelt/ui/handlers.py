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

        resp = client.post(
            f"/v1/sessions/{session_id}/query/execute",
            json={"model_id": model_id, "query": query_dict, "dialect": dialect},
            timeout=120,
        )
        if resp.status_code == 404:
            client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
                model_yaml, api_url, None, None
            )
            resp = client.post(
                f"/v1/sessions/{session_id}/query/execute",
                json={"model_id": model_id, "query": query_dict, "dialect": dialect},
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
        data = resp.json()

        sql: str = data["sql"]
        formatted = _format_sql(sql)
        explain_yaml = _build_explain_yaml(data)

        columns = data.get("columns", [])
        rows = data.get("rows", [])
        row_count = data.get("row_count", 0)
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
