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


#: Characters whose quoted form depends on the ``sql_mode``: a backslash
#: escapes, or under NO_BACKSLASH_ESCAPES does not, and a NUL is the escape
#: ``\\0`` or else no quoted form at all (the ADBC driver refuses it raw).
_MODE_DEPENDENT = ("\\", "\x00")


def _literal(value: Any, charset: str, backslash_escapes: bool) -> str:
    """*value* as a MySQL literal, for a statement the ADBC driver is given
    finished (its own parameter binding mangles text, bytes and durations).

    Text is a quoted literal, as PyMySQL writes it, so it takes the session's
    collation and goes wherever a quoted string does (``SHOW ... LIKE``). A
    quote is doubled, which every ``sql_mode`` reads alike; a backslash and a
    NUL are escaped only where *backslash_escapes* says the session reads
    escapes. Without escapes a NUL has no quoted form: the text is the quoted
    pieces around it joined by ``CHAR(0 USING <charset>)``, which keeps the
    session's collation and a literal's coercibility. Bytes are a hex literal.
    Everything else (numbers, dates, decimals, durations) is PyMySQL's
    rendering, made of digits and separators only.
    """
    from pymysql.converters import escape_item, escape_string

    if isinstance(value, str):
        if backslash_escapes:
            return "'" + str(escape_string(value)) + "'"
        pieces = ["'" + piece.replace("'", "''") + "'" for piece in value.split("\x00")]
        if len(pieces) == 1:
            return pieces[0]
        return "CONCAT(" + f", CHAR(0 USING {charset}), ".join(pieces) + ")"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "(" + ",".join(_literal(v, charset, backslash_escapes) for v in value) + ")"
    return str(escape_item(value, charset))


def _mode_dependent(value: Any) -> bool:
    """Whether *value*, or any text within it, holds a backslash or a NUL."""
    if isinstance(value, str):
        return any(c in value for c in _MODE_DEPENDENT)
    if isinstance(value, dict):
        return any(_mode_dependent(v) for v in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_mode_dependent(v) for v in value)
    return False


def _sets_sql_mode(sql: str) -> bool:
    """Whether *sql* may change the session's ``sql_mode``: ``SET [SESSION]
    sql_mode``, ``SET @@sql_mode``, a ``SET_VAR`` hint. A stored routine runs
    under its own mode and gives the caller's back."""
    return "sql_mode" in sql.lower()


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
        # How the session reads a backslash in a quoted literal, kept current
        # without a query before each statement: one would replace what
        # ROW_COUNT() and FOUND_ROWS() report. Read now, and again after any
        # statement that may change it.
        self._backslash_escapes = True
        if backend == "adbc":
            self._read_sql_mode()

    def _read_sql_mode(self) -> None:
        cursor = self._native.cursor()
        try:
            cursor.execute("SELECT @@session.sql_mode")
            (mode,) = cursor.fetchone()
        finally:
            cursor.close()
        self._backslash_escapes = "NO_BACKSLASH_ESCAPES" not in str(mode).upper().split(",")

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
        statement = sql if parameters is None else self._interpolate(sql, parameters)
        result = self._execute_adbc(statement)
        if _sets_sql_mode(sql):
            self._read_sql_mode()
        return result

    def _execute_adbc(self, sql: str) -> _Result:
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
        literals, as PyMySQL does on the client: see :func:`_literal`."""
        if _sets_sql_mode(sql) and _mode_dependent(parameters):
            # A SET_VAR hint changes the mode for this statement alone, after
            # its literals are written.
            raise NotSupportedError(
                "A text parameter holding a backslash or NUL, in a statement that sets "
                "sql_mode itself, cannot be quoted for the mode it runs under: connect "
                "with backend='pymysql'."
            )

        def literal(value: Any) -> str:
            return _literal(value, self._charset, self._backslash_escapes)

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
