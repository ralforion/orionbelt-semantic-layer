"""Minimal Flight SQL protocol handling for DBeaver/JDBC compatibility.

DBeaver's Arrow Flight SQL JDBC driver sends protobuf-encoded commands
(CommandGetTables, CommandGetCatalogs, etc.) for schema browsing.
This module parses those commands and generates appropriate Arrow responses.
"""

from __future__ import annotations

import logging
from typing import Any

import pyarrow as pa

logger = logging.getLogger("ob_flight.flight_sql")

# Flight SQL command type URLs
CMD_GET_CATALOGS = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetCatalogs"
CMD_GET_DB_SCHEMAS = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetDbSchemas"
CMD_GET_TABLES = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetTables"
CMD_GET_TABLE_TYPES = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetTableTypes"
CMD_STATEMENT_QUERY = "type.googleapis.com/arrow.flight.protocol.sql.CommandStatementQuery"
CMD_GET_SQL_INFO = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetSqlInfo"
CMD_GET_XDBC_TYPE_INFO = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetXdbcTypeInfo"
CMD_GET_PRIMARY_KEYS = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetPrimaryKeys"
CMD_GET_IMPORTED_KEYS = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetImportedKeys"
CMD_GET_EXPORTED_KEYS = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetExportedKeys"
CMD_GET_CROSS_REFERENCE = "type.googleapis.com/arrow.flight.protocol.sql.CommandGetCrossReference"
CMD_PREPARED_STATEMENT_QUERY = (
    "type.googleapis.com/arrow.flight.protocol.sql.CommandPreparedStatementQuery"
)

# Flight SQL action types (used in do_action)
ACTION_CREATE_PREPARED_STATEMENT = "CreatePreparedStatement"
ACTION_CLOSE_PREPARED_STATEMENT = "ClosePreparedStatement"
ACTION_CREATE_PREPARED_STATEMENT_REQ = (
    "type.googleapis.com/arrow.flight.protocol.sql.ActionCreatePreparedStatementRequest"
)

# Standard Flight SQL schemas for catalog responses
CATALOG_SCHEMA = pa.schema([pa.field("catalog_name", pa.utf8())])

DB_SCHEMA_SCHEMA = pa.schema([
    pa.field("catalog_name", pa.utf8()),
    pa.field("db_schema_name", pa.utf8()),
])

TABLE_SCHEMA = pa.schema([
    pa.field("catalog_name", pa.utf8()),
    pa.field("db_schema_name", pa.utf8()),
    pa.field("table_name", pa.utf8()),
    pa.field("table_type", pa.utf8()),
    pa.field("table_schema", pa.binary()),
])

TABLE_TYPES_SCHEMA = pa.schema([pa.field("table_type", pa.utf8())])

PRIMARY_KEYS_SCHEMA = pa.schema([
    pa.field("catalog_name", pa.utf8()),
    pa.field("db_schema_name", pa.utf8()),
    pa.field("table_name", pa.utf8()),
    pa.field("column_name", pa.utf8()),
    pa.field("key_name", pa.utf8()),
    pa.field("key_sequence", pa.int32()),
])

IMPORTED_KEYS_SCHEMA = pa.schema([
    pa.field("pk_catalog_name", pa.utf8()),
    pa.field("pk_db_schema_name", pa.utf8()),
    pa.field("pk_table_name", pa.utf8()),
    pa.field("pk_column_name", pa.utf8()),
    pa.field("fk_catalog_name", pa.utf8()),
    pa.field("fk_db_schema_name", pa.utf8()),
    pa.field("fk_table_name", pa.utf8()),
    pa.field("fk_column_name", pa.utf8()),
    pa.field("key_sequence", pa.int32()),
    pa.field("fk_key_name", pa.utf8()),
    pa.field("pk_key_name", pa.utf8()),
    pa.field("update_rule", pa.uint8()),
    pa.field("delete_rule", pa.uint8()),
])

# SqlInfo response schema
SQL_INFO_SCHEMA = pa.schema([
    pa.field("info_name", pa.uint32()),
    pa.field("value", pa.dense_union([
        pa.field("string_value", pa.utf8()),
        pa.field("bool_value", pa.bool_()),
        pa.field("bigint_value", pa.int64()),
        pa.field("int32_bitmask", pa.int32()),
        pa.field("string_list", pa.list_(pa.utf8())),
        pa.field("int32_to_int32_list_map",
                 pa.map_(pa.int32(), pa.list_(pa.int32()))),
    ])),
])


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Read a protobuf varint, return (value, new_offset)."""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return result, offset


def parse_any(data: bytes) -> tuple[str, bytes] | None:
    """Parse a protobuf Any message, return (type_url, value) or None.

    The protobuf Any wire format:
      field 1 (tag 0x0a) = type_url (string, length-delimited)
      field 2 (tag 0x12) = value (bytes, length-delimited)
    """
    try:
        type_url = ""
        value = b""
        offset = 0
        while offset < len(data):
            # Read tag
            tag, offset = _read_varint(data, offset)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 2:  # length-delimited
                length, offset = _read_varint(data, offset)
                field_data = data[offset:offset + length]
                offset += length
                if field_number == 1:
                    type_url = field_data.decode("utf-8")
                elif field_number == 2:
                    value = field_data
            elif wire_type == 0:  # varint — skip
                _, offset = _read_varint(data, offset)
            else:
                return None  # unexpected wire type

        if type_url:
            return type_url, value
    except Exception:
        pass
    return None


def parse_statement_query(value: bytes) -> str | None:
    """Extract the SQL query string from a CommandStatementQuery protobuf.

    CommandStatementQuery: field 1 = query (string).
    """
    try:
        offset = 0
        while offset < len(value):
            tag, offset = _read_varint(value, offset)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 2:  # length-delimited
                length, offset = _read_varint(value, offset)
                field_data = value[offset:offset + length]
                offset += length
                if field_number == 1:
                    return field_data.decode("utf-8")
            elif wire_type == 0:  # varint — skip
                _, offset = _read_varint(value, offset)
            else:
                break
    except Exception:
        pass
    return None


def is_flight_sql_command(data: bytes) -> bool:
    """Check if raw bytes are a Flight SQL protobuf command."""
    result = parse_any(data)
    if result is None:
        return False
    type_url, _ = result
    return "arrow.flight.protocol.sql" in type_url


def build_catalogs_table() -> pa.Table:
    """Build response for CommandGetCatalogs."""
    return pa.table({"catalog_name": ["orionbelt"]}, schema=CATALOG_SCHEMA)


def build_db_schemas_table() -> pa.Table:
    """Build response for CommandGetDbSchemas."""
    return pa.table(
        {"catalog_name": ["orionbelt"], "db_schema_name": ["model"]},
        schema=DB_SCHEMA_SCHEMA,
    )


def build_tables_table(model: Any) -> pa.Table:
    """Build response for CommandGetTables from the semantic model."""
    names: list[str] = []
    catalogs: list[str] = []
    schemas: list[str] = []
    types: list[str] = []
    table_schemas: list[bytes] = []

    if hasattr(model, "data_objects") and model.data_objects:
        from ob_flight.catalog import object_to_schema

        for obj_name, obj in model.data_objects.items():
            label = getattr(obj, "label", obj_name) or obj_name
            names.append(label)
            catalogs.append("orionbelt")
            schemas.append("model")
            types.append("TABLE")
            # Serialize Arrow schema for the table_schema column
            # Flight SQL spec requires Schema serialization (not IPC stream)
            arrow_schema = object_to_schema(obj)
            table_schemas.append(arrow_schema.serialize().to_pybytes())

    # Virtual metadata tables (_dimensions, _measures, _metrics)
    from ob_flight.catalog import VIRTUAL_TABLES

    for vt_name, vt_schema in VIRTUAL_TABLES.items():
        names.append(vt_name)
        catalogs.append("orionbelt")
        schemas.append("model")
        types.append("TABLE")
        table_schemas.append(vt_schema.serialize().to_pybytes())

    return pa.table(
        {
            "catalog_name": catalogs,
            "db_schema_name": schemas,
            "table_name": names,
            "table_type": types,
            "table_schema": table_schemas,
        },
        schema=TABLE_SCHEMA,
    )


def build_table_types_table() -> pa.Table:
    """Build response for CommandGetTableTypes."""
    return pa.table({"table_type": ["TABLE"]}, schema=TABLE_TYPES_SCHEMA)


def build_empty_keys_table() -> pa.Table:
    """Build empty response for GetPrimaryKeys/GetImportedKeys/GetExportedKeys."""
    return pa.table(
        {f.name: pa.array([], type=f.type) for f in PRIMARY_KEYS_SCHEMA},
        schema=PRIMARY_KEYS_SCHEMA,
    )


def build_empty_imported_keys_table() -> pa.Table:
    """Build empty response for GetImportedKeys."""
    return pa.table(
        {f.name: pa.array([], type=f.type) for f in IMPORTED_KEYS_SCHEMA},
        schema=IMPORTED_KEYS_SCHEMA,
    )


def parse_create_prepared_statement(body: bytes) -> str | None:
    """Extract the SQL query from a CreatePreparedStatement action body.

    The body may be either:
    - A protobuf Any wrapping ActionCreatePreparedStatementRequest
    - A raw ActionCreatePreparedStatementRequest (field 1 = query string)
    """
    # Try as protobuf Any first
    parsed = parse_any(body)
    if parsed is not None:
        type_url, value = parsed
        if "ActionCreatePreparedStatementRequest" in type_url:
            return parse_statement_query(value)  # field 1 = query
        # If it's some other Any, try the value
        return parse_statement_query(value)

    # Try as raw ActionCreatePreparedStatementRequest
    return parse_statement_query(body)


def parse_prepared_statement_handle(data: bytes) -> bytes | None:
    """Extract the prepared_statement_handle from CommandPreparedStatementQuery.

    CommandPreparedStatementQuery: field 1 = prepared_statement_handle (bytes).
    """
    try:
        offset = 0
        while offset < len(data):
            tag, offset = _read_varint(data, offset)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 2:  # length-delimited
                length, offset = _read_varint(data, offset)
                field_data = data[offset:offset + length]
                offset += length
                if field_number == 1:
                    return field_data
            elif wire_type == 0:  # varint — skip
                _, offset = _read_varint(data, offset)
            else:
                break
    except Exception:
        pass
    return None


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    parts = []
    while value > 0x7F:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    parts.append(value & 0x7F)
    return bytes(parts)


def _encode_length_delimited(field_number: int, data: bytes) -> bytes:
    """Encode a protobuf length-delimited field."""
    tag = (field_number << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(data)) + data


_ACTION_CREATE_PREPARED_STATEMENT_RESULT_TYPE_URL = (
    "type.googleapis.com/arrow.flight.protocol.sql.ActionCreatePreparedStatementResult"
)


def build_prepared_statement_result(handle: bytes, schema: pa.Schema) -> bytes:
    """Build an ActionCreatePreparedStatementResult wrapped in a protobuf Any.

    The JDBC client parses the do_action result as a protobuf Any message,
    so we must wrap the inner message with the correct type_url.

    Inner message fields: 1=prepared_statement_handle, 2=dataset_schema, 3=parameter_schema
    """
    # Serialize the Arrow schema as IPC Schema message for field 2
    schema_bytes = schema.serialize().to_pybytes()

    # Build inner ActionCreatePreparedStatementResult
    inner = b""
    inner += _encode_length_delimited(1, handle)  # handle
    inner += _encode_length_delimited(2, schema_bytes)  # dataset_schema
    inner += _encode_length_delimited(3, b"")  # parameter_schema (empty)

    # Wrap in protobuf Any: field 1 = type_url (string), field 2 = value (bytes)
    any_msg = b""
    any_msg += _encode_length_delimited(
        1, _ACTION_CREATE_PREPARED_STATEMENT_RESULT_TYPE_URL.encode("utf-8")
    )
    any_msg += _encode_length_delimited(2, inner)
    return any_msg
