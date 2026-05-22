"""Unit tests for the ob-duckdb DB-API 2.0 driver.

Plain SQL tests run against DuckDB :memory: — no external services needed.
OBML tests mock the REST API call to ``/v1/query/sql``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import ob_duckdb
from ob_duckdb.connection import Connection
from ob_duckdb.exceptions import NotSupportedError, ProgrammingError
from ob_duckdb.type_codes import DATETIME, NUMBER, STRING


# ---------------------------------------------------------------------------
# PEP 249 module-level constants
# ---------------------------------------------------------------------------


def test_apilevel() -> None:
    assert ob_duckdb.apilevel == "2.0"


def test_threadsafety() -> None:
    assert ob_duckdb.threadsafety == 1


def test_paramstyle() -> None:
    assert ob_duckdb.paramstyle == "qmark"


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_returns_connection() -> None:
    conn = ob_duckdb.connect()
    assert isinstance(conn, Connection)
    conn.close()


def test_connect_memory_default() -> None:
    conn = ob_duckdb.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT 42 AS answer")
        assert cur.fetchone() == (42,)
    conn.close()


def test_connect_context_manager() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_connection_close_is_idempotent() -> None:
    conn = ob_duckdb.connect()
    conn.close()
    conn.close()  # should not raise


def test_connection_cursor_after_close_raises() -> None:
    conn = ob_duckdb.connect()
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.cursor()


def test_connection_commit() -> None:
    conn = ob_duckdb.connect()
    conn.commit()  # should not raise
    conn.close()


def test_connection_rollback() -> None:
    conn = ob_duckdb.connect()
    conn.rollback()  # should not raise
    conn.close()


# ---------------------------------------------------------------------------
# Cursor — plain SQL
# ---------------------------------------------------------------------------


def test_cursor_execute_select() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS a, 'hello' AS b, CAST(3.14 AS DOUBLE) AS c")
            row = cur.fetchone()
            assert row == (1, "hello", 3.14)


def test_cursor_description() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS num, 'txt' AS str, CURRENT_DATE AS dt")
            desc = cur.description
            assert desc is not None
            assert len(desc) == 3
            # Each entry is a 7-tuple
            assert all(len(col) == 7 for col in desc)
            # Column names
            assert desc[0][0] == "num"
            assert desc[1][0] == "str"
            assert desc[2][0] == "dt"


def test_cursor_description_type_codes() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT "
                "CAST(1 AS INTEGER) AS i, "
                "CAST('x' AS VARCHAR) AS v, "
                "CAST('2024-01-01' AS DATE) AS d, "
                "CAST(1.5 AS DOUBLE) AS f"
            )
            desc = cur.description
            assert desc is not None
            assert desc[0][1] == NUMBER
            assert desc[1][1] == STRING
            assert desc[2][1] == DATETIME
            assert desc[3][1] == NUMBER


def test_cursor_fetchall() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c')) AS t(id, name)")
            rows = cur.fetchall()
            assert len(rows) == 3
            assert rows[0] == (1, "a")
            assert rows[2] == (3, "c")


def test_cursor_fetchmany() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM range(10) AS t(n)")
            batch = cur.fetchmany(3)
            assert len(batch) == 3
            rest = cur.fetchall()
            assert len(rest) == 7


def test_cursor_fetchone_exhausted() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() is not None
            assert cur.fetchone() is None


def test_cursor_iteration() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM range(5) AS t(n)")
            rows = list(cur)
            assert len(rows) == 5
            assert rows[0] == (0,)


def test_cursor_execute_returns_self() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            result = cur.execute("SELECT 1")
            assert result is cur


def test_cursor_close_then_fetch_raises() -> None:
    with ob_duckdb.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        with pytest.raises(ProgrammingError, match="closed"):
            cur.fetchone()


def test_cursor_executemany_plain_sql() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
            cur.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b")])
            cur.execute("SELECT COUNT(*) FROM t")
            assert cur.fetchone() == (2,)


def test_cursor_executemany_obml_raises() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            obml = "select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n"
            with pytest.raises(NotSupportedError, match="executemany"):
                cur.executemany(obml, [])


def test_cursor_setinputsizes_noop() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.setinputsizes([])  # should not raise


def test_cursor_setoutputsize_noop() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.setoutputsize(1000)  # should not raise


def test_cursor_lastrowid_is_none() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            assert cur.lastrowid is None


def test_cursor_with_parameters() -> None:
    with ob_duckdb.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ? + ? AS result", [3, 4])
            assert cur.fetchone() == (7,)


# ---------------------------------------------------------------------------
# Cursor — OBML queries (mocked REST API)
# ---------------------------------------------------------------------------


@pytest.fixture
def duckdb_conn_with_data() -> Connection:
    """DuckDB connection with sample data (no model — OBML goes via REST)."""
    conn = ob_duckdb.connect()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE orders (
                order_id VARCHAR,
                region VARCHAR,
                amount DOUBLE
            )
        """)
        cur.execute("""
            INSERT INTO orders VALUES
                ('O1', 'EMEA', 100.0),
                ('O2', 'EMEA', 200.0),
                ('O3', 'APAC', 150.0),
                ('O4', 'AMER', 300.0),
                ('O5', 'AMER', 250.0)
        """)
    return conn


def _mock_api_response(sql: str) -> MagicMock:
    """Create a mock httpx response returning the given SQL."""
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = {"sql": sql}
    return resp


def test_obml_compile_and_execute(duckdb_conn_with_data: Connection) -> None:
    """Full round-trip: OBML → REST compile → execute on DuckDB."""
    compiled_sql = "SELECT region, SUM(amount) AS revenue FROM orders GROUP BY region"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)):
        with duckdb_conn_with_data.cursor() as cur:
            cur.execute("select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n")
            rows = cur.fetchall()
            assert len(rows) == 3
            desc = cur.description
            assert desc is not None
            assert len(desc) == 2


def test_obml_measures_only(duckdb_conn_with_data: Connection) -> None:
    """OBML with measures only (no dimensions)."""
    compiled_sql = "SELECT SUM(amount) AS revenue FROM orders"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)):
        with duckdb_conn_with_data.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Revenue\n")
            rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 1000.0


def test_obml_count_measure(duckdb_conn_with_data: Connection) -> None:
    """OBML count aggregation."""
    compiled_sql = "SELECT COUNT(order_id) AS order_count FROM orders"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)):
        with duckdb_conn_with_data.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Order Count\n")
            rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 5


def test_obml_with_limit(duckdb_conn_with_data: Connection) -> None:
    """OBML with limit clause."""
    compiled_sql = "SELECT region, SUM(amount) AS revenue FROM orders GROUP BY region LIMIT 2"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)):
        with duckdb_conn_with_data.cursor() as cur:
            cur.execute(
                "select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\nlimit: 2\n"
            )
            rows = cur.fetchall()
            assert len(rows) == 2


def test_obml_rest_api_url_forwarded(duckdb_conn_with_data: Connection) -> None:
    """The ob_api_url is forwarded to the REST call."""
    compiled_sql = "SELECT 1"
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)) as mock_post:
        with duckdb_conn_with_data.cursor() as cur:
            cur.execute("select:\n  measures:\n    - Revenue\n")
            url = mock_post.call_args.args[0]
            assert "/v1/query/sql" in url
            assert mock_post.call_args.kwargs["params"] == {"dialect": "duckdb"}


def test_plain_sql_passthrough(duckdb_conn_with_data: Connection) -> None:
    """Plain SQL is passed through without OBML compilation."""
    with duckdb_conn_with_data.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM orders")
        assert cur.fetchone() == (5,)
