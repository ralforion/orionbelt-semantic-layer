"""Arrow Flight SQL server for OrionBelt Semantic Layer."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pyarrow as pa
import pyarrow.flight as flight

from ob_driver_core.detection import is_obml, parse_obml

from ob_flight.catalog import model_to_flight_infos
from ob_flight.converters import rows_to_batch, schema_from_description
from ob_flight.db_router import connect as db_connect
from ob_flight.flight_sql import (
    ACTION_CLOSE_PREPARED_STATEMENT,
    ACTION_CREATE_PREPARED_STATEMENT,
    CMD_GET_CATALOGS,
    CMD_GET_CROSS_REFERENCE,
    CMD_GET_DB_SCHEMAS,
    CMD_GET_EXPORTED_KEYS,
    CMD_GET_IMPORTED_KEYS,
    CMD_GET_PRIMARY_KEYS,
    CMD_GET_SQL_INFO,
    CMD_GET_TABLE_TYPES,
    CMD_GET_TABLES,
    CMD_GET_XDBC_TYPE_INFO,
    CMD_PREPARED_STATEMENT_QUERY,
    CMD_STATEMENT_QUERY,
    build_catalogs_table,
    build_db_schemas_table,
    build_empty_imported_keys_table,
    build_empty_keys_table,
    build_prepared_statement_result,
    build_table_types_table,
    build_tables_table,
    is_flight_sql_command,
    parse_any,
    parse_create_prepared_statement,
    parse_prepared_statement_handle,
    parse_statement_query,
)

logger = logging.getLogger("ob_flight.server")


def _sql_type_to_arrow(sql_type: str) -> pa.DataType:
    """Map a SQL data type name to an Arrow type (best-effort)."""
    t = sql_type.lower()
    if "int" in t or t in ("serial", "bigserial", "smallserial"):
        return pa.int64()
    if "float" in t or "double" in t or "real" in t:
        return pa.float64()
    if "numeric" in t or "decimal" in t or "money" in t:
        return pa.float64()
    if "bool" in t:
        return pa.bool_()
    if "timestamp" in t:
        return pa.timestamp("us")
    if "date" in t:
        return pa.date32()
    if "time" in t:
        return pa.time64("us")
    if "bytea" in t or "blob" in t or "binary" in t:
        return pa.binary()
    return pa.utf8()


# Flight SQL catalog command type URLs that return metadata (no DB execution)
_CATALOG_COMMANDS = {
    CMD_GET_CATALOGS,
    CMD_GET_DB_SCHEMAS,
    CMD_GET_TABLES,
    CMD_GET_TABLE_TYPES,
    CMD_GET_SQL_INFO,
    CMD_GET_XDBC_TYPE_INFO,
    CMD_GET_PRIMARY_KEYS,
    CMD_GET_IMPORTED_KEYS,
    CMD_GET_EXPORTED_KEYS,
    CMD_GET_CROSS_REFERENCE,
}


class OBFlightServer(flight.FlightServerBase):
    """Arrow Flight server that compiles OBML queries via the OrionBelt pipeline.

    Runs inside the orionbelt-api process with direct access to
    CompilationPipeline and SessionManager — no HTTP hop.

    Handles Flight SQL protocol commands (protobuf) for DBeaver/JDBC compatibility
    in addition to plain-text SQL and OBML queries.
    """

    def __init__(
        self,
        location: str = "grpc://0.0.0.0:8815",
        *,
        auth_handler: flight.ServerAuthHandler | None = None,
        session_manager: Any = None,
        default_dialect: str = "duckdb",
        batch_size: int = 1024,
    ) -> None:
        super().__init__(location, auth_handler=auth_handler)
        self._session_manager = session_manager
        self._default_dialect = default_dialect
        self._batch_size = batch_size
        # Pending queries: ticket_id -> payload
        # payload is either ("sql", sql, dialect) or ("catalog", type_url)
        self._pending: dict[str, tuple[str, ...]] = {}
        # Prepared statements: handle_hex -> (sql, dialect, schema)
        self._prepared: dict[str, tuple[str, str, pa.Schema]] = {}

    def _get_model(self) -> tuple[Any, str]:
        """Get the default model from the session manager.

        Returns (model, dialect) tuple.
        Uses the default session's first model (single-model mode).
        """
        if self._session_manager is None:
            raise flight.FlightUnavailableError("No session manager configured")

        try:
            store = self._session_manager.get_store("__default__")
        except Exception:
            raise flight.FlightUnavailableError("No default session available")

        models = store.list_models()
        if not models:
            raise flight.FlightUnavailableError("No models loaded")

        model_id = models[0].model_id
        model = store.get_model(model_id)
        return model, self._default_dialect

    def _rewrite_table_names(self, sql: str, model: Any) -> str:
        """Rewrite compiled SQL for execution on the actual database.

        Two rewrites:
        1. Quoted label → physical code (DBeaver sends "Sales", DB has sales)
        2. Strip OBML schema prefix — the connection's search_path handles
           schema resolution, so PUBLIC.sales → sales avoids mismatches
           between the OBML model's schema field and the actual DB schema.
        """
        if not hasattr(model, "data_objects") or not model.data_objects:
            return sql
        for obj_name, obj in model.data_objects.items():
            label = getattr(obj, "label", obj_name) or obj_name
            code = getattr(obj, "code", None)
            if not code:
                continue
            # Replace quoted "Label" → code (DBeaver-generated SQL)
            if label != code:
                sql = sql.replace(f'"{label}"', code)
            # Strip schema/database prefix — connection context handles resolution
            # 3-part: ANALYTICS.PUBLIC.sales → sales (BigQuery, Snowflake, Databricks)
            # 2-part: PUBLIC.sales → sales (Postgres, MySQL, ClickHouse, DuckDB)
            database = getattr(obj, "database", None)
            schema_name = getattr(obj, "schema_name", None)
            if database and schema_name:
                sql = sql.replace(f"{database}.{schema_name}.{code}", code)
            if schema_name:
                sql = sql.replace(f"{schema_name}.{code}", code)
        return sql

    def _prepare_sql(self, sql: str) -> tuple[str, str, Any]:
        """Resolve model, rewrite table names, compile OBML if needed.

        Returns (final_sql, dialect, model).
        """
        model, dialect = self._get_model()
        if is_obml(sql):
            obml = parse_obml(sql)
            sql = self._compile_obml(obml, model, dialect)
            logger.info("Compiled OBML to SQL: %s", sql[:200])
        sql = self._rewrite_table_names(sql, model)
        return sql, dialect, model

    def _probe_schema(self, sql: str, dialect: str) -> pa.Schema:
        """Probe the database to determine the result schema for a query.

        Executes the query, peeks at a small batch for accurate type inference
        (UNION ALL queries may have NULL-padded columns in early rows).
        Falls back to a generic schema on error.
        """
        conn = db_connect(dialect)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            if cursor.description is None:
                return pa.schema([pa.field("status", pa.utf8())])
            rows = cursor.fetchmany(64)
            return schema_from_description(cursor.description, sample_rows=rows)
        except Exception as exc:
            logger.debug("Schema probe failed: %s", exc)
            return pa.schema([pa.field("result", pa.utf8())])
        finally:
            conn.close()

    def _query_db_schema(self, dialect: str) -> tuple[list[dict[str, str]], dict[str, pa.Schema]]:
        """Query the database for physical table and column metadata.

        Returns (tables, schemas_by_table) where:
          - tables: list of dicts with catalog_name, db_schema_name, table_name, table_type
          - schemas_by_table: table_name -> Arrow schema
        """
        # SQL to get tables and their columns varies by dialect
        if dialect in ("postgres", "mysql", "clickhouse", "snowflake", "databricks"):
            tables_sql = (
                "SELECT table_catalog, table_schema, table_name, table_type "
                "FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "ORDER BY table_name"
            )
            columns_sql = (
                "SELECT table_name, column_name, data_type, ordinal_position "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "ORDER BY table_name, ordinal_position"
            )
        elif dialect == "duckdb":
            tables_sql = (
                "SELECT table_catalog, table_schema, table_name, table_type "
                "FROM information_schema.tables "
                "ORDER BY table_name"
            )
            columns_sql = (
                "SELECT table_name, column_name, data_type, ordinal_position "
                "FROM information_schema.columns "
                "ORDER BY table_name, ordinal_position"
            )
        else:
            return [], {}

        conn = db_connect(dialect)
        try:
            cursor = conn.cursor()
            # Fetch tables
            cursor.execute(tables_sql)
            table_rows = cursor.fetchall()
            tables: list[dict[str, str]] = []
            for row in table_rows:
                tables.append({
                    "catalog_name": str(row[0] or ""),
                    "db_schema_name": str(row[1] or ""),
                    "table_name": str(row[2] or ""),
                    "table_type": str(row[3] or "TABLE").upper().replace("BASE ", ""),
                })

            # Fetch columns
            cursor.execute(columns_sql)
            col_rows = cursor.fetchall()
            schemas_by_table: dict[str, pa.Schema] = {}
            from collections import defaultdict
            cols_by_table: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for row in col_rows:
                tbl = str(row[0] or "")
                col_name = str(row[1] or "")
                data_type = str(row[2] or "").lower()
                cols_by_table[tbl].append((col_name, data_type))

            for tbl, cols in cols_by_table.items():
                fields = []
                for col_name, data_type in cols:
                    fields.append(pa.field(col_name, _sql_type_to_arrow(data_type)))
                schemas_by_table[tbl] = pa.schema(fields)

            return tables, schemas_by_table
        except Exception as exc:
            logger.warning("Failed to query database metadata: %s", exc)
            return [], {}
        finally:
            conn.close()

    def _compile_obml(self, obml: dict[str, Any], model: Any, dialect: str) -> str:
        """Compile OBML to SQL using the OrionBelt pipeline directly."""
        from orionbelt.compiler.pipeline import CompilationPipeline
        from orionbelt.models.query import QueryObject

        query = QueryObject.model_validate(obml)
        result = CompilationPipeline().compile(query, model, dialect)
        return result.sql

    def get_flight_info(
        self, context: flight.ServerCallContext, descriptor: flight.FlightDescriptor
    ) -> flight.FlightInfo:
        """Handle a query request — Flight SQL commands, OBML, or plain SQL."""
        command_bytes = descriptor.command
        ticket_id = str(uuid.uuid4())

        # Check for Flight SQL protobuf commands first
        if is_flight_sql_command(command_bytes):
            parsed = parse_any(command_bytes)
            assert parsed is not None
            type_url, value = parsed
            logger.debug("Flight SQL command: %s", type_url)

            if type_url == CMD_STATEMENT_QUERY:
                # Extract the SQL query from the protobuf
                sql = parse_statement_query(value)
                if sql is None:
                    raise flight.FlightServerError("Failed to parse SQL from Flight SQL command")
                sql, dialect, _ = self._prepare_sql(sql)
                self._pending[ticket_id] = ("sql", sql, dialect)
                schema = self._probe_schema(sql, dialect)

            elif type_url == CMD_PREPARED_STATEMENT_QUERY:
                # Look up prepared statement by handle
                handle = parse_prepared_statement_handle(value)
                if handle is None:
                    raise flight.FlightServerError("Invalid prepared statement handle")
                handle_hex = handle.hex()
                if handle_hex not in self._prepared:
                    raise flight.FlightServerError(f"Unknown prepared statement: {handle_hex}")
                sql, dialect, schema = self._prepared[handle_hex]
                self._pending[ticket_id] = ("sql", sql, dialect)

            elif type_url in _CATALOG_COMMANDS:
                # Store the raw command for do_get to handle
                self._pending[ticket_id] = ("catalog", type_url)
                schema = pa.schema([pa.field("result", pa.utf8())])

            else:
                raise flight.FlightServerError(f"Unsupported Flight SQL command: {type_url}")

            ticket = flight.Ticket(ticket_id.encode("utf-8"))
            endpoint = flight.FlightEndpoint(ticket, [])
            return flight.FlightInfo(schema, descriptor, [endpoint], -1, -1)

        # Plain text: SQL or OBML
        query_str = command_bytes.decode("utf-8")
        sql, dialect, _ = self._prepare_sql(query_str)
        self._pending[ticket_id] = ("sql", sql, dialect)
        schema = self._probe_schema(sql, dialect)
        ticket = flight.Ticket(ticket_id.encode("utf-8"))
        endpoint = flight.FlightEndpoint(ticket, [])
        return flight.FlightInfo(schema, descriptor, [endpoint], -1, -1)

    def do_get(
        self, context: flight.ServerCallContext, ticket: flight.Ticket
    ) -> flight.RecordBatchStream:
        """Execute a query or return catalog metadata."""
        ticket_id = ticket.ticket.decode("utf-8")

        if ticket_id not in self._pending:
            raise flight.FlightServerError(f"Unknown ticket: {ticket_id}")

        pending = self._pending.pop(ticket_id)
        kind = pending[0]

        if kind == "catalog":
            return self._handle_catalog_command(pending[1])

        # kind == "sql"
        _, sql, dialect = pending
        return self._execute_sql(str(sql), str(dialect))

    def _handle_catalog_command(self, type_url: str) -> flight.RecordBatchStream:
        """Handle Flight SQL catalog metadata commands.

        For CMD_GET_TABLES and CMD_GET_DB_SCHEMAS, queries the actual database
        for physical table/column metadata rather than using the semantic model.
        """
        if type_url == CMD_GET_CATALOGS:
            table = build_catalogs_table()
        elif type_url == CMD_GET_DB_SCHEMAS:
            table = self._build_db_schemas_from_db()
        elif type_url == CMD_GET_TABLES:
            table = self._build_tables_from_db()
        elif type_url == CMD_GET_TABLE_TYPES:
            table = build_table_types_table()
        elif type_url in (CMD_GET_PRIMARY_KEYS, CMD_GET_EXPORTED_KEYS, CMD_GET_CROSS_REFERENCE):
            table = build_empty_keys_table()
        elif type_url == CMD_GET_IMPORTED_KEYS:
            table = build_empty_imported_keys_table()
        elif type_url in (CMD_GET_SQL_INFO, CMD_GET_XDBC_TYPE_INFO):
            # Return empty results for info commands we don't support yet
            table = pa.table({"info": pa.array([], type=pa.utf8())})
        else:
            raise flight.FlightServerError(f"Unsupported catalog command: {type_url}")

        logger.debug("Catalog response for %s: %d rows", type_url.rsplit(".", 1)[-1], len(table))
        return flight.RecordBatchStream(table)

    def _build_db_schemas_from_db(self) -> pa.Table:
        """Query database for actual schema names."""
        db_tables, _ = self._query_db_schema(self._default_dialect)
        if db_tables:
            # Unique (catalog, schema) pairs
            seen: set[tuple[str, str]] = set()
            catalogs: list[str] = []
            schemas: list[str] = []
            for t in db_tables:
                key = (t["catalog_name"], t["db_schema_name"])
                if key not in seen:
                    seen.add(key)
                    catalogs.append(t["catalog_name"])
                    schemas.append(t["db_schema_name"])
            from ob_flight.flight_sql import DB_SCHEMA_SCHEMA
            return pa.table(
                {"catalog_name": catalogs, "db_schema_name": schemas},
                schema=DB_SCHEMA_SCHEMA,
            )
        return build_db_schemas_table()

    def _build_tables_from_db(self) -> pa.Table:
        """Query database for actual table metadata."""
        db_tables, schemas_by_table = self._query_db_schema(self._default_dialect)
        if db_tables:
            from ob_flight.flight_sql import TABLE_SCHEMA
            names: list[str] = []
            catalogs: list[str] = []
            db_schemas: list[str] = []
            types: list[str] = []
            table_schemas: list[bytes] = []
            for t in db_tables:
                tbl_name = t["table_name"]
                names.append(tbl_name)
                catalogs.append(t["catalog_name"])
                db_schemas.append(t["db_schema_name"])
                types.append(t["table_type"])
                arrow_schema = schemas_by_table.get(tbl_name, pa.schema([]))
                table_schemas.append(arrow_schema.serialize().to_pybytes())
            return pa.table(
                {
                    "catalog_name": catalogs,
                    "db_schema_name": db_schemas,
                    "table_name": names,
                    "table_type": types,
                    "table_schema": table_schemas,
                },
                schema=TABLE_SCHEMA,
            )
        # Fallback to model-based catalog
        try:
            model, _ = self._get_model()
            return build_tables_table(model)
        except Exception:
            return build_table_types_table()

    def _execute_sql(self, sql: str, dialect: str) -> flight.RecordBatchStream:
        """Execute SQL on the vendor database and stream results."""
        # Rewrite data object labels → physical table codes
        try:
            model, _ = self._get_model()
            sql = self._rewrite_table_names(sql, model)
        except Exception:
            pass  # no model loaded — execute as-is

        conn = db_connect(dialect)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)

            if cursor.description is None:
                schema = pa.schema([pa.field("status", pa.utf8())])
                batch = rows_to_batch([("OK",)], schema)
                table = pa.Table.from_batches([batch])
                return flight.RecordBatchStream(table)

            # Fetch first batch and scan rows for Arrow type inference
            # (UNION ALL queries may have NULL-padded columns in early rows)
            first_rows = cursor.fetchmany(self._batch_size)
            schema = schema_from_description(cursor.description, sample_rows=first_rows)

            batches: list[pa.RecordBatch] = []
            if first_rows:
                batches.append(rows_to_batch(first_rows, schema))
            while True:
                rows = cursor.fetchmany(self._batch_size)
                if not rows:
                    break
                batches.append(rows_to_batch(rows, schema))

            if not batches:
                batches = [rows_to_batch([], schema)]

            table = pa.Table.from_batches(batches)
            return flight.RecordBatchStream(table)
        finally:
            conn.close()

    def do_action(
        self, context: flight.ServerCallContext, action: flight.Action
    ) -> Any:
        """Handle Flight SQL actions (CreatePreparedStatement, ClosePreparedStatement)."""
        action_type = action.type

        if action_type == ACTION_CREATE_PREPARED_STATEMENT:
            sql = parse_create_prepared_statement(action.body.to_pybytes())
            if sql is None:
                raise flight.FlightServerError("Failed to parse prepared statement query")

            sql, dialect, _ = self._prepare_sql(sql)
            schema = self._probe_schema(sql, dialect)

            handle = uuid.uuid4().bytes
            handle_hex = handle.hex()
            self._prepared[handle_hex] = (sql, dialect, schema)
            logger.debug("Created prepared statement %s: %s", handle_hex, sql[:100])

            result_bytes = build_prepared_statement_result(handle, schema)
            yield flight.Result(pa.py_buffer(result_bytes))

        elif action_type == ACTION_CLOSE_PREPARED_STATEMENT:
            # Best-effort cleanup — handle may already be gone
            try:
                handle = action.body.to_pybytes()
                handle_hex = handle.hex()
                self._prepared.pop(handle_hex, None)
                logger.debug("Closed prepared statement %s", handle_hex)
            except Exception:
                pass
            yield flight.Result(pa.py_buffer(b""))

        else:
            raise flight.FlightServerError(f"Unsupported action: {action_type}")

    def list_flights(
        self, context: flight.ServerCallContext, criteria: bytes
    ) -> Any:
        """List physical database tables for DBeaver schema browser."""
        db_tables, schemas_by_table = self._query_db_schema(self._default_dialect)
        if db_tables:
            for t in db_tables:
                tbl_name = t["table_name"]
                schema = schemas_by_table.get(tbl_name, pa.schema([]))
                descriptor = flight.FlightDescriptor.for_path(
                    t["catalog_name"], t["db_schema_name"], tbl_name
                )
                info = flight.FlightInfo(schema, descriptor, [], -1, -1)
                yield info
        else:
            # Fallback to model-based listing
            try:
                model, _ = self._get_model()
            except Exception:
                return
            for info in model_to_flight_infos(model, "default"):
                yield info
