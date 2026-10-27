"""Running a federated query: its legs on their data sources, then the combine step.

``compiler/federation.py`` splits a query reading several data sources into
legs, one query per source, and a DuckDB query combining their results. This
module holds what running that plan needs, whatever the surface:

* **Admission.** A federated query holds a connection per leg, every leg's
  result and a DuckDB instance at once, so at most
  ``FEDERATION_MAX_CONCURRENT`` run in the process. The rest wait in order of
  arrival, at most ``FEDERATION_QUEUE_TIMEOUT_SECONDS``, then are refused as
  busy rather than left hanging. A plain semaphore is not enough: it is not
  first-come-first-served, and under load a waiter can starve for minutes.
* **Legs** run on a thread pool of their own (``FEDERATION_LEG_THREADS``),
  never the event loop's default executor: a query waiting there for its legs
  could otherwise hold the threads its legs need.
* **Normalising** a leg's Arrow table into types DuckDB combines exactly:
  Postgres ``numeric`` arrives as text, a wide decimal as ``decimal256``,
  which DuckDB cannot read, and an all-NULL column without a type.
* **The combine** runs in a fresh in-memory DuckDB, with its threads and
  memory set rather than DuckDB's defaults (all cores, 80% of RAM, per
  instance).

``design/PLAN_federation.md`` §7 has the measurements behind these bounds.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionResult,
    ExecutionUnavailableError,
    duckdb_execution_result,
)

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from orionbelt.compiler.federation import FederatedLeg, FederatedPlan

#: Arrow's widest ``decimal128``, the widest decimal DuckDB reads from Arrow.
_DECIMAL128_MAX_PRECISION = 38

_NUMERIC_TYPES = frozenset({"int", "float", "decimal"})
_TEMPORAL_TYPES = frozenset({"date", "datetime", "timestamp", "timestamp_tz"})


class FederationBusyError(ExecutionUnavailableError):
    """Every federated query slot stayed taken for the whole wait."""

    code = "FEDERATION_BUSY"


class FederatedLegError(ExecutionError):
    """A leg of a federated query failed or returned what cannot be combined."""


@dataclass(frozen=True)
class FederationLimits:
    """The bounds a federated query runs within; see the module docstring.

    Attributes:
        max_concurrent: Federated queries running at once in the process.
        queue_timeout_seconds: How long a query waits for a slot.
        leg_threads: Legs fetched at once in the process.
        max_leg_rows: The most rows one leg may return.
        combine_threads: DuckDB threads per combine step.
        combine_memory_limit: DuckDB ``memory_limit`` per combine step.
    """

    max_concurrent: int = 4
    queue_timeout_seconds: float = 30.0
    leg_threads: int = 16
    max_leg_rows: int = 1_000_000
    combine_threads: int = 2
    combine_memory_limit: str = "512MB"

    @classmethod
    def from_environment(cls) -> FederationLimits:
        """The limits the ``FEDERATION_*`` variables set, defaults elsewhere."""
        defaults = cls()

        def number(variable: str, default: Any) -> Any:
            raw = os.environ.get(variable)
            return type(default)(raw) if raw not in (None, "") else default

        return cls(
            max_concurrent=number("FEDERATION_MAX_CONCURRENT", defaults.max_concurrent),
            queue_timeout_seconds=number(
                "FEDERATION_QUEUE_TIMEOUT_SECONDS", defaults.queue_timeout_seconds
            ),
            leg_threads=number("FEDERATION_LEG_THREADS", defaults.leg_threads),
            max_leg_rows=number("FEDERATION_MAX_LEG_ROWS", defaults.max_leg_rows),
            combine_threads=number("FEDERATION_COMBINE_THREADS", defaults.combine_threads),
            combine_memory_limit=os.environ.get("FEDERATION_COMBINE_MEMORY_LIMIT")
            or defaults.combine_memory_limit,
        )


class AdmissionGate:
    """At most ``limit`` holders at once; the rest admitted in order of arrival.

    :meth:`request` never blocks: it returns a future that completes when the
    caller is admitted, so a thread waits on it with ``result(timeout)`` and a
    coroutine with ``asyncio.wrap_future`` without holding a thread. A caller
    that stops waiting calls :meth:`withdraw`, which also covers being admitted
    in the moment it gave up.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._active = 0
        self._waiting: deque[Future[None]] = deque()
        self._lock = threading.Lock()

    def request(self) -> Future[None]:
        """A future completed once the caller holds a slot."""
        ticket: Future[None] = Future()
        with self._lock:
            if self._active < self._limit and not self._waiting:
                self._active += 1
                ticket.set_result(None)
            else:
                self._waiting.append(ticket)
        return ticket

    def release(self) -> None:
        """Give a held slot to the next waiter still waiting, else free it."""
        with self._lock:
            while self._waiting:
                ticket = self._waiting.popleft()
                if ticket.set_running_or_notify_cancel():
                    ticket.set_result(None)
                    return
            self._active -= 1

    def withdraw(self, ticket: Future[None]) -> None:
        """Stop waiting on *ticket*; release its slot if it was granted meanwhile."""
        with self._lock:
            if ticket.cancel() or ticket.cancelled():
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                return
        self.release()

    @property
    def active(self) -> int:
        """Slots held now."""
        return self._active

    @property
    def waiting(self) -> int:
        """Callers waiting for a slot now."""
        return len(self._waiting)


_lock = threading.Lock()
_limits: FederationLimits | None = None
_gate: AdmissionGate | None = None
_leg_pool: ThreadPoolExecutor | None = None


def configure(limits: FederationLimits) -> None:
    """Set the process's federation limits; queries running keep the old ones."""
    global _limits, _gate, _leg_pool  # noqa: PLW0603 - process-wide bounds
    with _lock:
        _limits = limits
        _gate = AdmissionGate(limits.max_concurrent)
        old, _leg_pool = _leg_pool, None
    if old is not None:
        old.shutdown(wait=False)


def federation_limits() -> FederationLimits:
    """The process's federation limits, from the environment until configured."""
    global _limits  # noqa: PLW0603 - process-wide bounds
    with _lock:
        if _limits is None:
            _limits = FederationLimits.from_environment()
        return _limits


def admission_gate() -> AdmissionGate:
    """The process's gate for federated queries."""
    global _gate  # noqa: PLW0603 - process-wide bounds
    limits = federation_limits()
    with _lock:
        if _gate is None:
            _gate = AdmissionGate(limits.max_concurrent)
        return _gate


def leg_executor() -> ThreadPoolExecutor:
    """The process's thread pool for legs and combine steps."""
    global _leg_pool  # noqa: PLW0603 - process-wide bounds
    limits = federation_limits()
    with _lock:
        if _leg_pool is None:
            _leg_pool = ThreadPoolExecutor(
                max_workers=max(1, limits.leg_threads), thread_name_prefix="obsl-federation"
            )
        return _leg_pool


def busy_error(limits: FederationLimits) -> FederationBusyError:
    """The refusal for a query that waited its whole timeout for a slot."""
    return FederationBusyError(
        f"[FEDERATION_BUSY] {limits.max_concurrent} queries across data sources are "
        f"running, and none finished within {limits.queue_timeout_seconds:g} s. "
        "Retry later, or raise FEDERATION_MAX_CONCURRENT."
    )


def leg_label(leg: FederatedLeg) -> str:
    """How an error names a leg: its data source and dialect."""
    source = leg.data_source or f"the {leg.dialect} data source"
    return f"leg {leg.name} on {source} ({leg.dialect})"


def check_leg_rows(leg: FederatedLeg, row_count: int) -> None:
    """Refuse a leg that returned more than ``FEDERATION_MAX_LEG_ROWS`` rows.

    Its SQL asks for one row more than the limit, so a leg at the limit plus
    one was cut short, and combining it would total a part as the whole.

    Raises:
        FederatedLegError: ``FEDERATION_LEG_TOO_LARGE``.
    """
    limits = federation_limits()
    if row_count > limits.max_leg_rows:
        raise FederatedLegError(
            f"[FEDERATION_LEG_TOO_LARGE] {leg_label(leg)} returns more than "
            f"{limits.max_leg_rows} rows, the most one leg may return "
            "(FEDERATION_MAX_LEG_ROWS). Filter the query or select fewer dimensions."
        )


def leg_table(leg: FederatedLeg, result: ExecutionResult) -> Any:
    """A leg's rows as Arrow, in the types its declared columns combine as.

    Raises:
        FederatedLegError: A value no exact type holds.
    """
    import pyarrow as pa

    table = result.arrow_table
    if table is None:
        rows = result.rows
        names = [c.name for c in result.columns]
        table = pa.table({name: [row[i] for row in rows] for i, name in enumerate(names)})
    return normalize_leg(leg, table)


def normalize_leg(leg: FederatedLeg, table: Any) -> Any:
    """*table* with each column in a type DuckDB reads and combines exactly.

    * A numeric column that arrived as text - Postgres ``numeric`` through
      ADBC, or a cached copy of it - becomes a decimal at the scale its
      values carry.
    * ``decimal256`` narrows to ``decimal128``, which DuckDB reads, when every
      value fits; one that does not is refused, never rounded.
    * An all-NULL column without a type takes its declared one.
    * A date or timestamp that arrived as ISO text - a driver without Arrow
      hands rows over that way - is read back as one when every value parses.

    Other columns pass unchanged: the combine step casts its results to the
    model's types, as a query on one source does.
    """
    import pyarrow as pa

    from orionbelt.service.result_schema import obml_type_to_arrow

    declared = dict(leg.columns)
    columns = []
    for name in table.column_names:
        column = table.column(name)
        storage = getattr(column.type, "storage_type", None)
        if storage is not None:
            column = pa.chunked_array([chunk.storage for chunk in column.chunks], type=storage)
        kind = declared.get(name)
        if pa.types.is_null(column.type) and kind is not None:
            column = column.cast(obml_type_to_arrow(kind))
        elif kind in _NUMERIC_TYPES and (
            pa.types.is_string(column.type) or pa.types.is_large_string(column.type)
        ):
            column = _decimal_from_text(leg, name, column)
        elif kind in _TEMPORAL_TYPES and pa.types.is_string(column.type):
            column = _temporal_from_text(column, obml_type_to_arrow(kind))
        elif pa.types.is_decimal256(column.type):
            column = _narrow_decimal(leg, name, column)
        columns.append(column)
    return pa.table(columns, names=table.column_names)


def _decimal_from_text(leg: FederatedLeg, name: str, column: Any) -> Any:
    """A text column of numbers as ``decimal128`` at the scale its values carry."""
    import pyarrow as pa

    scale = 0
    integer_digits = 0
    for text in column.to_pylist():
        if text is None:
            continue
        try:
            value = Decimal(text)
        except InvalidOperation:
            value = Decimal("NaN")
        if not value.is_finite():
            raise FederatedLegError(
                f"[FEDERATION_LEG_TYPE] {leg_label(leg)} returns {text!r} in column "
                f"'{name}', which no decimal holds"
            )
        _, digits, exponent = value.as_tuple()
        assert isinstance(exponent, int)  # finite
        scale = max(scale, -exponent)
        # Zero, or a value below one, needs no integer digit: 0 and 1E-38 both
        # fit decimal128(38, 38).
        if any(digits):
            integer_digits = max(integer_digits, len(digits) + exponent)
    if integer_digits + scale > _DECIMAL128_MAX_PRECISION:
        raise _too_wide(leg, name)
    return column.cast(pa.decimal128(_DECIMAL128_MAX_PRECISION, scale))


def _temporal_from_text(column: Any, target: Any) -> Any:
    """ISO text as *target*, or the text unchanged where a value does not parse."""
    import pyarrow as pa

    try:
        return column.cast(target)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
        return column


def _narrow_decimal(leg: FederatedLeg, name: str, column: Any) -> Any:
    """``decimal256`` as ``decimal128`` at the same scale, when every value fits."""
    import pyarrow as pa

    try:
        return column.cast(pa.decimal128(_DECIMAL128_MAX_PRECISION, column.type.scale))
    except (pa.ArrowInvalid, ValueError):
        raise _too_wide(leg, name) from None


def _too_wide(leg: FederatedLeg, name: str) -> FederatedLegError:
    return FederatedLegError(
        f"[FEDERATION_LEG_TYPE] {leg_label(leg)} returns column '{name}' with more than "
        f"{_DECIMAL128_MAX_PRECISION} digits, more than the combine step holds exactly"
    )


def combine(plan: FederatedPlan, tables: dict[str, Any], tz: ZoneInfo | None) -> ExecutionResult:
    """Run the combine query over the legs' tables in a fresh in-memory DuckDB.

    Raises:
        ExecutionError: The combine query failed.
    """
    import duckdb

    limits = federation_limits()
    t0 = time.monotonic()
    con = duckdb.connect(
        config={
            "threads": max(1, limits.combine_threads),
            "memory_limit": limits.combine_memory_limit,
        }
    )
    try:
        for name, table in tables.items():
            con.register(name, table)
        return duckdb_execution_result(con.sql(plan.combine_sql), t0, tz=tz)
    except duckdb.Error as exc:
        raise ExecutionError(f"Combining the federated query's legs failed: {exc}") from exc
    finally:
        con.close()


__all__ = [
    "AdmissionGate",
    "FederatedLegError",
    "FederationBusyError",
    "FederationLimits",
    "admission_gate",
    "busy_error",
    "check_leg_rows",
    "combine",
    "configure",
    "federation_limits",
    "leg_executor",
    "leg_label",
    "leg_table",
    "normalize_leg",
]
