"""Who did what: one audit event per authenticated action, and per refusal.

``AUDIT_LOG`` turns it on: ``log`` writes each event through the logger
``orionbelt.audit`` (a JSON line with ``LOG_FORMAT=json`` or ``cloudrun``,
which log shippers take as is), ``file`` appends JSON lines to
``AUDIT_LOG_PATH``, ``custom`` hands each event to an object of your own,
made by ``AUDIT_SINK=<module>:<factory>``. Off by default.

An event says who (the caller's kind, ``sub``, the API key's id or the BI
token's id), what (a REST route template, a Flight command, a pgwire login or
query), on what (session, model), with what outcome, from which address, and
the request id. Never a token, a key, SQL text, a value or a row. A sink that
fails is logged and skipped: auditing never breaks a request.

REST records every ``/v1`` request; Flight SQL and the Postgres wire protocol
record each sign-in and each semantic query, not the catalog lookups BI tools
send by the hundred. See design/PLAN_auth_phase6.md (A5-A7).
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import structlog

from orionbelt.auth.errors import AuthConfigError
from orionbelt.auth.principal import Principal

AUDIT_OFF = "off"
AUDIT_TO_LOG = "log"
AUDIT_TO_FILE = "file"
AUDIT_TO_SINK = "custom"
_MODES = (AUDIT_OFF, AUDIT_TO_LOG, AUDIT_TO_FILE, AUDIT_TO_SINK)

logger = logging.getLogger("orionbelt.auth")


class AuditSink(Protocol):
    """Where audit events go: ``AUDIT_SINK`` names a factory that returns one.

    ``emit`` may be called from several threads at once, and should return
    quickly: it runs inside the request it records. An exception it raises is
    logged and the request goes on.
    """

    def emit(self, event: dict[str, Any]) -> None:
        """Record one event (a JSON-ready dict); may raise."""


class LogSink:
    """Each event through the logger ``orionbelt.audit``, its fields as the log line's."""

    def __init__(self) -> None:
        self._log = structlog.get_logger("orionbelt.audit")

    def emit(self, event: dict[str, Any]) -> None:
        self._log.info("audit", **event)


class FileSink:
    """Each event as a JSON line appended to a file, opened per event (rotation-safe)."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)


_sink: AuditSink | None = None


def configure_audit(mode: str, path: str = "", sink_spec: str = "") -> None:
    """Set where events go (``AUDIT_LOG``, ``AUDIT_LOG_PATH``, ``AUDIT_SINK``).

    Raises AuthConfigError. The file sink is opened, and a custom sink made,
    here, so a destination that cannot work refuses the start instead of
    losing every event.
    """
    global _sink  # noqa: PLW0603
    chosen = (mode or AUDIT_OFF).strip().lower()
    if chosen not in _MODES:
        raise AuthConfigError(f"AUDIT_LOG={mode!r} is not one of: {', '.join(_MODES)}.")
    if sink_spec.strip() and chosen != AUDIT_TO_SINK:
        raise AuthConfigError(
            f"AUDIT_SINK is set, but AUDIT_LOG is {chosen!r}: set AUDIT_LOG=custom to use it."
        )
    if chosen == AUDIT_OFF:
        _sink = None
        return
    if chosen == AUDIT_TO_SINK:
        _sink = _custom_sink(sink_spec.strip())
        logger.info("Audit: events go to AUDIT_SINK %s", sink_spec.strip())
        return
    if chosen == AUDIT_TO_LOG:
        # Its own level: LOG_LEVEL=WARNING quiets the application, not the audit.
        logging.getLogger("orionbelt.audit").setLevel(logging.INFO)
        _sink = LogSink()
    else:
        if not path.strip():
            raise AuthConfigError("AUDIT_LOG=file needs AUDIT_LOG_PATH.")
        sink = FileSink(path.strip())
        try:
            sink.path.parent.mkdir(parents=True, exist_ok=True)
            with sink.path.open("a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise AuthConfigError(
                f"AUDIT_LOG_PATH {path!r} cannot be written ({exc.strerror})."
            ) from None
        _sink = sink
    logger.info("Audit: events go to %s", "the log" if chosen == AUDIT_TO_LOG else path.strip())


def _custom_sink(spec: str) -> AuditSink:
    """The sink ``AUDIT_SINK=<module>:<factory>`` makes. Raises AuthConfigError."""
    module_name, separator, factory_name = spec.partition(":")
    if not separator or not module_name or not factory_name:
        raise AuthConfigError(
            "AUDIT_LOG=custom needs AUDIT_SINK=<module>:<factory>, "
            "for example mycompany.obsl_audit:make_sink."
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AuthConfigError(
            f"AUDIT_SINK {spec!r}: cannot import {module_name!r} ({exc})."
        ) from None
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise AuthConfigError(
            f"AUDIT_SINK {spec!r}: {module_name} has no callable {factory_name!r}."
        )
    try:
        made = factory()
    except Exception as exc:  # noqa: BLE001 - any failure of the factory refuses the start
        raise AuthConfigError(
            f"AUDIT_SINK {spec!r}: the factory failed ({type(exc).__name__}: {exc})."
        ) from None
    if not callable(getattr(made, "emit", None)):
        raise AuthConfigError(
            f"AUDIT_SINK {spec!r}: the factory returned no object with emit(event)."
        )
    sink: AuditSink = made
    return sink


def reset_audit() -> None:
    """No audit (tests, shutdown)."""
    global _sink  # noqa: PLW0603
    _sink = None


def audit_enabled() -> bool:
    """Whether events are recorded at all: callers skip building them otherwise."""
    return _sink is not None


def _who(principal: Principal | None) -> dict[str, str]:
    if principal is None:
        return {"kind": "anonymous"}
    who = {"kind": principal.kind}
    for name in ("sub", "key_id", "token_id"):
        value = getattr(principal, name)
        if value:
            who[name] = value
    return who


def record(
    *,
    surface: str,
    action: str,
    outcome: str,
    principal: Principal | None = None,
    reason: str | None = None,
    client: str | None = None,
    request_id: str | None = None,
    **target: str | None,
) -> None:
    """Record one event: ``surface`` (rest, flight, pgwire), ``action``, ``outcome``.

    ``reason`` is a refusal's code; ``target`` names what it was on
    (``session_id``, ``model_id``, ``database``), never a value of the query.
    """
    sink = _sink
    if sink is None:
        return
    event: dict[str, Any] = {
        "time": datetime.now(UTC).isoformat(),
        "surface": surface,
        "action": action,
        "outcome": outcome,
        "who": _who(principal),
    }
    if reason:
        event["reason"] = reason
    on = {name: value for name, value in target.items() if value}
    if on:
        event["target"] = on
    if client:
        event["client"] = client
    if request_id:
        event["request_id"] = request_id
    try:
        sink.emit(event)
    except Exception as exc:  # noqa: BLE001 - auditing never breaks a request
        logger.warning("Audit event lost: %s", type(exc).__name__)
