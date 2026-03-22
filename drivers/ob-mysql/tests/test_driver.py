"""Unit tests for the ob-mysql DB-API 2.0 driver.

All tests mock mysql.connector — no live MySQL needed.
OBML tests additionally mock the REST API call to ``/v1/query/sql``.
"""

from __future__ import annotations

from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

import ob_mysql
from ob_mysql.connection import Connection
from ob_mysql.exceptions import NotSupportedError, ProgrammingError
from ob_mysql.type_codes import DATETIME, NUMBER, STRING

# ---------------------------------------------------------------------------
# Helper to build a mock mysql.connector connection
# ---------------------------------------------------------------------------

# mysql-connector description columns
MysqlColumn = namedtuple(
    "Column",
    ["name", "type_code", "display_size", "internal_size", "precision", "scale", "null_ok"],
)


def _make_mock_native() -> MagicMock:
    """Return a mock mysql.connector connection."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    # Default: no results
    mock_cursor.description = None
    mock_cursor.rowcount = -1
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []
    mock_cursor.fetchmany.return_value = []
    mock_cursor.lastrowid = None
    return mock_conn


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
    assert ob_mysql.apilevel == "2.0"


def test_threadsafety() -> None:
    assert ob_mysql.threadsafety == 1


def test_paramstyle() -> None:
    assert ob_mysql.paramstyle == "format"


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_returns_connection() -> None:
    with patch("mysql.connector.connect") as mock_connect:
        mock_connect.return_value = _make_mock_native()
        conn = ob_mysql.connect(database="testdb", user="testuser")
        assert isinstance(conn, Connection)
        mock_connect.assert_called_once()
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["database"] == "testdb"
        assert kwargs["user"] == "testuser"


def test_connect_default_port() -> None:
    with patch("mysql.connector.connect") as mock_connect:
        mock_connect.return_value = _make_mock_native()
        ob_mysql.connect()
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["port"] == 3306


def test_connect_default_charset() -> None:
    with patch("mysql.connector.connect") as mock_connect:
        mock_connect.return_value = _make_mock_native()
        ob_mysql.connect()
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["charset"] == "utf8mb4"


def test_connect_context_manager() -> None:
    with patch("mysql.connector.connect") as mock_connect:
        mock_native = _make_mock_native()
        mock_connect.return_value = mock_native
        with ob_mysql.connect() as conn:
            assert isinstance(conn, Connection)
        mock_native.close.assert_called_once()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_connection_close_is_idempotent() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    conn.close()
    conn.close()  # should not raise
    mock_native.close.assert_called_once()


def test_connection_cursor_after_close_raises() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.cursor()


def test_connection_commit() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    conn.commit()
    mock_native.commit.assert_called_once()


def test_connection_rollback() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    conn.rollback()
    mock_native.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Cursor — plain SQL
# ---------------------------------------------------------------------------


def test_cursor_execute_calls_native() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        mock_native.cursor().execute.assert_called_once_with("SELECT 1")


def test_cursor_execute_with_params() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    with conn.cursor() as cur:
        cur.execute("SELECT %s + %s", [3, 4])
        mock_native.cursor().execute.assert_called_once_with("SELECT %s + %s", [3, 4])


def test_cursor_execute_returns_self() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    with conn.cursor() as cur:
        result = cur.execute("SELECT 1")
        assert result is cur


def test_cursor_description() -> None:
    mock_native = _make_mock_native()
    mock_cursor = mock_native.cursor()
    mock_cursor.description = [
        MysqlColumn("num", 3, None, None, None, None, None),  # LONG → NUMBER
        MysqlColumn("txt", 253, None, None, None, None, None),  # VAR_STRING → STRING
        MysqlColumn("dt", 12, None, None, None, None, None),  # DATETIME → DATETIME
    ]
    conn = Connection(mock_native)
    cur = conn.cursor()
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


def test_cursor_description_none_before_execute() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().description = None
    conn = Connection(mock_native)
    cur = conn.cursor()
    assert cur.description is None


def test_cursor_fetchone() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().fetchone.return_value = (42,)
    conn = Connection(mock_native)
    cur = conn.cursor()
    row = cur.fetchone()
    assert row == (42,)


def test_cursor_fetchone_exhausted() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().fetchone.return_value = None
    conn = Connection(mock_native)
    cur = conn.cursor()
    assert cur.fetchone() is None


def test_cursor_fetchall() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().fetchall.return_value = [(1, "a"), (2, "b"), (3, "c")]
    conn = Connection(mock_native)
    cur = conn.cursor()
    rows = cur.fetchall()
    assert len(rows) == 3
    assert rows[0] == (1, "a")


def test_cursor_fetchmany() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().fetchmany.return_value = [(1,), (2,), (3,)]
    conn = Connection(mock_native)
    cur = conn.cursor()
    batch = cur.fetchmany(3)
    assert len(batch) == 3
    mock_native.cursor().fetchmany.assert_called_with(3)


def test_cursor_iteration() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().fetchone.side_effect = [(0,), (1,), (2,), None]
    conn = Connection(mock_native)
    cur = conn.cursor()
    rows = list(cur)
    assert len(rows) == 3
    assert rows[0] == (0,)


def test_cursor_close_then_fetch_raises() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchone()


def test_cursor_executemany_plain_sql() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.executemany("INSERT INTO t VALUES (%s, %s)", [(1, "a"), (2, "b")])
    assert mock_native.cursor().execute.call_count == 2


def test_cursor_executemany_obml_raises() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    obml = "select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n"
    with pytest.raises(NotSupportedError, match="executemany"):
        cur.executemany(obml, [])


def test_cursor_setinputsizes_noop() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.setinputsizes([])  # should not raise


def test_cursor_setoutputsize_noop() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.setoutputsize(1000)  # should not raise


def test_cursor_lastrowid() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().lastrowid = 42
    conn = Connection(mock_native)
    cur = conn.cursor()
    assert cur.lastrowid == 42


def test_cursor_rowcount() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().rowcount = 5
    conn = Connection(mock_native)
    cur = conn.cursor()
    assert cur.rowcount == 5


# ---------------------------------------------------------------------------
# Cursor — OBML queries (mocked REST API)
# ---------------------------------------------------------------------------


def test_obml_compile_and_execute() -> None:
    """OBML query is compiled via REST API then executed on MySQL."""
    mock_native = _make_mock_native()
    compiled_sql = "SELECT `region`, SUM(`amount`) AS `revenue` FROM `orders` GROUP BY `region`"
    mock_native.cursor().description = [
        MysqlColumn("region", 253, None, None, None, None, None),
        MysqlColumn("revenue", 5, None, None, None, None, None),
    ]
    mock_native.cursor().fetchall.return_value = [
        ("EMEA", 300.0),
        ("APAC", 150.0),
        ("AMER", 550.0),
    ]
    conn = Connection(mock_native)
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)):
        with conn.cursor() as cur:
            cur.execute("select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n")
            rows = cur.fetchall()
            assert len(rows) == 3
            # Verify the compiled SQL was passed to native cursor
            mock_native.cursor().execute.assert_called_once_with(compiled_sql)


def test_obml_rest_dialect_is_mysql() -> None:
    """REST API is called with dialect=mysql."""
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    compiled_sql = "SELECT 1"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post:
        with conn.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Revenue\n")
            url = mock_post.call_args.args[0]
            assert "/v1/query/sql" in url
            assert mock_post.call_args.kwargs["params"] == {"dialect": "mysql"}


def test_obml_custom_api_url() -> None:
    """Custom ob_api_url is forwarded to the REST call."""
    mock_native = _make_mock_native()
    conn = Connection(mock_native, ob_api_url="http://my-api:9000")
    compiled_sql = "SELECT 1"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post:
        with conn.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Revenue\n")
            url = mock_post.call_args.args[0]
            assert url == "http://my-api:9000/v1/query/sql"


def test_plain_sql_passthrough() -> None:
    """Plain SQL is passed through without OBML compilation — no REST call."""
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    with patch("httpx.post") as mock_post, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM orders")
        mock_post.assert_not_called()
        mock_native.cursor().execute.assert_called_once_with("SELECT COUNT(*) FROM orders")


# ---------------------------------------------------------------------------
# fetch_arrow_table()
# ---------------------------------------------------------------------------


def test_fetch_arrow_table() -> None:
    """fetch_arrow_table() converts rows to PyArrow Table."""
    mock_native = _make_mock_native()
    mock_cursor = mock_native.cursor()
    mock_cursor.description = [
        MysqlColumn("id", 3, None, None, None, None, None),
        MysqlColumn("name", 253, None, None, None, None, None),
    ]
    mock_cursor.fetchall.return_value = [(1, "Alice"), (2, "Bob")]
    conn = Connection(mock_native)
    cur = conn.cursor()
    table = cur.fetch_arrow_table()
    assert table.num_rows == 2
    assert table.num_columns == 2
    assert table.column_names == ["id", "name"]


def test_fetch_arrow_table_empty() -> None:
    """fetch_arrow_table() returns empty table with correct columns."""
    mock_native = _make_mock_native()
    mock_cursor = mock_native.cursor()
    mock_cursor.description = [
        MysqlColumn("id", 3, None, None, None, None, None),
        MysqlColumn("name", 253, None, None, None, None, None),
    ]
    mock_cursor.fetchall.return_value = []
    conn = Connection(mock_native)
    cur = conn.cursor()
    table = cur.fetch_arrow_table()
    assert table.num_rows == 0
    assert table.column_names == ["id", "name"]
