"""Unit tests for the ob-databricks DB-API 2.0 driver.

All tests mock ``databricks.sql`` — no live Databricks cluster needed.
OBML tests additionally mock the REST API call to ``/v1/query/sql``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import ob_databricks
from ob_databricks.connection import Connection
from ob_databricks.cursor import Cursor
from ob_databricks.exceptions import NotSupportedError, ProgrammingError
from ob_databricks.type_codes import DATETIME, NUMBER, STRING

# ---------------------------------------------------------------------------
# Helper to build a mock databricks connection
# ---------------------------------------------------------------------------

# databricks-sql-connector description columns are tuples:
# (name, type_code, display_size, internal_size, precision, scale, null_ok)
# type_code is a string type name (e.g. "int", "string", "timestamp")


def _make_dbr_desc(name: str, type_name: str) -> tuple[str, str, None, None, None, None, None]:
    """Build a single Databricks description column tuple."""
    return (name, type_name, None, None, None, None, None)


def _make_mock_native() -> MagicMock:
    """Return a mock databricks.sql connection."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    # Default: no results
    mock_cursor.description = None
    mock_cursor.rowcount = -1
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []
    mock_cursor.fetchmany.return_value = []
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
    assert ob_databricks.apilevel == "2.0"


def test_threadsafety() -> None:
    assert ob_databricks.threadsafety == 1


def test_paramstyle() -> None:
    assert ob_databricks.paramstyle == "pyformat"


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_returns_connection() -> None:
    with patch("databricks.sql.connect") as mock_connect:
        mock_connect.return_value = _make_mock_native()
        conn = ob_databricks.connect(
            server_hostname="adb-123.azuredatabricks.net",
            http_path="/sql/1.0/warehouses/abc",
            access_token="dapi_token",
        )
        assert isinstance(conn, Connection)
        mock_connect.assert_called_once()
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["server_hostname"] == "adb-123.azuredatabricks.net"
        assert kwargs["http_path"] == "/sql/1.0/warehouses/abc"
        assert kwargs["access_token"] == "dapi_token"


def test_connect_default_catalog_and_schema() -> None:
    with patch("databricks.sql.connect") as mock_connect:
        mock_connect.return_value = _make_mock_native()
        ob_databricks.connect(
            server_hostname="adb-123.azuredatabricks.net",
            http_path="/sql/1.0/warehouses/abc",
        )
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["catalog"] == "hive_metastore"
        assert kwargs["schema"] == "default"


def test_connect_custom_catalog_and_schema() -> None:
    with patch("databricks.sql.connect") as mock_connect:
        mock_connect.return_value = _make_mock_native()
        ob_databricks.connect(
            server_hostname="adb-123.azuredatabricks.net",
            http_path="/sql/1.0/warehouses/abc",
            catalog="unity_catalog",
            schema="analytics",
        )
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["catalog"] == "unity_catalog"
        assert kwargs["schema"] == "analytics"


def test_connect_context_manager() -> None:
    with patch("databricks.sql.connect") as mock_connect:
        mock_native = _make_mock_native()
        mock_connect.return_value = mock_native
        with ob_databricks.connect(
            server_hostname="adb-123.azuredatabricks.net",
            http_path="/sql/1.0/warehouses/abc",
        ) as conn:
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


def test_connection_commit_noop() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    conn.commit()  # should not raise, no-op
    # Databricks auto-commits — native commit is NOT called
    mock_native.commit.assert_not_called()


def test_connection_rollback_noop() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    conn.rollback()  # should not raise, no-op
    # Databricks does not support transactions — native rollback is NOT called
    mock_native.rollback.assert_not_called()


def test_connection_commit_after_close_raises() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.commit()


def test_connection_rollback_after_close_raises() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.rollback()


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
        cur.execute("SELECT %(a)s + %(b)s", {"a": 3, "b": 4})
        mock_native.cursor().execute.assert_called_once_with(
            "SELECT %(a)s + %(b)s", {"a": 3, "b": 4}
        )


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
        _make_dbr_desc("num", "int"),
        _make_dbr_desc("txt", "string"),
        _make_dbr_desc("dt", "timestamp"),
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


def test_cursor_description_type_mapping_comprehensive() -> None:
    """Verify all major Databricks types map correctly."""
    mock_native = _make_mock_native()
    mock_cursor = mock_native.cursor()
    mock_cursor.description = [
        _make_dbr_desc("c_bool", "boolean"),
        _make_dbr_desc("c_byte", "byte"),
        _make_dbr_desc("c_tinyint", "tinyint"),
        _make_dbr_desc("c_short", "short"),
        _make_dbr_desc("c_smallint", "smallint"),
        _make_dbr_desc("c_int", "int"),
        _make_dbr_desc("c_integer", "integer"),
        _make_dbr_desc("c_long", "long"),
        _make_dbr_desc("c_bigint", "bigint"),
        _make_dbr_desc("c_float", "float"),
        _make_dbr_desc("c_double", "double"),
        _make_dbr_desc("c_decimal", "decimal"),
        _make_dbr_desc("c_string", "string"),
        _make_dbr_desc("c_char", "char"),
        _make_dbr_desc("c_varchar", "varchar"),
        _make_dbr_desc("c_date", "date"),
        _make_dbr_desc("c_ts", "timestamp"),
        _make_dbr_desc("c_binary", "binary"),
        _make_dbr_desc("c_array", "array"),
        _make_dbr_desc("c_map", "map"),
        _make_dbr_desc("c_struct", "struct"),
    ]
    conn = Connection(mock_native)
    cur = conn.cursor()
    desc = cur.description
    assert desc is not None
    # boolean -> STRING
    assert desc[0][1] == STRING
    # numeric types -> NUMBER
    for i in range(1, 12):
        assert desc[i][1] == NUMBER, f"Column {desc[i][0]} should be NUMBER"
    # string types -> STRING
    for i in range(12, 15):
        assert desc[i][1] == STRING, f"Column {desc[i][0]} should be STRING"
    # date/time -> DATETIME
    assert desc[15][1] == DATETIME
    assert desc[16][1] == DATETIME
    # binary -> BINARY
    from ob_databricks.type_codes import BINARY

    assert desc[17][1] == BINARY
    # complex -> STRING
    for i in range(18, 21):
        assert desc[i][1] == STRING, f"Column {desc[i][0]} should be STRING"


def test_cursor_description_unknown_type_defaults_to_string() -> None:
    mock_native = _make_mock_native()
    mock_cursor = mock_native.cursor()
    mock_cursor.description = [
        _make_dbr_desc("c_unknown", "some_future_type"),
    ]
    conn = Connection(mock_native)
    cur = conn.cursor()
    desc = cur.description
    assert desc is not None
    assert desc[0][1] == STRING


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


def test_cursor_fetchmany_default_arraysize() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().fetchmany.return_value = [(1,)]
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.fetchmany()
    mock_native.cursor().fetchmany.assert_called_with(1)  # default arraysize


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


def test_cursor_close_then_fetchall_raises() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchall()


def test_cursor_close_then_fetchmany_raises() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchmany()


def test_cursor_close_then_execute_raises() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.execute("SELECT 1")


def test_cursor_close_is_idempotent() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.close()
    cur.close()  # should not raise
    mock_native.cursor().close.assert_called_once()


def test_cursor_executemany_plain_sql() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO t VALUES (%(a)s, %(b)s)",
        [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}],
    )
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


def test_cursor_lastrowid_is_none() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    cur = conn.cursor()
    assert cur.lastrowid is None


def test_cursor_rowcount() -> None:
    mock_native = _make_mock_native()
    mock_native.cursor().rowcount = 5
    conn = Connection(mock_native)
    cur = conn.cursor()
    assert cur.rowcount == 5


def test_cursor_context_manager() -> None:
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    with conn.cursor() as cur:
        assert isinstance(cur, Cursor)
    mock_native.cursor().close.assert_called_once()


# ---------------------------------------------------------------------------
# Cursor — OBML queries (mocked REST API)
# ---------------------------------------------------------------------------


def test_obml_compile_and_execute() -> None:
    """OBML query is compiled via REST API then executed on Databricks."""
    mock_native = _make_mock_native()
    compiled_sql = "SELECT region, SUM(amount) AS revenue FROM orders GROUP BY region"
    mock_native.cursor().description = [
        _make_dbr_desc("region", "string"),
        _make_dbr_desc("revenue", "double"),
    ]
    mock_native.cursor().fetchall.return_value = [
        ("EMEA", 300.0),
        ("APAC", 150.0),
        ("AMER", 550.0),
    ]
    conn = Connection(mock_native)
    with (
        patch("httpx.post", return_value=_mock_api_response(compiled_sql)),
        conn.cursor() as cur,
    ):
        cur.execute("select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n")
        rows = cur.fetchall()
        assert len(rows) == 3
        # Verify the compiled SQL was passed to native cursor
        mock_native.cursor().execute.assert_called_once_with(compiled_sql)


def test_obml_rest_dialect_is_databricks() -> None:
    """REST API is called with dialect=databricks."""
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    compiled_sql = "SELECT 1"
    with (
        patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post,
        conn.cursor() as cur,
    ):
        cur.execute("select:\n  measures:\n    - Revenue\n")
        url = mock_post.call_args.args[0]
        assert "/v1/query/sql" in url
        assert mock_post.call_args.kwargs["params"] == {"dialect": "databricks"}


def test_obml_custom_api_url() -> None:
    """Custom ob_api_url is forwarded to the REST call."""
    mock_native = _make_mock_native()
    conn = Connection(mock_native, ob_api_url="http://my-api:9000")
    compiled_sql = "SELECT 1"
    with (
        patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post,
        conn.cursor() as cur,
    ):
        cur.execute("select:\n  measures:\n    - Revenue\n")
        url = mock_post.call_args.args[0]
        assert url == "http://my-api:9000/v1/query/sql"


def test_obml_custom_timeout() -> None:
    """Custom ob_timeout is forwarded to the REST call."""
    mock_native = _make_mock_native()
    conn = Connection(mock_native, ob_timeout=60)
    compiled_sql = "SELECT 1"
    with (
        patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post,
        conn.cursor() as cur,
    ):
        cur.execute("select:\n  measures:\n    - Revenue\n")
        assert mock_post.call_args.kwargs["timeout"] == 60


def test_plain_sql_passthrough() -> None:
    """Plain SQL is passed through without OBML compilation — no REST call."""
    mock_native = _make_mock_native()
    conn = Connection(mock_native)
    with patch("httpx.post") as mock_post, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM orders")
        mock_post.assert_not_called()
        mock_native.cursor().execute.assert_called_once_with("SELECT COUNT(*) FROM orders")


def test_connect_names_the_client_in_the_user_agent() -> None:
    with patch("databricks.sql.connect") as mock_connect:
        ob_databricks.connect("host", "/path", client_name="app/1.0")
        assert mock_connect.call_args.kwargs["user_agent_entry"] == "app/1.0"
        ob_databricks.connect("host", "/path")
        assert "user_agent_entry" not in mock_connect.call_args.kwargs


def test_an_explicit_user_agent_entry_wins_over_the_client_name() -> None:
    with patch("databricks.sql.connect") as mock_connect:
        ob_databricks.connect("host", "/path", client_name="app/1.0", user_agent_entry="own/2")
        assert mock_connect.call_args.kwargs["user_agent_entry"] == "own/2"
