"""Session management — TTL-scoped ModelStore instances for multi-client use."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from orionbelt.service.model_store import ModelStore

_DEFAULT_SESSION_ID = "__default__"


class SessionNotFoundError(KeyError):
    """Raised when a session ID is not found or has expired."""


@dataclass
class SessionInfo:
    """Public session metadata (returned by list/get)."""

    session_id: str
    created_at: datetime
    last_accessed_at: datetime
    model_count: int
    metadata: dict[str, str]


@dataclass
class _Session:
    """Internal session state."""

    session_id: str
    store: ModelStore
    created_at: datetime
    last_accessed: float  # monotonic clock for TTL checks
    metadata: dict[str, str] = field(default_factory=dict)
    # Wall-clock times for reporting
    created_at_wall: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_accessed_wall: datetime = field(default_factory=lambda: datetime.now(UTC))


class SessionManager:
    """Manages TTL-scoped sessions, each holding its own ``ModelStore``.

    Thread-safe.  Call :meth:`start` to begin the background cleanup thread
    and :meth:`stop` to shut it down.
    """

    def __init__(self, ttl_seconds: int = 1800, cleanup_interval: int = 60) -> None:
        self._ttl = ttl_seconds
        self._cleanup_interval = cleanup_interval
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._stop_event = threading.Event()
        self._cleanup_thread: threading.Thread | None = None

    @property
    def ttl(self) -> int:
        """Session TTL in seconds."""
        return self._ttl

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
        """Create a new session and return its info."""
        session_id = secrets.token_hex(16)  # 32-char hex (128-bit)
        now_mono = time.monotonic()
        now_wall = datetime.now(UTC)
        session = _Session(
            session_id=session_id,
            store=ModelStore(),
            created_at=now_wall,
            last_accessed=now_mono,
            metadata=metadata or {},
            created_at_wall=now_wall,
            last_accessed_wall=now_wall,
        )
        with self._lock:
            self._sessions[session_id] = session
        return self._session_info(session)

    def get_store(self, session_id: str) -> ModelStore:
        """Get the ModelStore for a session, updating its last-accessed time.

        Raises :class:`SessionNotFoundError` if the session is missing or expired.
        """
        now_mono = time.monotonic()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(f"Session '{session_id}' not found")
            # Lazy expiration check
            if now_mono - session.last_accessed > self._ttl:
                del self._sessions[session_id]
                raise SessionNotFoundError(f"Session '{session_id}' has expired")
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
            if now_mono - session.last_accessed > self._ttl:
                del self._sessions[session_id]
                raise SessionNotFoundError(f"Session '{session_id}' has expired")
            session.last_accessed = now_mono
            session.last_accessed_wall = datetime.now(UTC)
            return self._session_info(session)

    def close_session(self, session_id: str) -> None:
        """Explicitly close a session."""
        with self._lock:
            if session_id not in self._sessions:
                raise SessionNotFoundError(f"Session '{session_id}' not found")
            del self._sessions[session_id]

    def list_sessions(self) -> list[SessionInfo]:
        """Return info for all non-expired sessions (excluding default)."""
        now_mono = time.monotonic()
        result: list[SessionInfo] = []
        with self._lock:
            for session in self._sessions.values():
                if session.session_id == _DEFAULT_SESSION_ID:
                    continue
                if now_mono - session.last_accessed <= self._ttl:
                    result.append(self._session_info(session))
        return result

    @property
    def active_count(self) -> int:
        """Number of active (non-expired) sessions."""
        now_mono = time.monotonic()
        with self._lock:
            return sum(
                1 for s in self._sessions.values() if now_mono - s.last_accessed <= self._ttl
            )

    def get_or_create_default(self) -> ModelStore:
        """Get (or lazily create) the default session."""
        with self._lock:
            session = self._sessions.get(_DEFAULT_SESSION_ID)
            if session is not None:
                session.last_accessed = time.monotonic()
                session.last_accessed_wall = datetime.now(UTC)
                return session.store
            now_wall = datetime.now(UTC)
            session = _Session(
                session_id=_DEFAULT_SESSION_ID,
                store=ModelStore(),
                created_at=now_wall,
                last_accessed=time.monotonic(),
                created_at_wall=now_wall,
                last_accessed_wall=now_wall,
            )
            self._sessions[_DEFAULT_SESSION_ID] = session
            return session.store

    # -- internal ------------------------------------------------------------

    @staticmethod
    def _session_info(session: _Session) -> SessionInfo:
        return SessionInfo(
            session_id=session.session_id,
            created_at=session.created_at_wall,
            last_accessed_at=session.last_accessed_wall,
            model_count=len(session.store.list_models()),
            metadata=session.metadata,
        )

    def _purge_expired(self) -> None:
        """Remove all expired sessions (called by cleanup thread)."""
        now_mono = time.monotonic()
        with self._lock:
            expired = [
                sid
                for sid, s in self._sessions.items()
                if sid != _DEFAULT_SESSION_ID and now_mono - s.last_accessed > self._ttl
            ]
            for sid in expired:
                del self._sessions[sid]

    def _cleanup_loop(self) -> None:
        """Background loop that periodically purges expired sessions."""
        while not self._stop_event.wait(timeout=self._cleanup_interval):
            self._purge_expired()
