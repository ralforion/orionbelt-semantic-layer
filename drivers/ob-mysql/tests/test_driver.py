"""Unit tests for the ob-mysql DB-API 2.0 driver.

Both clients are faked: PyMySQL by a cursor holding rows and column-definition
fields, the ADBC driver by a cursor holding an Arrow table. No live MySQL is
needed; ``tests/integration/test_ob_mysql_driver.py`` runs both against one.
OBML tests additionally mock the REST API call to ``/v1/query/sql``.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import adbc_driver_manager
import pyarrow as pa
import pytest

import ob_mysql
from ob_mysql.connection import Connection
from ob_mysql.exceptions import DataError, InterfaceError, NotSupportedError, ProgrammingError
from ob_mysql.type_codes import DATETIME, NUMBER, STRING

LONG = 3
DOUBLE = 5
DATETIME_CODE = 12
VAR_STRING = 253
UTF8MB4 = 255


def _field(name: str, type_code: int, *, flags: int = 0, length: int = 11, scale: int = 0) -> Any:
    """A PyMySQL ``FieldDescriptor``: the column-definition packet."""
    return SimpleNamespace(
        name=name, type_code=type_code, flags=flags, charsetnr=UTF8MB4, length=length, scale=scale
    )


def _pymysql_native(
    fields: list[Any] | None = None,
    rows: list[tuple[Any, ...]] | None = None,
    *,
    rowcount: int = -1,
    lastrowid: int | None = None,
) -> MagicMock:
    """A PyMySQL connection whose cursor returns *rows* under *fields*."""
    native = MagicMock()
    cursor = native.cursor.return_value
    cursor._result = SimpleNamespace(fields=fields) if fields is not None else None
    cursor.description = [(f.name, f.type_code) for f in fields] if fields is not None else None
    cursor.fetchall.return_value = rows or []
    cursor.rowcount = len(rows) if rows is not None else rowcount
    cursor.lastrowid = lastrowid
    return native


def _pymysql(native: MagicMock, **kwargs: Any) -> Connection:
    return Connection(native, backend="pymysql", **kwargs)


def _adbc_native(
    table: pa.Table | None = None, *, error: Exception | None = None, sql_mode: str = ""
) -> MagicMock:
    """An ADBC connection whose cursor returns *table*, or raises *error*, and
    reports *sql_mode* as the session's."""
    native = MagicMock()
    cursor = native.cursor.return_value
    cursor.fetchone.return_value = (sql_mode,)
    cursor.description = [(name, None) for name in table.column_names] if table else []
    cursor.rowcount = -1
    if error is not None:
        cursor.fetch_arrow_table.side_effect = error
    else:
        cursor.fetch_arrow_table.return_value = table
    return native


def _mock_api_response(sql: str) -> MagicMock:
    """Create a mock httpx response returning the given SQL."""
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = {"sql": sql}
    return resp


_ROWS_FIELDS = [_field("id", LONG), _field("name", VAR_STRING)]
_ROWS = [(1, "Alice"), (2, "Bob"), (3, "Carol")]


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
# connect(): which client
# ---------------------------------------------------------------------------


def test_connect_reads_through_adbc_when_the_driver_is_installed() -> None:
    adbc = MagicMock()
    with (
        patch("ob_mysql._connect_adbc", return_value=adbc) as connect_adbc,
        patch("pymysql.connect") as connect_pymysql,
    ):
        conn = ob_mysql.connect(host="db", port=3307, database="sales", user="me", password="pw")
    assert conn.backend == "adbc"
    assert conn._native is adbc
    connect_pymysql.assert_not_called()
    assert connect_adbc.call_args.args[0] == (
        "mysql://me:pw@db:3307/sales?tls=preferred&charset=utf8mb4"
    )


def test_connect_falls_back_to_pymysql_without_the_driver() -> None:
    with (
        patch("ob_mysql._connect_adbc", return_value=None),
        patch("pymysql.connect") as connect_pymysql,
    ):
        conn = ob_mysql.connect(database="sales", user="me")
    assert conn.backend == "pymysql"
    kwargs = connect_pymysql.call_args.kwargs
    assert (kwargs["database"], kwargs["user"], kwargs["port"]) == ("sales", "me", 3306)
    assert kwargs["charset"] == "utf8mb4"


def test_connect_insisting_on_adbc_without_the_driver_says_how_to_install_it() -> None:
    with (
        patch("ob_mysql._connect_adbc", return_value=None),
        pytest.raises(InterfaceError, match="dbc install mysql"),
    ):
        ob_mysql.connect(backend="adbc")


def test_connect_insisting_on_pymysql_never_tries_adbc() -> None:
    with patch("ob_mysql._connect_adbc") as connect_adbc, patch("pymysql.connect"):
        conn = ob_mysql.connect(backend="pymysql")
    connect_adbc.assert_not_called()
    assert conn.backend == "pymysql"


def test_certificate_files_go_through_pymysql() -> None:
    """The ADBC driver's URI takes no certificate files."""
    with patch("ob_mysql._connect_adbc") as connect_adbc, patch("pymysql.connect") as connect:
        conn = ob_mysql.connect(ssl_ca="/ca.pem", ssl_cert="/c.pem")
    connect_adbc.assert_not_called()
    assert conn.backend == "pymysql"
    assert connect.call_args.kwargs["ssl"] == {"ca": "/ca.pem", "cert": "/c.pem"}
    with pytest.raises(NotSupportedError, match="certificate"):
        ob_mysql.connect(ssl_ca="/ca.pem", backend="adbc")


def test_the_uri_quotes_credentials() -> None:
    uri = ob_mysql._adbc_uri(
        host="db", port=3306, database="my db", user="a@b", password="p:w/d", charset="utf8mb4"
    )
    assert uri == "mysql://a%40b:p%3Aw%2Fd@db:3306/my%20db?tls=preferred&charset=utf8mb4"


def test_an_adbc_connection_keeps_the_servers_time_zone() -> None:
    """The driver sets the session to UTC; the connection sets it back."""
    native = MagicMock()
    with patch("adbc_driver_manager.dbapi.connect", return_value=native) as connect:
        assert ob_mysql._connect_adbc("mysql://db/x") is native
    # ``db_kwargs`` rather than ``uri=``, which adbc-driver-manager 1.8 lacks.
    assert connect.call_args.kwargs == {"driver": "mysql", "db_kwargs": {"uri": "mysql://db/x"}}
    statement = native.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
    assert statement == "SET time_zone = @@global.time_zone"


def test_a_missing_driver_is_not_an_error_but_a_refused_login_is() -> None:
    missing = adbc_driver_manager.ProgrammingError(
        "NOT_FOUND: [Driver Manager] Could not load `mysql`",
        status_code=adbc_driver_manager.AdbcStatusCode.NOT_FOUND,
    )
    with patch("adbc_driver_manager.dbapi.connect", side_effect=missing):
        assert ob_mysql._connect_adbc("mysql://db/x") is None
    refused = adbc_driver_manager.OperationalError(
        "UNAUTHENTICATED: Access denied", status_code=adbc_driver_manager.AdbcStatusCode.IO
    )
    with (
        patch("adbc_driver_manager.dbapi.connect", side_effect=refused),
        pytest.raises(adbc_driver_manager.OperationalError),
    ):
        ob_mysql._connect_adbc("mysql://db/x")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_connect_context_manager() -> None:
    native = _pymysql_native()
    with _pymysql(native) as conn:
        assert isinstance(conn, Connection)
    native.close.assert_called_once()


def test_connection_close_is_idempotent() -> None:
    native = _pymysql_native()
    conn = _pymysql(native)
    conn.close()
    conn.close()
    native.close.assert_called_once()


def test_connection_cursor_after_close_raises() -> None:
    conn = _pymysql(_pymysql_native())
    conn.close()
    with pytest.raises(ProgrammingError, match="closed"):
        conn.cursor()


def test_connection_commit_and_rollback() -> None:
    native = _pymysql_native()
    conn = _pymysql(native)
    conn.commit()
    conn.rollback()
    native.commit.assert_called_once()
    native.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Cursor: plain SQL through PyMySQL
# ---------------------------------------------------------------------------


def test_cursor_execute_runs_the_statement() -> None:
    native = _pymysql_native(_ROWS_FIELDS, _ROWS)
    with _pymysql(native).cursor() as cur:
        assert cur.execute("SELECT 1") is cur
    native.cursor.return_value.execute.assert_called_once_with("SELECT 1", None)


def test_pymysql_escapes_its_own_parameters() -> None:
    """PyMySQL reads the session's sql_mode from the server's status flags."""
    native = _pymysql_native(_ROWS_FIELDS, _ROWS)
    with _pymysql(native).cursor() as cur:
        cur.execute("SELECT %s", ["it's"])
    native.cursor.return_value.execute.assert_called_once_with("SELECT %s", ["it's"])


def _adbc_statements(native: MagicMock) -> list[str]:
    return [c.args[0] for c in native.cursor.return_value.execute.call_args_list]


@pytest.mark.parametrize(
    ("sql_mode", "quoted"),
    [
        ("STRICT_TRANS_TABLES", "'it\\'s \\\\ \\' OR 1=1 -- '"),
        # A backslash is an ordinary character there: escaping the quote with
        # one would end the literal and let the rest run as SQL.
        ("STRICT_TRANS_TABLES,NO_BACKSLASH_ESCAPES", "'it''s \\ '' OR 1=1 -- '"),
    ],
)
def test_adbc_parameters_follow_the_sessions_sql_mode(sql_mode: str, quoted: str) -> None:
    native = _adbc_native(pa.table({"v": ["x"]}), sql_mode=sql_mode)
    Connection(native, backend="adbc").cursor().execute("SELECT %s", ["it's \\ ' OR 1=1 -- "])
    assert _adbc_statements(native) == ["SELECT @@session.sql_mode", f"SELECT {quoted}"]


def test_adbc_parameters_of_every_kind() -> None:
    native = _adbc_native(pa.table({"v": ["x"]}))
    cur = Connection(native, backend="adbc").cursor()
    cur.execute(
        "SELECT %s, %s, %s, %s, %s, %s IN %s",
        [b"\x00\x80\xff", None, 3, Decimal("1.50"), datetime.date(2024, 1, 2), 2, (1, "a")],
    )
    cur.execute("SELECT %(a)s", {"a": "x"})
    # Bytes as a hex literal: no text encoding, and no mode reads it differently.
    assert _adbc_statements(native)[1::2] == [
        "SELECT X'0080ff', NULL, 3, 1.50, '2024-01-02', 2 IN (1,'a')",
        "SELECT 'x'",
    ]


def test_cursor_description() -> None:
    fields = [_field("num", LONG), _field("txt", VAR_STRING), _field("dt", DATETIME_CODE)]
    native = _pymysql_native(fields, [])
    cur = _pymysql(native).cursor()
    assert cur.description is None
    cur.execute("SELECT 1")
    assert cur.description == (
        ("num", NUMBER, None, None, None, None, None),
        ("txt", STRING, None, None, None, None, None),
        ("dt", DATETIME, None, None, None, None, None),
    )


def test_fetch_methods_page_through_one_result() -> None:
    cur = _pymysql(_pymysql_native(_ROWS_FIELDS, _ROWS)).cursor()
    cur.execute("SELECT 1")
    assert cur.fetchone() == (1, "Alice")
    assert cur.fetchmany(1) == [(2, "Bob")]
    assert cur.fetchall() == [(3, "Carol")]
    assert cur.fetchone() is None
    cur.execute("SELECT 1")
    assert list(cur) == _ROWS


def test_fetch_arrow_table_takes_the_rows_not_yet_fetched() -> None:
    cur = _pymysql(_pymysql_native(_ROWS_FIELDS, _ROWS)).cursor()
    cur.execute("SELECT 1")
    cur.fetchone()
    table = cur.fetch_arrow_table()
    assert table.to_pylist() == [{"id": 2, "name": "Bob"}, {"id": 3, "name": "Carol"}]
    assert table.schema.field("id").type == pa.int32()
    assert cur.fetchall() == []


def test_fetch_arrow_table_empty_keeps_the_columns() -> None:
    cur = _pymysql(_pymysql_native(_ROWS_FIELDS, [])).cursor()
    cur.execute("SELECT 1")
    table = cur.fetch_arrow_table()
    assert table.num_rows == 0
    assert table.schema.names == ["id", "name"]
    assert table.schema.field("name").type == pa.string()


def test_a_statement_without_a_result_set() -> None:
    native = _pymysql_native(None, rowcount=2, lastrowid=42)
    cur = _pymysql(native).cursor()
    cur.execute("INSERT INTO t VALUES (1), (2)")
    assert (cur.description, cur.rowcount, cur.lastrowid) == (None, 2, 42)
    assert cur.fetchall() == []
    with pytest.raises(ProgrammingError, match="No result set"):
        cur.fetch_arrow_table()


def test_cursor_close_then_fetch_raises() -> None:
    cur = _pymysql(_pymysql_native()).cursor()
    cur.close()
    with pytest.raises(ProgrammingError, match="closed"):
        cur.fetchone()


def test_cursor_executemany_plain_sql() -> None:
    native = _pymysql_native(None)
    cur = _pymysql(native).cursor()
    cur.executemany("INSERT INTO t VALUES (%s, %s)", [(1, "a"), (2, "b")])
    calls = [c.args for c in native.cursor.return_value.execute.call_args_list]
    assert calls == [("INSERT INTO t VALUES (%s, %s)", p) for p in [(1, "a"), (2, "b")]]


def test_cursor_executemany_obml_raises() -> None:
    cur = _pymysql(_pymysql_native()).cursor()
    obml = "select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n"
    with pytest.raises(NotSupportedError, match="executemany"):
        cur.executemany(obml, [])


def test_cursor_setinputsizes_and_setoutputsize_are_noops() -> None:
    cur = _pymysql(_pymysql_native()).cursor()
    cur.setinputsizes([])
    cur.setoutputsize(1000)


# ---------------------------------------------------------------------------
# Cursor: ADBC
# ---------------------------------------------------------------------------


def test_adbc_result_is_normalized_and_paged() -> None:
    table = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "amount": pa.array([1, 2], pa.decimal128(9, 2)).cast(pa.decimal64(9, 2)),
        }
    )
    native = _adbc_native(table)
    cur = Connection(native, backend="adbc").cursor()
    cur.execute("SELECT id, amount FROM t")
    native_cursor = native.cursor.return_value
    native_cursor.adbc_statement.set_options.assert_called_once_with(
        **{"mysql.query.zero_datetime_behavior": "convert_to_null"}
    )
    assert cur.description is not None
    assert [d[1] for d in cur.description] == [NUMBER, NUMBER]
    assert cur.rowcount == 2
    assert cur.fetchone() is not None
    remaining = cur.fetch_arrow_table()
    assert remaining.num_rows == 1
    assert remaining.schema.field("amount").type == pa.decimal128(9, 2)


def test_adbc_reports_a_result_it_cannot_read() -> None:
    """ADBC refuses a TIME outside a day. Reading it again through another
    client would read it outside this session (without its uncommitted rows,
    temporary tables or variables), so the failure is reported instead."""
    error = adbc_driver_manager.OperationalError(
        "UNKNOWN: [mysql] failed to append value to column 0: hour out of range",
        status_code=adbc_driver_manager.AdbcStatusCode.UNKNOWN,
    )
    conn = Connection(_adbc_native(pa.table({"t": [1]}), error=error), backend="adbc")
    with pytest.raises(DataError, match="backend='pymysql'") as raised:
        conn.cursor().execute("SELECT t FROM durations")
    assert raised.value.__cause__ is error


def test_adbc_other_errors_are_raised() -> None:
    error = adbc_driver_manager.OperationalError(
        "IO: connection lost", status_code=adbc_driver_manager.AdbcStatusCode.IO
    )
    conn = Connection(_adbc_native(pa.table({"t": [1]}), error=error), backend="adbc")
    with pytest.raises(adbc_driver_manager.OperationalError, match="connection lost"):
        conn.cursor().execute("SELECT 1")


def test_adbc_statement_without_a_result_set() -> None:
    native = _adbc_native(None)
    cur = Connection(native, backend="adbc").cursor()
    cur.execute("CREATE TABLE t (i INT)")
    assert cur.description is None
    native.cursor.return_value.fetch_arrow_table.assert_not_called()


# ---------------------------------------------------------------------------
# Cursor: OBML queries (mocked REST API)
# ---------------------------------------------------------------------------


def test_obml_compile_and_execute() -> None:
    """OBML query is compiled via REST API then executed on MySQL."""
    compiled_sql = "SELECT `region`, SUM(`amount`) AS `revenue` FROM `orders` GROUP BY `region`"
    fields = [_field("region", VAR_STRING), _field("revenue", DOUBLE)]
    native = _pymysql_native(fields, [("EMEA", 300.0), ("APAC", 150.0), ("AMER", 550.0)])
    conn = _pymysql(native)
    with patch("httpx.post", return_value=_mock_api_response(compiled_sql)), conn.cursor() as cur:
        cur.execute("select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n")
        assert len(cur.fetchall()) == 3
    native.cursor.return_value.execute.assert_called_once_with(compiled_sql, None)


def test_obml_rest_dialect_is_mysql() -> None:
    conn = _pymysql(_pymysql_native(_ROWS_FIELDS, _ROWS))
    with (
        patch("httpx.post", return_value=_mock_api_response("SELECT 1")) as mock_post,
        conn.cursor() as cur,
    ):
        cur.execute("select:\n  measures:\n    - Revenue\n")
    assert "/v1/query/sql" in mock_post.call_args.args[0]
    assert mock_post.call_args.kwargs["params"] == {"dialect": "mysql"}


def test_obml_custom_api_url() -> None:
    conn = _pymysql(_pymysql_native(_ROWS_FIELDS, _ROWS), ob_api_url="http://my-api:9000")
    with (
        patch("httpx.post", return_value=_mock_api_response("SELECT 1")) as mock_post,
        conn.cursor() as cur,
    ):
        cur.execute("select:\n  measures:\n    - Revenue\n")
    assert mock_post.call_args.args[0] == "http://my-api:9000/v1/query/sql"


def test_plain_sql_passthrough() -> None:
    """Plain SQL is passed through without OBML compilation: no REST call."""
    native = _pymysql_native(_ROWS_FIELDS, _ROWS)
    conn = _pymysql(native)
    with patch("httpx.post") as mock_post, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM orders")
    mock_post.assert_not_called()
    native.cursor.return_value.execute.assert_called_once_with("SELECT COUNT(*) FROM orders", None)
