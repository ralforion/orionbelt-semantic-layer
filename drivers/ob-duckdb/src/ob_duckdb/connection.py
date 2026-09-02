"""PEP 249 Connection wrapping ``duckdb.DuckDBPyConnection``."""

from __future__ import annotations

import contextlib

import duckdb

from ob_duckdb.cursor import Cursor
from ob_duckdb.exceptions import ProgrammingError


class Connection:
    """DB-API 2.0 connection that wraps a native DuckDB connection.

    OBML queries are compiled to SQL via the OrionBelt REST API
    (single-model mode, ``/v1/query/sql`` shortcut).
    """

    def __init__(
        self,
        native: duckdb.DuckDBPyConnection,
        *,
        ob_api_url: str = "http://localhost:8000",
        ob_timeout: int = 30,
    ) -> None:
        self._native = native
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Connection is closed.")

    def cursor(self) -> Cursor:
        """Return a new Cursor for this connection."""
        self._check_open()
        native_cursor = self._native.cursor()
        return Cursor(
            native_cursor,
            ob_api_url=self._ob_api_url,
            ob_timeout=self._ob_timeout,
        )

    def commit(self) -> None:
        """Commit — DuckDB auto-commits by default, so this is usually a no-op."""
        self._check_open()
        self._native.commit()

    def rollback(self) -> None:
        """Rollback the current transaction.

        No-op if no transaction is active (DuckDB auto-commits by default).
        """
        self._check_open()
        # No active transaction is not an error here: DuckDB auto-commits, so
        # a rollback with nothing open is the ordinary case.
        with contextlib.suppress(duckdb.TransactionException):
            self._native.rollback()

    def close(self) -> None:
        """Close the connection."""
        if not self._closed:
            self._native.close()
            self._closed = True

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
