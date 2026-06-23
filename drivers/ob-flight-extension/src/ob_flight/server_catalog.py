"""Catalog / metadata helpers for :class:`~ob_flight.server.OBFlightServer`.

Extracted from ``server.py`` (Phase 5.5) as a pure code move. The helper
functions take the ``OBFlightServer`` instance as their first argument
(``server``) so the class can delegate to them as one-liners. The
``@staticmethod`` helpers that don't use the instance are plain module
functions here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.flight as flight

from ob_flight.catalog import (
    VIRTUAL_TABLES,
    build_dimensions_data,
    build_measures_data,
    build_metrics_data,
)
from ob_flight.converters import schema_from_description
from ob_flight.flight_sql import (
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
    build_catalogs_table,
    build_columns_table,
    build_db_schemas_table,
    build_empty_imported_keys_table,
    build_empty_keys_table,
    build_sql_info_table,
    build_table_types_table,
    build_tables_table,
)

if TYPE_CHECKING:
    from ob_flight.server import OBFlightServer

logger = logging.getLogger("ob_flight.server")


def handle_catalog_sql(server: OBFlightServer, sql: str, model: Any) -> pa.Table:
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

    # Fast-path SHOW / DESCRIBE / USE / SET by raw text — sqlglot logs a
    # "unsupported syntax. Falling back to ... Command" warning on each
    # of these in its default dialect, which spams the log on every
    # BI-tool catalog probe. The dispatch below is the same as the
    # Command branch, so skipping sqlglot here is a pure log-noise win.
    raw_upper = sql.strip().upper()
    if raw_upper.startswith(("SHOW ", "DESCRIBE ", "DESC ")):
        if raw_upper.startswith(("DESCRIBE ", "DESC ")) or "COLUMN" in raw_upper:
            return catalog_columns_table(model)
        return catalog_tables_table(model)
    if raw_upper.startswith(("USE ", "SET ")):
        return catalog_empty_table()

    try:
        ast = sqlglot.parse_one(sql)
    except Exception:
        return catalog_empty_table()

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
            return catalog_columns_table(model)
        # Default: list tables
        return catalog_tables_table(model)

    # USE / SET — accept silently (Postgres clients send these on connect)
    if kind in {"Use", "Set"}:
        return catalog_empty_table()

    # SELECT against pg_catalog / information_schema or scalar probes
    if isinstance(ast, exp.Select):
        from_node = ast.args.get("from")
        if from_node is None:
            # Scalar probe: SELECT 1, SELECT version(), SELECT current_schema()
            return catalog_scalar_probe_table(ast)
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
            return catalog_tables_table(model)
        if "information_schema.columns" in target_sql or "pg_catalog.pg_attribute" in target_sql:
            return catalog_columns_table(model)
        if bare == "_dimensions_metadata" or bare == "dimensions":
            return build_dimensions_data(model)
        if bare == "_measures_metadata" or bare == "measures":
            return build_measures_data(model)
        if bare == "_metrics_metadata" or bare == "metrics":
            return build_metrics_data(model)
        if bare == "model":
            # ``SELECT * FROM <model>.model`` — column-shape probe
            # from a BI tool clicking the model table. Same payload
            # as the canonical ``information_schema.columns`` view
            # (one row per dim/measure/metric).
            return catalog_columns_table(model)

    # Unknown catalog probe — empty result. Tool moves on.
    return catalog_empty_table()


def catalog_tables_table(model: Any) -> pa.Table:
    """One row per queryable virtual table (model + metadata views).

    Drops the spec-mandated ``table_schema`` binary column for the
    text-mode ``SHOW TABLES`` / ``information_schema.tables`` path —
    the IPC bytes are unreadable in a CLI / pandas display. The
    protobuf ``CommandGetTables`` handler (``_build_tables_from_model``)
    keeps the full table for JDBC clients that decode the binary.
    """
    table = build_tables_table(model)
    if "table_schema" in table.column_names:
        table = table.drop_columns(["table_schema"])
    return table


def catalog_columns_table(model: Any) -> pa.Table:
    """One row per dim/measure/metric of the model's virtual table."""
    return build_columns_table(model)


def catalog_empty_table() -> pa.Table:
    """Empty single-column response — used for unknown catalog probes."""
    schema = pa.schema([pa.field("result", pa.utf8())])
    return pa.table({"result": pa.array([], type=pa.utf8())}, schema=schema)


def catalog_scalar_probe_table(ast: Any) -> pa.Table:
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
        return catalog_empty_table()
    schema = pa.schema([pa.field(n, pa.utf8()) for n in names])
    return pa.table({n: [v] for n, v in zip(names, values, strict=True)}, schema=schema)


def detect_virtual_table(sql: str) -> str | None:
    """Detect a metadata-view reference (``_dimensions_metadata``, etc.).

    Word-boundary matching avoids false positives on names like
    ``sales_measures_metadata`` or ``total_metrics``.
    """
    import re

    sql_lower = sql.lower()
    # Check longest names first so ``_dimensions_metadata`` wins over
    # ``_dimensions`` when both would match the regex.
    for vt in sorted(VIRTUAL_TABLES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(vt)}\b", sql_lower):
            return vt
    return None


def query_virtual_table(
    server: OBFlightServer,
    vt_name: str,
    context: flight.ServerCallContext | None = None,
) -> flight.RecordBatchStream:
    """Return data for a metadata view (``_dimensions_metadata`` etc.)."""
    model, _ = server._get_model(context)
    if vt_name == "_dimensions_metadata":
        table = build_dimensions_data(model)
    elif vt_name == "_measures_metadata":
        table = build_measures_data(model)
    elif vt_name == "_metrics_metadata":
        table = build_metrics_data(model)
    else:
        raise flight.FlightServerError(f"Unknown virtual table: {vt_name}")
    return flight.RecordBatchStream(table)


def probe_schema(server: OBFlightServer, sql: str, dialect: str) -> pa.Schema:
    """Probe the database to determine the result schema for a query.

    Executes the query, peeks at a small batch for accurate type inference
    (UNION ALL queries may have NULL-padded columns in early rows).
    Falls back to a generic schema on error.
    """
    vt = server._detect_virtual_table(sql)
    if vt is not None:
        return VIRTUAL_TABLES[vt]

    # Resolve ``db_connect`` through the ``ob_flight.server`` module so tests
    # that patch ``ob_flight.server.db_connect`` take effect.
    from ob_flight.server import db_connect

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


def resolve_model_for_catalog(
    server: OBFlightServer,
    catalog_filter: str | None,
    context: flight.ServerCallContext | None,
    db_schema_filter: str | None = None,
) -> Any:
    """Resolve the model for a metadata request.

    v2.5.0 catalog layout exposes a single ``orionbelt`` catalog with
    one schema per loaded model and a literal ``model`` table inside
    each schema. Model resolution therefore moves to the
    ``db_schema_filter_pattern`` (protobuf field 2): when a BI client
    expands ``orionbelt.<model_name>`` in the schema tree, that
    ``<model_name>`` arrives as the schema filter on the subsequent
    GetTables / GetColumns call.

    ``catalog_filter`` is honoured for legacy callers that still
    pre-v2.5 emit ``catalog=<model>`` (the pre-flip layout exposed
    models as catalogs) — it falls through as the second priority
    so the obsql CLI ``--model`` flag and existing integration
    tests keep working.

    Unknown selector → ``None`` (empty metadata, no fallback) so
    BI clients don't accidentally browse the wrong model.
    """
    # db_schema_filter is the v2.5.0 selector — preferred.
    if db_schema_filter and server._session_manager is not None:
        try:
            store = server._session_manager.get_store(db_schema_filter)
            model, _ = server._stamp_model(store, db_schema_filter)
            return model
        except Exception:
            return None
    # catalog_filter is the legacy pre-flip selector — fall back.
    if catalog_filter and server._session_manager is not None:
        try:
            store = server._session_manager.get_store(catalog_filter)
            model, _ = server._stamp_model(store, catalog_filter)
            return model
        except Exception:
            return None
    try:
        model, _ = server._get_model(context)
        return model
    except Exception:
        return None


def build_tables_from_model(
    server: OBFlightServer,
    context: flight.ServerCallContext | None = None,
    *,
    table_filter: str | None = None,
    catalog_filter: str | None = None,
    db_schema_filter: str | None = None,
) -> pa.Table:
    """Build the CommandGetTables response.

    Lists the ``model`` virtual table + ``dimensions`` /
    ``measures`` / ``metrics`` views and their ``_*_metadata``
    siblings. Data objects are intentionally hidden — they're not
    queryable through the semantic layer. ``table_filter``, when
    set, scopes the response to a single table name (DBeaver sends
    one filter request per expanded tree node). v2.5.0 layout uses
    ``db_schema_filter`` (protobuf field 2) for model selection;
    ``catalog_filter`` is honoured as a legacy fallback.
    """
    model = resolve_model_for_catalog(
        server, catalog_filter, context, db_schema_filter=db_schema_filter
    )
    return build_tables_table(model, table_filter=table_filter)


def build_columns_from_model(
    server: OBFlightServer,
    context: flight.ServerCallContext | None = None,
    *,
    table_filter: str | None = None,
    catalog_filter: str | None = None,
    db_schema_filter: str | None = None,
) -> pa.Table:
    """Build the CommandGetColumns response.

    Returns dim / measure / metric columns of the ``model`` virtual
    table plus the introspection columns of each metadata view.
    ``table_filter`` scopes the response to one table — without
    it, DBeaver displays the unfiltered union under every view
    (the cross-pollution bug v2.4.0 had until this commit).
    ``db_schema_filter`` selects the model in v2.5.0;
    ``catalog_filter`` is honoured as a legacy fallback.
    """
    model = resolve_model_for_catalog(
        server, catalog_filter, context, db_schema_filter=db_schema_filter
    )
    return build_columns_table(model, table_filter=table_filter)


def handle_catalog_command(
    server: OBFlightServer,
    type_url: str,
    cmd_value: bytes = b"",
    context: flight.ServerCallContext | None = None,
) -> flight.RecordBatchStream:
    """Handle Flight SQL catalog metadata commands.

    Multi-model aware: ``CommandGetCatalogs`` returns the list of
    loaded model names so BI tools see them in the catalog dropdown.
    ``CommandGetTables`` / ``CommandGetColumns`` apply the
    ``table_name_filter_pattern`` from the protobuf body — JDBC
    clients (DBeaver) send one filter request per expanded node and
    expect the response scoped to that node's table name.
    """
    from ob_flight.flight_sql import (
        parse_catalog_filter,
        parse_db_schema_filter,
        parse_table_filter,
    )

    table_filter = parse_table_filter(cmd_value) if cmd_value else None
    catalog_filter = parse_catalog_filter(cmd_value) if cmd_value else None
    db_schema_filter = parse_db_schema_filter(cmd_value) if cmd_value else None

    if type_url == CMD_GET_CATALOGS:
        # v2.5.0 layout: single ``orionbelt`` catalog. The
        # ``Database`` dropdown in DBeaver/Tableau/Power BI shows
        # one entry; the per-model selector is the schema dropdown.
        table = build_catalogs_table(server._list_available_model_names())
    elif type_url == CMD_GET_DB_SCHEMAS:
        # One row per loaded model — DBeaver renders these under
        # ``orionbelt`` in the schema tree.
        table = build_db_schemas_table(server._list_available_model_names())
    elif type_url == CMD_GET_TABLES:
        table = server._build_tables_from_model(
            context,
            table_filter=table_filter,
            catalog_filter=catalog_filter,
            db_schema_filter=db_schema_filter,
        )
    elif type_url == CMD_GET_COLUMNS:
        table = server._build_columns_from_model(
            context,
            table_filter=table_filter,
            catalog_filter=catalog_filter,
            db_schema_filter=db_schema_filter,
        )
    elif type_url == CMD_GET_TABLE_TYPES:
        table = build_table_types_table()
    elif type_url in (CMD_GET_PRIMARY_KEYS, CMD_GET_EXPORTED_KEYS, CMD_GET_CROSS_REFERENCE):
        table = build_empty_keys_table()
    elif type_url == CMD_GET_IMPORTED_KEYS:
        table = build_empty_imported_keys_table()
    elif type_url == CMD_GET_SQL_INFO:
        # Populate the standard SqlInfo entries so JDBC clients display
        # the server name (otherwise DBeaver shows "Server: ?").
        from orionbelt import __version__ as _obsl_version

        table = build_sql_info_table(_obsl_version)
    elif type_url == CMD_GET_XDBC_TYPE_INFO:
        # XDBC type info is request-shaped; an empty result is acceptable
        # for BI tools that fall back to driver-side type metadata.
        table = pa.table({"info": pa.array([], type=pa.utf8())})
    else:
        raise flight.FlightServerError(f"Unsupported catalog command: {type_url}")

    logger.debug("Catalog response for %s: %d rows", type_url.rsplit(".", 1)[-1], len(table))
    return flight.RecordBatchStream(table)
