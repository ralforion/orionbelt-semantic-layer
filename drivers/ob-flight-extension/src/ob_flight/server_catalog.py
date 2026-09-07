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
from pyarrow import flight

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
    build_empty_foreign_keys_table,
    build_empty_keys_table,
    build_sql_info_table,
    build_table_types_table,
    build_tables_table,
)

if TYPE_CHECKING:
    from ob_flight.server import OBFlightServer

logger = logging.getLogger("ob_flight.server")


def _dispatch_catalog_sql(
    server: OBFlightServer, sql: str, model: Any, *, project: bool = True
) -> tuple[pa.Table, bool]:
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
            return catalog_columns_table(model), True
        return catalog_tables_table(model), True
    if raw_upper.startswith(("USE ", "SET ")):
        return catalog_empty_table(), True

    try:
        ast = sqlglot.parse_one(sql)
    except Exception:
        return catalog_empty_table(), True

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
            return catalog_columns_table(model), True
        # Default: list tables
        return catalog_tables_table(model), True

    # USE / SET — accept silently (Postgres clients send these on connect)
    if kind in {"Use", "Set"}:
        return catalog_empty_table(), True

    # SELECT against pg_catalog / information_schema or scalar probes
    if isinstance(ast, exp.Select):
        # sqlglot stores the FROM clause under ``from`` (<30) or ``from_`` (30.x)
        # so information_schema / pg_catalog probes aren't misrouted to the
        # scalar-probe path. Read the node's own args only — a recursive lookup
        # would pick up a subquery's FROM for a top-level no-FROM scalar probe
        # like ``SELECT EXISTS(SELECT 1 FROM information_schema.tables)``.
        from_node = ast.args.get("from") or ast.args.get("from_")
        if from_node is None:
            # Scalar probe: SELECT 1, SELECT version(), SELECT current_schema()
            return catalog_scalar_probe_table(ast), True
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
        # Each of these returns a whole canned view, so the statement's WHERE
        # and SELECT list have to be applied to it - otherwise both are
        # accepted and discarded, and the client is told every table matched
        # its filter and handed columns it did not ask for.
        if "information_schema.tables" in target_sql or "pg_catalog.pg_class" in target_sql:
            return answer_catalog_view(catalog_tables_table(model), ast, project=project)
        if "information_schema.columns" in target_sql or "pg_catalog.pg_attribute" in target_sql:
            return answer_catalog_view(catalog_columns_table(model), ast, project=project)
        if bare == "_dimensions_metadata" or bare == "dimensions":
            return answer_catalog_view(build_dimensions_data(model), ast, project=project)
        if bare == "_measures_metadata" or bare == "measures":
            return answer_catalog_view(build_measures_data(model), ast, project=project)
        if bare == "_metrics_metadata" or bare == "metrics":
            return answer_catalog_view(build_metrics_data(model), ast, project=project)
        if bare == "model":
            # ``SELECT * FROM <model>.model`` — column-shape probe
            # from a BI tool clicking the model table. Same payload
            # as the canonical ``information_schema.columns`` view
            # (one row per dim/measure/metric), and so the same
            # treatment: it is a SELECT with a FROM, so it can carry a
            # WHERE and a select list. Returning the view directly
            # discarded both *and* reported the filter as applied, which
            # is worse than either - the bind check believed it.
            return answer_catalog_view(catalog_columns_table(model), ast, project=project)

    # Unknown catalog probe — empty result. Tool moves on.
    return catalog_empty_table(), True


def handle_catalog_sql(
    server: OBFlightServer, sql: str, model: Any, *, project: bool = True
) -> pa.Table:
    """Answer a catalog query from the model. See :func:`_dispatch_catalog_sql`.

    ``project=False`` keeps the view's full column set. Parameter typing needs
    it: a predicate names a column the SELECT list often does not, and typing
    ``WHERE ordinal_position > ?`` against a table projected to
    ``column_name`` found nothing and answered text.
    """
    return _dispatch_catalog_sql(server, sql, model, project=project)[0]


def handle_catalog_sql_checked(
    server: OBFlightServer, sql: str, model: Any
) -> tuple[pa.Table, bool]:
    """:func:`handle_catalog_sql`, plus whether the statement's WHERE applied.

    Separate because the answer cannot be recovered from the table: filtering
    runs before projection, so re-filtering the result would report a good
    predicate as unevaluable after the projection dropped the column it names.
    A branch with no predicate to evaluate reports ``True``.
    """
    return _dispatch_catalog_sql(server, sql, model)


def answer_catalog_view(
    table: pa.Table, ast: Any, *, project: bool = True
) -> tuple[pa.Table, bool]:
    """A canned view narrowed by the statement's WHERE and SELECT list.

    Returns the table and whether the *filter* applied, because that answer
    cannot be recovered afterwards: filtering runs first - a predicate may name
    a column the SELECT list does not, and ``SELECT table_name ... WHERE
    table_type = 'VIEW'`` is an ordinary thing to ask - and the projection then
    drops that column. Re-running the filter on the result would report a
    perfectly good predicate as unevaluable, having removed what it needs.
    """
    filtered, applied = filter_catalog_table(table, ast)
    if not project:
        # The caller wants the view's full column set - typing a parameter
        # needs the column its predicate names, which the projection may drop.
        return filtered, applied
    projected, _ = project_catalog_table(filtered, ast)
    return projected, applied


def _answer_catalog_view(table: pa.Table, ast: Any) -> pa.Table:
    """:func:`answer_catalog_view` for callers that only want the table."""
    return answer_catalog_view(table, ast)[0]


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
    """Stream the response for a Flight SQL catalog command.

    Thin wrapper over :func:`build_catalog_table`; the table is built
    separately so tests can assert it against the schema
    ``get_flight_info`` advertises. ``RecordBatchStream`` does not expose
    its schema, and a client-level test cannot reach the commands no
    client in this repo exercises.
    """
    table = build_catalog_table(server, type_url, cmd_value, context)
    logger.debug("Catalog response for %s: %d rows", type_url.rsplit(".", 1)[-1], len(table))
    return flight.RecordBatchStream(table)


def build_catalog_table(
    server: OBFlightServer,
    type_url: str,
    cmd_value: bytes = b"",
    context: flight.ServerCallContext | None = None,
) -> pa.Table:
    """Build the response table for Flight SQL catalog metadata commands.

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
    elif type_url == CMD_GET_PRIMARY_KEYS:
        table = build_empty_keys_table()
    elif type_url in (CMD_GET_IMPORTED_KEYS, CMD_GET_EXPORTED_KEYS, CMD_GET_CROSS_REFERENCE):
        # One shared foreign-key schema for all three, per FlightSql.proto.
        table = build_empty_foreign_keys_table()
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

    return table


# ---------------------------------------------------------------------------
# WHERE filtering over a catalog result
# ---------------------------------------------------------------------------
#
# The dispatch above answers a catalog query by returning a whole canned table,
# which meant a WHERE clause was accepted and discarded: ``... FROM
# information_schema.tables WHERE table_name = 'x'`` returned every table. A
# predicate silently ignored is worse than one refused - the client believes it
# filtered.
#
# These tables are small (a handful of rows, tens for columns), so the filter
# evaluates row by row in Python rather than building compute expressions. The
# clarity is worth more than the microseconds.
#
# A predicate this cannot evaluate leaves the table *unfiltered*, which is the
# behaviour that was there before, so no client that works today stops working.
# The caller is told, and the prepared-statement path uses that to decide
# whether a parameter can be honoured: binding a value into a predicate nobody
# can evaluate would be the silent-ignore bug again, wearing a parameter.

#: Sentinel for "this predicate cannot be evaluated here", distinct from False.
_UNKNOWN = object()


def _catalog_literal(node: Any) -> Any:
    """A Python scalar from a sqlglot literal, or ``_UNKNOWN``."""
    import sqlglot.expressions as exp

    if isinstance(node, exp.Literal):
        if node.is_int:
            return int(node.this)
        if node.is_number:
            return float(node.this)
        return str(node.this)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    return _UNKNOWN


def _catalog_column_value(node: Any, row: dict[str, Any]) -> Any:
    """The row's value for a column reference, or ``_UNKNOWN``.

    Matched case-insensitively: clients spell catalog columns both ways, and
    the canned tables are lower-case.
    """
    import sqlglot.expressions as exp

    if not isinstance(node, exp.Column):
        return _UNKNOWN
    name = node.name.lower()
    for key, value in row.items():
        if key.lower() == name:
            return value
    return _UNKNOWN


def _like_matches(value: Any, pattern: str, *, case_insensitive: bool) -> bool:
    r"""SQL ``LIKE`` semantics: ``%`` is any run, ``_`` is one character.

    Walked one character at a time rather than by substitution on an escaped
    string. Escaping and then un-escaping the two wildcards cannot tell a
    wildcard from a backslash-escaped literal, so ``LIKE '\_%'`` - the pattern
    a client sends to find the underscore-prefixed metadata views - matched
    nothing at all.
    """
    import re

    if value is None:
        return False
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            out.append(re.escape(pattern[index + 1]))  # an escaped literal
            index += 2
            continue
        if char == "%":
            out.append(".*")
        elif char == "_":
            out.append(".")
        else:
            out.append(re.escape(char))
        index += 1
    flags = re.IGNORECASE if case_insensitive else 0
    return re.fullmatch("".join(out), str(value), flags) is not None


def evaluate_catalog_predicate(node: Any, row: dict[str, Any]) -> Any:
    """Whether *row* satisfies *node*, or ``_UNKNOWN`` when it cannot be said.

    Covers the shapes catalog clients actually send - equality, inequality,
    ``LIKE``/``ILIKE``, ``IN``, ``IS NULL``, and boolean combinations - and
    admits ignorance for anything else rather than guessing a verdict.
    """
    import sqlglot.expressions as exp

    if isinstance(node, exp.Paren):
        return evaluate_catalog_predicate(node.this, row)
    if isinstance(node, exp.And):
        left = evaluate_catalog_predicate(node.this, row)
        right = evaluate_catalog_predicate(node.expression, row)
        if left is False or right is False:
            return False  # a false conjunct settles it, unknown or not
        if left is _UNKNOWN or right is _UNKNOWN:
            return _UNKNOWN
        return True
    if isinstance(node, exp.Or):
        left = evaluate_catalog_predicate(node.this, row)
        right = evaluate_catalog_predicate(node.expression, row)
        if left is True or right is True:
            return True
        if left is _UNKNOWN or right is _UNKNOWN:
            return _UNKNOWN
        return False
    if isinstance(node, exp.Not):
        inner = evaluate_catalog_predicate(node.this, row)
        return _UNKNOWN if inner is _UNKNOWN else not inner

    if isinstance(node, exp.Is):
        value = _catalog_column_value(node.this, row)
        if value is _UNKNOWN or not isinstance(node.expression, exp.Null):
            return _UNKNOWN
        return value is None

    if isinstance(node, exp.In):
        value = _catalog_column_value(node.this, row)
        if value is _UNKNOWN or node.args.get("query") is not None:
            return _UNKNOWN
        candidates = [_catalog_literal(e) for e in node.expressions]
        if any(c is _UNKNOWN for c in candidates):
            return _UNKNOWN
        return value in candidates

    if isinstance(node, exp.Like | exp.ILike):
        value = _catalog_column_value(node.this, row)
        pattern = _catalog_literal(node.expression)
        if value is _UNKNOWN or not isinstance(pattern, str):
            return _UNKNOWN
        return _like_matches(value, pattern, case_insensitive=isinstance(node, exp.ILike))

    comparisons: dict[Any, Any] = {
        exp.EQ: lambda a, b: a == b,
        exp.NEQ: lambda a, b: a != b,
        exp.GT: lambda a, b: a > b,
        exp.GTE: lambda a, b: a >= b,
        exp.LT: lambda a, b: a < b,
        exp.LTE: lambda a, b: a <= b,
    }
    for node_type, compare in comparisons.items():
        if isinstance(node, node_type):
            # Either side may be the column.
            left = _catalog_column_value(node.this, row)
            right = _catalog_literal(node.expression)
            if left is _UNKNOWN:
                left = _catalog_literal(node.this)
                right = _catalog_column_value(node.expression, row)
            if left is _UNKNOWN or right is _UNKNOWN:
                return _UNKNOWN
            if left is None or right is None:
                return False  # SQL: a comparison with NULL is not true
            try:
                return bool(compare(left, right))
            except TypeError:
                return _UNKNOWN
    return _UNKNOWN


def filter_catalog_table(table: pa.Table, ast: Any) -> tuple[pa.Table, bool]:
    """Apply *ast*'s WHERE to *table*. Returns the table and whether it applied.

    ``False`` means a predicate could not be evaluated and the table came back
    untouched - the caller decides what that is worth. Nothing is filtered
    partially: a WHERE either governs the whole result or none of it.
    """
    import sqlglot.expressions as exp

    if not isinstance(ast, exp.Select):
        return table, True
    where = ast.args.get("where")
    if where is None:
        return table, True

    keep: list[bool] = []
    for row in table.to_pylist():
        verdict = evaluate_catalog_predicate(where.this, row)
        if verdict is _UNKNOWN:
            return table, False
        keep.append(bool(verdict))
    return table.filter(pa.array(keep, type=pa.bool_())), True


def project_catalog_table(table: pa.Table, ast: Any) -> tuple[pa.Table, bool]:
    """Reduce *table* to the columns the SELECT list names, in its order.

    Returns the table and whether the projection applied. ``False`` leaves it
    whole, which is what every catalog query got before: the dispatch answered
    with a canned view and the SELECT list was not read, so a client asking for
    one column received four.

    ``SELECT *`` is the whole view by definition. A select list this cannot
    honour - an expression, an aggregate, a name the view does not have -
    leaves the table alone rather than guessing, for the same reason the filter
    does: the columns are what the advertised schema is built from, and a
    schema that disagrees with the stream is worse than a wide one.

    An alias renames: ``SELECT table_name AS name`` yields ``name``, because
    that is the column the client will look for.
    """
    import sqlglot.expressions as exp

    if not isinstance(ast, exp.Select) or not ast.expressions:
        return table, False

    by_lower = {name.lower(): name for name in table.column_names}
    chosen: list[str] = []
    labels: list[str] = []
    for item in ast.expressions:
        if isinstance(item, exp.Star):
            return table, False  # the whole view, by definition
        target = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(target, exp.Column):
            return table, False
        source = by_lower.get(target.name.lower())
        if source is None:
            return table, False
        chosen.append(source)
        labels.append(item.alias if isinstance(item, exp.Alias) else source)

    projected = table.select(chosen)
    return projected.rename_columns(labels), True


def catalog_placeholder_schema(sql: str, table: pa.Table) -> pa.Schema:
    """The Arrow schema of *sql*'s ``?`` parameters, from the catalog view.

    A catalog predicate compares against a catalog column, so the view's own
    schema is what types the parameter. The semantic model cannot: it knows
    nothing called ``ordinal_position``, so every catalog parameter was
    advertised as text and a client honouring that failed a supported numeric
    filter.

    One field per placeholder, named ``$1``, ``$2``, … in binding order. An
    unresolvable one is typed utf8 rather than dropped, so the field count
    stays the parameter count.
    """
    import sqlglot
    import sqlglot.expressions as exp
    from sqlglot.errors import SqlglotError

    try:
        parsed = sqlglot.parse_one(sql)
    except SqlglotError:
        return pa.schema([])

    by_lower = {name.lower(): name for name in table.column_names}
    fields: list[pa.Field] = []
    for index, placeholder in enumerate(parsed.find_all(exp.Placeholder), start=1):
        arrow_type = pa.utf8()
        parent = placeholder.parent
        if parent is not None:
            other = parent.expression if parent.this is placeholder else parent.this
            if isinstance(other, exp.Column):
                source = by_lower.get(other.name.lower())
                if source is not None:
                    arrow_type = table.schema.field(source).type
        fields.append(pa.field(f"${index}", arrow_type))
    return pa.schema(fields)
