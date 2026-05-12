"""Arrow Flight SQL server for OrionBelt Semantic Layer."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

import pyarrow as pa
import pyarrow.flight as flight

from ob_driver_core.detection import is_obml, parse_obml

from ob_flight.catalog import (
    VIRTUAL_TABLES,
    build_dimensions_data,
    build_measures_data,
    build_metrics_data,
    model_to_flight_infos,
    model_virtual_table_name,
)
from ob_flight.converters import rows_to_batch, schema_from_description
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
    build_catalogs_table,
    build_columns_table,
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


# Query modes for the Flight SQL surface. See PLAN_flight_natural_sql.md §3.2.
# OBSL is a semantic layer, not a JDBC proxy — there are no escape hatches.
_MODE_SEMANTIC = "semantic"
"""OBSQL query against the model's virtual table — compiled through the pipeline."""
_MODE_CATALOG = "catalog"
"""SHOW / DESCRIBE / information_schema / pg_catalog / canned probes — answered
from the model, never touches the warehouse."""
_MODE_REJECTED = "rejected"
"""Anything else — raw SQL against unknown targets, data-object labels, etc.
Rejects with RAW_SQL_REJECTED."""


# Catalog FROM-target prefixes / system-function tokens — anything matching is
# routed to a model-backed catalog handler instead of the warehouse.
_CATALOG_SCHEMAS = ("information_schema.", "pg_catalog.")
_CATALOG_VIRTUAL_TABLES = ("_dimensions", "_measures", "_metrics")
_CATALOG_STATEMENT_KINDS = {
    "Show",  # SHOW TABLES, SHOW COLUMNS, SHOW DATABASES (some dialects)
    "Describe",  # DESCRIBE / DESC
    "Use",  # USE <database>
    "Set",  # SET <var> = <value>
    "Command",  # sqlglot's fallback for dialect-unknown commands like SHOW
}
_CATALOG_SCALAR_PROBES = {
    "version",
    "current_database",
    "current_schema",
    "current_user",
    "current_role",
    "session_user",
    "user",
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
        self._lock = threading.Lock()
        # Pending queries: ticket_id -> (payload, timestamp)
        # payload is either ("sql", sql, dialect) or ("catalog", type_url)
        self._pending: dict[str, tuple[tuple[str, ...], float]] = {}
        # Prepared statements: handle_hex -> (sql, dialect, schema)
        self._prepared: dict[str, tuple[str, str, pa.Schema]] = {}
        # TTL for pending tickets (seconds) — entries older than this are evicted
        self._pending_ttl = 300

    def _store_pending(self, ticket_id: str, payload: tuple[str, ...]) -> None:
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

    def _get_model(self) -> tuple[Any, str]:
        """Get the default model from the session manager.

        Returns (model, dialect) tuple.
        Uses the default session's first model (single-model mode). Stamps
        the model_id onto the model as ``_ob_model_id`` so the virtual-table
        name resolver and catalog code can find it.
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
        # Surface the model_id for downstream catalog code that needs to
        # produce a stable virtual-table name. Pydantic v2 doesn't allow
        # setattr on undeclared fields by default, so we use the underlying
        # __dict__ to side-step validation.
        try:
            model.__dict__["_ob_model_id"] = model_id
        except Exception:
            pass
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

    def _classify_sql(self, sql: str, model: Any) -> str:
        """Classify a SQL query into one of three handling modes.

        Returns one of:

        * ``_MODE_SEMANTIC`` — OBSQL query against the model's virtual table.
        * ``_MODE_CATALOG`` — discovery query (``SHOW``, ``DESCRIBE``,
          ``information_schema.*``, ``pg_catalog.*``, canned probes like
          ``SELECT version()``). Routed to model-backed responses;
          **never reaches the warehouse**.
        * ``_MODE_REJECTED`` — anything else (raw SQL against unknown
          targets, FROM-<data-object-label>, multi-statement, parse
          failures). The caller raises ``RAW_SQL_REJECTED``.

        OBSL is a semantic layer, not a JDBC proxy — there are no escape
        hatches. See ``design/PLAN_flight_natural_sql.md`` §3.2.
        """
        # Strip the bare trailing ``WITH ROLLUP``/``WITH CUBE`` before parsing
        # — sqlglot requires a GROUP BY in front of those modifiers, but the
        # OBSQL surface lets callers write them as a trailing flag.
        from orionbelt.compiler.sql_translator import _strip_trailing_grouping

        cleaned, _ = _strip_trailing_grouping(sql)

        try:
            import sqlglot
            import sqlglot.expressions as exp

            ast = sqlglot.parse_one(cleaned)
        except Exception:
            return _MODE_REJECTED

        # SHOW / DESCRIBE / USE / SET — top-level non-Select catalog statements
        if type(ast).__name__ in _CATALOG_STATEMENT_KINDS:
            return _MODE_CATALOG

        if not isinstance(ast, exp.Select):
            # INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, MERGE,
            # multi-statement, Union, etc. all reject as raw — write ops
            # surface a more specific error in _prepare_sql.
            return _MODE_REJECTED

        # SELECT with no FROM — typically scalar probes (SELECT 1,
        # SELECT version(), SELECT current_schema()). Treat as catalog if
        # the projected names are known canned-probe functions.
        from_node = ast.args.get("from")
        if from_node is None:
            for proj in ast.expressions:
                if isinstance(proj, exp.Alias):
                    proj = proj.this
                if isinstance(proj, exp.Anonymous | exp.Func):
                    fname = (getattr(proj, "name", "") or "").lower()
                    if fname in _CATALOG_SCALAR_PROBES:
                        return _MODE_CATALOG
                if isinstance(proj, exp.Literal):
                    return _MODE_CATALOG  # SELECT 1 → connectivity probe
            return _MODE_REJECTED

        # FROM something — examine the target
        table_node = getattr(from_node, "this", None)
        if table_node is None and getattr(from_node, "expressions", None):
            table_node = from_node.expressions[0]
        if table_node is None:
            return _MODE_REJECTED

        # Pull the qualified name (`pg_catalog.pg_class` etc.) and the
        # bare identifier separately.
        full_sql = table_node.sql().lower()
        bare = getattr(table_node, "name", None) or table_node.sql()
        bare = str(bare).strip('"').strip("`").strip("'").lower()

        # Catalog schemas
        for prefix in _CATALOG_SCHEMAS:
            if prefix in full_sql:
                return _MODE_CATALOG
        # Virtual metadata tables shipped with the model
        if bare in _CATALOG_VIRTUAL_TABLES:
            return _MODE_CATALOG

        # Semantic — the model's virtual table
        vt = model_virtual_table_name(model).lower()
        if bare == vt:
            return _MODE_SEMANTIC

        # Anything else (including FROM-<data-object-label>) rejects.
        return _MODE_REJECTED

    def _semantic_result_schema(self, query: Any, model: Any) -> pa.Schema:
        """Build the result Arrow schema for a semantic query without DB I/O.

        Reads ``result_type`` from each selected dimension / measure / metric.
        See ``design/PLAN_flight_natural_sql.md`` §3.4 "Schema probe".
        """
        from ob_flight.catalog import _obml_type_to_arrow

        fields: list[pa.Field] = []
        dims = getattr(query.select, "dimensions", [])
        measures = getattr(query.select, "measures", [])
        for name in dims:
            label = name if isinstance(name, str) else getattr(name, "alias", None)
            if label is None:
                continue
            dim = model.dimensions.get(label)
            rt = getattr(getattr(dim, "result_type", None), "value", None) or "string"
            fields.append(pa.field(label, _obml_type_to_arrow(rt)))
        for label in measures:
            meas = model.measures.get(label)
            met = model.metrics.get(label) if meas is None else None
            if meas is not None:
                rt = getattr(getattr(meas, "result_type", None), "value", None) or "float"
                fields.append(pa.field(label, _obml_type_to_arrow(rt)))
            elif met is not None:
                fields.append(pa.field(label, pa.float64()))
            else:
                fields.append(pa.field(label, pa.float64()))
        if query.grouping is not None:
            # GROUPING() flag columns — int64, one per dimension. See
            # PLAN_with_rollup.md §"Output: GROUPING() flag columns".
            for name in dims:
                label = name if isinstance(name, str) else getattr(name, "alias", None)
                if label is None:
                    continue
                fields.append(pa.field(f"_g_{label}", pa.int64()))
        return pa.schema(fields)

    def _prepare_sql(self, sql: str) -> tuple[str, str, Any, pa.Schema | None, str]:
        """Resolve model, classify SQL, translate / compile / route.

        Returns ``(final_sql_or_token, dialect, model, schema_hint, mode)``.

        * ``mode == _MODE_SEMANTIC`` — ``final_sql_or_token`` is compiled
          warehouse SQL; ``schema_hint`` is the result schema computed
          from the model. Caller executes against the warehouse.
        * ``mode == _MODE_CATALOG`` — ``final_sql_or_token`` is the
          original SQL; the caller routes to ``_handle_catalog_sql``
          which returns model-backed metadata. ``schema_hint`` is None.
        * Anything else raises before returning.

        Hard rules (v2.4.0+, no env flags):

        * **Raw SQL pass-through is never allowed.** OBSL is a semantic
          layer, not a JDBC proxy. Unrecognised FROM targets reject
          with ``RAW_SQL_REJECTED``.
        * **Write operations (DDL / DML / TCL) are never allowed.** Only
          ``SELECT`` reaches the warehouse. Reject with
          ``WRITE_OPERATION_REJECTED``.
        * **Catalog discovery is always allowed**, never touches the
          warehouse — answered from the model.
        """
        from orionbelt.compiler.pipeline import CompilationPipeline
        from orionbelt.compiler.sql_translator import (
            SQLTranslationError,
            translate_sql_to_query,
        )

        model, dialect = self._get_model()

        # Write-op early reject — covers DDL/DML/TCL across all paths.
        # OBML YAML detection happens after this so YAML-wrapped writes
        # also can't sneak through (OBML has no write syntax, but it's
        # cheap defence-in-depth).
        self._reject_write_operation(sql)

        # OBML YAML wrapped as a SQL string — power-user path
        if is_obml(sql):
            obml = parse_obml(sql)
            sql = self._compile_obml(obml, model, dialect)
            logger.info("Compiled OBML to SQL: %s", sql[:200])
            sql = self._rewrite_table_names(sql, model)
            return sql, dialect, model, None, _MODE_SEMANTIC

        mode = self._classify_sql(sql, model)

        if mode == _MODE_SEMANTIC:
            try:
                query = translate_sql_to_query(sql, model)
            except SQLTranslationError as exc:
                detail = "; ".join(f"[{e.code}] {e.message}" for e in exc.errors)
                raise flight.FlightServerError(
                    f"OrionBelt Semantic QL translation failed: {detail}"
                ) from None
            compiled = CompilationPipeline().compile(query, model, dialect)
            sql = self._rewrite_table_names(compiled.sql, model)
            logger.info("Compiled OBSQL → %s", sql[:200])
            schema_hint = self._semantic_result_schema(query, model)
            return sql, dialect, model, schema_hint, _MODE_SEMANTIC

        if mode == _MODE_CATALOG:
            # Don't compile or rewrite — the caller routes the original SQL
            # to a model-backed catalog handler. Schema is computed there.
            return sql, dialect, model, None, _MODE_CATALOG

        # _MODE_REJECTED — no escape hatch.
        raise flight.FlightServerError(
            "[RAW_SQL_REJECTED] Raw SQL pass-through is not supported. "
            "OBSL accepts: (1) OBSQL queries against the model's virtual "
            "table, (2) compiled QueryObjects via the REST API, and "
            "(3) catalog discovery (SHOW / DESCRIBE / information_schema / "
            "pg_catalog). Arbitrary warehouse SQL is rejected by design."
        )

    @staticmethod
    def _reject_write_operation(sql: str) -> None:
        """Reject DDL / DML / TCL statements at the door.

        Parses the SQL with sqlglot and rejects anything whose top-level
        node is a write operation. ``SELECT`` and ``WITH ... SELECT`` CTEs
        pass; the catalog-specific ``SHOW`` / ``DESCRIBE`` / ``USE`` /
        ``SET`` statements also pass (handled by catalog mode). Anything
        else raises ``WRITE_OPERATION_REJECTED``.

        Defence-in-depth — the translator already rejects non-SELECT for
        semantic mode, but this guard ensures write ops can't reach the
        warehouse via *any* path.
        """
        try:
            import sqlglot
            import sqlglot.expressions as exp

            ast = sqlglot.parse_one(sql)
        except Exception:
            # Parse failure isn't a write op per se — let downstream
            # classification surface the right error.
            return
        if isinstance(ast, exp.Select):
            return
        if type(ast).__name__ in _CATALOG_STATEMENT_KINDS:
            return
        # Insert, Update, Delete, Drop, Create, Alter, Truncate, Merge,
        # Commit, Rollback, Grant, Revoke, etc. — all reject.
        kind = type(ast).__name__.upper()
        if kind in {"UNION"}:  # set ops surface as raw
            return
        raise flight.FlightServerError(
            f"[WRITE_OPERATION_REJECTED] {kind} statements are not allowed. "
            "OBSL is read-only — only SELECT queries (and catalog discovery) "
            "reach the warehouse."
        )

    def _handle_catalog_sql(self, sql: str, model: Any) -> pa.Table:
        """Answer a catalog/discovery SQL query from the model — no warehouse hop.

        Returns a :class:`pa.Table` so callers can wrap it in a
        ``RecordBatchStream`` once. Covers the common BI-tool / JDBC
        introspection probes:

        * ``SHOW TABLES`` → list of virtual tables (the model + metadata views)
        * ``SHOW COLUMNS FROM <model>`` / ``DESCRIBE <model>`` → dim+measure+metric
        * ``SELECT … FROM information_schema.tables`` → same as SHOW TABLES
        * ``SELECT … FROM information_schema.columns`` → flat column list
        * ``SELECT … FROM pg_catalog.*`` → mapped to the same model-backed responses
        * Canned scalar probes: ``SELECT 1``, ``SELECT version()``, ``current_schema()``

        Unrecognised catalog queries return an empty result set rather
        than failing — Postgres / MySQL clients probe a long tail of
        system tables, and breaking on every unknown probe blocks tool
        discovery. Empty results are the right default — clients adapt.
        """
        import sqlglot
        import sqlglot.expressions as exp

        try:
            ast = sqlglot.parse_one(sql)
        except Exception:
            return self._catalog_empty_table()

        kind = type(ast).__name__

        # SHOW TABLES / SHOW COLUMNS / DESCRIBE / etc.
        # sqlglot parses bare SHOW/DESCRIBE statements as ``Command`` when
        # the dialect doesn't have an explicit Show node; inspect the raw
        # text in that case.
        if kind in {"Show", "Describe", "Command"}:
            raw_text = sql.strip().upper()
            this = ast.args.get("this")
            target_arg = (str(this).upper() if this is not None else "") or (
                getattr(ast, "name", "") or ""
            ).upper()
            if (
                "COLUMN" in target_arg
                or kind == "Describe"
                or raw_text.startswith("DESC")
                or "SHOW COLUMN" in raw_text
            ):
                return self._catalog_columns_table(model)
            # Default: list tables
            return self._catalog_tables_table(model)

        # USE / SET — accept silently (Postgres clients send these on connect)
        if kind in {"Use", "Set"}:
            return self._catalog_empty_table()

        # SELECT against pg_catalog / information_schema or scalar probes
        if isinstance(ast, exp.Select):
            from_node = ast.args.get("from")
            if from_node is None:
                # Scalar probe: SELECT 1, SELECT version(), SELECT current_schema()
                return self._catalog_scalar_probe_table(ast)
            target_sql = ""
            table_node = getattr(from_node, "this", None) or (
                from_node.expressions[0] if from_node.expressions else None
            )
            if table_node is not None:
                target_sql = table_node.sql().lower()
            bare = ""
            if table_node is not None:
                bare_raw = getattr(table_node, "name", None) or table_node.sql()
                bare = str(bare_raw).strip('"').strip("`").strip("'").lower()
            if "information_schema.tables" in target_sql or "pg_catalog.pg_class" in target_sql:
                return self._catalog_tables_table(model)
            if (
                "information_schema.columns" in target_sql
                or "pg_catalog.pg_attribute" in target_sql
            ):
                return self._catalog_columns_table(model)
            if bare == "_dimensions":
                return build_dimensions_data(model)
            if bare == "_measures":
                return build_measures_data(model)
            if bare == "_metrics":
                return build_metrics_data(model)

        # Unknown catalog probe — empty result. Tool moves on.
        return self._catalog_empty_table()

    @staticmethod
    def _catalog_tables_table(model: Any) -> pa.Table:
        """One row per queryable virtual table (model + metadata views)."""
        from ob_flight.flight_sql import build_tables_table

        return build_tables_table(model)

    @staticmethod
    def _catalog_columns_table(model: Any) -> pa.Table:
        """One row per dim/measure/metric of the model's virtual table."""
        from ob_flight.flight_sql import build_columns_table

        return build_columns_table(model)

    @staticmethod
    def _catalog_empty_table() -> pa.Table:
        """Empty single-column response — used for unknown catalog probes."""
        schema = pa.schema([pa.field("result", pa.utf8())])
        return pa.table({"result": pa.array([], type=pa.utf8())}, schema=schema)

    def _catalog_scalar_probe_table(self, ast: Any) -> pa.Table:
        """Answer common scalar probes — SELECT 1, version(), current_schema()."""
        import sqlglot.expressions as exp

        values: list[str] = []
        names: list[str] = []
        for i, proj in enumerate(ast.expressions):
            alias_name: str | None = None
            inner = proj
            if isinstance(proj, exp.Alias):
                alias_name = proj.alias_or_name
                inner = proj.this
            if isinstance(inner, exp.Literal):
                values.append(str(inner.this))
                names.append(alias_name or f"col_{i + 1}")
                continue
            fname = (getattr(inner, "name", "") or "").lower()
            if fname in {"version"}:
                values.append("OrionBelt Semantic Layer (OBSL)")
            elif fname in {"current_database"}:
                values.append("orionbelt")
            elif fname in {"current_schema"}:
                values.append("model")
            elif fname in {"current_user", "current_role", "session_user", "user"}:
                values.append("obsl")
            else:
                values.append("")
            names.append(alias_name or fname or f"col_{i + 1}")
        if not values:
            return self._catalog_empty_table()
        schema = pa.schema([pa.field(n, pa.utf8()) for n in names])
        return pa.table({n: [v] for n, v in zip(names, values, strict=True)}, schema=schema)

    @staticmethod
    def _detect_virtual_table(sql: str) -> str | None:
        """Detect if SQL queries a virtual metadata table (_dimensions, etc.).

        Uses word-boundary matching to avoid false positives on table/column
        names like ``sales_dimensions`` or ``total_measures``.
        """
        import re

        sql_lower = sql.lower()
        for vt in VIRTUAL_TABLES:
            # Match the virtual table name as a standalone word
            if re.search(rf"\b{re.escape(vt)}\b", sql_lower):
                return vt
        return None

    def _query_virtual_table(self, vt_name: str) -> flight.RecordBatchStream:
        """Return data for a virtual metadata table."""
        model, _ = self._get_model()
        if vt_name == "_dimensions":
            table = build_dimensions_data(model)
        elif vt_name == "_measures":
            table = build_measures_data(model)
        elif vt_name == "_metrics":
            table = build_metrics_data(model)
        else:
            raise flight.FlightServerError(f"Unknown virtual table: {vt_name}")
        return flight.RecordBatchStream(table)

    def _probe_schema(self, sql: str, dialect: str) -> pa.Schema:
        """Probe the database to determine the result schema for a query.

        Executes the query, peeks at a small batch for accurate type inference
        (UNION ALL queries may have NULL-padded columns in early rows).
        Falls back to a generic schema on error.
        """
        vt = self._detect_virtual_table(sql)
        if vt is not None:
            return VIRTUAL_TABLES[vt]

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

    def _build_tables_from_model(self) -> pa.Table:
        """Build the CommandGetTables response.

        Lists the semantic virtual table + ``_dimensions``/``_measures``/
        ``_metrics`` views. Data objects are intentionally hidden — they're
        not queryable through the semantic layer.
        """
        try:
            model, _ = self._get_model()
        except Exception:
            model = None
        return build_tables_table(model)

    def _build_columns_from_model(self) -> pa.Table:
        """Build the CommandGetColumns response.

        Returns dim / measure / metric columns of the semantic virtual
        table. Data-object physical columns are intentionally hidden.
        """
        try:
            model, _ = self._get_model()
        except Exception:
            model = None
        return build_columns_table(model)

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
                prepared_sql, dialect, _, schema_hint, mode = self._prepare_sql(sql)
                if mode == _MODE_CATALOG:
                    self._store_pending(ticket_id, ("obsql_catalog", prepared_sql))
                    schema = pa.schema([pa.field("result", pa.utf8())])
                else:
                    self._store_pending(ticket_id, ("sql", prepared_sql, dialect))
                    schema = (
                        schema_hint
                        if schema_hint is not None
                        else self._probe_schema(prepared_sql, dialect)
                    )

            elif type_url == CMD_PREPARED_STATEMENT_QUERY:
                # Look up prepared statement by handle
                handle = parse_prepared_statement_handle(value)
                if handle is None:
                    raise flight.FlightServerError("Invalid prepared statement handle")
                handle_hex = handle.hex()
                if handle_hex not in self._prepared:
                    raise flight.FlightServerError(f"Unknown prepared statement: {handle_hex}")
                sql, dialect, schema = self._prepared[handle_hex]
                self._store_pending(ticket_id, ("sql", sql, dialect))

            elif type_url in _CATALOG_COMMANDS:
                # Store the raw command for do_get to handle
                self._store_pending(ticket_id, ("catalog", type_url))
                schema = pa.schema([pa.field("result", pa.utf8())])

            else:
                raise flight.FlightServerError(f"Unsupported Flight SQL command: {type_url}")

            ticket = flight.Ticket(ticket_id.encode("utf-8"))
            endpoint = flight.FlightEndpoint(ticket, [])
            return flight.FlightInfo(schema, descriptor, [endpoint], -1, -1)

        # Plain text: SQL or OBML
        query_str = command_bytes.decode("utf-8")
        prepared_sql, dialect, _, schema_hint, mode = self._prepare_sql(query_str)
        if mode == _MODE_CATALOG:
            self._store_pending(ticket_id, ("obsql_catalog", prepared_sql))
            schema = pa.schema([pa.field("result", pa.utf8())])
        else:
            self._store_pending(ticket_id, ("sql", prepared_sql, dialect))
            schema = (
                schema_hint
                if schema_hint is not None
                else self._probe_schema(prepared_sql, dialect)
            )
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
            return self._handle_catalog_command(pending[1])

        if kind == "obsql_catalog":
            # OBSQL-routed catalog SQL — answered from the model
            model, _ = self._get_model()
            table = self._handle_catalog_sql(str(pending[1]), model)
            return flight.RecordBatchStream(table)

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
            table = build_db_schemas_table()
        elif type_url == CMD_GET_TABLES:
            table = self._build_tables_from_model()
        elif type_url == CMD_GET_COLUMNS:
            table = self._build_columns_from_model()
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

    def _execute_sql(self, sql: str, dialect: str) -> flight.RecordBatchStream:
        """Execute SQL on the vendor database and stream results.

        Note: table name rewriting is already handled by ``_prepare_sql``
        during the ``get_flight_info`` phase — no need to rewrite here.
        """
        # Virtual metadata tables — served from model, no DB needed
        vt = self._detect_virtual_table(sql)
        if vt is not None:
            return self._query_virtual_table(vt)

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

    def do_action(self, context: flight.ServerCallContext, action: flight.Action) -> Any:
        """Handle Flight SQL actions (CreatePreparedStatement, ClosePreparedStatement)."""
        action_type = action.type

        if action_type == ACTION_CREATE_PREPARED_STATEMENT:
            sql = parse_create_prepared_statement(action.body.to_pybytes())
            if sql is None:
                raise flight.FlightServerError("Failed to parse prepared statement query")

            prepared_sql, dialect, _, schema_hint, mode = self._prepare_sql(sql)
            if mode == _MODE_CATALOG:
                # Prepared catalog probes are unusual but possible. Reuse the
                # generic catalog schema; the actual data is computed at do_get.
                sql = prepared_sql
                schema = pa.schema([pa.field("result", pa.utf8())])
            else:
                sql = prepared_sql
                schema = (
                    schema_hint if schema_hint is not None else self._probe_schema(sql, dialect)
                )

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

    def list_flights(self, context: flight.ServerCallContext, criteria: bytes) -> Any:
        """List the semantic virtual table + metadata views."""
        try:
            model, _ = self._get_model()
        except Exception:
            return
        for info in model_to_flight_infos(model, "default"):
            yield info
