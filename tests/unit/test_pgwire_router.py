"""Unit tests for pgwire/router.py — SemanticRouter orchestration."""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest

from orionbelt.pgwire.router import (
    SemanticRouter,
    _flatten_federation_subquery,
    _normalize_for_obsql,
    _rewrite_fetch_to_limit,
    _strip_collate_annotations,
    _unwrap_model_qualifier,
)
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


def test_execution_result_from_cache_envelope() -> None:
    """A cached envelope rebuilds into an ExecutionResult for wire encoding."""
    from types import SimpleNamespace

    from orionbelt.api.query_cache import _obml_type_to_hint, execution_result_from_envelope

    # Stored OBML/SQL column types map back to coarse pgwire hints.
    assert _obml_type_to_hint("decimal(18, 2)") == "number"
    assert _obml_type_to_hint("bigint") == "number"
    assert _obml_type_to_hint("timestamp") == "datetime"
    assert _obml_type_to_hint("string") == "string"

    envelope = SimpleNamespace(
        columns=[
            {"name": "Country", "type": "string", "format": None},
            {"name": "Total Sales", "type": "decimal(18, 2)", "format": "#,##0.00"},
        ],
        rows=[["DE", 1234.5], ["US", 9876.0]],
        row_count=2,
        execution_time_ms=0.0,
    )
    result = execution_result_from_envelope(envelope)
    assert [c.name for c in result.columns] == ["Country", "Total Sales"]
    assert [c.type_hint for c in result.columns] == ["string", "number"]
    assert result.columns[1].default_format == "#,##0.00"
    assert result.rows == [["DE", 1234.5], ["US", 9876.0]]
    assert result.row_count == 2


def test_loaded_model_names_excludes_transient_sessions_in_single_model_mode() -> None:
    """In admin-curated mode the router must not treat scratch sessions as
    catalog schemas — the catalog (CatalogEmulator) hides them, so routing a
    `FROM <scratch>.measures` reference to the catalog would 'table not found'.
    """
    from orionbelt.pgwire.catalog import CatalogEmulator

    # Curated mode: a protected model + a transient scratch session.
    mgr = SessionManager(is_single_model_mode=True)
    mgr.get_or_create_named("commerce").load_model(SAMPLE_MODEL_YAML)
    scratch = mgr.create_session()
    mgr.get_store(scratch.session_id).load_model(SAMPLE_MODEL_YAML)
    router = SemanticRouter(
        session_manager=mgr, default_dialect="duckdb", catalog=CatalogEmulator()
    )
    names = router._loaded_model_names()
    assert "commerce" in names
    assert scratch.session_id.lower() not in names
    # The router agrees with the catalog: a scratch-session schema ref is NOT
    # routed to the catalog in curated mode.
    assert router._references_model_schema(f"SELECT * FROM {scratch.session_id}.measures") is False

    # Dynamic mode still surfaces user sessions as catalog schemas.
    dyn = SessionManager(is_single_model_mode=False)
    s = dyn.create_session()
    dyn.get_store(s.session_id).load_model(SAMPLE_MODEL_YAML)
    dyn_router = SemanticRouter(
        session_manager=dyn, default_dialect="duckdb", catalog=CatalogEmulator()
    )
    assert s.session_id.lower() in dyn_router._loaded_model_names()


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


def test_handle_honours_bind_result_formats_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind.result_formats=[0, 1] → col 1 sent as 8-byte binary FLOAT8.

    pgjdbc uses Bind.result_formats to decode each column. If we send
    text when binary was requested, pgjdbc throws
    ``ArrayIndexOutOfBoundsException`` — see ``_encode_result``.
    """
    import struct as _struct

    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    monkeypatch.setattr(
        "orionbelt.pgwire.router.execute_sql",
        lambda sql, **_: _stub_execute_two_rows(),
    )

    reply = asyncio.run(
        router.handle(
            'SELECT "Customer Country", "Total Revenue" FROM commerce',
            database="commerce",
            result_formats=(0, 1),
        )
    )
    frames = _parse_frames(reply)
    _, desc = frames[0]
    offset = 2
    formats: list[int] = []
    for _ in range(2):
        end = desc.index(b"\x00", offset)
        name_len = end - offset + 1
        fmt_offset = offset + name_len + 16
        (fmt_code,) = _struct.unpack("!h", desc[fmt_offset : fmt_offset + 2])
        formats.append(fmt_code)
        offset = fmt_offset + 2
    assert formats == [0, 1]
    # First DataRow: col 1 must be exactly 8 bytes (binary FLOAT8).
    _, body = frames[1]
    n_offset = 2  # past n_vals
    (val0_len,) = _struct.unpack("!i", body[n_offset : n_offset + 4])
    val0_end = n_offset + 4 + val0_len
    (val1_len,) = _struct.unpack("!i", body[val0_end : val0_end + 4])
    assert val1_len == 8


def test_handle_preserves_tableau_user_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tableau wraps measures as ``SUM(... ) AS "sum:Total Revenue:ok"``.

    The semantic translator strips the wrap (correctly — the measure
    is already aggregated) but loses the user alias. The compiler then
    emits ``AS "Total Revenue"`` and Tableau, looking up its alias by
    name in the ResultSet, sees no matching column and renders NULL.
    The router re-parses the user SQL and rewrites the result column
    names back to the user's aliases.
    """
    import struct as _struct

    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    monkeypatch.setattr(
        "orionbelt.pgwire.router.execute_sql",
        lambda sql, **_: _stub_execute_two_rows(),
    )

    user_sql = (
        'SELECT CAST("Customer Country" AS TEXT) AS "Customer Country", '
        'SUM("Total Revenue") AS "sum:Total Revenue:ok" '
        "FROM commerce GROUP BY 1"
    )
    reply = asyncio.run(router.handle(user_sql, database="commerce"))
    frames = _parse_frames(reply)
    _, desc = frames[0]
    # Pull out the two column names from the RowDescription body.
    names: list[str] = []
    offset = 2  # past n_cols
    for _ in range(2):
        end = desc.index(b"\x00", offset)
        names.append(desc[offset:end].decode("utf-8"))
        offset = end + 1 + 18  # name + NUL + 18-byte per-col header
    assert names == ["Customer Country", "sum:Total Revenue:ok"]
    # Sanity-check there's a DataRow with two values.
    assert frames[1][0] == b"D"
    (n_vals,) = _struct.unpack("!H", frames[1][1][:2])
    assert n_vals == 2


def test_alias_rewrite_matches_by_inner_column_name_not_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alias rewrite must match by inner column name, not SELECT position.

    The CFL planner groups measures by their source fact object so the
    compiled column order can differ from the user's SELECT order.
    Renaming by position would put Tableau's alias for ``Total Sales``
    onto the column that actually contains ``Total Returns`` data —
    surfacing as the swapped-column bug Tableau showed in v2.5.0
    pre-release testing. Match by name so the rename is
    order-independent.
    """

    # User SELECT order: Dim, Revenue, Order Count.
    # Compiler returns: Dim, Order Count, Revenue (simulates CFL re-ordering).
    def fake_execute(_sql: str, **_: Any) -> ExecutionResult:
        return ExecutionResult(
            columns=[
                ColumnMeta(name="Customer Country", type_hint="string"),
                ColumnMeta(name="Order Count", type_hint="number"),
                ColumnMeta(name="Total Revenue", type_hint="number"),
            ],
            raw_rows=[["DE", 10, 100], ["US", 20, 200]],
            row_count=2,
        )

    mgr, _ = _make_manager_with_model()
    router = SemanticRouter(session_manager=mgr, default_dialect="duckdb")
    monkeypatch.setattr("orionbelt.pgwire.router.execute_sql", fake_execute)

    user_sql = (
        'SELECT CAST("Customer Country" AS TEXT) AS "Customer Country", '
        'SUM("Total Revenue") AS "sum:Total Revenue:ok", '
        'COUNT("Order Count") AS "cnt:Order Count:ok" '
        "FROM commerce GROUP BY 1"
    )
    reply = asyncio.run(router.handle(user_sql, database="commerce"))
    frames = _parse_frames(reply)
    _, desc = frames[0]
    names: list[str] = []
    offset = 2
    for _ in range(3):
        end = desc.index(b"\x00", offset)
        names.append(desc[offset:end].decode("utf-8"))
        offset = end + 1 + 18
    # Column order matches the executor's output. Names match by inner
    # column name — the Order Count column gets the Order Count alias
    # even though it came before Revenue in the executor's output,
    # opposite to the user's SELECT order.
    assert names == [
        "Customer Country",
        "cnt:Order Count:ok",
        "sum:Total Revenue:ok",
    ]


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


# ---------------------------------------------------------------------------
# COLLATE-annotation stripping — Dremio's pgjdbc adds ``COLLATE "C"`` to
# every text column in pushdown SQL. The OBSQL translator only accepts
# bare identifiers, so the router strips it before delegating.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("input_sql", "expected"),
    [
        # Dremio's exact pushdown shape — quoted "C" collation on a column ref.
        (
            'SELECT "t"."col" COLLATE "C" FROM t',
            'SELECT "t"."col" FROM t',
        ),
        # Bare identifier collation (Postgres default style).
        (
            "SELECT name COLLATE default FROM t",
            "SELECT name FROM t",
        ),
        # Schema-qualified collation (psql's pg_catalog.default form).
        (
            "SELECT name COLLATE pg_catalog.default FROM t",
            "SELECT name FROM t",
        ),
        # Multiple annotations in one statement.
        (
            'SELECT a COLLATE "C", b COLLATE "C" FROM t WHERE c COLLATE "C" = \'x\'',
            "SELECT a, b FROM t WHERE c = 'x'",
        ),
        # Lower-case keyword.
        (
            'SELECT a collate "C" FROM t',
            "SELECT a FROM t",
        ),
        # No-op when there is no COLLATE.
        (
            "SELECT a FROM t",
            "SELECT a FROM t",
        ),
    ],
)
def test_strip_collate_annotations(input_sql: str, expected: str) -> None:
    assert _strip_collate_annotations(input_sql) == expected


# ---------------------------------------------------------------------------
# FETCH/OFFSET → LIMIT/OFFSET rewrite — Dremio emits the SQL-standard
# pagination shape; the OBSQL translator only accepts ``LIMIT n``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("input_sql", "expected"),
    [
        # Dremio's exact pushdown shape — bare FETCH NEXT.
        (
            "SELECT a FROM t FETCH NEXT 5 ROWS ONLY",
            "SELECT a FROM t LIMIT 5",
        ),
        # FETCH FIRST is the other SQL-standard alias.
        (
            "SELECT a FROM t FETCH FIRST 10 ROWS ONLY",
            "SELECT a FROM t LIMIT 10",
        ),
        # OFFSET m ROWS FETCH NEXT n ROWS ONLY → LIMIT n OFFSET m.
        (
            "SELECT a FROM t OFFSET 20 ROWS FETCH NEXT 5 ROWS ONLY",
            "SELECT a FROM t LIMIT 5 OFFSET 20",
        ),
        # Standalone OFFSET m ROWS → OFFSET m (no LIMIT).
        (
            "SELECT a FROM t OFFSET 20 ROWS",
            "SELECT a FROM t OFFSET 20",
        ),
        # Lower-case keywords.
        (
            "select a from t fetch next 3 rows only",
            "select a from t LIMIT 3",
        ),
        # No-op when there's no FETCH/OFFSET ROWS.
        (
            "SELECT a FROM t LIMIT 5",
            "SELECT a FROM t LIMIT 5",
        ),
    ],
)
def test_rewrite_fetch_to_limit(input_sql: str, expected: str) -> None:
    assert _rewrite_fetch_to_limit(input_sql) == expected


@pytest.mark.parametrize(
    "input_sql,expected",
    [
        # Bare OBSQL — pass through unchanged.
        (
            'SELECT "Region" FROM "commerce" LIMIT 5',
            'SELECT "Region" FROM "commerce" LIMIT 5',
        ),
        # 2-part: pgjdbc / DBeaver pushdown shape.
        (
            'SELECT "Region", "Sales" FROM "commerce"."model" LIMIT 5',
            'SELECT "Region", "Sales" FROM "commerce" LIMIT 5',
        ),
        # 3-part: Tableau / Dremio Postgres-source pushdown shape.
        (
            'SELECT "Region", "Sales" FROM "orionbelt"."commerce"."model" LIMIT 5',
            'SELECT "Region", "Sales" FROM "commerce" LIMIT 5',
        ),
        # 3-part with table-alias-qualified column refs (Tableau).
        (
            'SELECT "model"."Region" FROM "orionbelt"."commerce"."model" LIMIT 5',
            'SELECT "Region" FROM "commerce" LIMIT 5',
        ),
        # JOIN form, both legs 3-part.
        (
            'SELECT * FROM "orionbelt"."a"."model" x JOIN "orionbelt"."b"."model" y ON x.k = y.k',
            'SELECT * FROM "a" x JOIN "b" y ON x.k = y.k',
        ),
    ],
)
def test_unwrap_model_qualifier(input_sql: str, expected: str) -> None:
    assert _unwrap_model_qualifier(input_sql) == expected


# Dremio's Postgres-source connector wraps the virtual ``model`` table in a
# trivial derived table and lifts WHERE / ORDER BY / FETCH to the outer query.
# _flatten_federation_subquery must collapse that wrapper so the OBSQL
# translator (which rejects subqueries) accepts the flattened result.
_DREMIO_PUSHDOWN = (
    'SELECT "Country Name", "Total Sales" '
    'FROM (SELECT "model"."Country Name" COLLATE "C", "model"."Total Sales" '
    'FROM "commerce"."model") AS "model" '
    'WHERE ("Total Sales" > 1000000) '
    'ORDER BY "Total Sales" DESC FETCH NEXT 5 ROWS ONLY'
)


def test_flatten_federation_subquery_collapses_dremio_wrapper() -> None:
    flat = _flatten_federation_subquery(_DREMIO_PUSHDOWN)
    # No derived table survives; the model table is the direct FROM target.
    assert "FROM (" not in flat.upper()
    assert '"commerce"."model"' in flat
    # The outer WHERE / ORDER BY survive the collapse.
    assert "WHERE" in flat.upper() and "ORDER BY" in flat.upper()
    # Re-rendering must NOT inject an explicit null ordering the client never
    # sent (the postgres generator adds ``NULLS LAST`` to a plain DESC, which
    # the translator would capture and change top-N for nullable measures).
    assert "NULLS" not in flat.upper()


def test_flatten_then_normalize_is_translatable() -> None:
    """The fully normalized Dremio pushdown is plain OBSQL the translator takes."""

    norm = _normalize_for_obsql(_DREMIO_PUSHDOWN)
    assert "FROM (" not in norm.upper()
    assert "COLLATE" not in norm.upper()
    assert "FETCH" not in norm.upper()
    assert "LIMIT 5" in norm.upper()


@pytest.mark.parametrize(
    "sql",
    [
        # Plain semantic query — no wrapper, must pass through untouched.
        'SELECT "Region", "Sales" FROM "commerce" LIMIT 10',
        # Genuinely nested subquery that is NOT our pushdown shape — must be
        # left alone so the translator still rejects it.
        "SELECT x FROM (SELECT y FROM (SELECT 1)) t",
        # Derived table whose base is not the ``model`` table — not ours.
        'SELECT a FROM (SELECT a FROM "other"."table") t',
    ],
)
def test_flatten_federation_subquery_leaves_non_matching_sql_unchanged(sql: str) -> None:
    assert _flatten_federation_subquery(sql) == sql


# When the saved Dremio view (a VDS over the source) itself carries
# ``ORDER BY ... LIMIT``, Dremio keeps those clauses INSIDE the derived table
# rather than on the outer query. The flattener must hoist them out so the
# OBSQL translator (no subqueries) still accepts the result.
_DREMIO_VIEW_PUSHDOWN = (
    'SELECT "Country Name", "Total Sales" '
    'FROM (SELECT "model"."Country Name" COLLATE "C", "model"."Total Sales" '
    'FROM "commerce"."model" '
    'ORDER BY "model"."Total Sales" DESC FETCH NEXT 5 ROWS ONLY) AS "model"'
)


def test_flatten_hoists_inner_order_and_limit() -> None:
    flat = _flatten_federation_subquery(_DREMIO_VIEW_PUSHDOWN)
    assert "FROM (" not in flat.upper()
    assert '"commerce"."model"' in flat
    # The inner ORDER BY / FETCH must survive, hoisted to the outer query.
    assert "ORDER BY" in flat.upper()
    assert "FETCH" in flat.upper() or "LIMIT" in flat.upper()
    assert "NULLS" not in flat.upper()
    # Fully normalized, it is plain OBSQL the translator accepts.
    norm = _normalize_for_obsql(_DREMIO_VIEW_PUSHDOWN)
    assert "FROM (" not in norm.upper()
    assert "LIMIT 5" in norm.upper()


# Filtering a saved view from outside nests deeper: Dremio wraps the view body
# in an inner derived table, puts the filter in a middle one, and (for an
# equality on a dimension) constant-folds the projected dimension into a literal.
_DREMIO_VIEW_MEASURE_FILTER = (
    'SELECT "Channel Name", "Total Sales", "Average Sale" '
    'FROM (SELECT "Channel Name" AS "Channel Name", "Total Sales" AS "Total Sales", '
    '"Average Sale" AS "Average Sale" '
    'FROM (SELECT "model"."Channel Name" COLLATE "C", "model"."Total Sales", '
    '"model"."Average Sale" FROM "commerce"."model") AS "model" '
    'WHERE ("Total Sales" > 1000000)) AS "model" '
    'ORDER BY "model"."Total Sales"'
)
_DREMIO_VIEW_DIM_EQUALITY = (
    'SELECT CAST(\'B2B\' COLLATE "C" AS VARCHAR(65536)) AS "Channel Name", '
    '"Total Sales", "Average Sale" '
    'FROM (SELECT "Channel Name" AS "Channel Name", "Total Sales" AS "Total Sales", '
    '"Average Sale" AS "Average Sale" '
    'FROM (SELECT "model"."Channel Name" COLLATE "C", "model"."Total Sales", '
    '"model"."Average Sale" FROM "commerce"."model") AS "model" '
    'WHERE ("Channel Name" = \'B2B\' COLLATE "C")) AS "model" '
    'ORDER BY "model"."Total Sales"'
)


def test_flatten_double_nested_measure_filter() -> None:
    """A measure/metric filter on a view nests twice with bare projections."""

    norm = _normalize_for_obsql(_DREMIO_VIEW_MEASURE_FILTER)
    assert "FROM (" not in norm.upper()
    assert "TOTAL SALES" in norm.upper() and "> 1000000" in norm
    assert "ORDER BY" in norm.upper()


def test_flatten_constant_folded_dimension_equality() -> None:
    """A ``WHERE dim = value`` on a view is constant-folded by Dremio; the
    literal projection must map back to the bare dimension + the equality."""

    norm = _normalize_for_obsql(_DREMIO_VIEW_DIM_EQUALITY)
    assert "FROM (" not in norm.upper()
    assert "CAST(" not in norm.upper()  # the literal projection is gone
    assert '"Channel Name"' in norm
    assert "= 'B2B'" in norm  # the folded equality is recovered as a filter


def test_flatten_collapses_cast_projections() -> None:
    """When the source columns are DECIMAL, Dremio wraps pushed-down columns in
    ``CAST(... AS DECIMAL(p,s))``. The flattener must look through the casts to
    the bare columns (the semantic layer re-derives the type)."""

    sql = (
        'SELECT "Product Category", "Total Sales" '
        'FROM (SELECT CAST("model"."Product Category" AS VARCHAR) AS "Product Category", '
        'CAST("model"."Total Sales" AS DECIMAL(38,6)) AS "Total Sales" '
        'FROM "commerce"."model" ORDER BY "model"."Total Sales" DESC FETCH NEXT 5 ROWS ONLY) '
        'AS "model"'
    )
    norm = _normalize_for_obsql(sql)
    assert "FROM (" not in norm.upper()
    assert "CAST(" not in norm.upper()
    assert '"Total Sales"' in norm
    assert "LIMIT 5" in norm.upper()


def test_flatten_bails_outer_filter_over_inner_limit() -> None:
    """An outer WHERE/ORDER over an inner LIMIT (e.g. filtering a top-N view's
    output) must NOT be hoisted below the limit — that would change results.
    The flattener bails so the translator rejects the genuine subquery."""

    # Outer WHERE over a top-5 view body.
    where_over_limit = (
        'SELECT "Country Name", "Total Sales" '
        'FROM (SELECT "model"."Country Name", "model"."Total Sales" '
        'FROM "commerce"."model" ORDER BY "model"."Total Sales" DESC FETCH NEXT 5 ROWS ONLY) '
        'AS "model" WHERE ("Country Name" = \'Singapore\')'
    )
    assert _flatten_federation_subquery(where_over_limit) == where_over_limit

    # Outer ORDER over a top-5 view body.
    order_over_limit = (
        'SELECT "Country Name", "Total Sales" '
        'FROM (SELECT "model"."Country Name", "model"."Total Sales" '
        'FROM "commerce"."model" FETCH NEXT 5 ROWS ONLY) AS "model" '
        'ORDER BY "Country Name"'
    )
    assert _flatten_federation_subquery(order_over_limit) == order_over_limit


def test_flatten_bails_when_inner_and_outer_both_order() -> None:
    """If the outer ALSO orders/limits, merging would change results — bail."""

    sql = (
        'SELECT "Country Name", "Total Sales" '
        'FROM (SELECT "model"."Country Name", "model"."Total Sales" '
        'FROM "commerce"."model" ORDER BY "model"."Total Sales" DESC LIMIT 5) AS "model" '
        'ORDER BY "Country Name" LIMIT 3'
    )
    assert _flatten_federation_subquery(sql) == sql
