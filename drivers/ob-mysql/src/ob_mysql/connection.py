"""PEP 249 Connection over the ADBC MySQL driver or PyMySQL."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ob_mysql.arrow_types import (
    Column,
    coerce_value,
    normalize_adbc,
    table_from_rows,
    type_object_for,
)
from ob_mysql.cursor import Cursor
from ob_mysql.exceptions import ProgrammingError

if TYPE_CHECKING:
    import pyarrow as pa

Backend = Literal["adbc", "pymysql"]

#: Statement option: read a zero date (``'0000-00-00'``) as NULL rather than
#: failing the result, as PyMySQL's path does (see
#: :func:`~ob_mysql.arrow_types.coerce_value`). A connection option is ignored.
_ZERO_DATETIME_OPTION = {"mysql.query.zero_datetime_behavior": "convert_to_null"}

#: The ADBC driver's message for a value it cannot put into the column's Arrow
#: type: in practice a TIME outside 00:00:00-23:59:59, which it reads as a
#: clock time rather than as MySQL's signed interval of up to 838 hours.
_UNREADABLE_VALUE = "failed to append value"

#: First keywords of a statement that only reads, so running it again through
#: PyMySQL changes nothing.
_READS = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "TABLE", "VALUES"})


def _only_reads(sql: str) -> bool:
    words = sql.lstrip(" \t\r\n(").split(None, 1)
    return bool(words) and words[0].upper() in _READS


@dataclass
class _Result:
    """One statement's result, fetched whole: an Arrow table from ADBC, rows
    from PyMySQL. Each form is built from the other on demand."""

    names: list[str]
    type_objects: list[object]
    rowcount: int = -1
    lastrowid: int | None = None
    table: pa.Table | None = None
    rows: list[tuple[Any, ...]] | None = None
    columns: list[Column] = field(default_factory=list)

    @classmethod
    def from_table(cls, table: pa.Table) -> _Result:
        return cls(
            names=table.column_names,
            type_objects=[type_object_for(f.type) for f in table.schema],
            rowcount=table.num_rows,
            table=table,
        )

    def all_rows(self) -> list[tuple[Any, ...]]:
        if self.rows is None:
            assert self.table is not None
            values = [column.to_pylist() for column in self.table.columns]
            self.rows = list(zip(*values, strict=True)) if values else []
        return self.rows

    def arrow_from(self, start: int) -> pa.Table:
        if self.table is not None:
            return self.table.slice(start)
        return table_from_rows(self.all_rows()[start:], self.columns)


class Connection:
    """DB-API 2.0 connection over the ADBC MySQL driver or PyMySQL.

    OBML queries are compiled to MySQL SQL via the OrionBelt REST API. Both
    clients return one Arrow schema for a result (see
    :mod:`ob_mysql.arrow_types`); :attr:`backend` says which one this is.
    """

    def __init__(
        self,
        native: Any,
        *,
        backend: Backend,
        ob_api_url: str = "http://localhost:8000",
        ob_timeout: int = 30,
        charset: str = "utf8mb4",
        fallback: Callable[[], Any] | None = None,
    ) -> None:
        self._native = native
        self.backend: Backend = backend
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout
        self._charset = charset
        # Opens a PyMySQL connection to the same server, for a result ADBC
        # cannot read; opened on first use and kept.
        self._fallback = fallback
        self._fallback_native: Any = None

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Connection is closed.")

    def cursor(self) -> Cursor:
        """Return a new Cursor for this connection."""
        self._check_open()
        return Cursor(self, ob_api_url=self._ob_api_url, ob_timeout=self._ob_timeout)

    def _execute(self, sql: str) -> _Result:
        """Run *sql* and read its whole result."""
        self._check_open()
        if self.backend == "pymysql":
            return self._execute_pymysql(self._native, sql)
        import adbc_driver_manager

        cursor = self._native.cursor()
        try:
            cursor.adbc_statement.set_options(**_ZERO_DATETIME_OPTION)
            cursor.execute(sql)
            if not cursor.description:
                return _Result(names=[], type_objects=[], rowcount=cursor.rowcount)
            try:
                table = cursor.fetch_arrow_table()
            except adbc_driver_manager.Error as error:
                if (
                    _UNREADABLE_VALUE not in str(error)
                    or self._fallback is None
                    or not _only_reads(sql)
                ):
                    raise
                if self._fallback_native is None:
                    self._fallback_native = self._fallback()
                return self._execute_pymysql(self._fallback_native, sql)
            return _Result.from_table(normalize_adbc(table))
        finally:
            cursor.close()

    @staticmethod
    def _execute_pymysql(native: Any, sql: str) -> _Result:
        cursor = native.cursor()
        try:
            cursor.execute(sql)
            if cursor.description is None:
                return _Result(
                    names=[],
                    type_objects=[],
                    rowcount=cursor.rowcount,
                    lastrowid=cursor.lastrowid,
                )
            # The column-definition packets: ``description`` drops the flags
            # (UNSIGNED, SET) and the character set the Arrow type needs.
            columns = [Column.from_pymysql(f) for f in cursor._result.fields]
            rows = [
                tuple(coerce_value(v, c) for v, c in zip(row, columns, strict=True))
                for row in cursor.fetchall()
            ]
            return _Result(
                names=[c.name for c in columns],
                type_objects=[c.type_object for c in columns],
                rowcount=cursor.rowcount,
                lastrowid=cursor.lastrowid,
                rows=rows,
                columns=columns,
            )
        finally:
            cursor.close()

    def _interpolate(self, sql: str, parameters: Any) -> str:
        """*sql* with ``%s`` / ``%(name)s`` *parameters* escaped into it.

        PyMySQL binds parameters on the client this way; ADBC is given the
        finished statement, so both backends take the ``format`` paramstyle.
        """
        from pymysql.converters import escape_item

        escaped: Any
        if isinstance(parameters, dict):
            escaped = {k: escape_item(v, self._charset) for k, v in parameters.items()}
        elif isinstance(parameters, (list, tuple)):
            escaped = tuple(escape_item(v, self._charset) for v in parameters)
        else:
            escaped = escape_item(parameters, self._charset)
        return str(sql % escaped)

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
            if self._fallback_native is not None:
                self._fallback_native.close()
            self._native.close()
            self._closed = True

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
