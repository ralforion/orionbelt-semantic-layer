"""Encrypting a secret OBSL must read back: AES-256-GCM under ``TOKEN_STORE_KEY``.

The BI token store keeps hashes of its own secrets, but an identity provider's
refresh token must be sent back to the provider, so it is stored encrypted.
Each key is 32 random bytes, given in base64; several may be configured, the
first encrypting and each one decrypting, so a key is rotated by putting the
new one first and dropping the old one once every value was written again. A
sealed value is bound to the row it belongs to (associated data), so it
cannot be moved to another row.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from orionbelt.auth.errors import AuthConfigError

KEY_BYTES = 32
_NONCE_BYTES = 12
_GENERATE = (
    'python3 -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"'
)


def key_id(key: bytes) -> str:
    """A key's name in the store: a hash of it, which says nothing about the key."""
    return hashlib.sha256(b"obsl-token-store-key:" + key).hexdigest()[:16]


def parse_keys(raw: str, *, setting: str = "TOKEN_STORE_KEY") -> tuple[bytes, ...]:
    """Base64 keys, separated by commas or whitespace; standard or URL-safe alphabet.

    Raises ``AuthConfigError`` naming the entry, never its value, when one is
    not 32 bytes of base64.
    """
    keys: list[bytes] = []
    for number, part in enumerate((p for p in re.split(r"[\s,]+", raw.strip()) if p), start=1):
        normalized = part.replace("-", "+").replace("_", "/")
        try:
            key = base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
        except (binascii.Error, ValueError):
            key = b""
        if len(key) != KEY_BYTES:
            raise AuthConfigError(
                f"{setting} entry {number} is not a base64-encoded 32-byte key. "
                f"Generate one with: {_GENERATE}"
            )
        keys.append(key)
    return tuple(keys)


class Sealer:
    """Encrypts with the first key, decrypts with any of them."""

    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys:
            raise ValueError("a Sealer needs at least one key")
        self._keys = {key_id(key): AESGCM(key) for key in keys}
        self._current = key_id(keys[0])

    def __repr__(self) -> str:
        return f"Sealer(keys={len(self._keys)})"

    @property
    def current_key(self) -> str:
        """The id of the key new values are sealed with."""
        return self._current

    def seal(self, plaintext: str, *, bound_to: str) -> tuple[bytes, str]:
        """``plaintext`` encrypted for the row ``bound_to``, and the id of the key used."""
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._keys[self._current].encrypt(nonce, plaintext.encode(), bound_to.encode())
        return nonce + sealed, self._current

    def open(self, sealed: bytes, key: str, *, bound_to: str) -> str | None:
        """The plaintext, or None: sealed under a key no longer configured, or tampered with."""
        aead = self._keys.get(key)
        if aead is None or len(sealed) <= _NONCE_BYTES:
            return None
        try:
            plaintext = aead.decrypt(
                sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:], bound_to.encode()
            )
        except InvalidTag:
            return None
        return plaintext.decode()
