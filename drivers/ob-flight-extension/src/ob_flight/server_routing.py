"""Session / model routing helpers for :class:`~ob_flight.server.OBFlightServer`.

Extracted from ``server.py`` (Phase 5.5) as a pure code move. The helper
functions take the ``OBFlightServer`` instance as their first argument
(``server``) so the class can delegate to them as one-liners.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow.flight as flight

if TYPE_CHECKING:
    from ob_flight.server import OBFlightServer


class _SessionRoutingMiddleware(flight.ServerMiddleware):  # type: ignore[misc]
    """Per-call middleware that captures the incoming session/model selector.

    BI tools (DBeaver, Tableau, Power BI) and JDBC clients pass the model
    name via the gRPC ``database`` header — set by
    ``Connection.setCatalog()`` on the Arrow Flight SQL JDBC driver, or
    by URL path ``/database`` on direct gRPC clients. ``x-obsl-model`` is
    accepted as an alias for clients that can't set the catalog header.

    See ``design/PLAN_flight_natural_sql.md`` multi-model addressing.
    """

    def __init__(self, selected_model: str | None) -> None:
        self.selected_model: str | None = selected_model

    def call_completed(self, exception: BaseException | None) -> None:
        pass


class _SessionRoutingFactory(flight.ServerMiddlewareFactory):  # type: ignore[misc]
    """Reads the connection's catalog / model selector from incoming gRPC
    metadata and produces a :class:`_SessionRoutingMiddleware` per call.

    Resolution order for the selector:
      1. ``database`` (standard JDBC catalog header)
      2. ``x-obsl-model`` (OBSL-specific alias)
      3. ``catalog`` (some clients send this instead of ``database``)
      4. None — caller's request enters with no explicit selector and the
         auto-resolve / __default__ paths in ``_get_model`` apply.
    """

    _SELECTOR_KEYS = ("database", "x-obsl-model", "catalog")

    def start_call(
        self, info: flight.CallInfo, headers: dict[str, list[str]]
    ) -> _SessionRoutingMiddleware:
        selected: str | None = None
        # Headers come in lowercased per gRPC convention; values are lists.
        for key in self._SELECTOR_KEYS:
            values = headers.get(key) or headers.get(key.lower())
            if values:
                raw = values[0]
                if raw:
                    selected = raw.strip().lower() or None
                    break
        return _SessionRoutingMiddleware(selected)


# Key used to register / look up the routing middleware.
_ROUTING_MIDDLEWARE_KEY = "obsl_routing"


def selector_from_context(
    context: flight.ServerCallContext | None,
) -> str | None:
    """Read the per-call routing selector from the middleware.

    Returns the (already-lowercased) model name set by the client's
    ``database`` / ``x-obsl-model`` / ``catalog`` gRPC header, or
    ``None`` if no selector was sent or the middleware isn't installed
    (e.g. in unit tests that bypass the real gRPC machinery).
    """
    if context is None:
        return None
    try:
        mw = context.get_middleware(_ROUTING_MIDDLEWARE_KEY)
    except Exception:
        return None
    if mw is None:
        return None
    return getattr(mw, "selected_model", None)


def list_available_model_names(server: OBFlightServer) -> list[str]:
    """List protected (admin-loaded) session ids in addressing order.

    Used by ``_get_model``'s error path and by the catalog endpoint.
    The session id IS the model name in multi-model mode; legacy
    single-model mode contributes ``__default__`` (not really an
    addressable name — see the auto-resolve branch).
    """
    if server._session_manager is None:
        return []
    try:
        ids: list[str] = server._session_manager.list_protected_session_ids()
        return ids
    except Exception:
        return []


def resolve_model_by_name(
    server: OBFlightServer,
    stashed_name: str,
    context: flight.ServerCallContext | None,
) -> Any:
    """Resolve a model by a stashed selector first, falling back to the
    current call's context. Used by ``do_get`` for ticket round-trips.
    """
    if stashed_name:
        try:
            store = server._session_manager.get_store(stashed_name)
            model, _ = server._stamp_model(store, stashed_name)
            return model
        except Exception:
            pass
    model, _ = server._get_model(context)
    return model


def get_model(
    server: OBFlightServer, context: flight.ServerCallContext | None = None
) -> tuple[Any, str]:
    """Resolve the model targeted by the current call.

    Returns ``(model, dialect)``. Resolution order:

    1. **Explicit selector** from the gRPC ``database`` /
       ``x-obsl-model`` / ``catalog`` header → that named session.
    2. **Legacy `__default__`** session (single-model mode via
       ``MODEL_FILE``).
    3. **Auto-resolve**: if exactly one admin-loaded session exists,
       use it without requiring a selector.
    4. **Rich error** listing the available model names and how to
       select one.

    Stamps ``_ob_model_id`` on the returned model so downstream
    catalog code can produce a stable virtual-table name.
    """
    if server._session_manager is None:
        raise flight.FlightUnavailableError("No session manager configured")

    selector = selector_from_context(context)

    # 1. Explicit selector
    if selector:
        try:
            store = server._session_manager.get_store(selector)
        except Exception:
            available = list_available_model_names(server)
            raise flight.FlightUnavailableError(
                format_unknown_model_error(selector, available)
            ) from None
        return server._stamp_model(store, selector)

    # 2. Legacy __default__ session (single-model mode)
    try:
        default_store = server._session_manager.get_store("__default__")
        return server._stamp_model(default_store, "__default__")
    except Exception:
        pass

    # 3. Auto-resolve when exactly one admin-loaded model exists
    protected = list_available_model_names(server)
    if len(protected) == 1:
        store = server._session_manager.get_store(protected[0])
        return server._stamp_model(store, protected[0])

    # 4. Ambiguous or empty → rich error
    if not protected:
        raise flight.FlightUnavailableError(
            "[NO_MODEL_AVAILABLE] No models are loaded on this server. "
            "Either set MODEL_FILES=<path,...> (or legacy MODEL_FILE) "
            "before starting the server, or load models dynamically "
            "via POST /v1/sessions + POST /v1/sessions/{id}/models."
        )
    raise flight.FlightUnavailableError(format_ambiguous_model_error(protected))


def stamp_model(server: OBFlightServer, store: Any, session_id: str) -> tuple[Any, str]:
    """Pull the (single) model out of a store and stamp the session
    id onto it as the virtual-table name. Returns ``(model, dialect)``.

    Per-model dialect resolution: prefer the OBML model's
    ``settings.defaultDialect`` if set; otherwise fall back to the
    server's process-wide ``_default_dialect`` (from ``DB_VENDOR``).
    """
    models = store.list_models()
    if not models:
        raise flight.FlightUnavailableError(
            f"Session '{session_id}' exists but has no models loaded."
        )
    model_id = models[0].model_id
    model = store.get_model(model_id)
    try:
        # In multi-model mode the session_id IS the model name —
        # use it as the virtual-table name. In legacy mode session_id
        # is __default__ and we fall back to internal model_id.
        virtual_name = session_id if not session_id.startswith("_") else model_id
        model.__dict__["_ob_model_id"] = virtual_name
    except Exception:
        pass

    # Per-model dialect override via OBML settings.defaultDialect
    model_dialect: str | None = None
    settings = getattr(model, "settings", None)
    if settings is not None:
        model_dialect = getattr(settings, "default_dialect", None)
    return model, model_dialect or server._default_dialect


def format_unknown_model_error(selector: str, available: list[str]) -> str:
    if not available:
        return (
            f"[UNKNOWN_MODEL] Model '{selector}' is not loaded and no "
            "models are available on this server. Either set "
            "MODEL_FILES=<path,...> at startup or load a model "
            "dynamically via REST."
        )
    return (
        f"[UNKNOWN_MODEL] Model '{selector}' is not loaded on this server. "
        f"Available models: {', '.join(sorted(available))}. "
        "Set the connection's `database` (or `catalog`) field to one "
        "of these names. In DBeaver: Connection → Database field. "
        "Pyarrow: client.do_get(...) with a FlightCallOptions header "
        "(b'database', b'<name>')."
    )


def format_ambiguous_model_error(available: list[str]) -> str:
    return (
        "[NO_MODEL_SELECTED] Multiple models are loaded and no selector "
        "was sent on this connection. Pick one by setting the "
        "connection's `database` field (or `x-obsl-model` header). "
        f"Available models: {', '.join(sorted(available))}.\n"
        "\n"
        "  DBeaver:    Connection → Database field = <name>\n"
        "  Tableau:    Same field on the Arrow Flight JDBC connector\n"
        "  pyarrow:    options = flight.FlightCallOptions(\n"
        "                  headers=[(b'database', b'<name>')])\n"
        "  REST:       Use /v1/sessions/<name>/query/semantic-ql\n"
        "\n"
        "Discover available models via GET /v1/models."
    )
