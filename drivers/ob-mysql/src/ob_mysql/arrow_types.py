"""One Arrow schema for a MySQL result, whichever client read it.

The driver reads MySQL through the ADBC MySQL driver when it is installed and
through PyMySQL otherwise (see :mod:`ob_mysql`). The two disagree on a handful
of types, so both are brought to the same schema here, and a column's type is
never a property of the rows that happened to come back:

=====================  ==========================  ===========================
MySQL                  ADBC returns                Both report
=====================  ==========================  ===========================
``DECIMAL(p, s)``      decimal32/64/128/256(p, s)  decimal128(p, s), or
                                                   decimal256 past 38 digits
``DATETIME``           timestamp[s|ms|us]          timestamp[us]
``TIMESTAMP``          timestamp[...] tz=UTC       timestamp[us], as MySQL
                                                   shows it in the session
``TIME``               time32/time64               duration[us]
``YEAR``               opaque extension (string)   int16
``BIT(n)``             binary                      uint64
``JSON``               json extension (string)     string
=====================  ==========================  ===========================

TIME is a signed interval of up to 838 hours rather than a clock reading, so
it is a duration. ADBC refuses a TIME outside a day outright; the connection
reads such a result through PyMySQL instead (see
:meth:`ob_mysql.connection.Connection._execute`).

PyMySQL hands over Python values and the column-definition packet, so its
table is built here from :class:`Column` specs: the field type, the flag bits
(``UNSIGNED``, ``SET``) and the character set fix the Arrow type before a
single value is read, and a decimal takes the precision and scale MySQL
declares for it, an empty or all-NULL result included.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from ob_mysql.type_codes import BINARY, DATETIME, MYSQL_TYPE_MAP, NUMBER, STRING

# MySQL field types, from the wire protocol. ``type_codes.MYSQL_TYPE_MAP``
# groups these into PEP 249 type objects; Arrow needs the exact width.
_DECIMAL = 0
_TINY = 1
_SHORT = 2
_LONG = 3
_FLOAT = 4
_DOUBLE = 5
_NULL = 6
_TIMESTAMP = 7
_LONGLONG = 8
_INT24 = 9
_DATE = 10
_TIME = 11
_DATETIME = 12
_YEAR = 13
_NEWDATE = 14
_VARCHAR = 15
_BIT = 16
_JSON = 245
_NEWDECIMAL = 246
_ENUM = 247
_SET = 248
_TINY_BLOB = 249
_MEDIUM_BLOB = 250
_LONG_BLOB = 251
_BLOB = 252
_VAR_STRING = 253
_STRING = 254
_GEOMETRY = 255

#: ``FieldFlag.UNSIGNED``. A ``BIGINT UNSIGNED`` does not fit ``int64``, so the
#: flag decides the signedness rather than the values deciding it.
_UNSIGNED_FLAG = 32

#: ``FieldFlag.SET``. MySQL sends a SET column as ``STRING`` carrying this flag
#: rather than as the ``SET`` type code.
_SET_FLAG = 2048

#: ``charsetnr`` for the binary collation. The string and blob types are one
#: type each on the wire; this is what separates text from bytes.
_BINARY_CHARSET = 63

_SIGNED_INTS: dict[int, pa.DataType] = {
    _TINY: pa.int8(),
    _SHORT: pa.int16(),
    _INT24: pa.int32(),
    _LONG: pa.int32(),
    _LONGLONG: pa.int64(),
}

_UNSIGNED_INTS: dict[int, pa.DataType] = {
    _TINY: pa.uint8(),
    _SHORT: pa.uint16(),
    _INT24: pa.uint32(),
    _LONG: pa.uint32(),
    _LONGLONG: pa.uint64(),
}

_FIXED_TYPES: dict[int, pa.DataType] = {
    _FLOAT: pa.float32(),
    _DOUBLE: pa.float64(),
    _NULL: pa.null(),
    _DATE: pa.date32(),
    _NEWDATE: pa.date32(),
    _YEAR: pa.int16(),
    _TIME: pa.duration("us"),
    _DATETIME: pa.timestamp("us"),
    _TIMESTAMP: pa.timestamp("us"),
    _JSON: pa.string(),
    # BIT tops out at 64 bits and the column carries the UNSIGNED flag, so
    # uint64 holds every value; the bytes are read big-endian, as MySQL's own
    # ``BIN()`` and ``+ 0`` read them.
    _BIT: pa.uint64(),
    _GEOMETRY: pa.binary(),
    # ENUM and SET have their own type codes in the protocol, but MySQL does not
    # use them on the wire: both arrive as STRING with a flag. Mapped anyway so a
    # server that does send them is not pushed onto the inference path.
    _ENUM: pa.string(),
    _SET: pa.string(),
}

#: Types whose Arrow mapping depends on ``charsetnr``: text unless the column
#: carries the binary collation.
_TEXT_OR_BINARY = frozenset(
    {_VARCHAR, _VAR_STRING, _STRING, _TINY_BLOB, _MEDIUM_BLOB, _LONG_BLOB, _BLOB}
)

_DECIMALS = frozenset({_DECIMAL, _NEWDECIMAL})

#: The widest precision ``decimal128`` holds; a wider MySQL DECIMAL (up to 65
#: digits) is a ``decimal256``.
_DECIMAL128_PRECISION = 38


@dataclass(frozen=True)
class Column:
    """One result column as MySQL's column-definition packet describes it."""

    name: str
    type_code: int
    flags: int = 0
    charset: int | None = None
    #: A decimal's declared precision and scale; ``None`` for other types.
    precision: int | None = None
    scale: int | None = None

    @classmethod
    def from_pymysql(cls, field: Any) -> Column:
        """The spec for a PyMySQL ``FieldDescriptor``.

        MySQL sends a decimal's display length rather than its precision: the
        digits, plus one for the sign unless the column is unsigned, plus one
        for the point when there is a scale. ``DECIMAL(18, 2)`` arrives as 20.
        """
        precision = scale = None
        if field.type_code in _DECIMALS:
            scale = field.scale
            sign = 0 if field.flags & _UNSIGNED_FLAG else 1
            precision = max(field.length - sign - (1 if scale else 0), scale, 1)
        return cls(
            name=field.name,
            type_code=field.type_code,
            flags=field.flags,
            charset=field.charsetnr,
            precision=precision,
            scale=scale,
        )

    @property
    def type_object(self) -> object:
        """The PEP 249 type object ``cursor.description`` reports."""
        return MYSQL_TYPE_MAP.get(self.type_code, STRING)


def _decimal(precision: int, scale: int) -> pa.DataType:
    if precision <= _DECIMAL128_PRECISION:
        return pa.decimal128(precision, scale)
    return pa.decimal256(precision, scale)


def arrow_type_for(column: Column) -> pa.DataType | None:
    """The Arrow type for *column*, or ``None`` for a field type MySQL has
    not had so far (its values are left to PyArrow to read)."""
    if column.type_code in _DECIMALS:
        assert column.precision is not None and column.scale is not None
        return _decimal(column.precision, column.scale)
    if column.type_code in _SIGNED_INTS:
        unsigned = bool(column.flags & _UNSIGNED_FLAG)
        return (_UNSIGNED_INTS if unsigned else _SIGNED_INTS)[column.type_code]
    if column.flags & _SET_FLAG:
        return pa.string()
    if column.type_code in _TEXT_OR_BINARY:
        return pa.binary() if column.charset == _BINARY_CHARSET else pa.string()
    return _FIXED_TYPES.get(column.type_code)


def coerce_value(value: object, column: Column) -> object:
    """*value* as the column's Arrow type takes it.

    PyMySQL returns BIT as bytes, a zero or otherwise invalid date
    (``'0000-00-00'``) as the text MySQL sent, and a DATETIME holding only a
    date as a ``date``. The ADBC driver reads a zero date as NULL, and so does
    this.
    """
    if value is None:
        return None
    code = column.type_code
    if code == _BIT and isinstance(value, bytes):
        return int.from_bytes(value, "big")
    if code in (_DATE, _NEWDATE, _DATETIME, _TIMESTAMP) and isinstance(value, str):
        return None
    if (
        code in (_DATETIME, _TIMESTAMP)
        and isinstance(value, datetime.date)
        and not isinstance(value, datetime.datetime)
    ):
        return datetime.datetime(value.year, value.month, value.day)
    if isinstance(value, (set, frozenset)):
        return ",".join(sorted(str(member) for member in value))
    return value


def table_from_rows(rows: list[tuple[Any, ...]], columns: list[Column]) -> pa.Table:
    """A table of *rows* (values already through :func:`coerce_value`) whose
    schema comes from *columns*, the same for any number of rows."""
    arrays: list[pa.Array] = []
    for index, column in enumerate(columns):
        values = [row[index] for row in rows]
        arrow_type = arrow_type_for(column)
        try:
            arrays.append(pa.array(values, type=arrow_type))
        except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
            # A value the declared type cannot hold must not cost the caller
            # the whole result set.
            arrays.append(pa.array(values))
    return pa.Table.from_arrays(arrays, names=[column.name for column in columns])


def _bits(array: pa.ChunkedArray) -> pa.ChunkedArray:
    """BIT bytes as the big-endian unsigned integer MySQL reads them as."""
    values = [None if v is None else int.from_bytes(v, "big") for v in array.to_pylist()]
    return pa.chunked_array([pa.array(values, type=pa.uint64())])


def _duration(array: pa.ChunkedArray) -> pa.ChunkedArray:
    """A ``time32`` / ``time64`` clock reading as the duration since midnight."""
    unit = array.type.unit
    per_us = {"s": 1_000_000, "ms": 1_000, "us": 1, "ns": None}[unit]
    # Arrow casts a time32 only to int32, and a time64 only to int64.
    width = pa.int32() if pa.types.is_time32(array.type) else pa.int64()
    ticks = array.cast(width).cast(pa.int64())
    micros = pc.divide(ticks, 1_000) if per_us is None else pc.multiply(ticks, per_us)
    return micros.cast(pa.duration("us"))


def normalize_adbc(table: pa.Table) -> pa.Table:
    """The ADBC driver's *table* in the schema PyMySQL's rows build.

    The driver labels a TIMESTAMP as UTC, but parses the text MySQL sends in
    the session's time zone (the connection sets it to the server's own); the
    label is dropped, leaving the wall clock MySQL shows, as PyMySQL reads it.
    """
    fields: list[pa.Field] = []
    columns: list[pa.ChunkedArray] = []
    for field, column in zip(table.schema, table.columns, strict=True):
        if isinstance(field.type, pa.BaseExtensionType):
            column = pa.chunked_array(
                [chunk.storage for chunk in column.chunks], type=field.type.storage_type
            )
        database_type = (field.metadata or {}).get(b"sql.database_type_name", b"")
        arrow_type = column.type
        if database_type == b"YEAR":
            column = column.cast(pa.int16())
        elif database_type == b"BIT":
            column = _bits(column)
        elif pa.types.is_decimal(arrow_type) and arrow_type.bit_width < 128:
            column = column.cast(pa.decimal128(arrow_type.precision, arrow_type.scale))
        elif pa.types.is_timestamp(arrow_type):
            column = column.cast(pa.timestamp("us"))
        elif pa.types.is_time(arrow_type):
            column = _duration(column)
        fields.append(pa.field(field.name, column.type))
        columns.append(column)
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


def type_object_for(arrow_type: pa.DataType) -> object:
    """The PEP 249 type object for a column of *arrow_type*."""
    if (
        pa.types.is_integer(arrow_type)
        or pa.types.is_floating(arrow_type)
        or pa.types.is_decimal(arrow_type)
    ):
        return NUMBER
    if (
        pa.types.is_temporal(arrow_type)
        or pa.types.is_date(arrow_type)
        or pa.types.is_duration(arrow_type)
    ):
        return DATETIME
    if pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
        return BINARY
    return STRING
