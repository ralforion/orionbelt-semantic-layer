"""Static API-key store with constant-time validation.

The store pre-builds a frozenset at startup; ``validate()`` is O(number of
keys) but uses ``secrets.compare_digest`` per key so a wrong key takes the
same time regardless of how many characters match (no timing side channel).
See design/PLAN_authentication.md §4.
"""

from __future__ import annotations

import secrets

# Reject keys shorter than this at startup (see §1 validation rules).
MIN_KEY_LENGTH = 16
# Below this length, or with too few distinct characters, a key is accepted but
# flagged as weak: SCRAM transcripts can be attacked offline if captured, so
# low-entropy keys are a real risk. The recommended format is a 40-hex-char
# random token (~160 bits).
RECOMMENDED_KEY_LENGTH = 32
_MIN_DISTINCT_CHARS = 10


def parse_keys(raw: str) -> frozenset[str]:
    """Split a comma-separated ``API_KEYS`` value into a deduped set.

    Whitespace is stripped and empty entries dropped. Duplicate keys are
    silently deduplicated.
    """
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def find_weak_keys(keys: frozenset[str]) -> list[str]:
    """Return masked prefixes of any keys shorter than ``MIN_KEY_LENGTH``.

    Only the first 4 characters are returned so error messages never leak a
    full key.
    """
    return [f"{k[:4]}..." for k in sorted(keys) if len(k) < MIN_KEY_LENGTH]


def find_low_strength_keys(keys: frozenset[str]) -> list[str]:
    """Return masked prefixes of keys that pass the hard floor but are weak.

    A key is flagged when it is shorter than ``RECOMMENDED_KEY_LENGTH`` or uses
    fewer than ``_MIN_DISTINCT_CHARS`` distinct characters (low entropy). These
    are accepted but warned about at startup.
    """
    return [
        f"{k[:4]}..."
        for k in sorted(keys)
        if len(k) < RECOMMENDED_KEY_LENGTH or len(set(k)) < _MIN_DISTINCT_CHARS
    ]


class KeyStore:
    """An immutable set of valid API keys."""

    def __init__(self, keys: frozenset[str]) -> None:
        self._keys = keys

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> frozenset[str]:
        """The configured keys. Used by SCRAM, which must know the secrets to
        verify a client proof (same trust boundary — the process holds them)."""
        return self._keys

    def validate(self, candidate: str) -> bool:
        """Return True when ``candidate`` matches a stored key (constant-time)."""
        if not candidate:
            return False
        candidate_bytes = candidate.encode("utf-8")
        # Iterate every key and compare_digest each one; never short-circuit
        # on a match so timing does not reveal which key (or how many) matched.
        matched = False
        for key in self._keys:
            if secrets.compare_digest(candidate_bytes, key.encode("utf-8")):
                matched = True
        return matched
