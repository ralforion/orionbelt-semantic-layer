"""PEP 249 type objects — re-exported from ob-driver-core plus DuckDB mapping.

DuckDB cursor.description uses uppercase type names like 'VARCHAR', 'INTEGER', etc.
The DUCKDB_TYPE_MAP maps these to PEP 249 type objects.
"""

from __future__ import annotations

from ob_driver_core.type_codes import (
    BINARY,
    DATETIME,
    NUMBER,
    ROWID,
    STRING,
    DBAPITypeObject,
)

__all__ = ["STRING", "BINARY", "NUMBER", "DATETIME", "ROWID", "DUCKDB_TYPE_MAP"]

DUCKDB_TYPE_MAP: dict[str, DBAPITypeObject] = {
    # String types
    "VARCHAR": STRING,
    "TEXT": STRING,
    "CHAR": STRING,
    "BOOLEAN": STRING,
    "UUID": STRING,
    "ENUM": STRING,
    # Numeric types
    "TINYINT": NUMBER,
    "SMALLINT": NUMBER,
    "INTEGER": NUMBER,
    "BIGINT": NUMBER,
    "HUGEINT": NUMBER,
    "UTINYINT": NUMBER,
    "USMALLINT": NUMBER,
    "UINTEGER": NUMBER,
    "UBIGINT": NUMBER,
    "FLOAT": NUMBER,
    "DOUBLE": NUMBER,
    "DECIMAL": NUMBER,
    # Date/time types
    "DATE": DATETIME,
    "TIME": DATETIME,
    "TIMESTAMP": DATETIME,
    "TIMESTAMP WITH TIME ZONE": DATETIME,
    "TIMESTAMP_S": DATETIME,
    "TIMESTAMP_MS": DATETIME,
    "TIMESTAMP_NS": DATETIME,
    "INTERVAL": DATETIME,
    # Binary types
    "BLOB": BINARY,
}
