"""PEP 249 type objects — re-exported from ob-driver-core plus Snowflake type mapping.

snowflake-connector-python cursor.description returns integer type IDs as type_code.
The SF_TYPE_MAP maps Snowflake field type IDs to PEP 249 type objects.
"""

from __future__ import annotations

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, ROWID, STRING

__all__ = ["STRING", "BINARY", "NUMBER", "DATETIME", "ROWID", "SF_TYPE_MAP"]

# Snowflake type IDs from snowflake.connector.constants.FIELD_ID_TO_NAME
SF_TYPE_MAP: dict[int, object] = {
    0: NUMBER,  # FIXED (NUMBER/INT/DECIMAL)
    1: NUMBER,  # REAL (FLOAT)
    2: STRING,  # TEXT (VARCHAR/STRING)
    3: DATETIME,  # DATE
    4: DATETIME,  # TIMESTAMP_NTZ
    5: STRING,  # VARIANT
    6: DATETIME,  # TIMESTAMP_LTZ
    7: DATETIME,  # TIMESTAMP_TZ
    8: DATETIME,  # TIMESTAMP (alias)
    9: STRING,  # OBJECT
    10: STRING,  # ARRAY
    11: BINARY,  # BINARY
    12: DATETIME,  # TIME
    13: STRING,  # BOOLEAN
}
