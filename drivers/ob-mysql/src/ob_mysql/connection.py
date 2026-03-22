"""PEP 249 Connection wrapping ``mysql.connector.connection``."""

from __future__ import annotations

from typing import Any

from ob_mysql.cursor import Cursor
from ob_mysql.exceptions import ProgrammingError


class Connection:
    """DB-API 2.0 connection that wraps a mysql-connector-python connection.

    OBML queries are compiled to MySQL SQL via the OrionBelt REST API.
    ``fetch_arrow_table()`` is emulated by fetching rows and converting
    to a PyArrow Table (mysql-connector-python has no native Arrow support).
    """

    def __init__(
        self,
        native: Any,
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
        """Commit the current transaction."""
        self._check_open()
        self._native.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        self._check_open()
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
