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
from ob_mysql.exceptions import DataError, NotSupportedError, ProgrammingError

if TYPE_CHECKING:
    from collections.abc import Callable

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


#: The ADBC driver's message when the server will not prepare a statement, and
#: the server's errors for one it cannot prepare (``SHOW ... LIKE ?``): 1064
#: syntax, 1295 not supported by the prepared-statement protocol.
_PREPARE_FAILED = "failed to prepare statement"
_UNPREPARABLE = ("Error 1064 ", "Error 1295 ")


def _mode_dependent(text: str) -> bool:
    """Whether quoting *text* depends on the ``sql_mode``: a backslash escapes,
    or under NO_BACKSLASH_ESCAPES does not, and a NUL is the escape ``\\0`` or
    else has no quoted form (the ADBC driver refuses it raw)."""
    return "\\" in text or "\x00" in text


def _render(sql: str, parameters: Any, scalar: Callable[[Any], str]) -> str:
    """*sql* with its ``%s`` / ``%(name)s`` *parameters* written into it, each
    value by *scalar*. A list, tuple or set is ``(a,b)``, its elements written
    by *scalar* too, at any depth: PyMySQL escapes the elements of one with
    backslashes whatever the ``sql_mode``."""

    def literal(value: Any) -> str:
        if isinstance(value, (list, tuple, set, frozenset)):
            return "(" + ",".join(literal(v) for v in value) + ")"
        return scalar(value)

    if isinstance(parameters, dict):
        return str(sql % _Named(parameters, literal))
    if isinstance(parameters, (list, tuple)):
        return str(sql % tuple(literal(v) for v in parameters))
    return str(sql % literal(parameters))


def _adbc_literal(text: Callable[[str], str]) -> Callable[[Any], str]:
    """How one value is written for the ADBC driver, whose own binding mangles
    durations.

    Text is a quoted literal, as PyMySQL writes it, so it takes the session's
    collation and goes wherever a quoted string does (``SHOW ... LIKE``). A
    quote is doubled, which every ``sql_mode`` reads alike; text holding a
    backslash or a NUL is written by *text*. Bytes are a hex literal.
    Everything else (numbers, dates, decimals, durations) is PyMySQL's
    rendering, made of digits and separators only.
    """
    from pymysql.converters import escape_item

    def scalar(value: Any) -> str:
        if isinstance(value, str):
            return text(value) if _mode_dependent(value) else "'" + value.replace("'", "''") + "'"
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "X'" + bytes(value).hex() + "'"
        return str(escape_item(value, "utf8mb4"))

    return scalar


class _Named:
    """Named parameters, rendered as ``%`` looks them up: in the statement's
    order, so the placeholders a renderer leaves are bound in that order."""

    def __init__(self, values: dict[str, Any], literal: Callable[[Any], str]) -> None:
        self._values = values
        self._literal = literal

    def __getitem__(self, key: str) -> str:
        return self._literal(self._values[key])


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
    ) -> None:
        self._native = native
        self.backend: Backend = backend
        self._closed = False
        self._ob_api_url = ob_api_url
        self._ob_timeout = ob_timeout

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
        if parameters is None:
            return self._execute_adbc(sql)
        # Text whose quoting depends on the sql_mode is bound by the server,
        # which reads it alike under every mode: nothing to keep track of, and
        # no query before the statement to replace ROW_COUNT() / FOUND_ROWS().
        bound: list[str] = []

        def bind(text: str) -> str:
            bound.append(text)
            return "?"

        statement = _render(sql, parameters, _adbc_literal(bind))
        if not bound:
            return self._execute_adbc(statement)
        import adbc_driver_manager

        try:
            return self._execute_adbc(statement, tuple(bound))
        except adbc_driver_manager.Error as error:
            message = str(error)
            if _PREPARE_FAILED not in message or not any(e in message for e in _UNPREPARABLE):
                raise
        # A statement the server cannot prepare, such as SHOW ... LIKE: the
        # text is quoted for the mode the session is in now. Asking replaces
        # ROW_COUNT() and FOUND_ROWS(), which no such statement reads.
        return self._execute_adbc(
            _render(sql, parameters, _adbc_literal(self._quote_for_session()))
        )

    def _quote_for_session(self) -> Callable[[str], str]:
        """How the session reads a quoted literal now: with backslash escapes,
        as PyMySQL quotes, or under NO_BACKSLASH_ESCAPES, with only the quote
        doubled (where a NUL has no quoted form)."""
        from pymysql.converters import escape_string

        cursor = self._native.cursor()
        try:
            cursor.execute("SELECT @@session.sql_mode")
            (mode,) = cursor.fetchone()
        finally:
            cursor.close()
        if "NO_BACKSLASH_ESCAPES" not in str(mode).upper().split(","):
            return lambda text: "'" + str(escape_string(text)) + "'"

        def quote(text: str) -> str:
            if "\x00" in text:
                raise NotSupportedError(
                    "A NUL in a parameter of a statement MySQL cannot prepare (such as "
                    "SHOW ... LIKE) has no quoted form under NO_BACKSLASH_ESCAPES: "
                    "connect with backend='pymysql'."
                )
            return "'" + text.replace("'", "''") + "'"

        return quote

    def _execute_adbc(self, sql: str, bound: tuple[str, ...] | None = None) -> _Result:
        import adbc_driver_manager

        cursor = self._native.cursor()
        try:
            cursor.adbc_statement.set_options(**_ZERO_DATETIME_OPTION)
            cursor.execute(sql, bound)
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
        # PyMySQL writes a value as the session's sql_mode reads it (it follows
        # the server's status flags), but not the elements of a sequence.
        statement = sql if parameters is None else _render(sql, parameters, self._native.literal)
        cursor = self._native.cursor()
        try:
            cursor.execute(statement)
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
