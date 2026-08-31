"""Build a PyArrow schema from MySQL's own column metadata.

``mysql-connector-python`` has no native Arrow result format, so this driver
assembles the table itself. Letting PyArrow infer the types from the row
values -- which is what it used to do -- makes a column's type a property of
the rows that happened to come back: one result set typed a ``DECIMAL(18, 2)``
column as ``decimal128(3, 2)`` and another typed the same column as
``decimal128(10, 9)``, an empty result typed every column as string, and an
all-NULL column came back as Arrow's ``null`` type. Consumers that carry a
schema across calls (Flight's ``FlightInfo``, the result cache) cannot work
with a type that moves.

MySQL states the type in the column-definition packet, so the type comes from
``cursor.description`` instead: entry 1 is the field type and entry 7 the flag
bits, which together fix the Arrow type before a single value is read.

The one thing ``description`` will not give up is decimal width, and it is the
one place this module cannot deliver on the rule above. MySQL sends a column
length and a scale in that packet, but ``MySQLProtocol.parse_column`` unpacks
both into throwaway locals and its caller drops the packet, so ``description``
reports ``precision`` and ``scale`` as ``None`` on the pure-Python and the
C-extension paths alike.

The scale survives only in the values. The connector builds each value from
MySQL's decimal text, which carries the column's own scale, so
``DECIMAL(18, 2)`` yields ``Decimal('10.00')`` rather than ``Decimal('10')``.
Taking the widest scale in the column recovers the declared one for any result
holding at least one non-null value, and the precision is widened to the
decimal128 limit rather than guessed, so the type can never claim the column
holds fewer digits than it does.

**A decimal column with no value to read the scale from is the exception.** An
empty result or an all-NULL column falls back to
:data:`_DEFAULT_DECIMAL_SCALE`, so ``DECIMAL(18, 2)`` reports
``decimal128(38, 0)`` there and ``decimal128(38, 2)`` once a value arrives. A
fixed scale would remove the difference, but not for free: it would rescale
every value, and ``Decimal('10.00')`` carried at a fixed scale of 30 reaches
JSON and TSV with thirty decimal places. No value is misrepresented by the
fallback, since in both cases there is no value; what it costs is a schema a
consumer cannot compare across an empty and a non-empty result. A consumer
needing one stable schema has to take it from the model rather than from the
driver, which is what the Flight surface already does.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa

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
#: rather than as the ``SET`` type code, and mysql-connector-python turns the
#: value into a Python ``set``. Reading the flag is the only way to know that
#: before a value arrives.
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
    # The connector returns TIME as a ``timedelta``: it is a signed interval of
    # up to 838 hours, not a clock reading, so it is a duration and not
    # ``time64``.
    _TIME: pa.duration("us"),
    _DATETIME: pa.timestamp("us"),
    _TIMESTAMP: pa.timestamp("us"),
    _JSON: pa.string(),
    # Measured: mysql-connector-python returns BIT as a Python ``int``, not as
    # bytes -- ``BIT(8)`` holding b'10101010' arrives as 170. BIT tops out at 64
    # bits and the column carries the UNSIGNED flag, so uint64 holds every value.
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

#: Widest decimal128 / decimal256 precision. MySQL's own DECIMAL maximum is 65
#: digits, so decimal256 covers every value the server can send.
_DECIMAL128_MAX_PRECISION = 38
_DECIMAL256_MAX_PRECISION = 76

#: Scale for a decimal column with no values to read it from. Only reachable
#: for an empty or all-NULL result, where no value can be misrepresented by it.
_DEFAULT_DECIMAL_SCALE = 0


def _decimal_type(values: list[Any]) -> pa.DataType:
    """The narrowest decimal type that holds every value in *values* exactly."""
    scale = _DEFAULT_DECIMAL_SCALE
    integral_digits = 1
    for value in values:
        if not isinstance(value, Decimal):
            continue
        sign, digits, exponent = value.as_tuple()
        if not isinstance(exponent, int):
            # NaN and the infinities have a symbolic exponent and no scale.
            continue
        scale = max(scale, -exponent if exponent < 0 else 0)
        integral_digits = max(integral_digits, len(digits) + min(exponent, 0))
    precision = max(integral_digits + scale, 1)
    if precision <= _DECIMAL128_MAX_PRECISION:
        return pa.decimal128(_DECIMAL128_MAX_PRECISION, scale)
    if precision <= _DECIMAL256_MAX_PRECISION:
        return pa.decimal256(_DECIMAL256_MAX_PRECISION, scale)
    # Unreachable against a MySQL server (DECIMAL tops out at 65 digits), but a
    # value Arrow cannot hold is better rendered than raised on.
    return pa.string()


def arrow_type_for(description_entry: tuple[Any, ...], values: list[Any]) -> pa.DataType:
    """The Arrow type for one column of a result set.

    *description_entry* is one entry of ``cursor.description``; *values* is
    that column's values, read only to recover a decimal's scale.
    """
    field_type = description_entry[1]
    flags = description_entry[7] if len(description_entry) > 7 else 0
    charset = description_entry[8] if len(description_entry) > 8 else None

    if field_type in _DECIMALS:
        return _decimal_type(values)
    if field_type in _SIGNED_INTS:
        unsigned = bool((flags or 0) & _UNSIGNED_FLAG)
        return (_UNSIGNED_INTS if unsigned else _SIGNED_INTS)[field_type]
    if (flags or 0) & _SET_FLAG:
        # A SET arrives as a Python ``set``, which no string array will take.
        # ``_coerce`` renders it back to the comma-separated form MySQL itself
        # prints, so the column is one scalar per row rather than a list whose
        # type would depend on what came back.
        return pa.string()
    if field_type in _TEXT_OR_BINARY:
        return pa.binary() if charset == _BINARY_CHARSET else pa.string()
    fixed = _FIXED_TYPES.get(field_type)
    if fixed is not None:
        return fixed
    # An unmapped field type keeps the old behaviour rather than guessing: let
    # PyArrow read the values. Reached only if MySQL adds a type.
    return _infer(values)


def _infer(values: list[Any]) -> pa.DataType:
    try:
        return pa.array(values).type
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
        return pa.string()


def _coerce(value: object, arrow_type: pa.DataType) -> object:
    """Nudge the few values whose Python type does not match *arrow_type*.

    Without this the array build would fail and fall through to inference,
    which is the row-dependence this module exists to remove.
    """
    # A DATETIME column can hold a DATE-only value, which the connector returns
    # as ``date``. Arrow will not put a ``date`` in a timestamp column.
    if (
        pa.types.is_timestamp(arrow_type)
        and isinstance(value, datetime.date)
        and not isinstance(value, datetime.datetime)
    ):
        return datetime.datetime(value.year, value.month, value.day)
    # A SET is a Python ``set``, and an unordered one: the connector has already
    # dropped the definition order MySQL prints, so sorting is what is left to
    # make the rendering deterministic rather than a choice against it.
    if isinstance(value, (set, frozenset)) and pa.types.is_string(arrow_type):
        return ",".join(sorted(str(member) for member in value))
    return value


def table_from_rows(rows: list[Any], description: list[tuple[Any, ...]]) -> pa.Table:
    """Assemble a PyArrow Table whose schema comes from *description*.

    The schema is the same for a given column whether the result has a
    thousand rows, one row, or none.
    """
    names = [entry[0] for entry in description]
    columns: list[list[Any]] = [[] for _ in names]
    for row in rows:
        for index in range(len(names)):
            columns[index].append(row[index])

    arrays: list[pa.Array] = []
    for index, entry in enumerate(description):
        arrow_type = arrow_type_for(entry, columns[index])
        values = [_coerce(value, arrow_type) for value in columns[index]]
        try:
            arrays.append(pa.array(values, type=arrow_type))
        except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
            # A value the declared type cannot hold must not cost the caller
            # the whole result set.
            arrays.append(pa.array(values))
    return pa.Table.from_arrays(arrays, names=names)
