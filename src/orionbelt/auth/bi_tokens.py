"""Tokens for BI tools: long-lived, revocable secrets a signed-in user creates.

BI tools (Tableau, Power BI, DBeaver, ODBC and JDBC drivers) hold a static
password and cannot sign in with an identity provider. A BI token, created by
a signed-in user for themselves, authenticates as that user with the scopes it
was given, until it expires or is revoked. Only under ``AUTH_MODE=oidc``: a
token belongs to a person. See design/PLAN_bi_tokens.md and
docs/guide/bi-tokens.md.

A token is opaque, ``obsl_tok_<id>_<secret>``: the id finds it, the secret
(160 random bits) proves it. The store keeps a SHA-256 of the secret, never
the token, and a SCRAM-SHA-256 verifier for the Postgres wire protocol, which
can only be computed while the token is known, at creation: a Postgres client
logs in with the token's id as its user name and the token as its password,
and SCRAM checks it against that verifier without the token crossing the
network.

A token may also hold a **warehouse sign-in**: the identity provider's
refresh token, which OBSL got by signing the user in once as its own client
when the token was created. A data source that logs in as the user
(``DB_<NAME>_AUTH=user``) needs the user's identity provider token, which a BI
token is not; OBSL redeems the refresh token for one. It is the one secret
the store must read back, so it is encrypted (``auth/sealing.py``), bound to
its row, and dropped when the token is revoked or expires.

The store is one SQLite file, opened on first use: a server that never
creates a token leaves no file behind.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orionbelt.auth.errors import TokenInvalidError
from orionbelt.auth.principal import Principal
from orionbelt.auth.sealing import Sealer

TOKEN_PREFIX = "obsl_tok_"
_TOKEN_RE = re.compile(r"^obsl_tok_([0-9a-f]{12})_([0-9a-f]{40})$")
#: A Postgres user name that names a token: its id, or ``tok_`` and its id.
_USER_RE = re.compile(r"^(?:tok_)?([0-9a-f]{12})$")
#: Derives an unknown id's decoy SCRAM salt; per process.
_DECOY_KEY = secrets.token_bytes(32)
_SECONDS_PER_DAY = 86400.0
#: SCRAM-SHA-256 iteration count of the stored verifier (RFC 7677's minimum).
_SCRAM_ITERATIONS = 4096
#: Failed attempts on one token id within the window, before it is refused.
_MAX_FAILURES = 10
_FAILURE_WINDOW_SECONDS = 60.0
#: Ids whose failures are counted at once; past it the oldest is forgotten.
_MAX_TRACKED_IDS = 10_000
#: ``last_used_at`` is written at most this often per token.
_LAST_USED_EVERY_SECONDS = 60.0
#: One message for an unknown id and a wrong secret: neither says which.
_INVALID = "BI token is not valid"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS bi_tokens (
        id TEXT PRIMARY KEY,
        secret_sha256 TEXT NOT NULL,
        owner_sub TEXT NOT NULL,
        owner_email TEXT,
        label TEXT NOT NULL,
        scopes TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_used_at REAL,
        revoked_at REAL,
        scram_salt BLOB NOT NULL,
        scram_iterations INTEGER NOT NULL,
        scram_stored_key BLOB NOT NULL,
        scram_server_key BLOB NOT NULL,
        idp_refresh_ct BLOB,
        idp_refresh_key TEXT,
        idp_refresh_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS bi_tokens_owner ON bi_tokens (owner_sub)",
)
#: Added after the first release of the store: a file written before gets them.
_SIGN_IN_COLUMNS = (
    ("idp_refresh_ct", "BLOB"),
    ("idp_refresh_key", "TEXT"),
    ("idp_refresh_at", "REAL"),
)
_COLUMNS = (
    "id, owner_sub, owner_email, label, scopes, created_at, expires_at, last_used_at, "
    "revoked_at, idp_refresh_ct IS NOT NULL"
)
_NO_SIGN_IN = "idp_refresh_ct = NULL, idp_refresh_key = NULL, idp_refresh_at = NULL"


def is_bi_token(credential: str) -> bool:
    """Whether ``credential`` has the shape of a BI token."""
    return bool(_TOKEN_RE.match(credential))


@dataclass(frozen=True)
class BiToken:
    """A token's record: everything but its secret."""

    id: str
    owner_sub: str
    owner_email: str | None
    label: str
    scopes: frozenset[str]
    created_at: float  # unix seconds
    expires_at: float
    last_used_at: float | None
    revoked_at: float | None
    #: Holds the identity provider's refresh token, for sources that log in as the user.
    warehouse_sign_in: bool = False


@dataclass(frozen=True)
class ScramVerifier:
    """What a SCRAM-SHA-256 login is checked against (RFC 5802); never the password."""

    salt: bytes
    iterations: int
    stored_key: bytes
    server_key: bytes


def token_id_of_user(user: str) -> str | None:
    """The token id a Postgres user name names (``3f2a9c41b7e0`` or ``tok_3f2a9c41b7e0``)."""
    match = _USER_RE.match(user)
    return match.group(1) if match else None


def _refuse_inactive(revoked_at: float | None, expires_at: float, now: float) -> None:
    if revoked_at is not None:
        raise TokenInvalidError("BI token was revoked")
    if expires_at <= now:
        raise TokenInvalidError("BI token expired")


def _record(row: tuple[Any, ...]) -> BiToken:
    token_id, owner_sub, owner_email, label, scopes, created, expires, used, revoked, sign_in = row
    return BiToken(
        id=str(token_id),
        owner_sub=str(owner_sub),
        owner_email=str(owner_email) if owner_email is not None else None,
        label=str(label),
        scopes=frozenset(str(scopes).split()),
        created_at=float(created),
        expires_at=float(expires),
        last_used_at=float(used) if used is not None else None,
        revoked_at=float(revoked) if revoked is not None else None,
        warehouse_sign_in=bool(sign_in),
    )


def _sha256(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def scram_keys(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    """SCRAM-SHA-256 ``StoredKey`` and ``ServerKey`` of ``password`` (RFC 5802)."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return hashlib.sha256(client_key).digest(), server_key


class TokenStore:
    """BI tokens in one SQLite file. Thread-safe; one OBSL process at a time.

    ``sealer`` encrypts warehouse sign-ins; without one a token holds none.
    ``on_sign_in_revoked`` is called with each refresh token a revocation
    drops, after the store committed it, to revoke it at the identity
    provider too.
    """

    def __init__(
        self,
        path: str,
        *,
        default_days: int = 90,
        max_days: int = 365,
        sealer: Sealer | None = None,
        on_sign_in_revoked: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.default_days = default_days
        self.max_days = max_days
        self.sealer = sealer
        self._on_sign_in_revoked = on_sign_in_revoked
        self._prepared = False
        self._prepare_lock = threading.Lock()
        self._lock = threading.Lock()
        # id -> (failures, window start), oldest first: bounded, never scanned.
        self._failures: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._last_used_written: dict[str, float] = {}

    # --- creating and managing ---------------------------------------------------

    def create(
        self,
        *,
        owner_sub: str,
        owner_email: str | None,
        label: str,
        scopes: frozenset[str],
        days: int,
        refresh_token: str | None = None,
    ) -> tuple[str, BiToken]:
        """A new token for ``owner_sub``: the token (shown once) and its record.

        ``refresh_token`` is the warehouse sign-in the token holds, if any.
        """
        if not 1 <= days <= self.max_days:
            raise ValueError(f"days must be between 1 and {self.max_days}")
        if refresh_token is not None and self.sealer is None:
            raise ValueError("a warehouse sign-in needs TOKEN_STORE_KEY")
        now = time.time()
        with self._connection(create=True) as conn:
            if conn is None:  # create=True always opens the file
                raise RuntimeError(f"cannot open the BI token store {self.path}")
            for _ in range(5):  # an id is 48 random bits: a clash is rare
                token_id, secret = secrets.token_hex(6), secrets.token_hex(20)
                token = f"{TOKEN_PREFIX}{token_id}_{secret}"
                salt = secrets.token_bytes(16)
                stored_key, server_key = scram_keys(token, salt, _SCRAM_ITERATIONS)
                record = BiToken(
                    id=token_id,
                    owner_sub=owner_sub,
                    owner_email=owner_email,
                    label=label,
                    scopes=scopes,
                    created_at=now,
                    expires_at=now + days * _SECONDS_PER_DAY,
                    last_used_at=None,
                    revoked_at=None,
                    warehouse_sign_in=refresh_token is not None,
                )
                sealed, key = self._seal(refresh_token, token_id)
                try:
                    conn.execute(
                        "INSERT INTO bi_tokens (id, secret_sha256, owner_sub, owner_email, "
                        "label, scopes, created_at, expires_at, scram_salt, scram_iterations, "
                        "scram_stored_key, scram_server_key, idp_refresh_ct, idp_refresh_key, "
                        "idp_refresh_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            token_id,
                            _sha256(secret),
                            owner_sub,
                            owner_email,
                            label,
                            " ".join(sorted(scopes)),
                            record.created_at,
                            record.expires_at,
                            salt,
                            _SCRAM_ITERATIONS,
                            stored_key,
                            server_key,
                            sealed,
                            key,
                            now if sealed is not None else None,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                return token, record
        raise RuntimeError("could not create a unique BI token id")

    def list_tokens(self, owner_sub: str | None) -> list[BiToken]:
        """The tokens not revoked, newest first: of ``owner_sub``, or everyone's (None)."""
        with self._connection() as conn:
            if conn is None:
                return []
            where = "revoked_at IS NULL"
            params: tuple[str, ...] = ()
            if owner_sub is not None:
                where += " AND owner_sub = ?"
                params = (owner_sub,)
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM bi_tokens WHERE {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [_record(row) for row in rows]

    def revoke(self, token_id: str, *, owner_sub: str | None) -> bool:
        """Revoke a token, of ``owner_sub`` only unless None (an admin). True if one was."""
        query = "UPDATE bi_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"
        params: tuple[object, ...] = (time.time(), token_id)
        if owner_sub is not None:
            query += " AND owner_sub = ?"
            params += (owner_sub,)
        with self._connection() as conn:
            if conn is None:
                return False
            revoked = conn.execute(query, params).rowcount == 1
            dropped = self._drop_sign_ins(conn, "id = ?", (token_id,)) if revoked else []
        self._sign_ins_revoked(dropped)
        return revoked

    def revoke_owner(self, owner_sub: str) -> int:
        """Revoke every token of ``owner_sub`` (offboarding); how many were."""
        with self._connection() as conn:
            if conn is None:
                return 0
            revoked = conn.execute(
                "UPDATE bi_tokens SET revoked_at = ? WHERE owner_sub = ? AND revoked_at IS NULL",
                (time.time(), owner_sub),
            ).rowcount
            dropped = self._drop_sign_ins(
                conn, "owner_sub = ? AND revoked_at IS NOT NULL", (owner_sub,)
            )
        self._sign_ins_revoked(dropped)
        return revoked

    # --- warehouse sign-ins --------------------------------------------------------

    def refresh_token(self, token_id: str) -> str | None:
        """The identity provider's refresh token the token holds, or None.

        None also for a revoked or expired token, whose sign-in is dropped
        here, and for one sealed under a key no longer configured.
        """
        if self.sealer is None:
            return None
        with self._connection() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT idp_refresh_ct, idp_refresh_key, revoked_at, expires_at "
                "FROM bi_tokens WHERE id = ? AND idp_refresh_ct IS NOT NULL",
                (token_id,),
            ).fetchone()
            if row is None:
                return None
            sealed, key, revoked_at, expires_at = row
            if revoked_at is not None or float(expires_at) <= time.time():
                conn.execute(f"UPDATE bi_tokens SET {_NO_SIGN_IN} WHERE id = ?", (token_id,))
                return None
        return self.sealer.open(bytes(sealed), str(key), bound_to=token_id)

    def replace_refresh_token(self, token_id: str, refresh_token: str) -> None:
        """Keep the refresh token the identity provider issued in place of the last one.

        A sign-in dropped meanwhile (the token revoked) stays dropped.
        """
        sealed, key = self._seal(refresh_token, token_id)
        with self._connection() as conn:
            if conn is not None:
                conn.execute(
                    "UPDATE bi_tokens SET idp_refresh_ct = ?, idp_refresh_key = ?, "
                    "idp_refresh_at = ? WHERE id = ? AND idp_refresh_ct IS NOT NULL "
                    "AND revoked_at IS NULL",
                    (sealed, key, time.time(), token_id),
                )

    def drop_refresh_token(self, token_id: str) -> None:
        """Forget the token's sign-in: the identity provider no longer honours it."""
        with self._connection() as conn:
            if conn is not None:
                conn.execute(f"UPDATE bi_tokens SET {_NO_SIGN_IN} WHERE id = ?", (token_id,))

    def has_sign_in(self, token_id: str) -> bool:
        """Whether the token holds a warehouse sign-in, as far as the store knows."""
        with self._connection() as conn:
            return (
                conn is not None
                and conn.execute(
                    "SELECT 1 FROM bi_tokens WHERE id = ? AND idp_refresh_ct IS NOT NULL "
                    "AND revoked_at IS NULL",
                    (token_id,),
                ).fetchone()
                is not None
            )

    def sweep(self) -> int:
        """Drop the sign-ins of revoked and expired tokens; how many were (at startup).

        Not revoked at the identity provider: nobody holds them any more, and
        it ends them by itself once they go unused.
        """
        with self._connection() as conn:
            if conn is None:
                return 0
            return conn.execute(
                f"UPDATE bi_tokens SET {_NO_SIGN_IN} WHERE idp_refresh_ct IS NOT NULL "
                "AND (revoked_at IS NOT NULL OR expires_at <= ?)",
                (time.time(),),
            ).rowcount

    def _seal(self, refresh_token: str | None, token_id: str) -> tuple[bytes | None, str | None]:
        if refresh_token is None:
            return None, None
        if self.sealer is None:
            raise ValueError("a warehouse sign-in needs TOKEN_STORE_KEY")
        return self.sealer.seal(refresh_token, bound_to=token_id)

    def _drop_sign_ins(
        self, conn: sqlite3.Connection, where: str, params: Sequence[object]
    ) -> list[str]:
        """Drop the sign-ins of the rows ``where`` selects; their refresh tokens, readable."""
        rows = conn.execute(
            f"SELECT id, idp_refresh_ct, idp_refresh_key FROM bi_tokens "
            f"WHERE idp_refresh_ct IS NOT NULL AND {where}",
            tuple(params),
        ).fetchall()
        conn.execute(
            f"UPDATE bi_tokens SET {_NO_SIGN_IN} WHERE idp_refresh_ct IS NOT NULL AND {where}",
            tuple(params),
        )
        if self.sealer is None:
            return []
        opened = (
            self.sealer.open(bytes(sealed), str(key), bound_to=str(token_id))
            for token_id, sealed, key in rows
        )
        return [token for token in opened if token is not None]

    def _sign_ins_revoked(self, refresh_tokens: list[str]) -> None:
        if self._on_sign_in_revoked is None:
            return
        for refresh_token in refresh_tokens:
            self._on_sign_in_revoked(refresh_token)

    # --- authenticating ------------------------------------------------------------

    def authenticate(self, token: str) -> Principal:
        """The token's owner as the caller, with the token's scopes.

        Raises:
            TokenInvalidError: Unknown, wrong, revoked or expired; or its id
                had too many failed attempts in the last minute.
        """
        match = _TOKEN_RE.match(token)
        if match is None:
            raise TokenInvalidError(_INVALID)
        token_id, secret = match.groups()
        if self._blocked(token_id):
            raise TokenInvalidError(_INVALID)
        with self._connection() as conn:
            row = (
                conn.execute(
                    f"SELECT secret_sha256, {_COLUMNS} FROM bi_tokens WHERE id = ?",
                    (token_id,),
                ).fetchone()
                if conn is not None
                else None
            )
        if row is None or not hmac.compare_digest(str(row[0]), _sha256(secret)):
            self._failed(token_id)
            raise TokenInvalidError(_INVALID)
        return self._signed_in(_record(row[1:]))

    def scram_verifier(self, token_id: str) -> ScramVerifier:
        """The verifier a Postgres SCRAM login with this token's id is checked against.

        An unknown id gets a decoy: the same salt on every attempt, as a real
        id has, and keys no password matches, so a client cannot tell an
        unknown id from a wrong password.
        """
        with self._connection() as conn:
            row = (
                conn.execute(
                    "SELECT scram_salt, scram_iterations, scram_stored_key, scram_server_key "
                    "FROM bi_tokens WHERE id = ?",
                    (token_id,),
                ).fetchone()
                if conn is not None
                else None
            )
        if row is None:
            salt = hmac.new(_DECOY_KEY, token_id.encode(), hashlib.sha256).digest()[:16]
            return ScramVerifier(
                salt, _SCRAM_ITERATIONS, secrets.token_bytes(32), secrets.token_bytes(32)
            )
        salt, iterations, stored_key, server_key = row
        return ScramVerifier(bytes(salt), int(iterations), bytes(stored_key), bytes(server_key))

    def scram_login(self, token_id: str, *, proven: bool) -> Principal:
        """The token's owner as the caller, once a SCRAM login with its id ``proven`` it.

        Raises:
            TokenInvalidError: The proof failed (it counts towards the id's
                lock), the id is locked, or the token is revoked or expired.
        """
        if not proven:
            self._failed(token_id)
            raise TokenInvalidError(_INVALID)
        if self._blocked(token_id):
            raise TokenInvalidError(_INVALID)
        with self._connection() as conn:
            row = (
                conn.execute(
                    f"SELECT {_COLUMNS} FROM bi_tokens WHERE id = ?", (token_id,)
                ).fetchone()
                if conn is not None
                else None
            )
        if row is None:
            raise TokenInvalidError(_INVALID)
        return self._signed_in(_record(row))

    def _signed_in(self, record: BiToken) -> Principal:
        """The owner of a token that proved itself, unless it is revoked or expired."""
        now = time.time()
        _refuse_inactive(record.revoked_at, record.expires_at, now)
        self._touch(record.id, now)
        return Principal(
            kind="user",
            sub=record.owner_sub,
            email=record.owner_email,
            scopes=record.scopes,
            token_id=record.id,
        )

    def check_active(self, token_id: str) -> None:
        """Raise unless the token ``token_id`` is still valid: not revoked, not expired.

        For a connection that proved the token once and keeps running (the
        Postgres protocol): checked again before each query, so a revoked or
        expired token stops it.

        Raises:
            TokenInvalidError: Unknown, revoked or expired.
        """
        with self._connection() as conn:
            row = (
                conn.execute(
                    "SELECT revoked_at, expires_at FROM bi_tokens WHERE id = ?", (token_id,)
                ).fetchone()
                if conn is not None
                else None
            )
        if row is None:
            raise TokenInvalidError(_INVALID)
        revoked_at, expires_at = row
        _refuse_inactive(revoked_at, float(expires_at), time.time())

    def _blocked(self, token_id: str) -> bool:
        with self._lock:
            entry = self._failures.get(token_id)
            if entry is None:
                return False
            count, started = entry
            if time.monotonic() - started >= _FAILURE_WINDOW_SECONDS:
                del self._failures[token_id]
                return False
            return count >= _MAX_FAILURES

    def _failed(self, token_id: str) -> None:
        """Count a failure; at most ``_MAX_TRACKED_IDS`` ids, the oldest forgotten first.

        A flood of made-up ids can push out a real one's count, but not make
        guessing a 160-bit secret any likelier; what the bound keeps in check
        is memory and the time each failure takes.
        """
        now = time.monotonic()
        with self._lock:
            count, started = self._failures.pop(token_id, (0, now))
            if now - started >= _FAILURE_WINDOW_SECONDS:
                count, started = 0, now
            self._failures[token_id] = (count + 1, started)
            while len(self._failures) > _MAX_TRACKED_IDS:
                self._failures.popitem(last=False)

    def _touch(self, token_id: str, now: float) -> None:
        """Record the token as used, at most once a minute."""
        with self._lock:
            last = self._last_used_written.get(token_id)
            if last is not None and now - last < _LAST_USED_EVERY_SECONDS:
                return
            self._last_used_written[token_id] = now
        with self._connection() as conn:
            if conn is not None:
                conn.execute("UPDATE bi_tokens SET last_used_at = ? WHERE id = ?", (now, token_id))

    @contextlib.contextmanager
    def _connection(self, *, create: bool = False) -> Iterator[sqlite3.Connection | None]:
        """A connection to the store, committed on success; None when there is no file yet."""
        new = not self.path.exists()
        if new and not create:
            yield None
            return
        if new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        try:
            if new:
                os.chmod(self.path, 0o600)  # hashes only, but nobody else's business
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in _SCHEMA:
                conn.execute(statement)
            self._prepare(conn)
            with conn:
                yield conn
        finally:
            conn.close()

    def _prepare(self, conn: sqlite3.Connection) -> None:
        """Add the columns a file written by an earlier release lacks, once per store."""
        if self._prepared:
            return
        with self._prepare_lock:
            if self._prepared:
                return
            present = {row[1] for row in conn.execute("PRAGMA table_info(bi_tokens)")}
            for name, kind in _SIGN_IN_COLUMNS:
                if name not in present:
                    conn.execute(f"ALTER TABLE bi_tokens ADD COLUMN {name} {kind}")
            conn.commit()
            self._prepared = True
