"""Unit tests for the ob-dremio DB-API 2.0 driver.

All tests mock the ADBC Flight SQL connection — no live Dremio needed.
OBML tests additionally mock the REST API call to ``/v1/query/sql``.

The live counterpart is ``tests/integration/dremio/test_dremio_adbc_driver.py``
in the repository root, which runs this driver against a real Dremio
container. Mocks prove the wiring; only Dremio proves the protocol.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import ob_dremio
from ob_dremio.connection import Connection
from ob_dremio.cursor import Cursor
from ob_dremio.exceptions import NotSupportedError, ProgrammingError
from ob_dremio.type_codes import BINARY, DATETIME, NUMBER, STRING

_ADBC_CONNECT = "adbc_driver_flightsql.dbapi.connect"


# ---------------------------------------------------------------------------
# Helpers to build a mock ADBC connection and Arrow objects
# ---------------------------------------------------------------------------


def _make_mock_connection() -> MagicMock:
    """Return a mock ``adbc_driver_flightsql.dbapi.Connection``."""
    native = MagicMock()
    # Default: return empty table
    _set_result(native, _make_arrow_table([], [], []))
    return native


def _make_arrow_field(name: str, type_str: str) -> MagicMock:
    """Create a mock Arrow schema field."""
    field = MagicMock()
    field.name = name
    field.type = MagicMock()
    field.type.__str__ = MagicMock(return_value=type_str)
    return field


def _make_arrow_schema(names: list[str], type_strs: list[str]) -> MagicMock:
    """Create a mock Arrow schema."""
    fields = [_make_arrow_field(n, t) for n, t in zip(names, type_strs, strict=False)]
    schema = MagicMock()
    schema.__iter__ = MagicMock(return_value=iter(fields))
    return schema


def _make_arrow_table(
    rows: list[tuple[object, ...]],
    column_names: list[str],
    column_types: list[str],
) -> MagicMock:
    """Create a mock Arrow Table with the given data."""
    table = MagicMock()
    table.num_rows = len(rows)
    table.column_names = column_names
    table.schema = _make_arrow_schema(column_names, column_types)

    # to_pydict returns {col_name: [values...]}
    col_dict: dict[str, list[object]] = {name: [] for name in column_names}
    for row in rows:
        for i, name in enumerate(column_names):
            col_dict[name].append(row[i])
    table.to_pydict.return_value = col_dict
    return table


def _set_result(native: MagicMock, table: MagicMock) -> None:
    """Configure a mock ADBC connection so its cursor yields the given table."""
    native.cursor.return_value.fetch_arrow_table.return_value = table


def _native_cursor(native: MagicMock) -> MagicMock:
    """The single mock cursor ``Connection.cursor()`` hands out."""
    cursor: MagicMock = native.cursor.return_value
    return cursor


def _mock_api_response(sql: str) -> MagicMock:
    """Create a mock httpx response returning the given SQL."""
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = {"sql": sql}
    return resp


# ---------------------------------------------------------------------------
# PEP 249 module-level constants
# ---------------------------------------------------------------------------


def test_apilevel() -> None:
    assert ob_dremio.apilevel == "2.0"


def test_threadsafety() -> None:
    assert ob_dremio.threadsafety == 1


def test_paramstyle() -> None:
    assert ob_dremio.paramstyle == "qmark"


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_returns_connection() -> None:
    with patch(_ADBC_CONNECT) as mock_adbc_connect:
        mock_adbc_connect.return_value = _make_mock_connection()
        conn = ob_dremio.connect(host="dremio-host", port=32010)
        assert isinstance(conn, Connection)
        mock_adbc_connect.assert_called_once_with(
            "grpc://dremio-host:32010", db_kwargs={}, autocommit=True
        )


def test_connect_with_tls() -> None:
    with patch(_ADBC_CONNECT) as mock_adbc_connect:
        mock_adbc_connect.return_value = _make_mock_connection()
        ob_dremio.connect(host="dremio-host", port=32010, tls=True)
        assert mock_adbc_connect.call_args.args == ("grpc+tls://dremio-host:32010",)


def test_connect_with_auth() -> None:
    """Credentials become ADBC options; the driver runs the token exchange.

    The hand-rolled client called ``authenticate_basic_token`` and then had
    to attach the returned bearer to every later RPC by hand.
    """
    with patch(_ADBC_CONNECT) as mock_adbc_connect:
        mock_adbc_connect.return_value = _make_mock_connection()
        conn = ob_dremio.connect(host="dremio-host", username="user", password="pass")
        assert isinstance(conn, Connection)
        assert mock_adbc_connect.call_args.kwargs["db_kwargs"] == {
            "username": "user",
            "password": "pass",
        }


def test_connect_declares_autocommit() -> None:
    """Otherwise ADBC tries to *disable* autocommit, Dremio refuses, and every
    connection warns "conn will not be DB-API 2.0 compliant"."""
    with patch(_ADBC_CONNECT) as mock_adbc_connect:
        mock_adbc_connect.return_value = _make_mock_connection()
        ob_dremio.connect()
        assert mock_adbc_connect.call_args.kwargs["autocommit"] is True


def test_connect_omits_absent_credentials() -> None:
    """An anonymous connection must not send an empty username."""
    with patch(_ADBC_CONNECT) as mock_adbc_connect:
        mock_adbc_connect.return_value = _make_mock_connection()
        ob_dremio.connect(host="dremio-host")
        assert mock_adbc_connect.call_args.kwargs["db_kwargs"] == {}


def test_connect_db_kwargs_override_derived_options() -> None:
    """The escape hatch for Dremio's routing headers, and it wins."""
    with patch(_ADBC_CONNECT) as mock_adbc_connect:
        mock_adbc_connect.return_value = _make_mock_connection()
        ob_dremio.connect(
            username="user",
            db_kwargs={
                "username": "override",
                "adbc.flight.sql.rpc.call_header.routing_queue": "bi",
            },
        )
        assert mock_adbc_connect.call_args.kwargs["db_kwargs"] == {
            "username": "override",
            "adbc.flight.sql.rpc.call_header.routing_queue": "bi",
        }


def test_connect_custom_port() -> None:
    with patch(_ADBC_CONNECT) as mock_adbc_connect:
        mock_adbc_connect.return_value = _make_mock_connection()
        ob_dremio.connect(port=443, tls=True)
        assert mock_adbc_connect.call_args.args == ("grpc+tls://localhost:443",)


def test_connect_context_manager() -> None:
    with patch(_ADBC_CONNECT) as mock_adbc_connect:
        mock_conn = _make_mock_connection()
        mock_adbc_connect.return_value = mock_conn
        with ob_dremio.connect() as conn:
            assert isinstance(conn, Connection)
        mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_connection_close_is_idempotent() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    conn.close()
    conn.close()  # should not raise
    mock_conn.close.assert_called_once()


def test_connection_cursor_after_close_raises() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.cursor()


def test_connection_commit_noop() -> None:
    """commit() is a no-op — Dremio has no transactions."""
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    conn.commit()  # should not raise


def test_connection_commit_after_close_raises() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.commit()


def test_connection_rollback_noop() -> None:
    """rollback() is a no-op — Dremio has no transactions."""
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    conn.rollback()  # should not raise


def test_connection_rollback_after_close_raises() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.rollback()


def test_connection_cursor_returns_cursor() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    assert isinstance(cur, Cursor)


def test_connection_passes_ob_params_to_cursor() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn, ob_api_url="http://my-api:9000", ob_timeout=60)
    cur = conn.cursor()
    assert cur._ob_api_url == "http://my-api:9000"
    assert cur._ob_timeout == 60


# ---------------------------------------------------------------------------
# Cursor — plain SQL
# ---------------------------------------------------------------------------


def test_cursor_execute_runs_on_the_native_cursor() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        _native_cursor(mock_conn).execute.assert_called_once_with("SELECT 1")
        _native_cursor(mock_conn).fetch_arrow_table.assert_called_once()


def test_cursor_execute_binds_parameters() -> None:
    """Placeholders reach the native cursor instead of Dremio's parser.

    The Flight client this driver used to hold had nowhere to put bound
    values, so the statement went out with its ``?`` intact and Dremio
    failed it with a Calcite ``RexDynamicParam`` error.
    """
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    with conn.cursor() as cur:
        cur.execute("SELECT x FROM t WHERE y = ?", ("US",))
    _native_cursor(mock_conn).execute.assert_called_once_with(
        "SELECT x FROM t WHERE y = ?", ("US",)
    )


def test_cursor_execute_without_parameters_passes_none_along() -> None:
    """A statement with no parameters must not send an empty binding."""
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    _native_cursor(mock_conn).execute.assert_called_once_with("SELECT 1")


def test_each_execute_gets_its_own_native_cursor() -> None:
    """Re-preparing per execution, deliberately.

    ADBC skips re-preparing when the SQL text is unchanged, and Dremio then
    answers the second execution with the first one's rows even though new
    parameters were bound. Silent, wrong, and only reachable through reuse.
    """
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    with conn.cursor() as cur:
        cur.execute("SELECT x FROM t WHERE y = ?", ("a",))
        cur.execute("SELECT x FROM t WHERE y = ?", ("b",))
    assert mock_conn.cursor.call_count == 2
    # And each is released as soon as its table is in hand.
    assert _native_cursor(mock_conn).close.call_count == 2


def test_a_statement_is_released_even_when_it_fails() -> None:
    mock_conn = _make_mock_connection()
    _native_cursor(mock_conn).execute.side_effect = RuntimeError("boom")
    conn = Connection(mock_conn)
    with conn.cursor() as cur, pytest.raises(RuntimeError, match="boom"):
        cur.execute("SELECT 1")
    _native_cursor(mock_conn).close.assert_called_once()


def test_cursor_execute_returns_self() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    with conn.cursor() as cur:
        result = cur.execute("SELECT 1")
        assert result is cur


def test_cursor_description_with_arrow_types() -> None:
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(42, "hello", "2024-01-15")],
        column_names=["num", "txt", "dt"],
        column_types=["int64", "utf8", "timestamp[ns]"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    desc = cur.description
    assert desc is not None
    assert len(desc) == 3
    assert all(len(col) == 7 for col in desc)
    assert desc[0][0] == "num"
    assert desc[0][1] == NUMBER
    assert desc[1][0] == "txt"
    assert desc[1][1] == STRING
    assert desc[2][0] == "dt"
    assert desc[2][1] == DATETIME


def test_cursor_description_decimal_with_params() -> None:
    """decimal128(18, 2) should map to NUMBER after stripping params."""
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(3.14,)],
        column_names=["price"],
        column_types=["decimal128(18, 2)"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    desc = cur.description
    assert desc is not None
    assert desc[0][1] == NUMBER


def test_cursor_description_none_before_execute() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    assert cur.description is None


def test_cursor_rowcount_after_execute() -> None:
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(1,), (2,), (3,)],
        column_names=["id"],
        column_types=["int32"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT id FROM t")
    assert cur.rowcount == 3


def test_cursor_rowcount_default() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    assert cur.rowcount == -1


def test_cursor_fetchone() -> None:
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(42, "hello")],
        column_names=["a", "b"],
        column_types=["int32", "utf8"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    row = cur.fetchone()
    assert row == (42, "hello")


def test_cursor_fetchone_exhausted() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")  # empty result
    assert cur.fetchone() is None


def test_cursor_fetchone_sequential() -> None:
    """fetchone() should advance through rows one at a time."""
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(1,), (2,), (3,)],
        column_names=["id"],
        column_types=["int32"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT id FROM t")
    assert cur.fetchone() == (1,)
    assert cur.fetchone() == (2,)
    assert cur.fetchone() == (3,)
    assert cur.fetchone() is None


def test_cursor_fetchall() -> None:
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(1, "a"), (2, "b"), (3, "c")],
        column_names=["id", "name"],
        column_types=["int32", "utf8"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    rows = cur.fetchall()
    assert len(rows) == 3
    assert rows[0] == (1, "a")
    assert rows[2] == (3, "c")


def test_cursor_fetchall_after_fetchone() -> None:
    """fetchall() should return only remaining rows."""
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(1,), (2,), (3,)],
        column_names=["id"],
        column_types=["int32"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    cur.fetchone()  # consume first row
    remaining = cur.fetchall()
    assert len(remaining) == 2
    assert remaining[0] == (2,)


def test_cursor_fetchmany() -> None:
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(1,), (2,), (3,), (4,), (5,)],
        column_names=["id"],
        column_types=["int32"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    batch = cur.fetchmany(3)
    assert len(batch) == 3
    assert batch[0] == (1,)
    assert batch[2] == (3,)
    # Remaining
    rest = cur.fetchmany(10)
    assert len(rest) == 2


def test_cursor_fetchmany_default_arraysize() -> None:
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(1,), (2,), (3,)],
        column_names=["id"],
        column_types=["int32"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.arraysize = 2
    cur.execute("SELECT 1")
    batch = cur.fetchmany()
    assert len(batch) == 2


def test_cursor_iteration() -> None:
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[(0,), (1,), (2,)],
        column_names=["id"],
        column_types=["int32"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    rows = list(cur)
    assert len(rows) == 3
    assert rows[0] == (0,)
    assert rows[2] == (2,)


def test_cursor_close_then_fetch_raises() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchone()


def test_cursor_close_then_fetchall_raises() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchall()


def test_cursor_close_then_fetchmany_raises() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchmany()


def test_cursor_close_then_execute_raises() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.execute("SELECT 1")


def test_cursor_executemany_obml_raises() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    obml = "select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n"
    with pytest.raises(NotSupportedError, match="executemany"):
        cur.executemany(obml, [])


def test_cursor_executemany_plain_sql() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.executemany("INSERT INTO t VALUES (?)", [("a",), ("b",)])
    # One statement per parameter set — ADBC's own executemany binds the
    # batch over DoPut, which Dremio answers with "acceptPut is not
    # implemented".
    assert _native_cursor(mock_conn).execute.call_count == 2


def test_cursor_setinputsizes_noop() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.setinputsizes([])  # should not raise


def test_cursor_setoutputsize_noop() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.setoutputsize(1000)  # should not raise


def test_cursor_lastrowid_is_none() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    cur = conn.cursor()
    assert cur.lastrowid is None


def test_cursor_context_manager() -> None:
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    with conn.cursor() as cur:
        assert isinstance(cur, Cursor)
    # After exiting, cursor should be closed
    with pytest.raises(ProgrammingError, match="closed"):
        cur.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Cursor — OBML queries (mocked REST API)
# ---------------------------------------------------------------------------


def test_obml_compile_and_execute() -> None:
    """OBML query is compiled via REST API then executed on Dremio Flight."""
    mock_conn = _make_mock_connection()
    compiled_sql = "SELECT region, sum(amount) AS revenue FROM orders GROUP BY region"
    table = _make_arrow_table(
        rows=[("EMEA", 300.0), ("APAC", 150.0), ("AMER", 550.0)],
        column_names=["region", "revenue"],
        column_types=["utf8", "double"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)):
        with conn.cursor() as cur:
            cur.execute("select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n")
            rows = cur.fetchall()
            assert len(rows) == 3
            # Verify the compiled SQL reached the native cursor
            _native_cursor(mock_conn).execute.assert_called_once()


def test_obml_rest_dialect_is_dremio() -> None:
    """REST API is called with dialect=dremio."""
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    compiled_sql = "SELECT 1"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post:
        with conn.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Revenue\n")
            url = mock_post.call_args.args[0]
            assert "/v1/query/sql" in url
            assert mock_post.call_args.kwargs["params"] == {"dialect": "dremio"}


def test_obml_custom_api_url() -> None:
    """Custom ob_api_url is forwarded to the REST call."""
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn, ob_api_url="http://my-api:9000")
    compiled_sql = "SELECT 1"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post:
        with conn.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Revenue\n")
            url = mock_post.call_args.args[0]
            assert url == "http://my-api:9000/v1/query/sql"


def test_plain_sql_passthrough() -> None:
    """Plain SQL is passed through without OBML compilation — no REST call."""
    mock_conn = _make_mock_connection()
    conn = Connection(mock_conn)
    with patch("httpx.post") as mock_post, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM orders")
        mock_post.assert_not_called()
        _native_cursor(mock_conn).execute.assert_called_once()


def test_obml_execute_with_description() -> None:
    """After OBML execute, description should reflect compiled result columns."""
    mock_conn = _make_mock_connection()
    compiled_sql = "SELECT region, sum(amount) AS revenue FROM orders GROUP BY region"
    table = _make_arrow_table(
        rows=[("EMEA", 300.0)],
        column_names=["region", "revenue"],
        column_types=["utf8", "double"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)):
        with conn.cursor() as cur:
            cur.execute("select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n")
            desc = cur.description
            assert desc is not None
            assert len(desc) == 2
            assert desc[0][0] == "region"
            assert desc[0][1] == STRING
            assert desc[1][0] == "revenue"
            assert desc[1][1] == NUMBER


# ---------------------------------------------------------------------------
# Type code mapping
# ---------------------------------------------------------------------------


def test_unknown_type_defaults_to_string() -> None:
    """Unknown Arrow types should default to STRING."""
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[([1, 2, 3],)],
        column_names=["arr"],
        column_types=["list<int32>"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    desc = cur.description
    assert desc is not None
    # list<int32> is not in ARROW_TYPE_MAP — defaults to STRING
    assert desc[0][1] == STRING


def test_type_map_covers_common_arrow_types() -> None:
    """Verify the type map has entries for common Arrow types."""
    from ob_dremio.type_codes import ARROW_TYPE_MAP

    for int_type in ["int8", "int16", "int32", "int64", "uint32", "uint64"]:
        assert ARROW_TYPE_MAP[int_type] == NUMBER
    for float_type in ["float", "double"]:
        assert ARROW_TYPE_MAP[float_type] == NUMBER
    assert ARROW_TYPE_MAP["decimal128"] == NUMBER
    assert ARROW_TYPE_MAP["utf8"] == STRING
    assert ARROW_TYPE_MAP["string"] == STRING
    assert ARROW_TYPE_MAP["bool"] == STRING
    assert ARROW_TYPE_MAP["date32"] == DATETIME
    assert ARROW_TYPE_MAP["date64"] == DATETIME
    assert ARROW_TYPE_MAP["timestamp"] == DATETIME
    assert ARROW_TYPE_MAP["time32"] == DATETIME
    assert ARROW_TYPE_MAP["binary"] == BINARY
    assert ARROW_TYPE_MAP["large_binary"] == BINARY


def test_timestamp_with_params_maps_to_datetime() -> None:
    """timestamp[ns, tz=UTC] should map to DATETIME after stripping params."""
    mock_conn = _make_mock_connection()
    table = _make_arrow_table(
        rows=[("2024-01-15T10:30:00",)],
        column_names=["ts"],
        column_types=["timestamp[ns, tz=UTC]"],
    )
    _set_result(mock_conn, table)
    conn = Connection(mock_conn)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    desc = cur.description
    assert desc is not None
    assert desc[0][1] == DATETIME


# ---------------------------------------------------------------------------
# Exception re-exports
# ---------------------------------------------------------------------------


def test_exceptions_importable() -> None:
    """All PEP 249 exceptions should be importable from ob_dremio.exceptions."""
    from ob_dremio.exceptions import (
        DatabaseError,
        DataError,
        Error,
        IntegrityError,
        InterfaceError,
        InternalError,
        NotSupportedError,
        OperationalError,
        ProgrammingError,
        Warning,
    )

    # Verify hierarchy
    assert issubclass(DatabaseError, Error)
    assert issubclass(ProgrammingError, DatabaseError)
    assert issubclass(OperationalError, DatabaseError)
    assert issubclass(IntegrityError, DatabaseError)
    assert issubclass(DataError, DatabaseError)
    assert issubclass(InternalError, DatabaseError)
    assert issubclass(NotSupportedError, DatabaseError)
    assert issubclass(InterfaceError, Error)
    # Warning is separate
    assert Warning is not None
