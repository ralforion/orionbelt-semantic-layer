"""PEP 249 Connection over the ADBC MySQL driver or PyMySQL."""

from __future__ import annotations

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
from ob_mysql.exceptions import DataError, ProgrammingError

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


def _literal(value: Any, charset: str, no_backslash_escapes: bool) -> str:
    """*value* as a MySQL literal, for a statement the ADBC driver is given
    finished (its own parameter binding mangles text, bytes and durations).

    Text is escaped the way the session's ``sql_mode`` reads it: with
    ``NO_BACKSLASH_ESCAPES`` a backslash is an ordinary character and only a
    quote is doubled. Bytes are a hex literal, which no mode reads differently
    and which needs no text encoding. Everything else (numbers, dates,
    decimals, durations) is PyMySQL's rendering, made of digits and
    separators only.
    """
    from pymysql.converters import escape_item, escape_string

    if isinstance(value, str):
        if no_backslash_escapes:
            return "'" + value.replace("'", "''") + "'"
        return "'" + str(escape_string(value)) + "'"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "(" + ",".join(_literal(v, charset, no_backslash_escapes) for v in value) + ")"
    return str(escape_item(value, charset))


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
    ) -> None:
        self._native = native
        self.backend: Backend = backend
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout
        self._charset = charset

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("Connection is closed.")

    def cursor(self) -> Cursor:
        """Return a new Cursor for this connection."""
        self._check_open()
        return Cursor(self, ob_api_url=self._ob_api_url, ob_timeout=self._ob_timeout)

    def _execute(self, sql: str, parameters: Any = None) -> _Result:
        """Run *sql* with its ``format``-style *parameters* and read its whole
        result."""
        self._check_open()
        if self.backend == "pymysql":
            return self._execute_pymysql(sql, parameters)
        import adbc_driver_manager

        if parameters is not None:
            sql = self._interpolate(sql, parameters)
        cursor = self._native.cursor()
        try:
            cursor.adbc_statement.set_options(**_ZERO_DATETIME_OPTION)
            cursor.execute(sql)
            if not cursor.description:
                return _Result(names=[], type_objects=[], rowcount=cursor.rowcount)
            try:
                table = cursor.fetch_arrow_table()
            except adbc_driver_manager.Error as error:
                if _UNREADABLE_VALUE not in str(error):
                    raise
                # Reading it through another client would read it outside
                # this session: without its uncommitted rows, temporary tables
                # or variables.
                raise DataError(
                    f"The ADBC MySQL driver cannot read this result ({error}). A TIME "
                    "outside 00:00:00-23:59:59 is the known case: select it as "
                    "TIME_TO_SEC(...) or as text, or connect with backend='pymysql'."
                ) from error
            return _Result.from_table(normalize_adbc(table))
        finally:
            cursor.close()

    def _execute_pymysql(self, sql: str, parameters: Any) -> _Result:
        # PyMySQL escapes the parameters itself, as the session's sql_mode
        # reads them.
        cursor = self._native.cursor()
        try:
            cursor.execute(sql, parameters)
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
        """*sql* with ``%s`` / ``%(name)s`` *parameters* written into it as
        literals, as PyMySQL does on the client: see :func:`_literal`.

        The session's ``sql_mode`` is read for every statement: a ``SET`` in
        between may have changed how it reads a backslash.
        """
        cursor = self._native.cursor()
        try:
            cursor.execute("SELECT @@session.sql_mode")
            (mode,) = cursor.fetchone()
        finally:
            cursor.close()
        plain = "NO_BACKSLASH_ESCAPES" in str(mode).upper().split(",")

        def literal(value: Any) -> str:
            return _literal(value, self._charset, plain)

        escaped: Any
        if isinstance(parameters, dict):
            escaped = {k: literal(v) for k, v in parameters.items()}
        elif isinstance(parameters, (list, tuple)):
            escaped = tuple(literal(v) for v in parameters)
        else:
            escaped = literal(parameters)
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
            self._native.close()
            self._closed = True

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
