"""Unit tests for the ob-clickhouse DB-API 2.0 driver.

All tests mock clickhouse-connect — no live ClickHouse needed.
OBML tests additionally mock the REST API call to ``/v1/query/sql``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import ob_clickhouse
from ob_clickhouse.connection import Connection
from ob_clickhouse.cursor import Cursor
from ob_clickhouse.exceptions import NotSupportedError, ProgrammingError
from ob_clickhouse.type_codes import DATETIME, NUMBER, STRING


# ---------------------------------------------------------------------------
# Helpers to build mock clickhouse-connect objects
# ---------------------------------------------------------------------------


def _make_mock_client() -> MagicMock:
    """Return a mock clickhouse-connect Client."""
    client = MagicMock()
    # Default: query returns empty result
    result = MagicMock()
    result.result_rows = []
    result.column_names = []
    result.column_types = []
    client.query.return_value = result
    return client


def _make_query_result(
    rows: list[tuple[object, ...]],
    column_names: list[str],
    column_types: list[str],
) -> MagicMock:
    """Create a mock QueryResult with the given data."""
    result = MagicMock()
    result.result_rows = rows
    result.column_names = column_names
    # clickhouse-connect column_types are ClickHouseType objects; str() gives
    # the type name.  We mock them so str(t) returns the desired string.
    mock_types = []
    for t in column_types:
        mt = MagicMock()
        mt.__str__ = MagicMock(return_value=t)
        mock_types.append(mt)
    result.column_types = mock_types
    return result


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
    assert ob_clickhouse.apilevel == "2.0"


def test_threadsafety() -> None:
    assert ob_clickhouse.threadsafety == 1


def test_paramstyle() -> None:
    assert ob_clickhouse.paramstyle == "pyformat"


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_returns_connection() -> None:
    with patch("clickhouse_connect.get_client") as mock_get_client:
        mock_get_client.return_value = _make_mock_client()
        conn = ob_clickhouse.connect(host="ch-host", database="mydb")
        assert isinstance(conn, Connection)
        mock_get_client.assert_called_once()
        kwargs = mock_get_client.call_args.kwargs
        assert kwargs["host"] == "ch-host"
        assert kwargs["database"] == "mydb"
        assert kwargs["port"] == 8123
        assert kwargs["username"] == "default"
        assert kwargs["password"] == ""
        assert kwargs["secure"] is False


def test_connect_with_custom_port() -> None:
    with patch("clickhouse_connect.get_client") as mock_get_client:
        mock_get_client.return_value = _make_mock_client()
        ob_clickhouse.connect(port=9000, secure=True)
        kwargs = mock_get_client.call_args.kwargs
        assert kwargs["port"] == 9000
        assert kwargs["secure"] is True


def test_connect_with_settings() -> None:
    with patch("clickhouse_connect.get_client") as mock_get_client:
        mock_get_client.return_value = _make_mock_client()
        ob_clickhouse.connect(settings={"max_threads": "4"})
        kwargs = mock_get_client.call_args.kwargs
        assert kwargs["settings"] == {"max_threads": "4"}


def test_connect_context_manager() -> None:
    with patch("clickhouse_connect.get_client") as mock_get_client:
        mock_client = _make_mock_client()
        mock_get_client.return_value = mock_client
        with ob_clickhouse.connect() as conn:
            assert isinstance(conn, Connection)
        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_connection_close_is_idempotent() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    conn.close()
    conn.close()  # should not raise
    mock_client.close.assert_called_once()


def test_connection_cursor_after_close_raises() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.cursor()


def test_connection_commit_noop() -> None:
    """commit() is a no-op — ClickHouse has no transactions."""
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    conn.commit()  # should not raise


def test_connection_commit_after_close_raises() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.commit()


def test_connection_rollback_noop() -> None:
    """rollback() is a no-op — ClickHouse has no transactions."""
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    conn.rollback()  # should not raise


def test_connection_rollback_after_close_raises() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.rollback()


def test_connection_cursor_returns_cursor() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    assert isinstance(cur, Cursor)


def test_connection_passes_ob_params_to_cursor() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client, ob_api_url="http://my-api:9000", ob_timeout=60)
    cur = conn.cursor()
    assert cur._ob_api_url == "http://my-api:9000"
    assert cur._ob_timeout == 60


# ---------------------------------------------------------------------------
# Cursor — plain SQL
# ---------------------------------------------------------------------------


def test_cursor_execute_calls_client_query() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        mock_client.query.assert_called_once_with("SELECT 1")


def test_cursor_execute_with_params() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    with conn.cursor() as cur:
        cur.execute("SELECT %(a)s + %(b)s", {"a": 3, "b": 4})
        mock_client.query.assert_called_once_with(
            "SELECT %(a)s + %(b)s", parameters={"a": 3, "b": 4}
        )


def test_cursor_execute_returns_self() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    with conn.cursor() as cur:
        result = cur.execute("SELECT 1")
        assert result is cur


def test_cursor_description_with_types() -> None:
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(42, "hello", "2024-01-15")],
        column_names=["num", "txt", "dt"],
        column_types=["Int64", "String", "DateTime"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
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


def test_cursor_description_nullable_type() -> None:
    """Nullable wrapper should be stripped before type lookup."""
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(None,)],
        column_names=["val"],
        column_types=["Nullable(Int32)"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    desc = cur.description
    assert desc is not None
    assert desc[0][1] == NUMBER


def test_cursor_description_decimal_with_params() -> None:
    """Decimal(18,2) should map to NUMBER after stripping params."""
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(3.14,)],
        column_names=["price"],
        column_types=["Decimal(18,2)"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    desc = cur.description
    assert desc is not None
    assert desc[0][1] == NUMBER


def test_cursor_description_none_before_execute() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    assert cur.description is None


def test_cursor_rowcount_after_execute() -> None:
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(1,), (2,), (3,)],
        column_names=["id"],
        column_types=["Int32"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT id FROM t")
    assert cur.rowcount == 3


def test_cursor_rowcount_default() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    assert cur.rowcount == -1


def test_cursor_fetchone() -> None:
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(42, "hello")],
        column_names=["a", "b"],
        column_types=["Int32", "String"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    row = cur.fetchone()
    assert row == (42, "hello")


def test_cursor_fetchone_exhausted() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT 1")  # empty result
    assert cur.fetchone() is None


def test_cursor_fetchone_sequential() -> None:
    """fetchone() should advance through rows one at a time."""
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(1,), (2,), (3,)],
        column_names=["id"],
        column_types=["Int32"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT id FROM t")
    assert cur.fetchone() == (1,)
    assert cur.fetchone() == (2,)
    assert cur.fetchone() == (3,)
    assert cur.fetchone() is None


def test_cursor_fetchall() -> None:
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(1, "a"), (2, "b"), (3, "c")],
        column_names=["id", "name"],
        column_types=["Int32", "String"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    rows = cur.fetchall()
    assert len(rows) == 3
    assert rows[0] == (1, "a")
    assert rows[2] == (3, "c")


def test_cursor_fetchall_after_fetchone() -> None:
    """fetchall() should return only remaining rows."""
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(1,), (2,), (3,)],
        column_names=["id"],
        column_types=["Int32"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    cur.fetchone()  # consume first row
    remaining = cur.fetchall()
    assert len(remaining) == 2
    assert remaining[0] == (2,)


def test_cursor_fetchmany() -> None:
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(1,), (2,), (3,), (4,), (5,)],
        column_names=["id"],
        column_types=["Int32"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
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
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(1,), (2,), (3,)],
        column_names=["id"],
        column_types=["Int32"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.arraysize = 2
    cur.execute("SELECT 1")
    batch = cur.fetchmany()
    assert len(batch) == 2


def test_cursor_iteration() -> None:
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[(0,), (1,), (2,)],
        column_names=["id"],
        column_types=["Int32"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    rows = list(cur)
    assert len(rows) == 3
    assert rows[0] == (0,)
    assert rows[2] == (2,)


def test_cursor_close_then_fetch_raises() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchone()


def test_cursor_close_then_fetchall_raises() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchall()


def test_cursor_close_then_fetchmany_raises() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchmany()


def test_cursor_close_then_execute_raises() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.execute("SELECT 1")


def test_cursor_executemany_plain_sql() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO t VALUES (%(a)s, %(b)s)",
        [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}],
    )
    assert mock_client.query.call_count == 2


def test_cursor_executemany_obml_raises() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    obml = "select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n"
    with pytest.raises(NotSupportedError, match="executemany"):
        cur.executemany(obml, [])


def test_cursor_setinputsizes_noop() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.setinputsizes([])  # should not raise


def test_cursor_setoutputsize_noop() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.setoutputsize(1000)  # should not raise


def test_cursor_lastrowid_is_none() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    cur = conn.cursor()
    assert cur.lastrowid is None


def test_cursor_context_manager() -> None:
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    with conn.cursor() as cur:
        assert isinstance(cur, Cursor)
    # After exiting, cursor should be closed
    with pytest.raises(ProgrammingError, match="closed"):
        cur.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Cursor — OBML queries (mocked REST API)
# ---------------------------------------------------------------------------


def test_obml_compile_and_execute() -> None:
    """OBML query is compiled via REST API then executed on ClickHouse."""
    mock_client = _make_mock_client()
    compiled_sql = "SELECT region, sum(amount) AS revenue FROM orders GROUP BY region"
    qr = _make_query_result(
        rows=[("EMEA", 300.0), ("APAC", 150.0), ("AMER", 550.0)],
        column_names=["region", "revenue"],
        column_types=["String", "Float64"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)):
        with conn.cursor() as cur:
            cur.execute("select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n")
            rows = cur.fetchall()
            assert len(rows) == 3
            # Verify the compiled SQL was passed to client.query
            mock_client.query.assert_called_once_with(compiled_sql)


def test_obml_rest_dialect_is_clickhouse() -> None:
    """REST API is called with dialect=clickhouse."""
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    compiled_sql = "SELECT 1"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post:
        with conn.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Revenue\n")
            url = mock_post.call_args.args[0]
            assert "/v1/query/sql" in url
            assert mock_post.call_args.kwargs["params"] == {"dialect": "clickhouse"}


def test_obml_custom_api_url() -> None:
    """Custom ob_api_url is forwarded to the REST call."""
    mock_client = _make_mock_client()
    conn = Connection(mock_client, ob_api_url="http://my-api:9000")
    compiled_sql = "SELECT 1"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post:
        with conn.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Revenue\n")
            url = mock_post.call_args.args[0]
            assert url == "http://my-api:9000/v1/query/sql"


def test_plain_sql_passthrough() -> None:
    """Plain SQL is passed through without OBML compilation — no REST call."""
    mock_client = _make_mock_client()
    conn = Connection(mock_client)
    with patch("httpx.post") as mock_post:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders")
            mock_post.assert_not_called()
            mock_client.query.assert_called_once_with("SELECT count(*) FROM orders")


def test_obml_execute_with_description() -> None:
    """After OBML execute, description should reflect compiled result columns."""
    mock_client = _make_mock_client()
    compiled_sql = "SELECT region, sum(amount) AS revenue FROM orders GROUP BY region"
    qr = _make_query_result(
        rows=[("EMEA", 300.0)],
        column_names=["region", "revenue"],
        column_types=["String", "Float64"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
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
    """Unknown ClickHouse types should default to STRING."""
    mock_client = _make_mock_client()
    qr = _make_query_result(
        rows=[([1, 2, 3],)],
        column_names=["arr"],
        column_types=["Array(Int32)"],
    )
    mock_client.query.return_value = qr
    conn = Connection(mock_client)
    cur = conn.cursor()
    cur.execute("SELECT 1")
    desc = cur.description
    assert desc is not None
    # Array is not in CH_TYPE_MAP (base after split is "Array") → defaults to STRING
    assert desc[0][1] == STRING


def test_type_map_covers_common_types() -> None:
    """Verify the type map has entries for common ClickHouse types."""
    from ob_clickhouse.type_codes import CH_TYPE_MAP

    for int_type in ["Int8", "Int16", "Int32", "Int64", "UInt32", "UInt64"]:
        assert CH_TYPE_MAP[int_type] == NUMBER
    for float_type in ["Float32", "Float64"]:
        assert CH_TYPE_MAP[float_type] == NUMBER
    assert CH_TYPE_MAP["String"] == STRING
    assert CH_TYPE_MAP["UUID"] == STRING
    assert CH_TYPE_MAP["Date"] == DATETIME
    assert CH_TYPE_MAP["DateTime"] == DATETIME
    assert CH_TYPE_MAP["DateTime64"] == DATETIME


# ---------------------------------------------------------------------------
# Exception re-exports
# ---------------------------------------------------------------------------


def test_exceptions_importable() -> None:
    """All PEP 249 exceptions should be importable from ob_clickhouse.exceptions."""
    from ob_clickhouse.exceptions import (
        DataError,
        DatabaseError,
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
