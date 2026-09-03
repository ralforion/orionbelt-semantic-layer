"""PEP 249 type objects — re-exported from ob-driver-core plus ClickHouse type mapping.

clickhouse-connect returns column types as ``ClickHouseType`` objects whose
``str()`` representation is the ClickHouse type name (e.g. ``"Int64"``,
``"Decimal(18,2)"``).  ``CH_TYPE_MAP`` maps the base type name (before any
parenthesised parameters) to PEP 249 type objects.
"""

from __future__ import annotations

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, ROWID, STRING

__all__ = ["BINARY", "CH_TYPE_MAP", "DATETIME", "NUMBER", "ROWID", "STRING"]

# Map base ClickHouse type names → PEP 249 type objects.
# We strip parenthesised parameters (e.g. ``Decimal(18,2)`` → ``Decimal``)
# before lookup, so only the base name is needed here.
CH_TYPE_MAP: dict[str, object] = {
    # Numeric
    "Int8": NUMBER,
    "Int16": NUMBER,
    "Int32": NUMBER,
    "Int64": NUMBER,
    "Int128": NUMBER,
    "Int256": NUMBER,
    "UInt8": NUMBER,
    "UInt16": NUMBER,
    "UInt32": NUMBER,
    "UInt64": NUMBER,
    "UInt128": NUMBER,
    "UInt256": NUMBER,
    "Float32": NUMBER,
    "Float64": NUMBER,
    "Decimal": NUMBER,
    "Decimal32": NUMBER,
    "Decimal64": NUMBER,
    "Decimal128": NUMBER,
    "Decimal256": NUMBER,
    # String
    "String": STRING,
    "FixedString": STRING,
    "UUID": STRING,
    "Enum8": STRING,
    "Enum16": STRING,
    "IPv4": STRING,
    "IPv6": STRING,
    "Bool": STRING,
    # Date/Time
    "Date": DATETIME,
    "Date32": DATETIME,
    "DateTime": DATETIME,
    "DateTime64": DATETIME,
    # Binary — ClickHouse stores blobs as String; no distinct binary type
}
