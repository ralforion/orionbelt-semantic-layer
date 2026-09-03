"""PEP 249 type objects — re-exported from ob-driver-core plus BigQuery type mapping.

google-cloud-bigquery cursor.description returns string type names.
The BQ_TYPE_MAP maps them to PEP 249 type objects.
"""

from __future__ import annotations

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, ROWID, STRING

__all__ = ["BINARY", "BQ_TYPE_MAP", "DATETIME", "NUMBER", "ROWID", "STRING"]

# BigQuery standard SQL type names
# https://cloud.google.com/bigquery/docs/reference/standard-sql/data-types
BQ_TYPE_MAP: dict[str, object] = {
    # Numeric types
    "INTEGER": NUMBER,
    "INT64": NUMBER,
    "FLOAT": NUMBER,
    "FLOAT64": NUMBER,
    "NUMERIC": NUMBER,
    "BIGNUMERIC": NUMBER,
    "BOOLEAN": STRING,
    "BOOL": STRING,
    # String types
    "STRING": STRING,
    "JSON": STRING,
    # Date/time types
    "DATE": DATETIME,
    "TIME": DATETIME,
    "DATETIME": DATETIME,
    "TIMESTAMP": DATETIME,
    # Binary types
    "BYTES": BINARY,
    # Geo/struct/array — map to STRING for display
    "GEOGRAPHY": STRING,
    "STRUCT": STRING,
    "RECORD": STRING,
    "ARRAY": STRING,
}
