"""Bounded, in-memory cache of compilation results.

Compilation is a pure function of the query, the model and the dialect: it
reads no environment, clock or settings. A repeated query against the same
loaded model therefore compiles to the same :class:`CompilationResult`, and the
cache returns a copy of the first one instead of compiling again.

Keys:

* the **model instance**, not its id or name. A caller can only hit entries for
  a model it already holds, so the cache grants no access. Each model gets a
  token on first use; a weak reference retires the token, and every entry under
  it, when the model is garbage collected. A model shared by several sessions
  (``ModelCache``) stays alive, and its entries stay valid, until the last one
  releases it. Loaded models are immutable once resolved.
* the **dialect** name;
* the **query**, encoded from ``QueryObject.model_dump()`` with every scalar
  tagged by type (``1`` and ``True``, ``"2024-01-01"`` and ``date(2024, 1, 1)``
  and ``Decimal("1.0")`` and ``Decimal("1.00")`` all differ). Order is kept
  everywhere, mapping items included: the compiler renders a mapping filter
  value in insertion order, so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}``
  compile to different SQL and must not share an entry. A value of any other
  type bypasses the cache. The encoding is stored as its SHA-256 digest, so a
  key is 32 bytes whatever the query's size.

Results are deep-copied on the way in and on the way out, so a caller that
mutates what it got back (the CLI assigns formatted SQL to ``result.sql``)
cannot change a later hit. Failed compilations are not cached. The cache is
bounded by entry count and by estimated bytes; an entry larger than
``max_entry_bytes`` is not stored. Compilation runs outside the lock; two
threads missing on the same key may both compile, and the second result
replaces the first.

This cache holds SQL, never rows: result-cache eligibility and freshness are
decided on every execution, hit or miss.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import threading
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
    from orionbelt.models.query import QueryObject
    from orionbelt.models.semantic import SemanticModel


class _UnkeyableError(Exception):
    """A query value with no faithful, typed encoding."""


def _encode(value: Any) -> Any:
    """A hashable, type-tagged encoding of a dumped query value."""
    if value is None or isinstance(value, bool | int | float | str):
        return (type(value).__name__, value)
    if isinstance(value, Enum):
        return (type(value).__qualname__, value.value)
    if isinstance(value, Decimal):
        return ("Decimal", str(value))
    if isinstance(value, datetime | date | time):
        return (type(value).__name__, value.isoformat())
    if isinstance(value, timedelta):
        return ("timedelta", value.days, value.seconds, value.microseconds)
    if isinstance(value, list | tuple):
        return (type(value).__name__, tuple(_encode(v) for v in value))
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise _UnkeyableError
        # Insertion order, not sorted: see the module docstring.
        return ("dict", tuple((k, _encode(v)) for k, v in value.items()))
    raise _UnkeyableError


def query_key(query: QueryObject) -> bytes | None:
    """The cache key of *query*, or ``None`` when it cannot be keyed.

    The SHA-256 digest of the typed encoding: 32 bytes whatever the query's
    size, as exact as the encoding itself.
    """
    try:
        encoded = repr(_encode(query.model_dump(mode="python")))
    except _UnkeyableError:
        return None
    return hashlib.sha256(encoded.encode()).digest()


@dataclass(frozen=True)
class CompilationCacheStats:
    enabled: bool
    entries: int
    bytes: int
    max_entries: int
    max_bytes: int
    hits: int
    misses: int
    bypasses: int
    evictions: int


def _size(key: tuple[int, str, bytes], result: CompilationResult) -> int:
    """Rough bytes held by one entry: the key plus the result's repr."""
    return len(key[1]) + len(key[2]) + len(repr(result))


class CompilationCache:
    """Process-wide cache of compilation results, bounded by entries and bytes.

    ``max_entries=0`` disables it: :meth:`compile` then always compiles.
    """

    def __init__(
        self,
        max_entries: int = 0,
        max_bytes: int = 64 * 1024 * 1024,
        max_entry_bytes: int = 1024 * 1024,
    ) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._max_entry_bytes = max_entry_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[int, str, bytes], tuple[CompilationResult, int]] = (
            OrderedDict()
        )
        self._bytes = 0
        # id(model) -> (weak reference, token). The token, not the id, keys the
        # entries: an id can be reused once its model is gone.
        self._tokens: dict[int, tuple[weakref.ref[SemanticModel], int]] = {}
        self._next_token = itertools.count(1)
        # Tokens of collected models, drained under the lock. The weakref
        # callback may run during garbage collection inside a locked section,
        # so it only appends here.
        self._retired: deque[tuple[int, int]] = deque()
        self._hits = 0
        self._misses = 0
        self._bypasses = 0
        self._evictions = 0

    @property
    def enabled(self) -> bool:
        return self._max_entries > 0

    def compile(
        self,
        pipeline: CompilationPipeline,
        query: QueryObject,
        model: SemanticModel,
        dialect: str,
    ) -> CompilationResult:
        """Compile *query*, or return a copy of the cached result for it."""
        if not self.enabled:
            return pipeline.compile(query, model, dialect)
        qkey = query_key(query)
        if qkey is None:
            with self._lock:
                self._bypasses += 1
            return pipeline.compile(query, model, dialect)

        with self._lock:
            self._drain_retired()
            key = (self._token(model), dialect, qkey)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                self._hits += 1
                return copy.deepcopy(entry[0])
            self._misses += 1

        result = pipeline.compile(query, model, dialect)
        stored = copy.deepcopy(result)
        size = _size(key, stored)
        if size > self._max_entry_bytes:
            return result
        with self._lock:
            self._drain_retired()
            if key[0] != self._live_token(model):
                return result  # the model was retired while compiling
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            self._entries[key] = (stored, size)
            self._bytes += size
            self._evict_to_budget()
        return result

    def clear(self) -> int:
        """Drop every entry; counters are kept. Returns the number dropped."""
        with self._lock:
            dropped = len(self._entries)
            self._entries.clear()
            self._bytes = 0
            return dropped

    def stats(self) -> CompilationCacheStats:
        with self._lock:
            self._drain_retired()
            return CompilationCacheStats(
                enabled=self.enabled,
                entries=len(self._entries),
                bytes=self._bytes,
                max_entries=self._max_entries,
                max_bytes=self._max_bytes,
                hits=self._hits,
                misses=self._misses,
                bypasses=self._bypasses,
                evictions=self._evictions,
            )

    # -- internals; called with the lock held --------------------------------

    def _token(self, model: SemanticModel) -> int:
        live = self._live_token(model)
        if live is not None:
            return live
        token = next(self._next_token)
        model_id = id(model)
        retired = self._retired

        def _collected(_: weakref.ref[SemanticModel]) -> None:
            retired.append((model_id, token))

        self._tokens[model_id] = (weakref.ref(model, _collected), token)
        return token

    def _live_token(self, model: SemanticModel) -> int | None:
        entry = self._tokens.get(id(model))
        if entry is not None and entry[0]() is model:
            return entry[1]
        return None

    def _drain_retired(self) -> None:
        while self._retired:
            model_id, token = self._retired.popleft()
            current = self._tokens.get(model_id)
            if current is not None and current[1] == token:
                del self._tokens[model_id]
            for key in [k for k in self._entries if k[0] == token]:
                self._bytes -= self._entries.pop(key)[1]
                self._evictions += 1

    def _evict_to_budget(self) -> None:
        while self._entries and (
            len(self._entries) > self._max_entries or self._bytes > self._max_bytes
        ):
            _, (_, size) = self._entries.popitem(last=False)
            self._bytes -= size
            self._evictions += 1
