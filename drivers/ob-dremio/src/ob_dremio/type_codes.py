"""PEP 249 type objects — re-exported from ob-driver-core plus Arrow type mapping.

Dremio returns results via Arrow Flight — column types are ``pyarrow.DataType``
objects whose ``str()`` representation is the Arrow type name (e.g. ``"int64"``,
``"timestamp[ns]"``).  ``ARROW_TYPE_MAP`` maps the base type name (before any
bracket/parenthesised parameters) to PEP 249 type objects.
"""

from __future__ import annotations

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, ROWID, STRING

__all__ = ["ARROW_TYPE_MAP", "BINARY", "DATETIME", "NUMBER", "ROWID", "STRING"]

# Map base Arrow type string representations to PEP 249 type objects.
# We strip bracket/parenthesised parameters (e.g. ``timestamp[ns]`` -> ``timestamp``,
# ``decimal128(18, 2)`` -> ``decimal128``) before lookup.
ARROW_TYPE_MAP: dict[str, object] = {
    # Numeric
    "int8": NUMBER,
    "int16": NUMBER,
    "int32": NUMBER,
    "int64": NUMBER,
    "uint8": NUMBER,
    "uint16": NUMBER,
    "uint32": NUMBER,
    "uint64": NUMBER,
    "float16": NUMBER,
    "float": NUMBER,
    "double": NUMBER,
    "decimal128": NUMBER,
    # String
    "string": STRING,
    "utf8": STRING,
    "large_string": STRING,
    "large_utf8": STRING,
    "bool": STRING,
    # Date/Time
    "date32": DATETIME,
    "date64": DATETIME,
    "timestamp": DATETIME,
    "time32": DATETIME,
    "time64": DATETIME,
    "duration": DATETIME,
    # Binary
    "binary": BINARY,
    "large_binary": BINARY,
}
