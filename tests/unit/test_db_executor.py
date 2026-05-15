"""Unit tests for the db_executor service module."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionResult,
    ExecutionUnavailableError,
    _map_type_code,
    _serialize_row,
    _serialize_value,
    execute_sql,
)

_has_ob_driver_core = importlib.util.find_spec("ob_driver_core") is not None
_has_ob_flight = importlib.util.find_spec("ob_flight") is not None


class TestSerializeValue:
    def test_none(self) -> None:
        assert _serialize_value(None) is None

    def test_string(self) -> None:
        assert _serialize_value("hello") == "hello"

    def test_int(self) -> None:
        assert _serialize_value(42) == 42

    def test_float(self) -> None:
        assert _serialize_value(3.14) == 3.14

    def test_bool(self) -> None:
        assert _serialize_value(True) is True

    def test_datetime(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, 0)
        assert _serialize_value(dt) == "2024-01-15T10:30:00"

    def test_date(self) -> None:
        d = date(2024, 6, 1)
        assert _serialize_value(d) == "2024-06-01"

    def test_decimal(self) -> None:
        assert _serialize_value(Decimal("99.95")) == 99.95

    def test_bytes(self) -> None:
        assert _serialize_value(b"\x00\x01\x02") == "AAEC"

    def test_other_type(self) -> None:
        assert _serialize_value({"key": "val"}) == "{'key': 'val'}"


class TestSerializeRow:
    def test_mixed_row(self) -> None:
        row = ("US", 42, Decimal("100.5"), None, datetime(2024, 1, 1))
        result = _serialize_row(row)
        assert result == ["US", 42, 100.5, None, "2024-01-01T00:00:00"]


@pytest.mark.skipif(not _has_ob_driver_core, reason="ob_driver_core not installed")
class TestMapTypeCode:
    def test_number_type(self) -> None:
        from ob_driver_core.type_codes import NUMBER

        assert _map_type_code(NUMBER) == "number"

    def test_string_type(self) -> None:
        from ob_driver_core.type_codes import STRING

        assert _map_type_code(STRING) == "string"

    def test_datetime_type(self) -> None:
        from ob_driver_core.type_codes import DATETIME

        assert _map_type_code(DATETIME) == "datetime"

    def test_binary_type(self) -> None:
        from ob_driver_core.type_codes import BINARY

        assert _map_type_code(BINARY) == "binary"

    def test_unknown_defaults_to_string(self) -> None:
        assert _map_type_code("unknown") == "string"

    def test_none_defaults_to_string(self) -> None:
        assert _map_type_code(None) == "string"


def _mock_get_connection(mock_conn: MagicMock):
    """Create a mock context manager for get_connection."""

    @contextmanager
    def _cm(dialect: str, **kw):  # noqa: ARG001
        yield mock_conn

    return _cm


class TestExecuteSql:
    _needs_ob_flight = pytest.mark.skipif(not _has_ob_flight, reason="ob_flight not installed")

    def test_import_error_raises_unavailable(self) -> None:
        with (
            patch.dict("sys.modules", {"ob_flight": None, "ob_flight.db_router": None}),
            pytest.raises(ExecutionUnavailableError, match="ob-flight-extension"),
        ):
            execute_sql("SELECT 1", dialect="duckdb")

    @_needs_ob_flight
    def test_successful_execution(self) -> None:
        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("country", "STRING", None, None, None, None, None),
            ("revenue", "NUMBER", None, None, None, None, None),
        ]
        mock_cursor.fetchall.return_value = [("US", 100), ("UK", 200)]
        # Disable Arrow path — force PEP 249 fetchall
        del mock_cursor.fetch_arrow_table

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # Use postgres dialect (duckdb now takes the native _execute_duckdb path)
        with patch(
            "ob_flight.db_router.get_connection",
            side_effect=_mock_get_connection(mock_conn),
            create=True,
        ):
            result = execute_sql("SELECT country, revenue FROM t", dialect="postgres")

        assert isinstance(result, ExecutionResult)
        assert result.row_count == 2
        assert len(result.columns) == 2
        assert result.columns[0].name == "country"
        assert result.rows == [["US", 100], ["UK", 200]]
        assert result.execution_time_ms >= 0
        mock_cursor.close.assert_called_once()

    @_needs_ob_flight
    def test_duckdb_direct_execution(self) -> None:
        """DuckDB uses a direct native path (no get_connection)."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [("US", 100)]
        mock_result.description = [("country",), ("revenue",)]

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        with (
            patch(
                "ob_flight.db_router.get_credentials",
                return_value={"database": ":memory:"},
                create=True,
            ),
            patch("duckdb.connect", return_value=mock_conn),
        ):
            result = execute_sql("SELECT 1", dialect="duckdb")

        assert isinstance(result, ExecutionResult)
        assert result.row_count == 1
        assert result.rows == [["US", 100]]
        mock_conn.close.assert_called_once()

    @_needs_ob_flight
    def test_db_error_raises_execution_error(self) -> None:
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = RuntimeError("connection refused")
        del mock_cursor.fetch_arrow_table
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with (
            patch(
                "ob_flight.db_router.get_connection",
                side_effect=_mock_get_connection(mock_conn),
                create=True,
            ),
            pytest.raises(ExecutionError, match="connection refused"),
        ):
            execute_sql("SELECT 1", dialect="postgres")

    @_needs_ob_flight
    def test_unsupported_dialect_raises_unavailable(self) -> None:
        def _raise_key_error(dialect: str, **kw):  # noqa: ARG001
            raise KeyError("Unsupported dialect: 'mysql'")

        with (
            patch(
                "ob_flight.db_router.get_connection",
                side_effect=_raise_key_error,
                create=True,
            ),
            pytest.raises(ExecutionUnavailableError, match="mysql"),
        ):
            execute_sql("SELECT 1", dialect="mysql")

    @_needs_ob_flight
    def test_cursor_closed_on_error(self) -> None:
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = RuntimeError("boom")
        del mock_cursor.fetch_arrow_table
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with (
            patch(
                "ob_flight.db_router.get_connection",
                side_effect=_mock_get_connection(mock_conn),
                create=True,
            ),
            pytest.raises(ExecutionError),
        ):
            execute_sql("SELECT 1", dialect="postgres")

        mock_cursor.close.assert_called_once()


class TestExecutionResult:
    def test_lazy_rows_from_raw(self) -> None:
        from orionbelt.service.db_executor import ColumnMeta

        result = ExecutionResult(
            columns=[ColumnMeta("a", "string")],
            raw_rows=[["x"], ["y"]],
            row_count=2,
        )
        assert result.rows == [["x"], ["y"]]

    def test_empty_result(self) -> None:
        result = ExecutionResult(columns=[], row_count=0)
        assert result.rows == []
