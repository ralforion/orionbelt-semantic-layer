"""PEP 249 DB-API 2.0 type objects.

Type objects compare equal to any value in their set, enabling:
    if cursor.description[i][1] == NUMBER:
        ...
"""

from __future__ import annotations


class DBAPITypeObject:
    """PEP 249 type object that compares equal to any value in its set."""

    def __init__(self, *values: str) -> None:
        self._values = frozenset(values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return other in self._values
        if isinstance(other, DBAPITypeObject):
            return self._values == other._values
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        return f"DBAPITypeObject({', '.join(sorted(self._values))})"


STRING = DBAPITypeObject("STRING", "VARCHAR", "TEXT", "CHAR", "NVARCHAR", "NCHAR")
BINARY = DBAPITypeObject("BINARY", "BLOB", "VARBINARY", "BYTEA", "BYTES")
NUMBER = DBAPITypeObject(
    "NUMBER",
    "INT",
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
    "NUMERIC",
    "REAL",
    "HUGEINT",
)
DATETIME = DBAPITypeObject(
    "DATETIME",
    "DATE",
    "TIME",
    "TIMESTAMP",
    "TIMESTAMPTZ",
    "TIMESTAMP_NTZ",
    "TIMESTAMP_LTZ",
    "TIMESTAMP_TZ",
)
ROWID = DBAPITypeObject("ROWID")
