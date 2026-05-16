"""Unit tests for pgwire/router.py — SemanticRouter orchestration."""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest

from orionbelt.pgwire.router import SemanticRouter
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult
from orionbelt.service.session_manager import SessionManager
from tests.conftest import SAMPLE_MODEL_YAML

# ---------------------------------------------------------------------------
# Helpers — parse the wire bytes we get back so assertions can be readable.
# ---------------------------------------------------------------------------


def _parse_frames(blob: bytes) -> list[tuple[bytes, bytes]]:
    """Decompose a router reply into (tag, body) frames."""

    frames: list[tuple[bytes, bytes]] = []
    offset = 0
    while offset < len(blob):
        tag = blob[offset : offset + 1]
        (length,) = struct.unpack("!I", blob[offset + 1 : offset + 5])
        body = blob[offset + 5 : offset + 1 + length]
        frames.append((tag, body))
        offset += 1 + length
    return frames


def _make_manager_with_model() -> tuple[SessionManager, str]:
    """Single-session manager holding the SAMPLE_MODEL_YAML model.

    Returns ``(manager, model_id)``. Caller is responsible for stop().
    """

    mgr = SessionManager()
    store = mgr.get_or_create_named("commerce")
    result = store.load_model(SAMPLE_MODEL_YAML)
    return mgr, result.model_id


# ---------------------------------------------------------------------------
# Canned responses
# ---------------------------------------------------------------------------


def test_select_one_does_not_need_a_model() -> None:
    mgr = SessionManager()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    reply = asyncio.run(router.handle("SELECT 1", database=""))
    frames = _parse_frames(reply)
    tags = [t for t, _ in frames]
    assert tags == [b"T", b"D", b"C"]


def test_blank_query_returns_command_complete_only() -> None:
    mgr = SessionManager()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    reply = asyncio.run(router.handle("   ;  ", database=""))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"C"]


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def test_resolve_target_finds_named_session() -> None:
    mgr, _model_id = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    target = router._resolve_target("commerce")  # noqa: SLF001 — unit test reaches into private API
    assert target.model_id


def test_resolve_target_falls_back_to_default_session() -> None:
    mgr = SessionManager()
    default_store = mgr.get_or_create_default()
    result = default_store.load_model(SAMPLE_MODEL_YAML)
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    target = router._resolve_target("unknown-name")  # noqa: SLF001
    assert target.model_id == result.model_id


def test_resolve_target_raises_when_no_model_loaded() -> None:
    mgr = SessionManager()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    reply = asyncio.run(router.handle('SELECT "Customer Country" FROM m', database=""))
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"E"]
    # ErrorResponse body carries fields; SQLSTATE = undefined_database.
    assert b"C3D000\x00" in frames[0][1]


# ---------------------------------------------------------------------------
# Translate → compile → execute pipeline (execute_sql mocked)
# ---------------------------------------------------------------------------


def _stub_execute_two_rows() -> ExecutionResult:
    """Mimic what execute_sql would return for a 'country, revenue' query."""

    return ExecutionResult(
        columns=[
            ColumnMeta(name="Customer Country", type_hint="string"),
            ColumnMeta(name="Total Revenue", type_hint="number"),
        ],
        raw_rows=[["DE", 1234.5], ["US", 9876]],
        row_count=2,
    )


def test_semantic_query_round_trips_to_data_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")

    def fake_execute(sql: str, **_: Any) -> ExecutionResult:
        assert "SELECT" in sql.upper()
        return _stub_execute_two_rows()

    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", fake_execute)

    reply = asyncio.run(
        router.handle(
            'SELECT "Customer Country", "Total Revenue" FROM commerce',
            database="commerce",
        )
    )
    frames = _parse_frames(reply)
    tags = [t for t, _ in frames]
    assert tags == [b"T", b"D", b"D", b"C"]

    # RowDescription — two columns with the expected names.
    _, desc = frames[0]
    (n_cols,) = struct.unpack("!H", desc[:2])
    assert n_cols == 2

    # DataRow values — two columns each.
    for tag, body in frames[1:3]:
        assert tag == b"D"
        (n_vals,) = struct.unpack("!H", body[:2])
        assert n_vals == 2

    # CommandComplete carries the row count.
    _, cmd = frames[3]
    assert cmd.startswith(b"SELECT 2")


def test_translator_error_surfaces_as_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")

    # Reference a column the model doesn't expose — the translator will
    # reject it before we ever call execute_sql.
    def fake_execute(*_args: Any, **_kwargs: Any) -> ExecutionResult:
        raise AssertionError("execute_sql must not be called on translation failure")

    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", fake_execute)

    reply = asyncio.run(
        router.handle('SELECT "Nonexistent Column" FROM commerce', database="commerce")
    )
    frames = _parse_frames(reply)
    assert [t for t, _ in frames] == [b"E"]


def test_execution_unavailable_surfaces_as_57p03(monkeypatch: pytest.MonkeyPatch) -> None:
    from orionbelt.service.db_executor import ExecutionUnavailableError

    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")

    def fake_execute(*_args: Any, **_kwargs: Any) -> ExecutionResult:
        raise ExecutionUnavailableError("driver missing")

    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", fake_execute)

    reply = asyncio.run(
        router.handle('SELECT "Customer Country" FROM commerce', database="commerce")
    )
    frames = _parse_frames(reply)
    assert frames[0][0] == b"E"
    assert b"C57P03\x00" in frames[0][1]


def test_execution_error_surfaces_as_22000(monkeypatch: pytest.MonkeyPatch) -> None:
    from orionbelt.service.db_executor import ExecutionError

    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")

    def fake_execute(*_args: Any, **_kwargs: Any) -> ExecutionResult:
        raise ExecutionError("syntax error at the end of input")

    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", fake_execute)

    reply = asyncio.run(
        router.handle('SELECT "Customer Country" FROM commerce', database="commerce")
    )
    frames = _parse_frames(reply)
    assert frames[0][0] == b"E"
    assert b"C22000\x00" in frames[0][1]


def test_uses_default_dialect_when_compiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default_dialect set at router construction is passed through."""

    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="postgres")
    seen: dict[str, Any] = {}

    def fake_execute(sql: str, *, dialect: str, **_: Any) -> ExecutionResult:
        seen["dialect"] = dialect
        return _stub_execute_two_rows()

    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", fake_execute)
    asyncio.run(router.handle('SELECT "Customer Country" FROM commerce', database="commerce"))
    assert seen["dialect"] == "postgres"
