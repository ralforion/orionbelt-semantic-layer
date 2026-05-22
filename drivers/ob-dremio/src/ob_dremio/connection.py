"""PEP 249 Connection wrapping a pyarrow Flight client for Dremio.

Dremio has no transactions — ``commit()`` and ``rollback()`` are no-ops
that simply verify the connection is still open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ob_dremio.cursor import Cursor
from ob_dremio.exceptions import ProgrammingError

if TYPE_CHECKING:
    import pyarrow.flight  # type: ignore[import-untyped]


class Connection:
    """DB-API 2.0 connection wrapping a pyarrow FlightClient for Dremio.

    OBML queries are compiled to SQL via the OrionBelt REST API
    (single-model mode, ``/v1/query/sql`` shortcut).
    """

    def __init__(
        self,
        client: pyarrow.flight.FlightClient,
        *,
        call_options: pyarrow.flight.FlightCallOptions | None = None,
        ob_api_url: str = "http://localhost:8000",
        ob_timeout: int = 30,
    ) -> None:
        self._client = client
        self._call_options = call_options
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Connection is closed.")

    def cursor(self) -> Cursor:
        """Return a new Cursor backed by the shared FlightClient.

        pyarrow FlightClient does not have its own cursor concept — our
        ``Cursor`` wraps the client directly.
        """
        self._check_open()
        return Cursor(
            self._client,
            call_options=self._call_options,
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
        """Close the connection and the underlying FlightClient."""
        if not self._closed:
            self._client.close()
            self._closed = True

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
