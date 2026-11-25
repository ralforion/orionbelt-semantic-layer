"""Static API-key store with constant-time validation.

The store pre-builds a frozenset at startup; ``validate()`` is O(number of
keys) but uses ``secrets.compare_digest`` per key so a wrong key takes the
same time regardless of how many characters match (no timing side channel).
See design/PLAN_authentication.md §4.

A key is named by its **id** (:func:`key_id`, eight hex characters of a hash)
in logs, errors and ``GET /v1/auth/status``: it says nothing about the key,
and an admin computes it from a key to find which one it is. The store
remembers when each key was last used, in this process only: keys live in the
environment, so there is nowhere to keep it.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time

# Reject keys shorter than this at startup (see §1 validation rules).
MIN_KEY_LENGTH = 16
# Below this length, or with too few distinct characters, a key is refused at
# startup as too weak: SCRAM transcripts can be attacked offline if captured,
# so low-entropy keys are a real risk. The recommended format is a 40-hex-char
# random token (~160 bits).
RECOMMENDED_KEY_LENGTH = 32
_MIN_DISTINCT_CHARS = 10


def parse_keys(raw: str) -> frozenset[str]:
    """Split a comma-separated ``API_KEYS`` value into a deduped set.

    Whitespace is stripped and empty entries dropped. Duplicate keys are
    silently deduplicated.
    """
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def key_id(key: str) -> str:
    """A key's name: eight hex characters of a SHA-256 of it, which reveal nothing of it."""
    return hashlib.sha256(b"obsl-key:" + key.encode("utf-8")).hexdigest()[:8]


def find_weak_keys(keys: frozenset[str]) -> list[str]:
    """Return the ids of any keys shorter than ``MIN_KEY_LENGTH``."""
    return sorted(key_id(k) for k in keys if len(k) < MIN_KEY_LENGTH)


def find_low_strength_keys(keys: frozenset[str]) -> list[str]:
    """Return the ids of keys that pass the hard floor but are weak.

    A key is flagged when it is shorter than ``RECOMMENDED_KEY_LENGTH`` or uses
    fewer than ``_MIN_DISTINCT_CHARS`` distinct characters (low entropy).
    ``init_auth`` refuses to start when any key is flagged.
    """
    return sorted(
        key_id(k)
        for k in keys
        if len(k) < RECOMMENDED_KEY_LENGTH or len(set(k)) < _MIN_DISTINCT_CHARS
    )


class KeyStore:
    """The configured API keys, each with its id and when this process last saw it used."""

    def __init__(self, keys: frozenset[str]) -> None:
        self._keys = keys
        self._ids = {key: key_id(key) for key in keys}
        self._last_used: dict[str, float] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> frozenset[str]:
        """The configured keys. Used by SCRAM, which must know the secrets to
        verify a client proof (same trust boundary — the process holds them)."""
        return self._keys

    def ids(self) -> list[str]:
        """The keys' ids, sorted."""
        return sorted(self._ids.values())

    def match(self, candidate: str) -> str | None:
        """The id of the key ``candidate`` is, recording its use; None (constant-time)."""
        if not candidate:
            return None
        candidate_bytes = candidate.encode("utf-8")
        # Iterate every key and compare_digest each one; never short-circuit
        # on a match so timing does not reveal which key (or how many) matched.
        matched = None
        for key, identifier in self._ids.items():
            if secrets.compare_digest(candidate_bytes, key.encode("utf-8")):
                matched = identifier
        if matched is not None:
            self.used(matched)
        return matched

    def validate(self, candidate: str) -> bool:
        """Return True when ``candidate`` matches a stored key (constant-time)."""
        return self.match(candidate) is not None

    def id_of(self, key: str) -> str | None:
        """The id of a configured key (``key`` itself, as SCRAM found it), or None."""
        return self._ids.get(key)

    def used(self, identifier: str) -> None:
        """Record that the key ``identifier`` authenticated a caller now."""
        with self._lock:
            self._last_used[identifier] = time.time()

    def last_used(self) -> dict[str, float | None]:
        """Each key's id, and when it last authenticated a caller in this process (unix time)."""
        with self._lock:
            return {identifier: self._last_used.get(identifier) for identifier in self.ids()}
