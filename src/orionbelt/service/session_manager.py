"""Session management — TTL-scoped ModelStore instances for multi-client use."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from orionbelt.service.model_store import ModelStore

logger = logging.getLogger("orionbelt.session")

_DEFAULT_SESSION_ID = "__default__"


class SessionNotFoundError(KeyError):
    """Raised when a session ID has never existed or was explicitly closed."""


class SessionExpiredError(KeyError):
    """Raised when a session existed but has expired (idle TTL or absolute max-age)."""


class SessionCapacityError(Exception):
    """Raised when the global session cap is reached."""


@dataclass
class SessionInfo:
    """Public session metadata (returned by list/get)."""

    session_id: str
    created_at: datetime
    last_accessed_at: datetime
    model_count: int
    metadata: dict[str, str]
    expires_at: datetime
    max_expires_at: datetime


@dataclass
class _Session:
    """Internal session state."""

    session_id: str
    store: ModelStore
    created_at: datetime
    created_at_mono: float  # monotonic clock for absolute max-age checks
    last_accessed: float  # monotonic clock for idle TTL checks
    metadata: dict[str, str] = field(default_factory=dict)
    # Wall-clock times for reporting
    created_at_wall: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_accessed_wall: datetime = field(default_factory=lambda: datetime.now(UTC))


class SessionManager:
    """Manages TTL-scoped sessions, each holding its own ``ModelStore``.

    Thread-safe.  Call :meth:`start` to begin the background cleanup thread
    and :meth:`stop` to shut it down.

    Parameters
    ----------
    ttl_seconds:
        Sliding idle timeout — sessions expire after this many seconds of
        inactivity.
    max_age_seconds:
        Absolute maximum session lifetime regardless of activity.
    max_sessions:
        Global cap on concurrent sessions.  ``create_session`` raises
        :class:`SessionCapacityError` when at capacity.
    max_models_per_session:
        Maximum models a single session may hold.  Passed through to each
        ``ModelStore`` instance.
    cleanup_interval:
        Seconds between background purge sweeps.
    is_single_model_mode:
        When True the ``__default__`` session is kept alive and excluded
        from purge.  When False (no ``MODEL_FILE``), the default session
        is treated like any other and subject to TTL/max-age expiry.
    """

    def __init__(
        self,
        ttl_seconds: int = 1800,
        max_age_seconds: int = 86400,
        max_sessions: int = 500,
        max_models_per_session: int = 10,
        cleanup_interval: int = 60,
        is_single_model_mode: bool = False,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_age = max_age_seconds
        self._max_sessions = max_sessions
        self._max_models = max_models_per_session
        self._cleanup_interval = cleanup_interval
        self._is_single_model_mode = is_single_model_mode
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._stop_event = threading.Event()
        self._cleanup_thread: threading.Thread | None = None

    @property
    def ttl(self) -> int:
        """Session TTL in seconds."""
        return self._ttl

    @property
    def max_age(self) -> int:
        """Absolute max session lifetime in seconds."""
        return self._max_age

    @property
    def max_sessions(self) -> int:
        """Global concurrent session cap."""
        return self._max_sessions

    @property
    def max_models_per_session(self) -> int:
        """Maximum models a single session may hold."""
        return self._max_models

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the background cleanup daemon thread."""
        if self._cleanup_thread is not None:
            return
        self._stop_event.clear()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="session-cleanup"
        )
        self._cleanup_thread.start()

    def stop(self) -> None:
        """Signal the cleanup thread to stop and wait for it."""
        self._stop_event.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=5)
            self._cleanup_thread = None

    # -- public API ----------------------------------------------------------

    def create_session(self, metadata: dict[str, str] | None = None) -> SessionInfo:
        """Create a new session and return its info.

        Raises :class:`SessionCapacityError` when the global session cap
        is reached.
        """
        now_mono = time.monotonic()
        now_wall = datetime.now(UTC)
        session_id = secrets.token_hex(16)  # 32-char hex (128-bit)
        session = _Session(
            session_id=session_id,
            store=ModelStore(max_models=self._max_models),
            created_at=now_wall,
            created_at_mono=now_mono,
            last_accessed=now_mono,
            metadata=metadata or {},
            created_at_wall=now_wall,
            last_accessed_wall=now_wall,
        )
        with self._lock:
            # Count only non-default, non-expired sessions toward the cap.
            active = sum(
                1
                for s in self._sessions.values()
                if s.session_id != _DEFAULT_SESSION_ID and not self._is_expired(s, now_mono)
            )
            if active >= self._max_sessions:
                logger.warning(
                    "Session cap reached (%d/%d), rejecting create",
                    active,
                    self._max_sessions,
                )
                raise SessionCapacityError(
                    f"Maximum number of concurrent sessions reached ({self._max_sessions})"
                )
            self._sessions[session_id] = session
        logger.info("Session created: %s", session_id)
        return self._session_info(session)

    def get_store(self, session_id: str) -> ModelStore:
        """Get the ModelStore for a session, updating its last-accessed time.

        Raises :class:`SessionExpiredError` if the session has expired.
        Raises :class:`SessionNotFoundError` if the session ID is unknown.
        """
        now_mono = time.monotonic()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(f"Session '{session_id}' not found")
            if self._is_expired(session, now_mono):
                reason = self._expiry_reason(session, now_mono)
                del self._sessions[session_id]
                logger.info("Session expired on access: %s (%s)", session_id, reason)
                raise SessionExpiredError(f"Session '{session_id}' has expired ({reason})")
            session.last_accessed = now_mono
            session.last_accessed_wall = datetime.now(UTC)
            return session.store

    def get_session(self, session_id: str) -> SessionInfo:
        """Get session info (also refreshes last-accessed)."""
        now_mono = time.monotonic()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(f"Session '{session_id}' not found")
            if self._is_expired(session, now_mono):
                reason = self._expiry_reason(session, now_mono)
                del self._sessions[session_id]
                logger.info("Session expired on access: %s (%s)", session_id, reason)
                raise SessionExpiredError(f"Session '{session_id}' has expired ({reason})")
            session.last_accessed = now_mono
            session.last_accessed_wall = datetime.now(UTC)
            return self._session_info(session)

    def close_session(self, session_id: str) -> None:
        """Explicitly close a session."""
        with self._lock:
            if session_id not in self._sessions:
                raise SessionNotFoundError(f"Session '{session_id}' not found")
            del self._sessions[session_id]
        logger.info("Session closed: %s", session_id)

    def list_sessions(self) -> list[SessionInfo]:
        """Return info for all non-expired sessions (excluding default)."""
        now_mono = time.monotonic()
        result: list[SessionInfo] = []
        with self._lock:
            for session in self._sessions.values():
                if session.session_id == _DEFAULT_SESSION_ID:
                    continue
                if not self._is_expired(session, now_mono):
                    result.append(self._session_info(session))
        return result

    @property
    def active_count(self) -> int:
        """Number of active (non-expired) sessions."""
        now_mono = time.monotonic()
        with self._lock:
            return sum(1 for s in self._sessions.values() if not self._is_expired(s, now_mono))

    def get_or_create_default(self) -> ModelStore:
        """Get (or lazily create) the default session."""
        with self._lock:
            session = self._sessions.get(_DEFAULT_SESSION_ID)
            if session is not None:
                session.last_accessed = time.monotonic()
                session.last_accessed_wall = datetime.now(UTC)
                return session.store
            now_mono = time.monotonic()
            now_wall = datetime.now(UTC)
            session = _Session(
                session_id=_DEFAULT_SESSION_ID,
                store=ModelStore(max_models=self._max_models),
                created_at=now_wall,
                created_at_mono=now_mono,
                last_accessed=now_mono,
                created_at_wall=now_wall,
                last_accessed_wall=now_wall,
            )
            self._sessions[_DEFAULT_SESSION_ID] = session
            return session.store

    # -- internal ------------------------------------------------------------

    def _is_expired(self, session: _Session, now_mono: float) -> bool:
        """Check if a session has exceeded idle TTL or absolute max-age."""
        idle = now_mono - session.last_accessed > self._ttl
        aged = now_mono - session.created_at_mono > self._max_age
        return idle or aged

    def _expiry_reason(self, session: _Session, now_mono: float) -> str:
        """Return a human-readable reason why a session expired."""
        idle_elapsed = now_mono - session.last_accessed
        age_elapsed = now_mono - session.created_at_mono
        if age_elapsed > self._max_age:
            return f"max-age {self._max_age}s exceeded after {age_elapsed:.0f}s"
        return f"idle {self._ttl}s exceeded after {idle_elapsed:.0f}s"

    def _session_info(self, session: _Session) -> SessionInfo:
        now_wall = datetime.now(UTC)
        idle_remaining = self._ttl - (time.monotonic() - session.last_accessed)
        age_remaining = self._max_age - (time.monotonic() - session.created_at_mono)

        # expires_at = when the idle TTL would fire (from last access)
        expires_at = now_wall + timedelta(seconds=max(0.0, idle_remaining))
        # max_expires_at = absolute deadline (from creation)
        max_expires_at = now_wall + timedelta(seconds=max(0.0, age_remaining))

        return SessionInfo(
            session_id=session.session_id,
            created_at=session.created_at_wall,
            last_accessed_at=session.last_accessed_wall,
            model_count=len(session.store.list_models()),
            metadata=session.metadata,
            expires_at=expires_at,
            max_expires_at=max_expires_at,
        )

    def _purge_expired(self) -> None:
        """Remove all expired sessions (called by cleanup thread)."""
        now_mono = time.monotonic()
        with self._lock:
            # In single-model mode, keep the default session alive.
            # Otherwise, purge it like any other session.
            skip_default = self._is_single_model_mode
            expired = [
                sid
                for sid, s in self._sessions.items()
                if (not skip_default or sid != _DEFAULT_SESSION_ID)
                and self._is_expired(s, now_mono)
            ]
            for sid in expired:
                reason = self._expiry_reason(self._sessions[sid], now_mono)
                del self._sessions[sid]
                logger.info("Session purged: %s (%s)", sid, reason)
        if expired:
            logger.info(
                "Purge sweep: removed %d session(s), %d remaining",
                len(expired),
                len(self._sessions),
            )

    def _cleanup_loop(self) -> None:
        """Background loop that periodically purges expired sessions."""
        while not self._stop_event.wait(timeout=self._cleanup_interval):
            self._purge_expired()
