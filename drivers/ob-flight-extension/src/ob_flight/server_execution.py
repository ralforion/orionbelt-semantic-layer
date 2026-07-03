"""SQL classification / preparation / execution helpers for
:class:`~ob_flight.server.OBFlightServer`.

Extracted from ``server.py`` (Phase 5.5) as a pure code move. The helper
functions take the ``OBFlightServer`` instance as their first argument
(``server``) so the class can delegate to them as one-liners.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.flight as flight

from ob_driver_core.detection import is_obml, parse_obml  # type: ignore[import-untyped]

from ob_flight.converters import rows_to_batch, schema_from_description

if TYPE_CHECKING:
    from ob_flight.server import OBFlightServer

logger = logging.getLogger("ob_flight.server")


def _arrow_to_obsl_type_hint(arrow_type: pa.DataType) -> str:
    """Map an Arrow DataType to the OBSL ``ColumnMetadata.type`` vocabulary
    (``string`` / ``number`` / ``datetime`` / ``binary``). Used when Flight
    writes to the shared result cache so REST readers decode column types
    correctly instead of falling back to ``string``.
    """
    if pa.types.is_integer(arrow_type) or pa.types.is_floating(arrow_type):
        return "number"
    if pa.types.is_decimal(arrow_type):
        return "number"
    if (
        pa.types.is_date(arrow_type)
        or pa.types.is_timestamp(arrow_type)
        or pa.types.is_time(arrow_type)
    ):
        return "datetime"
    if pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
        return "binary"
    return "string"


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
_LABEL_VIEW_NAMES = ("dimensions", "measures", "metrics")
"""Label views — `SELECT "X" FROM dimensions` routes to semantic mode."""

_METADATA_VIEW_NAMES = ("_dimensions_metadata", "_measures_metadata", "_metrics_metadata")
"""Metadata views — `SELECT * FROM _dimensions_metadata` returns introspection rows."""

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


def rewrite_table_names(server: OBFlightServer, sql: str, model: Any) -> str:
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


def classify_sql(server: OBFlightServer, sql: str, model: Any) -> str:
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

    # SHOW / DESCRIBE / USE / SET — short-circuit before sqlglot. sqlglot
    # logs a "unsupported syntax. Falling back to ... Command" warning on
    # each of these in its default dialect, which spams the log on every
    # BI-tool catalog probe.
    cleaned_upper = cleaned.strip().upper()
    if cleaned_upper.startswith(("SHOW ", "DESCRIBE ", "DESC ", "USE ", "SET ")):
        return _MODE_CATALOG

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

    # SELECT with no FROM:
    # * canned-probe functions (version, current_schema, …) → CATALOG
    # * literal-only (SELECT 1) → CATALOG (connectivity probe)
    # * column identifiers that all match the model's dims / measures /
    #   metrics → SEMANTIC. "No FROM" is shorthand for "FROM <model>"
    #   on a single-model connection, so requiring the FROM is a tax.
    #   Any identifier that doesn't match falls through to REJECTED
    #   so users get RAW_SQL_REJECTED rather than UNKNOWN_SELECT_ITEM.
    from_node = ast.args.get("from")
    if from_node is None:
        known_labels = {label.lower() for label in model.dimensions}
        known_labels |= {label.lower() for label in model.measures}
        known_labels |= {label.lower() for label in model.metrics}

        saw_canned_probe = False
        saw_literal_only = True
        identifier_count = 0
        unmatched_identifier = False
        for proj in ast.expressions:
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(inner, exp.Anonymous | exp.Func):
                fname = (getattr(inner, "name", "") or "").lower()
                # sqlglot's typed function nodes (CurrentSchema,
                # CurrentUser, …) carry an empty ``.name`` — fall
                # back to the class name so canned-probe detection
                # still fires for ``SELECT current_schema()``.
                if not fname:
                    fname = type(inner).__name__.lower()
                if fname in _CATALOG_SCALAR_PROBES:
                    saw_canned_probe = True
                saw_literal_only = False
            elif isinstance(inner, exp.Literal):
                pass  # keep saw_literal_only True
            elif isinstance(inner, exp.Column):
                col_name = (getattr(inner, "name", "") or "").lower()
                # Bare ``SESSION_USER`` / ``CURRENT_USER`` etc. parse
                # as Columns. They're system info, not model
                # references — treat them as canned probes so the
                # whole SELECT routes to catalog.
                if col_name in _CATALOG_SCALAR_PROBES:
                    saw_canned_probe = True
                    saw_literal_only = False
                    continue
                identifier_count += 1
                saw_literal_only = False
                if col_name not in known_labels:
                    unmatched_identifier = True
            else:
                saw_literal_only = False
        if identifier_count > 0 and not unmatched_identifier:
            return _MODE_SEMANTIC
        if saw_canned_probe or saw_literal_only:
            return _MODE_CATALOG
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
    # Metadata views — return introspection rows (name / data_object / …).
    if bare in _METADATA_VIEW_NAMES:
        return _MODE_CATALOG

    # Schema-qualified metadata / label view. Three shapes the BI
    # tools fire after picking a table from the schema tree:
    #
    #   FROM <model>.dimensions                       (2-part, pgjdbc/DBeaver pushdown)
    #   FROM "orionbelt"."<model>"."dimensions"       (3-part, Tableau pushdown)
    #   FROM <model>.model / FROM <model>.measures    (2-part, generic)
    #
    # All three are "metadata for THIS model" → route to the
    # catalog where ``SELECT *`` is honoured. Bare label-view
    # references (``FROM dimensions``) keep the OBSQL
    # category-filtered virtual-table semantics so existing
    # surface behaviour is preserved.
    #
    # Compare the schema qualifier against BOTH the
    # ``_ob_model_id`` (stamped session name = pg_namespace
    # nspname BI tools see in the tree) AND the OBML
    # ``name:`` field — they can differ when the OBML doesn't
    # declare ``name:`` explicitly and falls back to a filename
    # stem, in which case ``model.name`` is empty and only the
    # stamp is reliable.
    schema = (
        (getattr(table_node, "db", None) or table_node.text("db") or "")
        .strip('"')
        .strip("`")
        .strip("'")
        .lower()
    )
    stamped_name = (getattr(model, "_ob_model_id", "") or "").lower()
    obml_name = (getattr(model, "name", "") or "").lower()
    model_schemas = {n for n in (stamped_name, obml_name) if n}
    if (
        schema
        and schema in model_schemas
        and bare in (_LABEL_VIEW_NAMES + _METADATA_VIEW_NAMES + ("model",))
    ):
        return _MODE_CATALOG

    # Semantic — the model's virtual table, OR a per-category label view
    # (_dimensions/_measures/_metrics). Label views are aliases for the
    # model VT restricted to one category, so `SELECT "X" FROM _dimensions`
    # compiles through the standard semantic pipeline.
    from ob_flight.catalog import model_virtual_table_name

    vt = model_virtual_table_name(model).lower()
    if bare == vt or bare in _LABEL_VIEW_NAMES:
        return _MODE_SEMANTIC

    # Anything else (including FROM-<data-object-label>) rejects.
    return _MODE_REJECTED


def semantic_result_schema(server: OBFlightServer, query: Any, model: Any) -> pa.Schema:
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


def prepare_sql(
    server: OBFlightServer,
    sql: str,
    context: flight.ServerCallContext | None = None,
) -> tuple[str, str, Any, pa.Schema | None, str, dict[str, Any] | None]:
    """Resolve model, classify SQL, translate / compile / route.

    Returns ``(final_sql_or_token, dialect, model, schema_hint, mode,
    cache_meta)``. ``cache_meta`` is a dict with ``key``, ``ttl``,
    ``session_id``, ``model_id``, ``physical_tables`` when the semantic
    query is cacheable; ``None`` otherwise (catalog mode, no cache
    backend, OBML, or oversize-skip).

    * ``mode == _MODE_SEMANTIC`` — ``final_sql_or_token`` is compiled
      warehouse SQL; ``schema_hint`` is the result schema computed
      from the model. Caller executes against the warehouse.
    * ``mode == _MODE_CATALOG`` — ``final_sql_or_token`` is the
      original SQL; the caller routes to ``_handle_catalog_sql``
      which returns model-backed metadata. ``schema_hint`` is None.
    * Anything else raises before returning.

    ``context`` carries the per-call gRPC metadata used to select the
    target model (``database`` / ``x-obsl-model`` headers).

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

    model, dialect = server._get_model(context)

    # Write-op early reject — covers DDL/DML/TCL across all paths.
    # OBML YAML detection happens after this so YAML-wrapped writes
    # also can't sneak through (OBML has no write syntax, but it's
    # cheap defence-in-depth).
    server._reject_write_operation(sql)

    # OBML YAML wrapped as a SQL string — power-user path
    if is_obml(sql):
        obml = parse_obml(sql)
        logger.info("OBML request:\n%s", sql)
        compiled = server._compile_obml(obml, model, dialect)
        sql = server._rewrite_table_names(compiled.sql, model)
        logger.info("Compiled SQL:\n%s", sql)
        # OBML compiles to deterministic SQL like OBSQL; share the cache.
        cache_meta = server._build_cache_meta(
            compiled_sql=sql,
            dialect=dialect,
            context=context,
            physical_tables=list(getattr(compiled, "physical_tables", [])),
        )
        return sql, dialect, model, None, _MODE_SEMANTIC, cache_meta

    mode = server._classify_sql(sql, model)

    if mode == _MODE_SEMANTIC:
        logger.info("OBSQL request:\n%s", sql)
        try:
            query = translate_sql_to_query(sql, model)
        except SQLTranslationError as exc:
            detail = "; ".join(f"[{e.code}] {e.message}" for e in exc.errors)
            raise flight.FlightServerError(
                f"OrionBelt Semantic QL translation failed: {detail}"
            ) from None
        compiled = CompilationPipeline().compile(query, model, dialect)
        sql = server._rewrite_table_names(compiled.sql, model)
        logger.info("Compiled SQL:\n%s", sql)
        schema_hint = server._semantic_result_schema(query, model)
        cache_meta = server._build_cache_meta(
            compiled_sql=sql,
            dialect=dialect,
            context=context,
            physical_tables=list(getattr(compiled, "physical_tables", [])),
        )
        return sql, dialect, model, schema_hint, _MODE_SEMANTIC, cache_meta

    if mode == _MODE_CATALOG:
        # Don't compile or rewrite — the caller routes the original SQL
        # to a model-backed catalog handler. Schema is computed there.
        return sql, dialect, model, None, _MODE_CATALOG, None

    # _MODE_REJECTED — no escape hatch.
    raise flight.FlightServerError(
        "[RAW_SQL_REJECTED] Raw SQL pass-through is not supported. "
        "OBSL accepts: (1) OBSQL queries against the model's virtual "
        "table, (2) compiled QueryObjects via the REST API, and "
        "(3) catalog discovery (SHOW / DESCRIBE / information_schema / "
        "pg_catalog). Arbitrary warehouse SQL is rejected by design."
    )


def build_cache_meta(
    server: OBFlightServer,
    *,
    compiled_sql: str,
    dialect: str,
    context: flight.ServerCallContext | None,
    physical_tables: list[str],
) -> dict[str, Any] | None:
    """Compute cache key + TTL for a semantic Flight query.

    Returns ``None`` when the cache backend is disabled or the TTL
    resolver decides the query isn't cacheable (no refresh contracts
    + ``no_cache`` unknown policy). The caller drops the cache_meta
    and runs the warehouse query directly.
    """
    if server._cache is None or server._cache_config is None:
        return None
    backend = getattr(server._cache, "backend_name", "noop")
    if backend == "noop":
        return None

    # Non-deterministic SQL (RAND, NOW, CURRENT_DATE, TABLESAMPLE, ...)
    # must bypass the cache — the SQL hash is the cache key, so caching
    # would freeze one stale clock/random slice forever.
    from orionbelt.cache import is_nondeterministic_sql

    nondet, name = is_nondeterministic_sql(compiled_sql)
    if nondet:
        logger.info("cache skipped: non-deterministic SQL (%s)", name)
        return None

    # Resolve session_id from the selector header. Falls back to the
    # __default__ slot for legacy single-model deployments — matches
    # how the REST endpoints derive session_id for the cache key.
    selector = server._selector_from_context(context) or ""
    if not selector:
        # Use the protected list (auto-resolve) — single-entry path.
        protected = server._list_available_model_names()
        selector = protected[0] if len(protected) == 1 else "__default__"

    try:
        store = server._session_manager.get_store(selector)
        models = store.list_models()
        if not models:
            return None
        model_id = models[0].model_id
    except Exception:
        return None

    from orionbelt.cache import build_cache_key, build_datasource_key, compute_effective_ttl

    # v2 keys hash on the compiled SQL string — the canonical, dialect-
    # rendered form actually sent to the warehouse. v3 scopes the key to the
    # datasource (the dialect today), NOT the session, so Flight, REST, and
    # pgwire share one entry per compiled query. See orionbelt.cache.key.
    datasource = build_datasource_key(dialect)
    cache_key = build_cache_key(
        datasource=datasource,
        model_id=model_id,
        dialect=dialect,
        sql=compiled_sql,
    )

    # Resolve TTL from refresh contracts + heartbeats — same logic
    # the REST endpoint uses.
    try:
        contracts = store.refresh_contracts(model_id)
    except Exception:
        contracts = {}
    heartbeats: dict[str, Any] = {}
    snapshot = getattr(server._cache, "heartbeats_snapshot", None)
    if callable(snapshot):
        try:
            heartbeats = snapshot()
        except Exception:
            heartbeats = {}
    ttl_outcome = compute_effective_ttl(
        physical_tables=physical_tables,
        contracts=contracts,
        heartbeats=heartbeats,
        min_ttl_seconds=server._cache_config.min_ttl_seconds,
        max_ttl_seconds=server._cache_config.max_ttl_seconds,
        unknown_policy=server._cache_config.unknown_policy,
        unknown_default_ttl_seconds=server._cache_config.unknown_default_ttl_seconds,
    )
    if ttl_outcome.ttl is None:
        return None
    return {
        "key": cache_key,
        "ttl": int(ttl_outcome.ttl.seconds),
        "datasource": datasource,
        "model_id": model_id,
        "physical_tables": physical_tables,
        "dialect": dialect,
        "sql": compiled_sql,
    }


def reject_write_operation(sql: str) -> None:
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
    # Short-circuit catalog-discovery statements before sqlglot — those
    # are explicitly allowed and parsing them logs a noisy "unsupported
    # syntax. Falling back to Command" warning in sqlglot's default
    # dialect, which would fire on every BI-tool catalog probe.
    upper = sql.strip().upper()
    if upper.startswith(("SHOW ", "DESCRIBE ", "DESC ", "USE ", "SET ")):
        return

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


def compile_obml(server: OBFlightServer, obml: dict[str, Any], model: Any, dialect: str) -> Any:
    """Compile OBML to SQL using the OrionBelt pipeline directly.

    Returns the full ``CompilationResult`` so callers can access
    ``sql`` plus ``physical_tables`` (needed for the freshness-
    driven cache TTL resolution).
    """
    from orionbelt.compiler.pipeline import CompilationPipeline
    from orionbelt.models.query import QueryObject

    query = QueryObject.model_validate(obml)
    return CompilationPipeline().compile(query, model, dialect)


def execute_sql(
    server: OBFlightServer,
    sql: str,
    dialect: str,
    cache_meta: dict[str, Any] | None = None,
) -> flight.RecordBatchStream:
    """Execute SQL on the vendor database and stream results.

    Note: table name rewriting is already handled by ``_prepare_sql``
    during the ``get_flight_info`` phase — no need to rewrite here.

    When ``cache_meta`` is set (semantic mode + cache backend
    enabled), the function consults the freshness-driven result
    cache first: on hit it decodes the cached Arrow payload and
    returns it directly; on miss it executes against the warehouse
    and writes the resulting ``pa.Table`` back to the cache under
    the TTL resolved at prepare time. See PLAN_freshness_driven_cache.
    """
    # Virtual metadata tables — served from model, no DB needed
    vt = server._detect_virtual_table(sql)
    if vt is not None:
        return server._query_virtual_table(vt)

    # Cache lookup — only when the prepare step gave us a key + ttl.
    if cache_meta is not None and server._cache is not None:
        cached_table = server._cache_get_table(cache_meta["key"])
        if cached_table is not None:
            logger.info(
                "Flight cache HIT: key=%s rows=%d", cache_meta["key"], cached_table.num_rows
            )
            return flight.RecordBatchStream(cached_table)

    # Resolve ``db_connect`` through the ``ob_flight.server`` module so tests
    # that patch ``ob_flight.server.db_connect`` take effect.
    from ob_flight.server import db_connect

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
        first_rows = cursor.fetchmany(server._batch_size)
        schema = schema_from_description(cursor.description, sample_rows=first_rows)

        batches: list[pa.RecordBatch] = []
        if first_rows:
            batches.append(rows_to_batch(first_rows, schema))
        while True:
            rows = cursor.fetchmany(server._batch_size)
            if not rows:
                break
            batches.append(rows_to_batch(rows, schema))

        if not batches:
            batches = [rows_to_batch([], schema)]

        table = pa.Table.from_batches(batches)

        # Cache populate — write back after a successful warehouse run.
        if cache_meta is not None and server._cache is not None:
            server._cache_put_table(table, cache_meta)

        return flight.RecordBatchStream(table)
    finally:
        conn.close()


def cache_get_table(server: OBFlightServer, key: str) -> pa.Table | None:
    """Look up a Flight cache entry. Returns the decoded ``pa.Table`` or None.

    The cache stores gzip'd Arrow IPC payloads via the shared
    ``orionbelt.cache.result_codec`` envelope, so a Flight reader consumes a
    REST/pgwire writer's entry and vice versa (one entry per compiled query).
    Flight only needs the columnar data; the envelope metadata (sql, dialect,
    explain, …) rides in the schema and is harmless to carry along.
    """
    import asyncio

    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(server._cache.get(key))
        finally:
            loop.close()
    except Exception:
        logger.debug("cache.get failed for key=%s", key, exc_info=True)
        return None
    if result is None or not getattr(result, "payload", None):
        return None
    try:
        from orionbelt.cache import result_codec

        return result_codec.decode_table(result.payload)
    except Exception:
        logger.debug("cache decode failed for key=%s", key, exc_info=True)
        return None


def cache_put_table(server: OBFlightServer, table: pa.Table, cache_meta: dict[str, Any]) -> None:
    """Serialize a ``pa.Table`` to the shared ``result_codec`` envelope and
    store under ``cache_meta``.

    REST, pgwire, and Flight share the cache namespace — all encode the
    same gzip'd Arrow IPC envelope so the other surfaces can decode ``sql``,
    ``dialect``, ``columns``, etc. without falling back to defaults. Errors are
    swallowed; cache writes must never break query execution.
    """
    import asyncio

    from orionbelt.cache import result_codec

    rows = table.to_pylist()
    list_of_lists: list[list[Any]] = [
        [row.get(name) for name in table.column_names] for row in rows
    ]
    # REST's ColumnMetadata uses ``type`` (Pydantic field name) with the
    # vocabulary ``string`` / ``number`` / ``datetime`` / ``binary`` —
    # match it so a Flight-written cache entry decoded by REST yields
    # the right column types instead of falling back to ``string``.
    columns_meta = [
        {"name": field.name, "type": _arrow_to_obsl_type_hint(field.type)} for field in table.schema
    ]

    try:
        payload = result_codec.encode(
            columns=columns_meta,
            rows=list_of_lists,
            sql=cache_meta.get("sql", ""),
            dialect=cache_meta.get("dialect", ""),
            explain=None,
            warnings=[],
            sql_valid=True,
            execution_time_ms=0.0,
            timezone=None,
            resolved={},
            physical_tables=cache_meta.get("physical_tables", []),
        )
    except Exception:
        logger.debug("cache encode failed", exc_info=True)
        return

    from orionbelt.cache import query_hash as _query_hash

    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                server._cache.set(
                    cache_meta["key"],
                    payload,
                    ttl_seconds=cache_meta["ttl"],
                    physical_tables=cache_meta["physical_tables"],
                    datasource=cache_meta["datasource"],
                    model_id=cache_meta["model_id"],
                    query_hash=_query_hash(sql=cache_meta["sql"]),
                    dialect=cache_meta["dialect"],
                    row_count=table.num_rows,
                )
            )
        finally:
            loop.close()
        logger.info(
            "Flight cache STORE: key=%s ttl=%ds rows=%d size=%dB",
            cache_meta["key"],
            cache_meta["ttl"],
            table.num_rows,
            len(payload),
        )
    except Exception:
        logger.debug("cache.set failed", exc_info=True)
