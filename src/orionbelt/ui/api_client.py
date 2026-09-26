"""HTTP/auth client helpers for the Gradio UI.

Thin wrappers over the OrionBelt REST API: credential handling, settings /
dialect / diagram / OBSL fetches, OSI convert calls, and the shared
``_ensure_session_and_model`` helper. The module-level ``_API_HEADERS`` dict
is the single source of truth for request headers — ``set_api_credentials``
mutates it in place and every fetch reads it, so it MUST live in exactly one
module.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import gradio as gr
import httpx

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
_DEFAULT_API_KEY_HEADER = "X-API-Key"


def set_api_credentials(api_key: str | None, header_name: str = _DEFAULT_API_KEY_HEADER) -> None:
    """Attach (or clear) the API key the UI forwards on every REST call.

    The UI is a thin client of the REST API; when the API runs with
    ``AUTH_MODE=api_key`` the UI must present a valid key on each request.
    Browser users never see it. See design/PLAN_authentication.md §3.4.
    """
    header_name = (header_name or _DEFAULT_API_KEY_HEADER).strip() or _DEFAULT_API_KEY_HEADER
    # Drop any previously-set key header (idempotent across re-configures).
    for existing in [k for k in _API_HEADERS if k.lower() == header_name.lower()]:
        del _API_HEADERS[existing]
    if api_key:
        _API_HEADERS[header_name] = api_key


def _warn_if_auth_required_without_key(api_base: str, api_key: str | None) -> None:
    """Log a clear startup error when the API needs a key but the UI has none.

    Probes the unauthenticated ``/health`` endpoint (which reports
    ``auth_mode``) so the operator sees an actionable message instead of the
    user hitting cryptic 401s in the browser.
    """
    if api_key:
        return
    try:
        resp = httpx.get(f"{api_base}/health", timeout=5, headers=_API_HEADERS)
        auth_mode = resp.json().get("auth_mode", "none")
    except Exception:
        return  # API not up yet / unreachable — nothing actionable to say
    if auth_mode and auth_mode != "none":
        print(
            f"ERROR: API at {api_base} requires authentication (auth_mode={auth_mode}) "
            "but OBSL_API_KEY is not set. The UI will get 401s on every call. "
            "Set OBSL_API_KEY (and API_KEY_HEADER if customised) on the UI process."
        )


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

    from orionbelt.ui.rendering import _format_convert_status

    status = _format_convert_status(
        "OSI → OBML Import", data.get("warnings", []), data.get("validation", {})
    )
    return data.get("output_yaml", ""), status, ""


def _export_to_osi(obml_yaml: str, api_base: str) -> tuple[Any, str, str]:
    """Convert OBML YAML to OSI via the API.

    Returns ``(osi_yaml, status, osi_yaml)`` where the first value is the clean
    OSI YAML shown in the preview box, the second is the validation status line,
    and the third is the same YAML handed to the browser-download JS.
    """
    if not obml_yaml or not obml_yaml.strip():
        return gr.update(value="", label="Generated SQL"), "Error: No OBML model YAML to export", ""

    try:
        resp = httpx.post(
            f"{api_base}/v1/convert/obml-to-osi",
            json={"input_yaml": obml_yaml},
            headers=_API_HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            detail = resp.json().get("detail", resp.text)
            return gr.update(value="", label="Generated SQL"), f"Error: {detail}", ""
        data = resp.json()
    except Exception as exc:
        return (
            gr.update(value="", label="Generated SQL"),
            f"Error: OBML → OSI conversion failed\n{exc}",
            "",
        )

    from orionbelt.ui.rendering import _format_convert_status

    status = _format_convert_status(
        "OBML → OSI Export", data.get("warnings", []), data.get("validation", {})
    )
    output: str = data.get("output_yaml", "")
    return gr.update(value=output, label="OSI YAML (exported)"), status, output


def _fetch_obsl_turtle(
    model_yaml: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
) -> tuple[str, dict[str, str] | None, dict[str, str] | None]:
    """Fetch the OBSL-Core Turtle graph for the current model.

    Returns ``(turtle_str, session_state, model_state)``.  Falls back to
    local generation when the API is unreachable.
    """
    if not model_yaml or not model_yaml.strip():
        return "", session_state, model_state

    try:
        client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
            model_yaml, api_url, session_state, model_state
        )
        resp = client.get(f"/v1/sessions/{session_id}/models/{model_id}/graph")
        if resp.status_code == 404:
            client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
                model_yaml, api_url, None, None
            )
            resp = client.get(f"/v1/sessions/{session_id}/models/{model_id}/graph")
        resp.raise_for_status()
        return resp.text, session_state, model_state
    except _ModelValidationError:
        return "", session_state, model_state
    except httpx.ConnectError:
        # API not available — fall back to local generation
        try:
            from orionbelt.obsl.exporter import export_obsl
            from orionbelt.parser.loader import TrackedLoader
            from orionbelt.parser.resolver import ReferenceResolver

            raw, sm = TrackedLoader().load_string(model_yaml)
            model, result = ReferenceResolver().resolve(raw, sm)
            if not result.valid:
                return "", session_state, model_state
            g = export_obsl(model, "model")
            return g.serialize(format="turtle"), session_state, model_state
        except Exception:
            return "", session_state, model_state
    except Exception:
        return "", session_state, model_state


def _run_sparql(
    model_yaml: str,
    api_url: str,
    query: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
) -> tuple[dict[str, Any] | None, str, dict[str, str] | None, dict[str, str] | None]:
    """Run a read-only SPARQL query against the current model's OBSL graph.

    Returns ``(result, error, session_state, model_state)``: ``result`` is the
    API's SPARQL response body (``type``, ``variables``, ``results``,
    ``boolean``) and ``error`` is empty on success, or ``result`` is ``None``
    and ``error`` says why. A model that fails validation and a query the
    endpoint refuses (update operations, syntax) both come back as errors.
    Falls back to exporting and querying the graph locally when the API is
    unreachable, so the standalone UI still answers.
    """
    # A comment-only editor is the "API unreachable" placeholder, not a model.
    if not model_yaml or not any(
        line.strip() and not line.lstrip().startswith("#") for line in model_yaml.splitlines()
    ):
        return None, "No model loaded.", session_state, model_state
    if not query or not query.strip():
        return None, "Enter a SPARQL query or pick an example.", session_state, model_state

    try:
        client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
            model_yaml, api_url, session_state, model_state
        )
        path = f"/v1/sessions/{session_id}/models/{model_id}/sparql"
        resp = client.post(path, json={"query": query})
        if resp.status_code == 404:
            client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
                model_yaml, api_url, None, None
            )
            resp = client.post(
                f"/v1/sessions/{session_id}/models/{model_id}/sparql", json={"query": query}
            )
        if resp.status_code in (400, 422):
            detail = resp.json().get("detail", resp.text)
            return None, _format_api_errors(detail), session_state, model_state
        resp.raise_for_status()
        return resp.json(), "", session_state, model_state
    except _ModelValidationError as exc:
        return None, _format_api_errors(exc.detail), session_state, model_state
    except httpx.ConnectError:
        # API not available: export and query the graph in-process. The same
        # refusals apply (update operations, syntax), reported the same way.
        try:
            return _run_sparql_locally(model_yaml, query), "", session_state, model_state
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, never raised into Gradio
            return None, str(exc), session_state, model_state
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, never raised into Gradio
        return None, f"SPARQL request failed: {exc}", session_state, model_state


def _rules_request(
    model_yaml: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
    method: str,
    suffix: str,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str, dict[str, str] | None, dict[str, str] | None]:
    """One call against the current model's ``/rules`` surface.

    Returns ``(body, error, session_state, model_state)``: ``body`` is the
    JSON response on success, else ``None`` with ``error`` saying why. The
    rules endpoints need the API (listing compiles, evaluation runs SQL), so
    an unreachable API is an error here, not a local fallback.
    """
    if not model_yaml or not any(
        line.strip() and not line.lstrip().startswith("#") for line in model_yaml.splitlines()
    ):
        return None, "No model loaded.", session_state, model_state
    try:
        client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
            model_yaml, api_url, session_state, model_state
        )
        path = f"/v1/sessions/{session_id}/models/{model_id}/rules{suffix}"
        resp = client.request(method, path, json=payload, timeout=300)
        if resp.status_code == 404 and "not found" in resp.text.lower() and "Session" in resp.text:
            client, session_id, model_id, session_state, model_state = _ensure_session_and_model(
                model_yaml, api_url, None, None
            )
            path = f"/v1/sessions/{session_id}/models/{model_id}/rules{suffix}"
            resp = client.request(method, path, json=payload, timeout=300)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            return None, _format_api_errors(detail), session_state, model_state
        return resp.json(), "", session_state, model_state
    except _ModelValidationError as exc:
        return None, _format_api_errors(exc.detail), session_state, model_state
    except httpx.ConnectError:
        return None, f"API unreachable at {api_url}.", session_state, model_state
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, never raised into Gradio
        return None, f"Rules request failed: {exc}", session_state, model_state


def _model_request(
    model_yaml: str,
    api_url: str,
    session_state: dict[str, str] | None,
    model_state: dict[str, str] | None,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    text: bool = False,
) -> tuple[Any, str, dict[str, str] | None, dict[str, str] | None]:
    """One call against the current model, recovering from an expired session.

    *path* may use ``{session_id}`` and ``{model_id}``; a *payload* key
    ``model_id`` set to ``None`` is filled in. Returns ``(body, error,
    session_state, model_state)`` with ``body`` the JSON response (the text,
    with *text*), or ``None`` and ``error`` saying why.
    """
    if not model_yaml or not model_yaml.strip():
        return None, "No model loaded.", session_state, model_state

    def send(fresh: bool) -> tuple[httpx.Response, dict[str, str], dict[str, str]]:
        client, session_id, model_id, ss, ms = _ensure_session_and_model(
            model_yaml, api_url, None if fresh else session_state, None if fresh else model_state
        )
        body = dict(payload) if payload is not None else None
        if body is not None and "model_id" in body and body["model_id"] is None:
            body["model_id"] = model_id
        url = path.format(session_id=session_id, model_id=model_id)
        return client.request(method, url, json=body, params=params), ss, ms

    try:
        resp, session_state, model_state = send(fresh=False)
        if resp.status_code == 404 and "Session" in resp.text:
            resp, session_state, model_state = send(fresh=True)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            return None, _format_api_errors(detail), session_state, model_state
        return (resp.text if text else resp.json()), "", session_state, model_state
    except _ModelValidationError as exc:
        return None, _format_api_errors(exc.detail), session_state, model_state
    except httpx.ConnectError:
        return None, f"API unreachable at {api_url}.", session_state, model_state
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, never raised into Gradio
        return None, f"Request failed: {exc}", session_state, model_state


def _run_sparql_locally(model_yaml: str, query: str) -> dict[str, Any] | None:
    """Export the graph in-process and query it: the API-unreachable path.

    Same read-only rule as the endpoint: :func:`execute_sparql` refuses
    update operations, and that refusal is reported like any other error.
    """
    from orionbelt.obsl.exporter import export_obsl
    from orionbelt.obsl.sparql import execute_sparql
    from orionbelt.parser.loader import TrackedLoader
    from orionbelt.parser.resolver import ReferenceResolver

    raw, sm = TrackedLoader().load_string(model_yaml)
    model, result = ReferenceResolver().resolve(raw, sm)
    if not result.valid:
        return None
    outcome = execute_sparql(export_obsl(model, "model"), query)
    return {
        "type": outcome.type,
        "variables": outcome.variables,
        "results": outcome.results,
        "boolean": outcome.boolean,
        "warnings": outcome.warnings,
    }


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
        from orionbelt.ui.rendering import _generate_mermaid_er_local

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


_cached_dialects: dict[str, list[str]] = {}


def _fetch_dialects(api_url: str) -> list[str]:
    """Fetch dialect names from the API, falling back to hardcoded list (cached).

    Cached because the dialect list genuinely never changes per
    deployment — unlike settings (which carries the loaded model_yaml,
    where caching a failure used to lock the UI into the bundled
    fallback — issue #89).
    """
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
    """Fetch public settings from the API. Retries on transient failure.

    Returns ``{"_unreachable": True}`` when every retry fails — callers
    use that flag to distinguish *"API is in self-service mode"* (empty
    settings is legitimate) from *"API is unreachable"* (we shouldn't
    fall back to a stale bundled starter, that's the issue #89 bug).

    Not cached: pre-v2.7.6 a single transient failure (Cloud Run cold
    start exceeding the 5-second client timeout) wrote ``{}`` to the
    cache, sticking forever and silently swapping the deployed model
    out for ``examples/orionbelt_1_commerce.yaml``. The session-wide model
    fetch happens once at UI startup; the cache served no real purpose.
    """
    url = api_url.rstrip("/")
    last_exc: Exception | None = None
    # 3-attempt retry with simple backoff covers Cloud Run cold-start
    # (typically 3-5s warm-up) without holding the UI hostage.
    for delay in (0, 1.5, 3.0):
        if delay:
            time.sleep(delay)
        try:
            resp = httpx.get(f"{url}/v1/settings", timeout=5, headers=_API_HEADERS)
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
            return payload
        except Exception as exc:
            last_exc = exc
            continue
    return {
        "_unreachable": True,
        "_error": f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown",
    }


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
