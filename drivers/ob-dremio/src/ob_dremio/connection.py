"""PEP 249 Connection wrapping ``adbc_driver_flightsql.dbapi.Connection``.

Dremio has no transactions — ``commit()`` and ``rollback()`` are no-ops
that simply verify the connection is still open. ADBC exposes both, but
calling them would ask Dremio to manage a transaction it does not have.
"""

from __future__ import annotations

from typing import Any

from ob_dremio.cursor import Cursor
from ob_dremio.exceptions import ProgrammingError


class Connection:
    """DB-API 2.0 connection that wraps an ADBC Flight SQL connection.

    Dremio serves Arrow Flight SQL natively, so results arrive as Arrow and
    cursors expose ``fetch_arrow_table()`` without a conversion hop.
    OBML queries are compiled to SQL via the OrionBelt REST API
    (single-model mode, ``/v1/query/sql`` shortcut).
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
        """Return a new Cursor bound to this connection.

        The native statement is created per execution rather than per cursor
        — see :meth:`Cursor._execute_sql` for why reuse is unsafe against
        Dremio.
        """
        self._check_open()
        return Cursor(
            self._native,
            ob_api_url=self._ob_api_url,
            ob_timeout=self._ob_timeout,
        )

    def commit(self) -> None:
        """No-op — Dremio has no transactions."""
        self._check_open()

    def rollback(self) -> None:
        """No-op — Dremio has no transactions."""
        self._check_open()

    def close(self) -> None:
        """Close the connection and the underlying ADBC connection."""
        if not self._closed:
            self._native.close()
            self._closed = True

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
