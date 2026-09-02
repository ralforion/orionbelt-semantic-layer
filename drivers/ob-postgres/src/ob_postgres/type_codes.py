"""PEP 249 type objects — re-exported from ob-driver-core plus Postgres OID mapping.

psycopg2 cursor.description returns integer OIDs as type_code.
The PG_OID_MAP maps common OIDs to PEP 249 type objects.
"""

from __future__ import annotations

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, ROWID, STRING

__all__ = ["BINARY", "DATETIME", "NUMBER", "PG_OID_MAP", "ROWID", "STRING"]

# Common PostgreSQL OIDs — see pg_type catalog
# https://www.postgresql.org/docs/current/catalog-pg-type.html
PG_OID_MAP: dict[int, object] = {
    # Boolean
    16: STRING,  # bool
    # Numeric types
    20: NUMBER,  # int8 (bigint)
    21: NUMBER,  # int2 (smallint)
    23: NUMBER,  # int4 (integer)
    26: NUMBER,  # oid
    700: NUMBER,  # float4
    701: NUMBER,  # float8
    790: NUMBER,  # money
    1700: NUMBER,  # numeric/decimal
    # String types
    18: STRING,  # char
    19: STRING,  # name
    25: STRING,  # text
    1042: STRING,  # bpchar (char(n))
    1043: STRING,  # varchar
    2950: STRING,  # uuid
    114: STRING,  # json
    3802: STRING,  # jsonb
    # Date/time types
    1082: DATETIME,  # date
    1083: DATETIME,  # time
    1114: DATETIME,  # timestamp
    1184: DATETIME,  # timestamptz
    1186: DATETIME,  # interval
    1266: DATETIME,  # timetz
    # Binary types
    17: BINARY,  # bytea
}
