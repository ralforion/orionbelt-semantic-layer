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
* **Key injection.** A fact leg whose dimension legs run a filter of the
  query may wait for them and read only the keys they returned, up to the
  fact source's ``KEY_INJECTION_MAX_KEYS``; or join a small dimension leg's
  keys as a map, which the combine step then reads in that leg's place.
* **Normalising** a leg's Arrow table into types DuckDB combines exactly:
  Postgres ``numeric`` arrives as text, a wide decimal as ``decimal256``,
  which DuckDB cannot read, and an all-NULL column without a type.
* **The combine** runs in a fresh in-memory DuckDB, with its threads and
  memory set rather than DuckDB's defaults (all cores, 80% of RAM, per
  instance).
* **Leg size.** A leg is streamed batch by batch, and refused once it holds
  more than ``FEDERATION_MAX_LEG_MB`` in memory or returns more than
  ``FEDERATION_MAX_LEG_ROWS`` rows: rows alone do not say what a leg takes.
* **Spilling** a large leg to disk, into ``FEDERATION_SPILL_DIR``, else the
  system's temp directory unless that is in memory (:func:`spill_directory`):
  a leg is held in memory up to ``FEDERATION_SPILL_THRESHOLD_MB``, then written
  to a file in the Arrow IPC stream format, which the combine step reads
  memory-mapped, so the operating system can page it out. Each query writes
  into a directory of its own, removed when it ends, within
  ``FEDERATION_SPILL_MAX_MB`` and a process-wide ``FEDERATION_SPILL_BUDGET_MB``.

``design/PLAN_federation.md`` §7 has the measurements behind these bounds.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from orionbelt.dialect.registry import DialectRegistry
from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionResult,
    ExecutionUnavailableError,
    duckdb_execution_result,
)

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from ob_flight.datasources import DataSource

    from orionbelt.compiler.federation import FederatedLeg, FederatedPlan

_MB = 1024 * 1024

#: Arrow's widest ``decimal128``, the widest decimal DuckDB reads from Arrow.
_DECIMAL128_MAX_PRECISION = 38

_NUMERIC_TYPES = frozenset({"int", "float", "decimal"})
_TEMPORAL_TYPES = frozenset({"date", "datetime", "timestamp", "timestamp_tz"})


class FederationBusyError(ExecutionUnavailableError):
    """Every federated query slot stayed taken for the whole wait."""

    code = "FEDERATION_BUSY"


class FederatedLegError(ExecutionError):
    """A leg of a federated query failed or returned what cannot be combined."""


class FederationSpillFullError(ExecutionUnavailableError):
    """The process's spill budget is taken by the queries running now."""

    code = "FEDERATION_SPILL_FULL"


@dataclass(frozen=True)
class FederationLimits:
    """The bounds a federated query runs within; see the module docstring.

    Attributes:
        max_concurrent: Federated queries running at once in the process.
        queue_timeout_seconds: How long a query waits for a slot.
        leg_threads: Legs fetched at once in the process.
        max_leg_rows: The most rows one leg may return.
        max_leg_bytes: The most memory one leg may hold; a leg spilled to disk
            holds the columns normalizing converts (:func:`hold_leg`), and is
            bounded by ``spill_max_bytes`` otherwise.
        combine_threads: DuckDB threads per combine step.
        combine_memory_limit: DuckDB ``memory_limit`` per combine step.
        spill_dir: Where large legs are spilled; ``None`` keeps every leg in
            memory. It must be real disk: on an in-memory filesystem, such as
            Cloud Run's, spilling saves nothing. :meth:`from_environment`
            defaults it with :func:`spill_directory`.
        spill_threshold_bytes: A leg past this size is spilled.
        spill_max_bytes: The most one query may spill.
        spill_budget_bytes: The most the queries running at once may spill.
    """

    max_concurrent: int = 4
    queue_timeout_seconds: float = 30.0
    leg_threads: int = 16
    max_leg_rows: int = 5_000_000
    max_leg_bytes: int = 512 * _MB
    combine_threads: int = 2
    combine_memory_limit: str = "512MB"
    spill_dir: str | None = None
    spill_threshold_bytes: int = 64 * _MB
    spill_max_bytes: int = 4096 * _MB
    spill_budget_bytes: int = 16384 * _MB

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
            max_leg_bytes=number("FEDERATION_MAX_LEG_MB", 512) * _MB,
            combine_threads=number("FEDERATION_COMBINE_THREADS", defaults.combine_threads),
            combine_memory_limit=os.environ.get("FEDERATION_COMBINE_MEMORY_LIMIT")
            or defaults.combine_memory_limit,
            spill_dir=spill_directory(os.environ.get("FEDERATION_SPILL_DIR")),
            spill_threshold_bytes=number("FEDERATION_SPILL_THRESHOLD_MB", 64) * _MB,
            spill_max_bytes=number("FEDERATION_SPILL_MAX_MB", 4096) * _MB,
            spill_budget_bytes=number("FEDERATION_SPILL_BUDGET_MB", 16384) * _MB,
        )


#: Filesystems whose files are memory: a leg spilled there frees none.
_IN_MEMORY_FILESYSTEMS = frozenset({"tmpfs", "ramfs"})

#: Linux's table of the process's mounts.
_MOUNTINFO = "/proc/self/mountinfo"


def spill_directory(configured: str | None) -> str | None:
    """Where large legs spill: ``FEDERATION_SPILL_DIR``, else the temp directory.

    ``off`` turns spilling off. Unset, legs spill into the system's temp
    directory (``TMPDIR``, else ``/tmp`` or the platform's), unless it is in
    memory: a tmpfs or ramfs, or Cloud Run's filesystem, where a spilled leg
    takes the memory it was meant to free. ``None`` keeps every leg in memory.
    """
    if configured is not None and configured.strip():
        directory = configured.strip()
        return None if directory.lower() == "off" else directory
    try:
        directory = tempfile.gettempdir()
    except OSError:  # No writable temp directory at all.
        return None
    return None if _in_memory(directory) else directory


def _in_memory(path: str) -> bool:
    """Whether the files under *path* are held in memory rather than on disk."""
    if os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"):
        # Cloud Run's filesystem is in memory, whatever its mount says.
        return True
    return _filesystem_type(path) in _IN_MEMORY_FILESYSTEMS


def _filesystem_type(path: str) -> str | None:
    """The type of the filesystem *path* is on, from Linux's mount table; else ``None``.

    The mount whose mount point is the longest one holding *path*, the last
    one mounted where several share it. A line of the table is
    ``id parent major:minor root mount-point options [optional...] - type
    source super-options``, with a space in a path written ``\\040``.
    """
    try:
        with open(_MOUNTINFO, encoding="utf-8") as mounts:
            lines = mounts.read().splitlines()
    except OSError:
        return None
    target = os.path.realpath(path)
    found: tuple[int, str] | None = None
    for line in lines:
        fields, _, after = line.partition(" - ")
        parts = fields.split()
        if len(parts) < 5 or not after:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[4])
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and (found is None or len(point) >= found[0]):
            found = (len(point), after.split()[0])
    return found[1] if found is not None else None


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
_spilled_bytes = 0  # Spilled by the queries running now, under ``_lock``.
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
        larger = (
            "or raise FEDERATION_MAX_LEG_ROWS: a leg past FEDERATION_SPILL_THRESHOLD_MB "
            "goes to disk."
            if limits.spill_dir is not None
            else "or hold larger legs: raise FEDERATION_MAX_LEG_ROWS, with "
            "FEDERATION_SPILL_DIR set to a directory on disk to keep them there."
        )
        raise FederatedLegError(
            f"[FEDERATION_LEG_TOO_LARGE] {leg_label(leg)} returns more than "
            f"{limits.max_leg_rows} rows, the most one leg may return "
            f"(FEDERATION_MAX_LEG_ROWS). {_why_so_many(leg, larger)}"
        )


def check_leg_bytes(leg: FederatedLeg, nbytes: int) -> None:
    """Refuse a leg holding more than ``FEDERATION_MAX_LEG_MB`` in memory.

    Rows alone do not say what a leg takes: an integer key takes 8 bytes a
    row, a text field its length plus 4. A leg spilled to disk counts the
    columns normalizing converts (:func:`hold_leg`), and is bounded by
    ``FEDERATION_SPILL_MAX_MB`` otherwise.

    Raises:
        FederatedLegError: ``FEDERATION_LEG_TOO_LARGE``.
    """
    limits = federation_limits()
    if nbytes > limits.max_leg_bytes:
        larger = (
            "or raise FEDERATION_MAX_LEG_MB."
            if limits.spill_dir is not None
            else "or keep larger legs on disk: set FEDERATION_SPILL_DIR to a directory "
            "on disk, or raise FEDERATION_MAX_LEG_MB to hold them in memory."
        )
        raise FederatedLegError(
            f"[FEDERATION_LEG_TOO_LARGE] {leg_label(leg)} holds more than "
            f"{limits.max_leg_bytes // _MB} MB in memory, the most one leg may hold "
            f"(FEDERATION_MAX_LEG_MB). {_why_so_many(leg, larger)}"
        )


def _why_so_many(leg: FederatedLeg, larger: str) -> str:
    """What makes *leg* as large as it is, and what would make it smaller.

    A leg of groups returns a row per key of each join between data sources
    it holds, beside the query's dimensions: revenue by region, with the
    customers on another source, sums per customer, whatever the number of
    regions. A leg of rows, of a query of fields or of a measure computed
    from rows, returns its source's rows, which only a filter narrows.
    *larger* closes the advice: how to let a larger leg through.
    """
    if leg.raw:
        return (
            "A query of fields (select.fields) reads its data source's rows, a row "
            f"per row it reads: filter the query, {larger}"
        )
    if leg.rows is not None:
        return (
            "It returns the rows of its data source a measure computed from rows "
            "reads (a median, a percentile, a mode, ...), each distinct one once: its "
            f"size follows those rows, not how many groups the result has. Filter the "
            f"query, {larger}"
        )
    if not leg.joins:
        return f"Filter the query, select fewer dimensions, {larger}"
    keys = "keys of the joins" if len(leg.joins) > 1 else "key of the join"
    return (
        f"It returns a row per {keys} {', '.join(leg.joins)}, beside the query's "
        "dimensions: its size follows how many keys the query reads, not how many "
        "groups the result has. Filter the query (a filter on a data source's "
        "dimensions narrows its leg, and the measures' leg to the keys that leg "
        "returns), join the data sources on a column with fewer values (a region "
        f"or a category rather than a customer), {larger}"
    )


def check_rows_stood_for(leg: FederatedLeg, table: Any) -> None:
    """Refuse a leg returning rows that stand for more than ``FEDERATION_MAX_LEG_ROWS``.

    A leg returning its data source's rows rather than partial results
    returns each group of equal rows once, with how many it stands for
    (``leg.rows``), and the combine step computes over that many copies.
    Those are as many rows as the leg would have returned one by one, and
    they are bounded the same way.

    Raises:
        FederatedLegError: ``FEDERATION_LEG_TOO_LARGE``.
    """
    import pyarrow.compute as pc

    assert leg.rows is not None
    limits = federation_limits()
    stood_for = pc.sum(table.column(leg.rows)).as_py() or 0
    if stood_for > limits.max_leg_rows:
        raise FederatedLegError(
            f"[FEDERATION_LEG_TOO_LARGE] {leg_label(leg)} stands for {stood_for} rows of its "
            f"data source, more than the {limits.max_leg_rows} one leg may return "
            "(FEDERATION_MAX_LEG_ROWS), which the combine step computes a median, a "
            "correlation or another measure that does not combine from partial results "
            "from. Filter the query or raise the limit."
        )


def key_injection_max_keys(source: DataSource | None, dialect: str) -> int:
    """The most keys a fact leg on *source* reads as a list; 0 for none.

    ``DB_<NAME>_KEY_INJECTION_MAX_KEYS`` for a named source, then
    ``DB_KEY_INJECTION_MAX_KEYS``, then what the dialect measured
    (:attr:`Dialect.key_injection_max_keys`).
    """
    environ = source.environ if source is not None else os.environ
    variables = ["DB_KEY_INJECTION_MAX_KEYS"]
    if source is not None and source.prefix:
        variables.insert(0, f"{source.prefix}KEY_INJECTION_MAX_KEYS")
    for variable in variables:
        raw = environ.get(variable)
        if raw:
            return max(0, int(raw))
    return DialectRegistry.get(dialect).key_injection_max_keys


def injected_fact_leg(
    plan: FederatedPlan, tables: dict[str, Any], max_keys: int
) -> tuple[FederatedLeg, dict[str, Any]]:
    """The fact leg reading only the keys its dimension legs returned, or maps of them.

    A dimension leg the fact leg may join as a map of its keys (``plan.key_maps``)
    is read so, else as a list where it may be. At most *max_keys* keys in
    all, of lists and maps alike, the legs with the fewest first; a leg past
    what is left is not read from, and the fact leg reads every value of that
    key. *tables* holds the dimension legs' results. Lists and maps making the
    statement longer than the engine parses are not read either
    (:attr:`Dialect.max_statement_bytes`).

    Returns the fact leg, and for each leg it joins a map of, the rows the
    combine step reads in that leg's place (:func:`_key_map`).
    """
    # Each column's keys: a list, or a map with the rows numbered.
    found: dict[str, tuple[list[Any], tuple[str, Any] | None]] = {}
    for key_map in plan.key_maps:
        built = _key_map(tables[key_map.leg], key_map.column, max_keys)
        if built is not None:
            found[key_map.column] = (built[0], (key_map.leg, built[1]))
    for injection in plan.injections:
        if injection.column in found:
            continue
        values = tables[injection.leg].column(injection.values or injection.column)
        keys = _key_values(values, injection.abstract_type, max_keys)
        if keys is not None:
            found[injection.column] = (keys, None)
    lists: dict[str, list[Any]] = {}
    maps: dict[str, list[Any]] = {}
    rows: dict[str, Any] = {}
    left = max_keys
    for column, (keys, mapped) in sorted(found.items(), key=lambda item: len(item[1][0])):
        if len(keys) > left:
            break
        if mapped is None:
            lists[column] = keys
        else:
            maps[column] = keys
            rows[mapped[0]] = mapped[1]
        left -= len(keys)
    if not (lists or maps) or plan.inject is None:
        return plan.legs[0], {}
    fact = plan.inject(lists, maps)
    longest = DialectRegistry.get(fact.dialect).max_statement_bytes
    # Longer than the engine parses: it would refuse the query.
    if longest is not None and len(fact.sql.encode()) > longest:
        return plan.legs[0], {}
    return fact, rows


def _key_values(column: Any, abstract_type: str, max_keys: int) -> list[Any] | None:
    """*column*'s distinct values, sorted, as literals of *abstract_type*.

    ``None`` past *max_keys*, or for a value that is not one: an integer key
    read as a fraction, say, which a list of integers would not match. Also
    for a string holding a NUL, which no literal writes on every engine: the
    statement's text ends at it on Postgres.
    """
    import pyarrow.compute as pc

    distinct = pc.drop_null(pc.unique(column))
    if len(distinct) > max_keys:
        return None
    keys: list[Any] = []
    for value in distinct.to_pylist():
        if abstract_type == "int":
            key = _integer(value)
            if key is None:
                return None
            keys.append(key)
        elif isinstance(value, str) and "\x00" not in value:
            keys.append(value)
        else:
            return None
    return sorted(keys)


def _integer(value: Any) -> int | None:
    """*value* as an integer key's literal; ``None`` for a fraction or a boolean."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    if value != int(value):
        return None
    return int(value)


def _key_map(table: Any, column: str, max_keys: int) -> tuple[list[Any], Any] | None:
    """A map of *table*'s integer keys in *column*, and the rows its ordinals number.

    Rows equal in every other column, as DuckDB groups them (the combine step
    groups so), are one row of the map's and take one ordinal; ordinals count
    from 1 in the order of those values, so the same rows make the same map,
    and the fact leg joining it is the statement cached before. Returns the
    ``(key, ordinal)`` pairs, sorted, and one row per ordinal, the ordinal in
    *column*. ``None`` past *max_keys*, for no key, or for a key that is not
    an integer (:func:`_key_values`).
    """
    import duckdb
    import pyarrow as pa
    import pyarrow.compute as pc

    if pc.count_distinct(table.column(column)).as_py() > max_keys:
        return None
    q = DialectRegistry.get("duckdb").quote_identifier
    position = "__row"
    while position in table.column_names:
        position = f"_{position}"
    others = ", ".join(q(name) for name in table.column_names if name != column)
    sql = (
        f"SELECT min({q(position)}), list(DISTINCT {q(column)}) FROM leg "
        f"WHERE {q(column)} IS NOT NULL"
    )
    if others:
        sql += f" GROUP BY {others} ORDER BY {others}"
    con = duckdb.connect(config={"threads": 1})
    try:
        con.register(
            "leg", table.append_column(position, pa.array(range(table.num_rows), pa.int64()))
        )
        groups = [(first, keys) for first, keys in con.sql(sql).fetchall() if keys]
    finally:
        con.close()
    pairs: list[tuple[int, int]] = []
    for ordinal, (_, keys) in enumerate(groups, start=1):
        for value in keys:
            key = _integer(value)
            if key is None:
                return None
            pairs.append((key, ordinal))
    if not pairs or len(pairs) > max_keys:
        return None
    rows = table.take(pa.array([first for first, _ in groups], pa.int64()))
    rows = rows.set_column(
        rows.column_names.index(column), column, pa.array(range(1, len(groups) + 1), pa.int64())
    )
    return sorted(pairs), rows


def result_table(result: ExecutionResult) -> Any:
    """A leg's rows as Arrow, in the types its driver returned."""
    import pyarrow as pa

    table = result.arrow_table
    if table is None:
        rows = result.rows
        names = [c.name for c in result.columns]
        table = pa.table({name: [row[i] for row in rows] for i, name in enumerate(names)})
    return table


def hold_leg(leg: FederatedLeg, table: Any, *, spilled: bool) -> Any:
    """*table* normalized for the combine step, within ``FEDERATION_MAX_LEG_MB``.

    A leg in memory holds its table, and a spilled leg nothing but the mapped
    file. Beside it, each column normalizing converts (a PostgreSQL
    ``numeric`` read as text becomes a decimal) is new memory, counted
    against the limit before it is allocated: a leg is refused at the column
    that would take it past, not once every column is converted.

    Raises:
        FederatedLegError: ``FEDERATION_LEG_TOO_LARGE``, or a value no exact
            type holds (:func:`normalize_leg`).
    """
    return _normalize(leg, table, held=0 if spilled else table.nbytes)[0]


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
    * A timestamp without a zone that arrived with one is its wall-clock time
      in that zone: Databricks hands its session's ``TIMESTAMP`` over zoned,
      and ClickHouse a ``DateTime`` read in a zone (``toTimeZone``). DuckDB
      would read the instant in the server's zone, moving the clock.
    * The partial sums an ``avg`` divides (``leg.exact_sums``) become
      ``decimal128(38, scale)``, the scale the combine query divides them at;
      one holding more places, or a float, is refused, never rounded.
    * The sums and sums of squares a variance combines (``leg.integer_sums``)
      become whole numbers of ``10^-scale`` units, ``decimal128(38, 0)``, on
      the same terms.

    Other columns pass unchanged: the combine step casts its results to the
    model's types, as a query on one source does.
    """
    return _normalize(leg, table)[0]


def _normalize(leg: FederatedLeg, table: Any, held: int | None = None) -> tuple[Any, int]:
    """:func:`normalize_leg`'s table, and the bytes of the columns it converted.

    With *held*, the bytes the leg holds already, a conversion that would take
    it past ``FEDERATION_MAX_LEG_MB`` is refused before it is made.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    from orionbelt.service.result_schema import obml_type_to_arrow

    declared = dict(leg.columns)
    exact = dict(leg.exact_sums)
    units = dict(leg.integer_sums)
    budget = _Budget(leg, held)
    columns = []
    for name in table.column_names:
        column = table.column(name)
        storage = getattr(column.type, "storage_type", None)
        if storage is not None:
            # The same buffers, read as their storage type: nothing is copied.
            column = pa.chunked_array([chunk.storage for chunk in column.chunks], type=storage)
        stored = column
        length = len(column)
        kind = declared.get(name)
        if pa.types.is_null(column.type) and kind is not None:
            target = obml_type_to_arrow(kind)
            budget.reserve(target, length)
            column = column.cast(target)
        elif kind in _NUMERIC_TYPES and (
            pa.types.is_string(column.type) or pa.types.is_large_string(column.type)
        ):
            budget.reserve(pa.decimal128(_DECIMAL128_MAX_PRECISION, 0), length)
            column = _decimal_from_text(leg, name, column)
        elif kind in _TEMPORAL_TYPES and pa.types.is_string(column.type):
            target = obml_type_to_arrow(kind)
            budget.reserve(target, length)
            column = _temporal_from_text(column, target)
        elif kind == "timestamp" and pa.types.is_timestamp(column.type) and column.type.tz:
            budget.reserve(pa.timestamp(column.type.unit), length)
            column = pc.local_timestamp(column)
        elif pa.types.is_decimal256(column.type) and name not in units:
            # A sum of squares arrives at the scale the engine computed it in,
            # BigQuery's 38 places, and narrows only once rescaled to its own.
            budget.reserve(pa.decimal128(_DECIMAL128_MAX_PRECISION, 0), length)
            column = _narrow_decimal(leg, name, column)
        if name in exact or name in units:
            scale = exact[name] if name in exact else units[name]
            target = pa.decimal128(_DECIMAL128_MAX_PRECISION, scale)
            if column.type != target:
                budget.reserve(target, length)
            column = _exact_sum(leg, name, column, scale, variance=name in units)
            if name in units:
                column = _whole_units(column)
        budget.settle(stored, column)
        columns.append(column)
    return pa.table(columns, names=table.column_names), budget.converted


class _Budget:
    """What normalizing a leg allocates, within ``FEDERATION_MAX_LEG_MB``.

    Each conversion reserves the column it makes before making it, so a leg
    past the limit is refused at the column that would take it there; once
    made, the column counts the buffers it allocated. *held* is what the leg
    holds already; ``None`` counts without a limit.
    """

    def __init__(self, leg: FederatedLeg, held: int | None) -> None:
        self._leg = leg
        self._held = held
        self._pending = 0
        self.converted = 0

    def reserve(self, kind: Any, length: int) -> None:
        """Make room for a column of *length* values of Arrow type *kind*."""
        self._pending += _column_bytes(kind, length)
        self._check(self.converted + self._pending)

    def settle(self, stored: Any, column: Any) -> None:
        """Count what converting *stored* into *column* allocated."""
        self._pending = 0
        self.converted += _allocated(stored, column)
        self._check(self.converted)

    def _check(self, nbytes: int) -> None:
        if self._held is not None:
            check_leg_bytes(self._leg, self._held + nbytes)


def _column_bytes(kind: Any, length: int) -> int:
    """What a column of *length* values of Arrow type *kind* allocates: values and validity.

    A type without a fixed width counts its offsets.
    """
    try:
        bits = int(kind.bit_width)
    except ValueError:
        bits = 32
    return (bits * length + 7) // 8 + (length + 7) // 8


def _allocated(stored: Any, column: Any) -> int:
    """The bytes of *column*'s buffers that *stored*'s do not hold.

    A column normalizing left alone, or only read again as another type
    (:func:`_whole_units`), allocated nothing.
    """
    held = {
        buffer.address
        for chunk in stored.chunks
        for buffer in chunk.buffers()
        if buffer is not None
    }
    return sum(
        buffer.size
        for chunk in column.chunks
        for buffer in chunk.buffers()
        if buffer is not None and buffer.address not in held
    )


def _decimal_from_text(leg: FederatedLeg, name: str, column: Any) -> Any:
    """A text column of numbers as ``decimal128`` at the scale its values carry."""
    import pyarrow as pa

    scale = 0
    integer_digits = 0
    # A chunk at a time: a spilled leg's values are read from its file, and
    # only one chunk's become Python objects at once.
    for text in (text for chunk in column.chunks for text in chunk.to_pylist()):
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


def _exact_sum(
    leg: FederatedLeg, name: str, column: Any, scale: int, *, variance: bool = False
) -> Any:
    """Partial sums at *scale*, exactly, for an ``avg``'s division or a *variance*.

    Every value keeps its digits or the leg is refused: a sum at more places
    than *scale* would be rounded, and a floating-point sum is not exact to
    begin with (a ``float`` measure divides its doubles as doubles instead,
    and a variance of floats is refused at planning).
    """
    import pyarrow as pa

    sums = "the sums of a variance" if variance else "the sums of an average"
    if pa.types.is_floating(column.type):
        remedy = (
            "declare the column's type as its engine stores it"
            if variance
            else "declare the measure resultType float, or average a fixed-point column"
        )
        raise FederatedLegError(
            f"[FEDERATION_LEG_TYPE] {leg_label(leg)} returns {sums} in column '{name}' as "
            f"floating point, which the combine step cannot add exactly: {remedy}"
        )
    try:
        return column.cast(pa.decimal128(_DECIMAL128_MAX_PRECISION, scale))
    except (pa.ArrowInvalid, ValueError):
        raise FederatedLegError(
            f"[FEDERATION_LEG_TYPE] {leg_label(leg)} returns {sums} in column '{name}' "
            f"with more than {scale} decimal places, or more than "
            f"{_DECIMAL128_MAX_PRECISION} digits, which the combine step does not add "
            "exactly"
        ) from None


def _whole_units(column: Any) -> Any:
    """``decimal128(38, s)`` as the whole numbers of ``10^-s`` it stores.

    A decimal's storage is that integer already, so the buffers are read again
    at scale 0: nothing is computed and nothing can round.
    """
    import pyarrow as pa

    whole = pa.decimal128(_DECIMAL128_MAX_PRECISION, 0)
    return pa.chunked_array(
        [
            pa.Array.from_buffers(
                whole, len(chunk), chunk.buffers(), null_count=chunk.null_count, offset=chunk.offset
            )
            for chunk in column.chunks
        ],
        type=whole,
    )


def _too_wide(leg: FederatedLeg, name: str) -> FederatedLegError:
    return FederatedLegError(
        f"[FEDERATION_LEG_TYPE] {leg_label(leg)} returns column '{name}' with more than "
        f"{_DECIMAL128_MAX_PRECISION} digits, more than the combine step holds exactly"
    )


class QuerySpill:
    """Where one federated query spills its large legs, and how much it wrote.

    A directory of its own under ``spill_dir``, readable by this process
    alone (``mkdtemp`` creates it ``0700``) and never shared with another
    query: spilled legs hold the warehouse's rows. :meth:`close` removes it
    and returns what it wrote to the process's budget, whether the query
    succeeded or not.
    """

    def __init__(self, limits: FederationLimits) -> None:
        """Raises ExecutionUnavailableError when ``spill_dir`` cannot be written."""
        assert limits.spill_dir is not None
        try:
            os.makedirs(limits.spill_dir, exist_ok=True)
            self.directory = tempfile.mkdtemp(prefix="obsl-federation-", dir=limits.spill_dir)
        except OSError as exc:
            raise ExecutionUnavailableError(
                f"FEDERATION_SPILL_DIR '{limits.spill_dir}' cannot hold spilled legs: {exc}"
            ) from None
        self._limits = limits
        self._written = 0
        self._lock = threading.Lock()

    def reserve(self, leg: FederatedLeg, nbytes: int) -> None:
        """Account for *nbytes* more written for *leg*.

        Raises:
            FederatedLegError: The query would spill past ``FEDERATION_SPILL_MAX_MB``.
            FederationSpillFullError: The process's budget is taken.
        """
        global _spilled_bytes  # noqa: PLW0603 - process-wide budget
        limits = self._limits
        with self._lock:
            if self._written + nbytes > limits.spill_max_bytes:
                raise FederatedLegError(
                    f"[FEDERATION_SPILL_TOO_LARGE] {leg_label(leg)} takes the query past "
                    f"{limits.spill_max_bytes // _MB} MB spilled to disk, the most one query "
                    "may spill (FEDERATION_SPILL_MAX_MB). Filter the query or select fewer "
                    "dimensions."
                )
            with _lock:
                if _spilled_bytes + nbytes > limits.spill_budget_bytes:
                    raise FederationSpillFullError(
                        f"[FEDERATION_SPILL_FULL] The queries across data sources running now "
                        f"have spilled {_spilled_bytes // _MB} MB to disk, and {leg_label(leg)} "
                        f"would take them past {limits.spill_budget_bytes // _MB} MB "
                        "(FEDERATION_SPILL_BUDGET_MB). Retry later."
                    )
                _spilled_bytes += nbytes
            self._written += nbytes

    def close(self) -> None:
        """Remove the query's spilled files and free its share of the budget."""
        global _spilled_bytes  # noqa: PLW0603 - process-wide budget
        shutil.rmtree(self.directory, ignore_errors=True)
        with self._lock, _lock:
            _spilled_bytes -= self._written
            self._written = 0


class LegSink:
    """Streams a leg's record batches into one Arrow table, within its limits.

    Batches are held in memory, at most ``FEDERATION_MAX_LEG_MB`` of them
    (:func:`check_leg_bytes`), so a leg too large is refused as it arrives
    rather than once it is whole. Given the query's :class:`QuerySpill`,
    batches past its ``spill_threshold_bytes``, and the rest of the stream, go
    to a file in the query's directory instead, and the table returned reads
    that file memory-mapped.

    The file is in the IPC *stream* format, not the file format: a driver may
    send each batch with a dictionary of its own (ClickHouse does, for
    ``LowCardinality`` with ``output_format_arrow_low_cardinality_as_dictionary``),
    and the file format allows one dictionary per column. Read memory-mapped,
    the stream format still copies nothing.
    A small leg never touches the disk. ``spilled`` says which happened.
    """

    def __init__(self, leg: FederatedLeg, spill: QuerySpill | None = None) -> None:
        self._leg = leg
        self._spill = spill
        self.spilled = False

    def __call__(self, reader: Any) -> Any:
        import pyarrow as pa

        spill = self._spill
        held: list[Any] = []
        held_bytes = 0
        for batch in reader:
            held.append(batch)
            held_bytes += batch.nbytes
            if spill is not None and held_bytes > spill._limits.spill_threshold_bytes:
                return self._spill_stream(spill, reader, held)
            check_leg_bytes(self._leg, held_bytes)
        return pa.Table.from_batches(held, schema=reader.schema)

    def _spill_stream(self, spill: QuerySpill, reader: Any, held: list[Any]) -> Any:
        """Write *held* and the rest of *reader* to a file; the file as a table."""
        import pyarrow as pa
        from pyarrow import ipc

        self.spilled = True
        path = os.path.join(spill.directory, f"{self._leg.name}.arrows")
        try:
            with pa.OSFile(path, "wb") as sink, ipc.new_stream(sink, reader.schema) as writer:
                for batch in held:
                    self._write(spill, writer, batch)
                held.clear()
                for batch in reader:
                    self._write(spill, writer, batch)
        except OSError as exc:
            # The disk, not the database: most often a volume smaller than
            # the limits allow for.
            raise FederationSpillFullError(
                f"[FEDERATION_SPILL_FULL] {leg_label(self._leg)} could not be spilled to "
                f"FEDERATION_SPILL_DIR '{spill._limits.spill_dir}': {exc}. Give the "
                "directory more space, or lower FEDERATION_SPILL_BUDGET_MB to what it holds."
            ) from None
        # Left open: the table's buffers are the mapping. The file goes with
        # the query's directory; a mapped file outlives its name.
        return ipc.open_stream(pa.memory_map(path, "r")).read_all()

    def _write(self, spill: QuerySpill, writer: Any, batch: Any) -> None:
        spill.reserve(self._leg, batch.nbytes)
        writer.write_batch(batch)


def combine(
    plan: FederatedPlan,
    tables: dict[str, Any],
    tz: ZoneInfo | None,
    temp_directory: str | None = None,
) -> ExecutionResult:
    """Run the combine query over the legs' tables in a fresh in-memory DuckDB.

    ``temp_directory`` is where DuckDB spills its own join and sort state:
    the query's spill directory, when it has one, within
    ``FEDERATION_SPILL_MAX_MB``.

    Raises:
        FederatedLegError: A leg stands for too many rows (:func:`check_rows_stood_for`).
        ExecutionError: The combine query failed.
    """
    import duckdb

    for leg in plan.legs:
        if leg.rows is not None:
            check_rows_stood_for(leg, tables[leg.name])
    limits = federation_limits()
    t0 = time.monotonic()
    config: dict[str, Any] = {
        "threads": max(1, limits.combine_threads),
        "memory_limit": limits.combine_memory_limit,
    }
    if temp_directory is not None:
        config["temp_directory"] = temp_directory
        # DuckDB's own spill is bounded too, so the disk a query can take is
        # known: its legs within the budget, plus this.
        config["max_temp_directory_size"] = f"{limits.spill_max_bytes // _MB}MiB"
    con = duckdb.connect(config=config)
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
    "FederationSpillFullError",
    "LegSink",
    "QuerySpill",
    "admission_gate",
    "busy_error",
    "check_leg_bytes",
    "check_leg_rows",
    "check_rows_stood_for",
    "combine",
    "configure",
    "federation_limits",
    "injected_fact_leg",
    "key_injection_max_keys",
    "leg_executor",
    "hold_leg",
    "leg_label",
    "normalize_leg",
    "result_table",
    "spill_directory",
]
