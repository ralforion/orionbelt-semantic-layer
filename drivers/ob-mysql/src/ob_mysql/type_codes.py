"""PEP 249 type objects — re-exported from ob-driver-core plus MySQL field type mapping.

MySQL's column-definition packet carries an integer field type, which
MYSQL_TYPE_MAP maps to a PEP 249 type object.

MySQL field type constants:
https://dev.mysql.com/doc/dev/mysql-server/latest/field__types_8h.html
"""

from __future__ import annotations

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, ROWID, STRING

__all__ = ["BINARY", "DATETIME", "MYSQL_TYPE_MAP", "NUMBER", "ROWID", "STRING"]

# Common MySQL field type IDs from the wire protocol
MYSQL_TYPE_MAP: dict[int, object] = {
    # Numeric types
    0: NUMBER,  # DECIMAL
    1: NUMBER,  # TINY (tinyint)
    2: NUMBER,  # SHORT (smallint)
    3: NUMBER,  # LONG (int)
    4: NUMBER,  # FLOAT
    5: NUMBER,  # DOUBLE
    8: NUMBER,  # LONGLONG (bigint)
    9: NUMBER,  # INT24 (mediumint)
    16: NUMBER,  # BIT
    246: NUMBER,  # NEWDECIMAL
    # Date/time types
    7: DATETIME,  # TIMESTAMP
    10: DATETIME,  # DATE
    11: DATETIME,  # TIME
    12: DATETIME,  # DATETIME
    13: DATETIME,  # YEAR
    14: DATETIME,  # NEWDATE
    # String types
    6: STRING,  # NULL
    15: STRING,  # VARCHAR
    245: STRING,  # JSON
    253: STRING,  # VAR_STRING
    254: STRING,  # STRING
    # Binary types
    249: BINARY,  # TINY_BLOB
    250: BINARY,  # MEDIUM_BLOB
    251: BINARY,  # LONG_BLOB
    252: BINARY,  # BLOB
}
