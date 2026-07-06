"""Process-wide, content-addressed cache of compiled models.

Every session owns its own :class:`~orionbelt.service.model_store.ModelStore`,
so without this cache identical OBML content loaded into different sessions is
compiled independently (parse + resolve + join graph + health + OBSL-graph
export) and stamped with a fresh random ``model_id``. That is pure waste — the
classic case being admin-curated ``MODEL_FILES`` mode, where the same curated
model is re-compiled into every new user session.

``ModelCache`` holds one :class:`CompiledModel` per content hash and hands the
same immutable ``SemanticModel`` (and its derived artifacts) to every session
that loads matching bytes, keyed by a stable content-derived ``model_id``.
Entries are refcounted across sessions and evicted the instant the last
referencing store releases them, so the cache only ever holds models that at
least one live session is using.

Only plain dedup-eligible YAML loads are shared. Loads that fold in state the
YAML bytes don't capture (``raw_dict`` / ``extends`` / ``inherits`` /
``dedup=False``) stay private to their store and never touch this cache.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orionbelt.models.semantic import SemanticModel
    from orionbelt.service.model_store import GraphArtifact, ModelSummary


@dataclass
class CompiledModel:
    """One shared, refcounted compiled model keyed by its OBML content hash.

    ``model`` is immutable after resolution, so sharing a single instance
    across sessions is safe. ``raw`` is only ever read back through
    :meth:`ModelStore.get_raw`, which deep-copies before returning, so callers
    cannot mutate the shared dict.
    """

    model_id: str
    content_hash: str
    model: SemanticModel
    raw: dict[str, object]
    graph: GraphArtifact
    summary: ModelSummary
    refcount: int = 0


class ModelCache:
    """Thread-safe, refcounted, content-addressed store of compiled models.

    One instance lives per :class:`~orionbelt.service.session_manager.SessionManager`
    and is shared by every per-session ``ModelStore`` it creates.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_hash: dict[str, CompiledModel] = {}
        # model_id -> content_hash, so ``release`` can find the entry by id.
        self._by_id: dict[str, str] = {}

    def acquire(self, content_hash: str) -> CompiledModel | None:
        """Return the cached entry for ``content_hash`` (refcount += 1), or None.

        The caller is responsible for releasing the reference (via
        :meth:`ModelStore.remove_model` / :meth:`ModelStore.close`).
        """
        with self._lock:
            entry = self._by_hash.get(content_hash)
            if entry is not None:
                entry.refcount += 1
            return entry

    def insert_or_acquire(self, compiled: CompiledModel) -> CompiledModel:
        """Insert ``compiled`` at refcount 1, or adopt an existing entry.

        If a concurrent load already inserted the same content hash, that
        winner's refcount is incremented and it is returned (``compiled`` is
        discarded). Either way the returned entry carries exactly one new
        reference for the caller to hold.
        """
        with self._lock:
            existing = self._by_hash.get(compiled.content_hash)
            if existing is not None:
                existing.refcount += 1
                return existing
            compiled.refcount = 1
            self._by_hash[compiled.content_hash] = compiled
            self._by_id[compiled.model_id] = compiled.content_hash
            return compiled

    def release(self, model_id: str) -> None:
        """Drop one reference to a shared model; evict it at refcount 0.

        A no-op for unknown ids (private / already-evicted models).
        """
        with self._lock:
            content_hash = self._by_id.get(model_id)
            if content_hash is None:
                return
            entry = self._by_hash.get(content_hash)
            if entry is None:
                self._by_id.pop(model_id, None)
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                self._by_hash.pop(content_hash, None)
                self._by_id.pop(model_id, None)

    def stats(self) -> dict[str, int]:
        """Diagnostics: distinct shared entries and total live references."""
        with self._lock:
            return {
                "entries": len(self._by_hash),
                "refs": sum(e.refcount for e in self._by_hash.values()),
            }
