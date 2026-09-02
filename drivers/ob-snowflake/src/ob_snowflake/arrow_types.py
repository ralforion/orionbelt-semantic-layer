"""The Arrow schema a Snowflake result carries, taken from its metadata.

The connector answers Arrow natively, and picks an integer's width from the
*values* in the batch: measured, ``CAST(42 AS INTEGER)`` comes back ``int8`` and
``CAST(3000000000 AS INTEGER)`` comes back ``int64``, from one declaration -
``NUMBER(38, 0)`` in both cases. Two pages of one column can therefore disagree
about its type, which is the failure ``ob_mysql.arrow_types`` was written for
(#393): a consumer that reads the first page's schema is wrong about the second.

The description knows better than the values do. Snowflake reports the declared
``precision`` and ``scale`` per column, so a column that *is* an integer is
widened to ``int64`` once and stays there.

Only integers are touched. A ``NUMBER`` too wide for 64 bits arrives as
``decimal128(38, 0)`` - measured, ``CAST(1e30 AS INTEGER)`` does - and widening
that to ``int64`` would overflow a value the warehouse holds legally, so it is
left as it is. That leaves one residue, and it is the data's rather than the
schema's: a column whose first page fits an integer and whose second does not
still changes type, because nothing here can know the second page in advance.
"""

from __future__ import annotations

import pyarrow as pa

#: Snowflake's FIXED family - NUMBER, DECIMAL, INT and friends - from
#: ``snowflake.connector.constants.FIELD_ID_TO_NAME``. The only family whose
#: Arrow width the connector decides from the values.
_FIXED = 0

#: Arrow type per Snowflake field id, for a result that carried no batch at all.
#: Deliberately coarse: it describes an *empty* column, where the width cannot
#: be wrong about any value, and the point is to answer a schema rather than
#: ``None``.
_EMPTY_COLUMN_TYPE: dict[int, pa.DataType] = {
    1: pa.float64(),  # REAL
    2: pa.string(),  # TEXT
    3: pa.date32(),  # DATE
    4: pa.timestamp("ns"),  # TIMESTAMP_NTZ
    5: pa.string(),  # VARIANT
    6: pa.timestamp("ns", tz="UTC"),  # TIMESTAMP_LTZ
    7: pa.timestamp("ns", tz="UTC"),  # TIMESTAMP_TZ
    8: pa.timestamp("ns"),  # TIMESTAMP
    9: pa.string(),  # OBJECT
    10: pa.string(),  # ARRAY
    11: pa.binary(),  # BINARY
    12: pa.time64("ns"),  # TIME
    13: pa.bool_(),  # BOOLEAN
}


def _column_type(column: object) -> pa.DataType:
    """The Arrow type an empty column of this description should carry."""
    type_code = getattr(column, "type_code", None)
    if not isinstance(type_code, int):
        return pa.string()
    if type_code == _FIXED:
        scale = getattr(column, "scale", 0) or 0
        if scale == 0:
            return pa.int64()
        precision = getattr(column, "precision", None) or 38
        return pa.decimal128(precision, scale)
    return _EMPTY_COLUMN_TYPE.get(type_code, pa.string())


def stable_arrow_table(table: pa.Table | None, description: list[object] | None) -> pa.Table | None:
    """*table* with integer widths taken from *description* rather than values.

    Returns an empty table carrying the described schema where the connector
    answered ``None``, which is what it does for a result with no rows: a
    consumer that asks an empty result for its schema should get one.

    Best effort throughout - a description that does not line up with the table,
    or a cast the values refuse, leaves the table exactly as it arrived. A
    slightly loose schema beats a failed fetch.
    """
    if description is None:
        return table
    if table is None:
        return pa.table(
            {
                str(getattr(col, "name", f"c{i}")): pa.array([], type=_column_type(col))
                for i, col in enumerate(description)
            }
        )
    if table.num_columns != len(description):
        return table
    for i, column in enumerate(description):
        if getattr(column, "type_code", None) != _FIXED:
            continue
        if (getattr(column, "scale", 0) or 0) != 0:
            continue
        field = table.schema.field(i)
        if not pa.types.is_integer(field.type) or field.type == pa.int64():
            continue
        try:
            table = table.set_column(i, field.name, table.column(i).cast(pa.int64()))
        except pa.ArrowInvalid:  # pragma: no cover - a cast an int64 cannot hold
            return table
    return table
