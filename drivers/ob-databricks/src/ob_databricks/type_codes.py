"""PEP 249 type objects — re-exported from ob-driver-core plus Databricks type mapping.

databricks-sql-connector cursor.description returns string type names as type_code.
The DBR_TYPE_MAP maps Databricks type names to PEP 249 type objects.
"""

from __future__ import annotations

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, ROWID, STRING

__all__ = ["BINARY", "DATETIME", "DBR_TYPE_MAP", "NUMBER", "ROWID", "STRING"]

# Databricks SQL connector type names from cursor.description
# https://docs.databricks.com/en/sql/language-manual/sql-ref-datatypes.html
DBR_TYPE_MAP: dict[str, object] = {
    # Numeric
    "boolean": STRING,
    "byte": NUMBER,
    "tinyint": NUMBER,
    "short": NUMBER,
    "smallint": NUMBER,
    "int": NUMBER,
    "integer": NUMBER,
    "long": NUMBER,
    "bigint": NUMBER,
    "float": NUMBER,
    "double": NUMBER,
    "decimal": NUMBER,
    # String
    "string": STRING,
    "char": STRING,
    "varchar": STRING,
    # Date/Time
    "date": DATETIME,
    "timestamp": DATETIME,
    # Binary
    "binary": BINARY,
    # Complex (serialize as string)
    "array": STRING,
    "map": STRING,
    "struct": STRING,
}
