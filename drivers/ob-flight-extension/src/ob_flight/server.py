"""Arrow Flight SQL server for OrionBelt Semantic Layer."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

import pyarrow as pa
import pyarrow.flight as flight

from ob_flight import server_catalog, server_execution, server_routing
from ob_flight.db_router import connect as db_connect
from ob_flight.flight_sql import (
    ACTION_CLOSE_PREPARED_STATEMENT,
    ACTION_CREATE_PREPARED_STATEMENT,
    CMD_GET_CATALOGS,
    CMD_GET_COLUMNS,
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
    SQL_INFO_SCHEMA,
    build_prepared_statement_result,
    is_flight_sql_command,
    parse_any,
    parse_create_prepared_statement,
    parse_prepared_statement_handle,
    parse_statement_query,
)
from ob_flight.server_routing import (
    _ROUTING_MIDDLEWARE_KEY,
    _SessionRoutingFactory,
    _SessionRoutingMiddleware,
)

logger = logging.getLogger("ob_flight.server")

__all__ = [
    "OBFlightServer",
    "_SessionRoutingFactory",
    "_SessionRoutingMiddleware",
    "_ROUTING_MIDDLEWARE_KEY",
    "db_connect",
]


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
    CMD_GET_COLUMNS,
}

# Catalog query modes — re-exported from server_execution for back-compat.
_MODE_SEMANTIC = server_execution._MODE_SEMANTIC
_MODE_CATALOG = server_execution._MODE_CATALOG
_MODE_REJECTED = server_execution._MODE_REJECTED

# Back-compat name retained while callers transition.
_CATALOG_VIRTUAL_TABLES = server_execution._METADATA_VIEW_NAMES


class OBFlightServer(flight.FlightServerBase):  # type: ignore[misc]
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
        cache: Any = None,
        cache_config: Any = None,
    ) -> None:
        super().__init__(
            location,
            auth_handler=auth_handler,
            middleware={_ROUTING_MIDDLEWARE_KEY: _SessionRoutingFactory()},
        )
        self._session_manager = session_manager
        self._default_dialect = default_dialect
        self._batch_size = batch_size
        # Freshness-driven result cache + config. When ``cache`` is None
        # (or its backend is "noop") the Flight path skips all cache ops.
        # Wired from app.py lifespan so the Flight surface participates
        # in the same TTL / heartbeat contracts as REST /query/execute.
        self._cache = cache
        self._cache_config = cache_config
        self._lock = threading.Lock()
        # Pending queries: ticket_id -> (payload, timestamp). Payload is one
        # of: ``("sql", sql, dialect, cache_meta|None, advertised_schema|None)``,
        # ``("catalog", type_url, body_hex)``, or
        # ``("obsql_catalog_table", pa.Table)`` — so the element types are
        # heterogeneous (str / dict / pa.Schema / pa.Table). Use ``tuple[Any, ...]``.
        self._pending: dict[str, tuple[tuple[Any, ...], float]] = {}
        # Prepared statements: handle_hex -> (kind_or_sql, dialect_or_table,
        # schema, cache_meta|None). Element 2 is either the dialect string
        # (regular SQL) or a precomputed ``pa.Table`` (catalog mode marked
        # by ``kind_or_sql == "__catalog__"``), so the tuple's middle
        # elements are heterogeneous — Any here, narrowed at the read site.
        self._prepared: dict[str, tuple[Any, ...]] = {}
        # TTL for pending tickets (seconds) — entries older than this are evicted
        self._pending_ttl = 300

    def _store_pending(self, ticket_id: str, payload: tuple[Any, ...]) -> None:
        """Store a pending query with timestamp, evicting stale entries."""
        now = time.monotonic()
        with self._lock:
            # Evict expired entries
            expired = [k for k, (_, ts) in self._pending.items() if now - ts > self._pending_ttl]
            for k in expired:
                del self._pending[k]
            self._pending[ticket_id] = (payload, now)

    def _pop_pending(self, ticket_id: str) -> tuple[str, ...] | None:
        """Pop a pending query by ticket ID, returning None if not found or expired."""
        now = time.monotonic()
        with self._lock:
            entry = self._pending.pop(ticket_id, None)
        if entry is None:
            return None
        payload, ts = entry
        if now - ts > self._pending_ttl:
            return None
        return payload

    # ------------------------------------------------------------------
    # Session / model routing — delegates to ``server_routing``.
    # ------------------------------------------------------------------
    @staticmethod
    def _selector_from_context(
        context: flight.ServerCallContext | None,
    ) -> str | None:
        return server_routing.selector_from_context(context)

    def _list_available_model_names(self) -> list[str]:
        return server_routing.list_available_model_names(self)

    def _resolve_model_by_name(
        self,
        stashed_name: str,
        context: flight.ServerCallContext | None,
    ) -> Any:
        return server_routing.resolve_model_by_name(self, stashed_name, context)

    def _get_model(self, context: flight.ServerCallContext | None = None) -> tuple[Any, str]:
        return server_routing.get_model(self, context)

    def _stamp_model(self, store: Any, session_id: str) -> tuple[Any, str]:
        return server_routing.stamp_model(self, store, session_id)

    @staticmethod
    def _format_unknown_model_error(selector: str, available: list[str]) -> str:
        return server_routing.format_unknown_model_error(selector, available)

    @staticmethod
    def _format_ambiguous_model_error(available: list[str]) -> str:
        return server_routing.format_ambiguous_model_error(available)

    # ------------------------------------------------------------------
    # SQL classification / preparation / execution — delegates to
    # ``server_execution``.
    # ------------------------------------------------------------------
    def _rewrite_table_names(self, sql: str, model: Any) -> str:
        return server_execution.rewrite_table_names(self, sql, model)

    def _classify_sql(self, sql: str, model: Any) -> str:
        return server_execution.classify_sql(self, sql, model)

    def _semantic_result_schema(self, query: Any, model: Any) -> pa.Schema:
        return server_execution.semantic_result_schema(self, query, model)

    def _prepare_sql(
        self,
        sql: str,
        context: flight.ServerCallContext | None = None,
    ) -> tuple[str, str, Any, pa.Schema | None, str, dict[str, Any] | None]:
        return server_execution.prepare_sql(self, sql, context)

    def _build_cache_meta(
        self,
        *,
        compiled_sql: str,
        dialect: str,
        context: flight.ServerCallContext | None,
        physical_tables: list[str],
    ) -> dict[str, Any] | None:
        return server_execution.build_cache_meta(
            self,
            compiled_sql=compiled_sql,
            dialect=dialect,
            context=context,
            physical_tables=physical_tables,
        )

    @staticmethod
    def _reject_write_operation(sql: str) -> None:
        server_execution.reject_write_operation(sql)

    def _compile_obml(self, obml: dict[str, Any], model: Any, dialect: str) -> Any:
        return server_execution.compile_obml(self, obml, model, dialect)

    def _execute_sql(
        self,
        sql: str,
        dialect: str,
        cache_meta: dict[str, Any] | None = None,
        result_schema: pa.Schema | None = None,
    ) -> flight.RecordBatchStream:
        return server_execution.execute_sql(self, sql, dialect, cache_meta, result_schema)

    def _cache_get_table(self, key: str) -> pa.Table | None:
        return server_execution.cache_get_table(self, key)

    def _cache_put_table(self, table: pa.Table, cache_meta: dict[str, Any]) -> None:
        server_execution.cache_put_table(self, table, cache_meta)

    # ------------------------------------------------------------------
    # Catalog / metadata — delegates to ``server_catalog``.
    # ------------------------------------------------------------------
    def _handle_catalog_sql(self, sql: str, model: Any) -> pa.Table:
        return server_catalog.handle_catalog_sql(self, sql, model)

    @staticmethod
    def _catalog_tables_table(model: Any) -> pa.Table:
        return server_catalog.catalog_tables_table(model)

    @staticmethod
    def _catalog_columns_table(model: Any) -> pa.Table:
        return server_catalog.catalog_columns_table(model)

    @staticmethod
    def _catalog_empty_table() -> pa.Table:
        return server_catalog.catalog_empty_table()

    def _catalog_scalar_probe_table(self, ast: Any) -> pa.Table:
        return server_catalog.catalog_scalar_probe_table(ast)

    @staticmethod
    def _detect_virtual_table(sql: str) -> str | None:
        return server_catalog.detect_virtual_table(sql)

    def _query_virtual_table(
        self,
        vt_name: str,
        context: flight.ServerCallContext | None = None,
    ) -> flight.RecordBatchStream:
        return server_catalog.query_virtual_table(self, vt_name, context)

    def _probe_schema(self, sql: str, dialect: str) -> pa.Schema:
        return server_catalog.probe_schema(self, sql, dialect)

    def _resolve_model_for_catalog(
        self,
        catalog_filter: str | None,
        context: flight.ServerCallContext | None,
        db_schema_filter: str | None = None,
    ) -> Any:
        return server_catalog.resolve_model_for_catalog(
            self, catalog_filter, context, db_schema_filter
        )

    def _build_tables_from_model(
        self,
        context: flight.ServerCallContext | None = None,
        *,
        table_filter: str | None = None,
        catalog_filter: str | None = None,
        db_schema_filter: str | None = None,
    ) -> pa.Table:
        return server_catalog.build_tables_from_model(
            self,
            context,
            table_filter=table_filter,
            catalog_filter=catalog_filter,
            db_schema_filter=db_schema_filter,
        )

    def _build_columns_from_model(
        self,
        context: flight.ServerCallContext | None = None,
        *,
        table_filter: str | None = None,
        catalog_filter: str | None = None,
        db_schema_filter: str | None = None,
    ) -> pa.Table:
        return server_catalog.build_columns_from_model(
            self,
            context,
            table_filter=table_filter,
            catalog_filter=catalog_filter,
            db_schema_filter=db_schema_filter,
        )

    def _handle_catalog_command(
        self,
        type_url: str,
        cmd_value: bytes = b"",
        context: flight.ServerCallContext | None = None,
    ) -> flight.RecordBatchStream:
        return server_catalog.handle_catalog_command(self, type_url, cmd_value, context)

    # ------------------------------------------------------------------
    # Flight protocol overrides — stay on the server class.
    # ------------------------------------------------------------------
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
                (
                    prepared_sql,
                    dialect,
                    prep_model,
                    schema_hint,
                    mode,
                    cache_meta,
                ) = self._prepare_sql(sql, context=context)
                if mode == _MODE_CATALOG:
                    # Pre-compute the catalog table so the FlightInfo schema
                    # matches what do_get streams. Without this, JDBC clients
                    # see the placeholder ``result`` schema and the actual
                    # columns of e.g. ``_dimensions_metadata`` never appear.
                    table = self._handle_catalog_sql(prepared_sql, prep_model)
                    self._store_pending(ticket_id, ("obsql_catalog_table", table))
                    schema = table.schema
                else:
                    schema = (
                        schema_hint
                        if schema_hint is not None
                        else self._probe_schema(prepared_sql, dialect)
                    )
                    # Stash the advertised schema so a cache hit in do_get can
                    # be cast back to it (an empty result's cached blob decodes
                    # to null-typed columns otherwise).
                    self._store_pending(
                        ticket_id, ("sql", prepared_sql, dialect, cache_meta, schema)
                    )

            elif type_url == CMD_PREPARED_STATEMENT_QUERY:
                # Look up prepared statement by handle
                handle = parse_prepared_statement_handle(value)
                if handle is None:
                    raise flight.FlightServerError("Invalid prepared statement handle")
                handle_hex = handle.hex()
                if handle_hex not in self._prepared:
                    raise flight.FlightServerError(f"Unknown prepared statement: {handle_hex}")
                entry = self._prepared[handle_hex]
                first, payload, schema = entry[0], entry[1], entry[2]
                cache_meta_pp = entry[3] if len(entry) > 3 else None
                if first == "__catalog__":
                    # Precomputed catalog table — stream it directly.
                    self._store_pending(ticket_id, ("obsql_catalog_table", payload))
                else:
                    # Regular SQL path: first=sql, payload=dialect, schema=entry[2].
                    # Carry the advertised schema so a cache hit is cast to it.
                    self._store_pending(ticket_id, ("sql", first, payload, cache_meta_pp, schema))

            elif type_url in _CATALOG_COMMANDS:
                # Stash both the command type and its protobuf body so
                # do_get can parse filters (e.g. table_name_filter_pattern
                # for CommandGetTables / CommandGetColumns). Stored as hex
                # so the tuple stays plain str/bytes-only.
                self._store_pending(ticket_id, ("catalog", type_url, value.hex()))
                # SqlInfo has a structured spec schema (uint32 + dense_union).
                # JDBC clients inspect the FlightInfo schema before calling
                # do_get, so returning the placeholder `result` column makes
                # them treat the response as empty (DBeaver shows "Server: ?").
                if type_url == CMD_GET_SQL_INFO:
                    schema = SQL_INFO_SCHEMA
                else:
                    schema = pa.schema([pa.field("result", pa.utf8())])

            else:
                raise flight.FlightServerError(f"Unsupported Flight SQL command: {type_url}")

            ticket = flight.Ticket(ticket_id.encode("utf-8"))
            endpoint = flight.FlightEndpoint(ticket, [])
            return flight.FlightInfo(schema, descriptor, [endpoint], -1, -1)

        # Plain text: SQL or OBML
        query_str = command_bytes.decode("utf-8")
        (
            prepared_sql,
            dialect,
            prep_model,
            schema_hint,
            mode,
            cache_meta,
        ) = self._prepare_sql(query_str, context=context)
        if mode == _MODE_CATALOG:
            # Precompute table so FlightInfo advertises the real schema.
            table = self._handle_catalog_sql(prepared_sql, prep_model)
            self._store_pending(ticket_id, ("obsql_catalog_table", table))
            schema = table.schema
        else:
            schema = (
                schema_hint
                if schema_hint is not None
                else self._probe_schema(prepared_sql, dialect)
            )
            # Advertised schema rides along so a cache hit can be cast to it.
            self._store_pending(ticket_id, ("sql", prepared_sql, dialect, cache_meta, schema))
        ticket = flight.Ticket(ticket_id.encode("utf-8"))
        endpoint = flight.FlightEndpoint(ticket, [])
        return flight.FlightInfo(schema, descriptor, [endpoint], -1, -1)

    def do_get(
        self, context: flight.ServerCallContext, ticket: flight.Ticket
    ) -> flight.RecordBatchStream:
        """Execute a query or return catalog metadata."""
        ticket_id = ticket.ticket.decode("utf-8")

        pending = self._pop_pending(ticket_id)
        if pending is None:
            raise flight.FlightServerError(f"Unknown ticket: {ticket_id}")

        kind = pending[0]

        if kind == "catalog":
            # pending = ("catalog", type_url, value_hex)
            cmd_value = bytes.fromhex(pending[2]) if len(pending) > 2 else b""
            return self._handle_catalog_command(pending[1], cmd_value, context=context)

        if kind == "obsql_catalog_table":
            # Precomputed at get_flight_info time so the FlightInfo schema
            # advertised to the JDBC client matches the streamed payload.
            return flight.RecordBatchStream(pending[1])

        if kind == "obsql_catalog":
            # Legacy lazy path — kept for callers that store SQL + selector
            # instead of the precomputed table. Compute on demand.
            stashed_selector = str(pending[2]) if len(pending) > 2 else ""
            model = self._resolve_model_by_name(stashed_selector, context)
            table = self._handle_catalog_sql(str(pending[1]), model)
            return flight.RecordBatchStream(table)

        # kind == "sql" — 4th element is cache_meta, 5th the advertised schema
        sql = str(pending[1])
        dialect = str(pending[2])
        cache_meta_raw = pending[3] if len(pending) > 3 else None
        cache_meta: dict[str, Any] | None = (
            cache_meta_raw if isinstance(cache_meta_raw, dict) else None
        )
        schema_raw = pending[4] if len(pending) > 4 else None
        result_schema: pa.Schema | None = schema_raw if isinstance(schema_raw, pa.Schema) else None
        return self._execute_sql(sql, dialect, cache_meta=cache_meta, result_schema=result_schema)

    def do_action(self, context: flight.ServerCallContext, action: flight.Action) -> Any:
        """Handle Flight SQL actions (CreatePreparedStatement, ClosePreparedStatement)."""
        action_type = action.type

        if action_type == ACTION_CREATE_PREPARED_STATEMENT:
            sql = parse_create_prepared_statement(action.body.to_pybytes())
            if sql is None:
                raise flight.FlightServerError("Failed to parse prepared statement query")

            (
                prepared_sql,
                dialect,
                prep_model,
                schema_hint,
                mode,
                cache_meta,
            ) = self._prepare_sql(sql, context=context)
            if mode == _MODE_CATALOG:
                # DBeaver's SQL editor uses prepared statements, so we
                # MUST advertise the real schema here. Precompute the
                # catalog table (same trick as get_flight_info) and stash
                # the pa.Table for the eventual do_get.
                catalog_table = self._handle_catalog_sql(prepared_sql, prep_model)
                schema = catalog_table.schema
                # Sentinel first element lets CMD_PREPARED_STATEMENT_QUERY
                # dispatch to the catalog-table branch (precomputed pa.Table
                # instead of SQL/dialect to execute on the warehouse).
                handle = uuid.uuid4().bytes
                handle_hex = handle.hex()
                # 4th slot is cache_meta for SQL prepared statements; catalog
                # prepared statements have no cache plumbing — pad with None
                # so the tuple shape matches ``self._prepared``'s annotation.
                self._prepared[handle_hex] = ("__catalog__", catalog_table, schema, None)
            else:
                sql = prepared_sql
                schema = (
                    schema_hint if schema_hint is not None else self._probe_schema(sql, dialect)
                )
                handle = uuid.uuid4().bytes
                handle_hex = handle.hex()
                # Stash cache_meta on the prepared entry under a 4th slot
                # so CMD_PREPARED_STATEMENT_QUERY can forward it to do_get.
                self._prepared[handle_hex] = (sql, dialect, schema, cache_meta)

            logger.debug(
                "Created prepared statement %s (mode=%s, %d cols)",
                handle_hex,
                mode,
                len(schema),
            )

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

    def list_flights(self, context: flight.ServerCallContext, _criteria: bytes) -> Any:
        """List the semantic virtual table + metadata views."""
        from ob_flight.catalog import model_to_flight_infos

        try:
            model, _ = self._get_model(context)
        except Exception:
            return
        for info in model_to_flight_infos(model, "default"):
            yield info
