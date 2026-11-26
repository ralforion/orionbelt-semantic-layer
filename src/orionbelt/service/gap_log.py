"""What a model could not answer: one record per refused query, for its author.

``GAP_LOG`` sets what is kept. ``names`` (the default) keeps each error's code,
the name it is about and the names suggested instead, with the dimensions,
measures and fields the query asked for. ``full`` also keeps the error's
message and the query's filters, which can hold customer data. ``off`` keeps
nothing.

Records stay in memory, the newest ``GAP_LOG_SIZE`` across all models, and each
one is also written through the logger ``orionbelt.gaps`` (a JSON line with
``LOG_FORMAT=json`` or ``cloudrun``), so a log store keeps the history.

A caller reads back only the records its own queries made, matched by its
caller key (:func:`orionbelt.auth.caller_key`). A signed-in user with
``obsl:admin`` reads all of a model's records. A record made without a caller
key (``AUTH_MODE=none`` and no session in the request) is in the log only.
Recording never fails a query: an error while recording is logged and dropped.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from orionbelt.auth import SCOPE_ADMIN, Principal, current_caller
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.sql_translator import SQLTranslationError
from orionbelt.dialect.base import (
    UnsupportedAggregationError,
    UnsupportedFunctionError,
    UnsupportedGroupingError,
    UnsupportedNestedAccessError,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import CoalesceDimension, QueryObject
from orionbelt.service import metrics

GAP_OFF = "off"
GAP_NAMES = "names"
GAP_FULL = "full"
MODES = (GAP_OFF, GAP_NAMES, GAP_FULL)

#: The refusals that say a model cannot answer a query. Anything else that
#: fails a compile, such as an unknown model or dialect, is the request's
#: problem, not a gap in the model.
REFUSALS: tuple[type[Exception], ...] = (
    ResolutionError,
    FanoutError,
    SQLTranslationError,
    UnsupportedAggregationError,
    UnsupportedFunctionError,
    UnsupportedGroupingError,
    UnsupportedNestedAccessError,
)

# Context keys whose value names an artefact, a query item or an engine
# feature. Other keys can carry a value from the query, which ``names`` must
# not keep, so they are never read.
_SUBJECT_KEYS = (
    "measure",
    "metric",
    "dimension",
    "field",
    "item",
    "dataObject",
    "pathName",
    "aggregation",
    "function",
    "grouping",
)

logger = logging.getLogger(__name__)
_events = structlog.get_logger("orionbelt.gaps")


@dataclass(frozen=True)
class GapRecord:
    """One error of a refused query, as the gap log keeps it."""

    at: datetime
    model_id: str
    surface: str
    caller: str | None
    code: str
    subject: str | None
    suggestions: tuple[str, ...]
    dimensions: tuple[str, ...]
    measures: tuple[str, ...]
    fields: tuple[str, ...]
    #: Only with ``GAP_LOG=full``: the error's message and the query's filters.
    message: str | None = None
    filters: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class GapSummary:
    """The records of one (code, subject) pair, counted."""

    code: str
    subject: str | None
    count: int
    callers: int
    first_seen: datetime
    last_seen: datetime
    suggestions: tuple[str, ...]


class GapLog:
    """The records in memory: the newest ``size`` of them, across all models."""

    def __init__(self, mode: str, size: int) -> None:
        self.mode = mode
        self._records: deque[GapRecord] = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, records: Iterable[GapRecord]) -> None:
        """Keep ``records``, dropping the oldest beyond the size."""
        with self._lock:
            self._records.extend(records)

    def records(self, model_id: str, *, caller: str | None, see_all: bool) -> list[GapRecord]:
        """The model's records the caller may read, oldest first."""
        with self._lock:
            kept = list(self._records)
        return [
            r
            for r in kept
            if r.model_id == model_id and (see_all or (caller is not None and r.caller == caller))
        ]


_log: GapLog | None = None


def configure(mode: str, size: int) -> None:
    """Keep records as ``GAP_LOG`` and ``GAP_LOG_SIZE`` say; ``off`` drops them all."""
    global _log
    if mode not in MODES:
        raise ValueError(f"GAP_LOG must be one of {', '.join(MODES)}, not '{mode}'")
    if size < 1:
        raise ValueError(f"GAP_LOG_SIZE must be at least 1, not {size}")
    _log = None if mode == GAP_OFF else GapLog(mode, size)


def current_mode() -> str:
    """``GAP_LOG`` as configured: ``off`` until :func:`configure` turns it on."""
    return GAP_OFF if _log is None else _log.mode


def may_read_all(principal: Principal | None) -> bool:
    """Whether ``principal`` reads every caller's records: a signed-in admin only.

    API key and ``AUTH_MODE=none`` callers hold every scope, so the scope
    alone would let any of them read everyone's records.
    """
    return principal is not None and principal.kind == "user" and principal.has_scope(SCOPE_ADMIN)


def record_refusal(model_id: str, exc: Exception, query: QueryObject | None = None) -> None:
    """Keep one record per error of a refused query; nothing while the log is off."""
    log = _log
    if log is None:
        return
    try:
        records = _records_for(log.mode, model_id, exc, query)
        log.add(records)
        for record in records:
            _events.info("query_refused", **_log_fields(record))
    except Exception:
        logger.exception("Recording a refused query in the gap log failed")


def read(
    model_id: str,
    *,
    caller: str | None,
    see_all: bool,
    code: str | None = None,
    since: datetime | None = None,
) -> list[GapRecord]:
    """The model's records the caller may read, oldest first, optionally narrowed."""
    log = _log
    if log is None:
        return []
    records = log.records(model_id, caller=caller, see_all=see_all)
    return [
        r for r in records if (code is None or r.code == code) and (since is None or r.at >= since)
    ]


def summarize(records: Iterable[GapRecord]) -> list[GapSummary]:
    """One row per (code, subject), the most frequent first, then the latest."""
    groups: dict[tuple[str, str | None], list[GapRecord]] = {}
    for record in records:
        groups.setdefault((record.code, record.subject), []).append(record)
    rows = [
        GapSummary(
            code=code,
            subject=subject,
            count=len(group),
            callers=len({r.caller for r in group}),
            first_seen=group[0].at,
            last_seen=group[-1].at,
            suggestions=group[-1].suggestions,
        )
        for (code, subject), group in groups.items()
    ]
    rows.sort(key=lambda row: (-row.count, -row.last_seen.timestamp()))
    return rows


def _records_for(
    mode: str, model_id: str, exc: Exception, query: QueryObject | None
) -> list[GapRecord]:
    full = mode == GAP_FULL
    at = datetime.now(UTC)
    surface = metrics.SURFACE.get()
    caller = current_caller()
    dimensions, measures, fields = _asked(query)
    filters = _filters(query) if full else ()
    return [
        GapRecord(
            at=at,
            model_id=model_id,
            surface=surface,
            caller=caller,
            code=error.code,
            subject=_subject(error),
            suggestions=tuple(error.suggestions),
            dimensions=dimensions,
            measures=measures,
            fields=fields,
            message=error.message if full else None,
            filters=filters,
        )
        for error in _refusal_errors(exc)
    ]


def _refusal_errors(exc: Exception) -> list[SemanticError]:
    """The errors a refusal carries; a dialect refusal gets the code the API reports."""
    if isinstance(exc, ResolutionError | FanoutError | SQLTranslationError):
        return list(exc.errors)
    if isinstance(exc, UnsupportedAggregationError):
        return [_dialect_error("UNSUPPORTED_AGGREGATION", exc, {"aggregation": exc.aggregation})]
    if isinstance(exc, UnsupportedFunctionError):
        return [_dialect_error("UNSUPPORTED_FUNCTION", exc, {"function": exc.function})]
    if isinstance(exc, UnsupportedGroupingError):
        return [_dialect_error("UNSUPPORTED_GROUPING", exc, {"grouping": exc.grouping})]
    if isinstance(exc, UnsupportedNestedAccessError):
        return [_dialect_error("UNSUPPORTED_NESTED_ACCESS", exc, {"dataObject": exc.alias})]
    return []


def _dialect_error(code: str, exc: Exception, context: dict[str, Any]) -> SemanticError:
    return SemanticError(code=code, message=str(exc), context=context)


def _subject(error: SemanticError) -> str | None:
    """The name the error is about, read only from keys that hold a name."""
    context = error.context or {}
    for key in _SUBJECT_KEYS:
        value = context.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _asked(query: QueryObject | None) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The dimensions, measures and raw fields the query selected."""
    if query is None:
        return (), (), ()
    select = query.select
    dimensions = tuple(
        d.alias if isinstance(d, CoalesceDimension) else d for d in select.dimensions
    )
    return dimensions, tuple(select.measures), tuple(select.fields)


def _filters(query: QueryObject | None) -> tuple[dict[str, Any], ...]:
    if query is None:
        return ()
    return tuple(
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        for item in (*query.where, *query.having)
    )


def _log_fields(record: GapRecord) -> dict[str, Any]:
    fields = asdict(record)
    fields["at"] = record.at.isoformat()
    if record.message is None:
        fields.pop("message")
    if not record.filters:
        fields.pop("filters")
    return fields
