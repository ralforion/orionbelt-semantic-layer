"""Embedded ``pg_catalog`` / ``information_schema`` emulator (Step 3).

Builds an in-memory DuckDB and registers each loaded OBSL model as an
empty TABLE so DuckDB's native ``pg_catalog`` (auto-populated from the
schema) answers Postgres introspection probes — ``\\dt``, DBeaver schema
trees, Tableau's pre-flight checks — without us hand-rolling pg_class /
pg_namespace / pg_attribute.

Why a TABLE and not a VIEW: ``psql \\dt`` filters
``c.relkind IN ('r','p','')`` so views (``relkind='v'``) are skipped.
The tables hold zero rows; they exist purely so the catalog views
describe them. Semantic queries against the same model never reach
this connection — they're routed to the real warehouse by the router.

Caveat: DuckDB's ``pg_attribute.atttypid`` returns DuckDB's internal
type ids, not real Postgres OIDs. ``\\d <table>`` in psql will show
mislabeled types; clients that consult ``information_schema.columns``
instead (DBeaver, Tableau, Power BI) get the correct DuckDB SQL types.
Step 5 of design/PLAN_postgres_wire.md addresses this for BI-tool
fidelity.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import duckdb

from orionbelt.models.semantic import DataType, Dimension, Measure, Metric, SemanticModel
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult
from orionbelt.service.session_manager import SessionManager

logger = logging.getLogger(__name__)


# OBML DataType → DuckDB SQL type. Coarse mapping; column-level
# ``dataType`` overrides (e.g. ``decimal(18,2)``) are ignored on
# purpose — pg_attribute's mis-typing makes finer types invisible
# anyway and the catalog only needs to round-trip the *column* shape.
_DATATYPE_TO_DUCKDB: dict[DataType, str] = {
    DataType.STRING: "VARCHAR",
    DataType.JSON: "JSON",
    DataType.INT: "BIGINT",
    DataType.FLOAT: "DOUBLE",
    DataType.DATE: "DATE",
    DataType.TIME: "TIME",
    DataType.TIME_TZ: "TIMETZ",
    DataType.TIMESTAMP: "TIMESTAMP",
    DataType.TIMESTAMP_TZ: "TIMESTAMPTZ",
    DataType.BOOLEAN: "BOOLEAN",
}

# DuckDB identifier safety. Postgres allows arbitrary quoted names, so
# any printable Unicode is fair game inside the model.  We pre-validate
# the *model* name (used as the table name) more strictly because BI
# tools sometimes refuse quoted identifiers; column names stay quoted.
_SAFE_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


# Stub Postgres catalog functions referenced by psql ``\\dt`` / ``\\d``.
# DuckDB exposes ``pg_class`` / ``pg_namespace`` / ``pg_attribute`` as
# native views but stops short of these helper scalars; we hand-roll
# them as macros in the main schema and pair with a SQL pre-processor
# that strips ``pg_catalog.`` from function references (DuckDB rejects
# qualified function lookups into a system schema).
_STUB_MACROS: tuple[str, ...] = (
    "CREATE OR REPLACE MACRO pg_get_userbyid(uid) AS 'obsl'",
    "CREATE OR REPLACE MACRO pg_table_is_visible(oid) AS true",
    "CREATE OR REPLACE MACRO pg_type_is_visible(oid) AS true",
    "CREATE OR REPLACE MACRO pg_get_partkeydef(oid) AS NULL",
    "CREATE OR REPLACE MACRO pg_get_indexdef(oid) AS NULL",
    "CREATE OR REPLACE MACRO pg_get_constraintdef(oid) AS NULL",
    "CREATE OR REPLACE MACRO pg_get_expr(expr, oid) AS NULL",
    # psql 16 calls pg_get_expr/3 for default-expr lookups; DuckDB
    # macros are arity-distinguished so we register both overloads.
    "CREATE OR REPLACE MACRO pg_get_expr(expr, oid, pretty) AS NULL",
    "CREATE OR REPLACE MACRO pg_relation_is_publishable(oid) AS false",
    "CREATE OR REPLACE MACRO obj_description(oid, catalog) AS NULL",
    "CREATE OR REPLACE MACRO col_description(oid, col) AS NULL",
    "CREATE OR REPLACE MACRO format_type(oid, typemod) AS 'unknown'",
    "CREATE OR REPLACE MACRO pg_encoding_to_char(enc) AS 'UTF8'",
)


# Augmented catalog views in our own ``obsl_meta`` schema. DuckDB's
# native ``pg_catalog.pg_class`` / ``pg_attribute`` are missing columns
# psql 16 expects (relforcerowsecurity, relhasoids, …) and the
# atttypid values are DuckDB-internal type ids, not real Postgres OIDs.
# We can't write to the system catalog (``Cannot create entry in
# system catalog``) so we mirror those tables here and swap references
# with ``_REWRITES`` below.
_OBSL_META_DDL: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS obsl_meta",
    # pg_class: pass-through + missing columns as constants. psql 16's
    # \\d reads ~30 columns from pg_class; supplying sensible defaults
    # keeps the introspection query well-typed without changing
    # behaviour for the columns DuckDB already exposes correctly.
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_class AS
    SELECT
        c.*,
        CAST(false AS BOOLEAN) AS relforcerowsecurity,
        CAST(false AS BOOLEAN) AS relrowsecurity,
        CAST(false AS BOOLEAN) AS relhasoids,
        CAST(false AS BOOLEAN) AS relispartition,
        CAST(false AS BOOLEAN) AS relhastriggers,
        CAST(false AS BOOLEAN) AS relhasindex,
        CAST(false AS BOOLEAN) AS relhasrules,
        CAST(false AS BOOLEAN) AS relhassubclass,
        CAST(false AS BOOLEAN) AS relispopulated,
        CAST('d' AS VARCHAR) AS relreplident,
        CAST('p' AS VARCHAR) AS relpersistence,
        CAST(0 AS INTEGER) AS reloftype,
        CAST(0 AS INTEGER) AS relrewrite,
        CAST(0 AS INTEGER) AS reltoastrelid,
        CAST(0 AS INTEGER) AS relam,
        CAST(0 AS INTEGER) AS reltablespace,
        CAST(0 AS INTEGER) AS reloptions,
        CAST(0 AS INTEGER) AS relminmxid,
        CAST(0 AS INTEGER) AS relfrozenxid,
        CAST(0 AS BIGINT)  AS reltuples,
        CAST(0 AS INTEGER) AS relpages,
        CAST(0 AS INTEGER) AS relallvisible,
        CAST(0 AS INTEGER) AS relchecks,
        CAST('' AS VARCHAR) AS relacl,
        CAST('' AS VARCHAR) AS relpartbound
    FROM pg_catalog.pg_class c
    """,
    # Empty stub views for psql 16's "advanced relation" probes. These
    # tables don't exist in DuckDB and the underlying features (RLS,
    # publications, inheritance) don't apply to OBSL's virtual models
    # — empty rows are the semantically correct answer.
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_policy AS
    SELECT
        CAST(NULL AS INTEGER) AS oid,
        CAST(NULL AS VARCHAR) AS polname,
        CAST(NULL AS INTEGER) AS polrelid,
        CAST(NULL AS VARCHAR) AS polcmd,
        CAST(NULL AS BOOLEAN) AS polpermissive,
        CAST(NULL AS VARCHAR) AS polroles,
        CAST(NULL AS VARCHAR) AS polqual,
        CAST(NULL AS VARCHAR) AS polwithcheck
    WHERE FALSE
    """,
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_inherits AS
    SELECT
        CAST(NULL AS INTEGER) AS inhrelid,
        CAST(NULL AS INTEGER) AS inhparent,
        CAST(NULL AS INTEGER) AS inhseqno,
        CAST(NULL AS BOOLEAN) AS inhdetachpending
    WHERE FALSE
    """,
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_partitioned_table AS
    SELECT
        CAST(NULL AS INTEGER) AS partrelid,
        CAST(NULL AS VARCHAR) AS partstrat,
        CAST(NULL AS SMALLINT) AS partnatts,
        CAST(NULL AS INTEGER) AS partdefid,
        CAST(NULL AS VARCHAR) AS partattrs,
        CAST(NULL AS VARCHAR) AS partclass,
        CAST(NULL AS VARCHAR) AS partcollation,
        CAST(NULL AS VARCHAR) AS partexprs
    WHERE FALSE
    """,
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_publication AS
    SELECT
        CAST(NULL AS INTEGER) AS oid,
        CAST(NULL AS VARCHAR) AS pubname,
        CAST(NULL AS INTEGER) AS pubowner,
        CAST(NULL AS BOOLEAN) AS puballtables,
        CAST(NULL AS BOOLEAN) AS pubinsert,
        CAST(NULL AS BOOLEAN) AS pubupdate,
        CAST(NULL AS BOOLEAN) AS pubdelete,
        CAST(NULL AS BOOLEAN) AS pubtruncate,
        CAST(NULL AS BOOLEAN) AS pubviaroot
    WHERE FALSE
    """,
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_publication_rel AS
    SELECT
        CAST(NULL AS INTEGER) AS oid,
        CAST(NULL AS INTEGER) AS prpubid,
        CAST(NULL AS INTEGER) AS prrelid
    WHERE FALSE
    """,
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_publication_namespace AS
    SELECT
        CAST(NULL AS INTEGER) AS oid,
        CAST(NULL AS INTEGER) AS pnpubid,
        CAST(NULL AS INTEGER) AS pnnspid
    WHERE FALSE
    """,
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_roles AS
    SELECT
        CAST(10 AS INTEGER) AS oid,
        CAST('obsl' AS VARCHAR) AS rolname,
        CAST(true AS BOOLEAN) AS rolsuper,
        CAST(true AS BOOLEAN) AS rolinherit,
        CAST(true AS BOOLEAN) AS rolcreaterole,
        CAST(true AS BOOLEAN) AS rolcreatedb,
        CAST(true AS BOOLEAN) AS rolcanlogin,
        CAST(false AS BOOLEAN) AS rolreplication,
        CAST(false AS BOOLEAN) AS rolbypassrls,
        CAST(-1 AS INTEGER) AS rolconnlimit
    """,
    # pg_database: DuckDB exposes (oid, datname) only.  DBeaver's
    # "list databases" probe (db.oid, db.*) needs the full Postgres
    # column set or it errors on missing ``datallowconn`` /
    # ``datistemplate``. Defaults match a typical user-creatable DB.
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_database AS
    SELECT
        d.oid,
        d.datname,
        CAST(10 AS INTEGER) AS datdba,
        CAST(6 AS INTEGER) AS encoding,           -- UTF8
        CAST('en_US.UTF-8' AS VARCHAR) AS datcollate,
        CAST('en_US.UTF-8' AS VARCHAR) AS datctype,
        CAST(false AS BOOLEAN) AS datistemplate,
        CAST(true AS BOOLEAN) AS datallowconn,
        CAST(-1 AS INTEGER) AS datconnlimit,
        CAST(0 AS BIGINT) AS datfrozenxid,
        CAST(0 AS BIGINT) AS datminmxid,
        CAST(1663 AS INTEGER) AS dattablespace,    -- pg_default
        CAST(NULL AS VARCHAR) AS datacl,
        CAST('c' AS VARCHAR) AS datlocprovider,
        CAST(NULL AS VARCHAR) AS daticulocale,
        CAST(NULL AS VARCHAR) AS daticurules,
        CAST(NULL AS VARCHAR) AS datcollversion
    FROM pg_catalog.pg_database d
    """,
    # pg_collation: DuckDB's pg_catalog is missing this table entirely.
    # We expose an empty view; clients that LEFT JOIN it get NULLs which
    # is what they expect when collation isn't relevant.
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_collation AS
    SELECT
        CAST(NULL AS INTEGER) AS oid,
        CAST(NULL AS VARCHAR) AS collname,
        CAST(NULL AS INTEGER) AS collnamespace,
        CAST(NULL AS INTEGER) AS collowner,
        CAST(NULL AS VARCHAR) AS collprovider,
        CAST(NULL AS BOOLEAN) AS collisdeterministic,
        CAST(NULL AS INTEGER) AS collencoding,
        CAST(NULL AS VARCHAR) AS collcollate,
        CAST(NULL AS VARCHAR) AS collctype,
        CAST(NULL AS VARCHAR) AS collversion
    WHERE FALSE
    """,
    # pg_attribute: rebuild from information_schema.columns so atttypid
    # uses real Postgres OIDs. Without this, JDBC type lookups via
    # ``JOIN pg_type`` resolve to wrong type names (Step 3 caveat).
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_attribute AS
    SELECT
        c2.oid AS attrelid,
        isc.column_name AS attname,
        CAST(
            CASE LOWER(SPLIT_PART(isc.data_type, '(', 1))
                WHEN 'varchar' THEN 1043
                WHEN 'character varying' THEN 1043
                WHEN 'text' THEN 25
                WHEN 'bigint' THEN 20
                WHEN 'integer' THEN 23
                WHEN 'smallint' THEN 21
                WHEN 'tinyint' THEN 21
                WHEN 'hugeint' THEN 1700
                WHEN 'double' THEN 701
                WHEN 'double precision' THEN 701
                WHEN 'real' THEN 700
                WHEN 'boolean' THEN 16
                WHEN 'date' THEN 1082
                WHEN 'timestamp' THEN 1114
                WHEN 'timestamp without time zone' THEN 1114
                WHEN 'timestamp with time zone' THEN 1184
                WHEN 'time' THEN 1083
                WHEN 'time without time zone' THEN 1083
                WHEN 'time with time zone' THEN 1266
                WHEN 'blob' THEN 17
                WHEN 'bytea' THEN 17
                WHEN 'decimal' THEN 1700
                WHEN 'numeric' THEN 1700
                WHEN 'json' THEN 114
                WHEN 'uuid' THEN 2950
                ELSE 25
            END
        AS INTEGER) AS atttypid,
        CAST(isc.ordinal_position AS SMALLINT) AS attnum,
        CAST((isc.is_nullable = 'NO') AS BOOLEAN) AS attnotnull,
        CAST(false AS BOOLEAN) AS attisdropped,
        CAST(-1 AS SMALLINT) AS attlen,
        CAST(-1 AS INTEGER) AS atttypmod,
        CAST(false AS BOOLEAN) AS atthasdef,
        CAST(false AS BOOLEAN) AS attidentity,
        CAST(false AS BOOLEAN) AS attgenerated,
        CAST('' AS VARCHAR) AS attoptions,
        CAST('' AS VARCHAR) AS attfdwoptions,
        CAST('' AS VARCHAR) AS attmissingval,
        CAST(0 AS INTEGER) AS attinhcount,
        CAST(0 AS INTEGER) AS attstattarget,
        CAST(0 AS INTEGER) AS attndims,
        CAST('p' AS VARCHAR) AS attstorage,
        CAST(0 AS INTEGER) AS attcollation
    FROM information_schema.columns isc
    JOIN pg_catalog.pg_class c2 ON c2.relname = isc.table_name
    """,
)


# Catalog-table substitutions applied to incoming SQL: when a client
# references ``pg_catalog.pg_class`` we transparently swap it to
# ``obsl_meta.pg_class`` so the missing-column / mistyped-attribute
# problems documented in Step 3 disappear.
_TABLE_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bpg_catalog\.pg_class\b", re.IGNORECASE),
        "obsl_meta.pg_class",
    ),
    (
        re.compile(r"\bpg_catalog\.pg_attribute\b", re.IGNORECASE),
        "obsl_meta.pg_attribute",
    ),
    (
        re.compile(r"\bpg_catalog\.pg_collation\b", re.IGNORECASE),
        "obsl_meta.pg_collation",
    ),
    (re.compile(r"\bpg_catalog\.pg_policy\b", re.IGNORECASE), "obsl_meta.pg_policy"),
    (re.compile(r"\bpg_catalog\.pg_inherits\b", re.IGNORECASE), "obsl_meta.pg_inherits"),
    (
        re.compile(r"\bpg_catalog\.pg_partitioned_table\b", re.IGNORECASE),
        "obsl_meta.pg_partitioned_table",
    ),
    (re.compile(r"\bpg_catalog\.pg_publication\b", re.IGNORECASE), "obsl_meta.pg_publication"),
    (
        re.compile(r"\bpg_catalog\.pg_publication_rel\b", re.IGNORECASE),
        "obsl_meta.pg_publication_rel",
    ),
    (
        re.compile(r"\bpg_catalog\.pg_publication_namespace\b", re.IGNORECASE),
        "obsl_meta.pg_publication_namespace",
    ),
    (re.compile(r"\bpg_catalog\.pg_roles\b", re.IGNORECASE), "obsl_meta.pg_roles"),
    (re.compile(r"\bpg_catalog\.pg_database\b", re.IGNORECASE), "obsl_meta.pg_database"),
)


# Rewrites we apply to the SQL before handing it to DuckDB.  Each
# pattern targets a specific psql / pgAdmin quirk.  Order matters: the
# more specific rules (``OPERATOR(pg_catalog.~)``) run before the
# generic ``pg_catalog.<ident>`` prefix strip.
_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    # COLLATE pg_catalog.default → drop entirely.  DuckDB has no notion
    # of named collations and the default collation is implicit anyway.
    (re.compile(r"\bCOLLATE\s+pg_catalog\.\w+\b", re.IGNORECASE), ""),
    # OPERATOR(pg_catalog.~) → OPERATOR(~), and the same for other
    # comparison / regex operators psql wraps with the qualifier.
    (re.compile(r"OPERATOR\(\s*pg_catalog\.", re.IGNORECASE), "OPERATOR("),
    # ::pg_catalog.text / ::pg_catalog.regtype / ::pg_catalog.name —
    # collapse Postgres-specific type names that don't exist in DuckDB
    # to VARCHAR. The cast result type is only used for display, so the
    # loose coercion is fine for catalog probes.
    (
        re.compile(
            r"::\s*pg_catalog\.(text|name|regtype|regclass|regprocedure|regproc|regnamespace|oid|char)\b",
            re.IGNORECASE,
        ),
        "::VARCHAR",
    ),
    # Bare ``::regclass`` / ``::regtype`` / ``::name`` etc. without the
    # ``pg_catalog.`` qualifier. DBeaver and pgAdmin emit these in
    # introspection probes — e.g. ``classoid='pg_namespace'::regclass``.
    # Rewriting to VARCHAR keeps the surrounding comparison parseable;
    # the rows it filters on are usually empty stubs (pg_description)
    # so the loose coercion is harmless.
    (
        re.compile(
            r"::\s*(regclass|regtype|regprocedure|regproc|regnamespace|name)\b",
            re.IGNORECASE,
        ),
        "::VARCHAR",
    ),
    # Function / operator references prefixed with pg_catalog. — strip
    # the prefix so DuckDB resolves against the unqualified built-in or
    # our stub macros. We match any identifier immediately followed by
    # ``(`` to keep this rule disjoint from table references (which
    # never have parens after the table name).
    (
        re.compile(
            r"pg_catalog\.([a-z_][a-z0-9_]*)\s*\(",
            re.IGNORECASE,
        ),
        r"\1(",
    ),
)


def _rewrite_for_duckdb(sql: str) -> str:
    """Best-effort SQL rewrite so psql introspection runs on DuckDB.

    Touches only the patterns documented in ``_REWRITES`` plus the
    ``_TABLE_SUBSTITUTIONS`` that redirect ``pg_catalog.pg_class`` and
    ``pg_catalog.pg_attribute`` to our augmented ``obsl_meta`` views.
    Unrecognised constructs are left alone — the catalog branch is
    best-effort by design, and the caller's error response will surface
    anything we miss so we can extend the rules incrementally.
    """

    out = sql
    for pattern, replacement in _REWRITES:
        out = pattern.sub(replacement, out)
    for pattern, replacement in _TABLE_SUBSTITUTIONS:
        out = pattern.sub(replacement, out)
    return out


class CatalogEmulator:
    """Wraps an in-memory DuckDB connection used only for catalog probes.

    The emulator is intended to be created once and shared across all
    pgwire connections.  ``refresh()`` rebuilds the schema from the
    current :class:`SessionManager` state; ``execute()`` runs an
    arbitrary SQL string against the connection and returns an
    :class:`ExecutionResult` so the router can encode rows uniformly
    with the semantic path.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection = duckdb.connect(database=":memory:")
        # Map of table_name → stable signature so refresh can diff
        # against the SessionManager and only churn tables that
        # actually changed.  Recreating a table reassigns its
        # ``pg_class.oid``; ``psql \\d`` issues two probes back-to-back
        # and a stale oid between them is what we're guarding against.
        self._registered_signatures: dict[str, str] = {}
        for ddl in _STUB_MACROS:
            with contextlib.suppress(Exception):
                self._con.execute(ddl)
        for ddl in _OBSL_META_DDL:
            with contextlib.suppress(Exception):
                self._con.execute(ddl)

    # ------------------------------------------------------------------
    # Refresh — rebuild the in-memory schema from a SessionManager.
    # ------------------------------------------------------------------

    def refresh(self, session_manager: SessionManager) -> None:
        """Drop and recreate one empty TABLE per loaded model.

        Called on every catalog probe — the cost is dominated by the
        DDL round-trip (~microseconds for a handful of models) and the
        simpler design is worth more than a stale-cache invalidation
        protocol.
        """

        desired: dict[str, str] = {}
        ddls: dict[str, str] = {}
        for store_target, model in _iter_loaded_models(session_manager):
            table_name = _safe_model_table_name(store_target)
            ddl = _build_table_ddl(table_name, model)
            if ddl is None:
                continue
            desired[table_name] = ddl
            ddls[table_name] = ddl

        with self._lock:
            # Drop tables that no longer have a backing model.
            for table in list(self._registered_signatures):
                if table not in desired:
                    with contextlib.suppress(Exception):
                        self._con.execute(f'DROP TABLE IF EXISTS "{table}"')
                    self._registered_signatures.pop(table, None)

            # Create or recreate only tables whose DDL signature changed.
            # Stable tables keep their pg_class.oid across catalog probes
            # — critical for psql ``\\d``'s two-step lookup pattern.
            for table_name, signature in desired.items():
                if self._registered_signatures.get(table_name) == signature:
                    continue
                with contextlib.suppress(Exception):
                    self._con.execute(f'DROP TABLE IF EXISTS "{table_name}"')
                try:
                    self._con.execute(ddls[table_name])
                except duckdb.Error:  # pragma: no cover — defensive guard
                    logger.exception("Failed to register catalog table for model '%s'", table_name)
                    continue
                self._registered_signatures[table_name] = signature

    # ------------------------------------------------------------------
    # Execute — run a catalog/info-schema query through DuckDB.
    # ------------------------------------------------------------------

    def execute(self, sql: str) -> ExecutionResult:
        """Run ``sql`` against the embedded DuckDB.

        DuckDB's pg_catalog and information_schema are auto-populated
        from the schema we registered in :meth:`refresh`, so the
        caller doesn't need to special-case which table is being
        queried.  Errors bubble as ``duckdb.Error`` but are tagged as
        ``PGWIRE_CATALOG_PROBE_UNHANDLED`` warnings first so BI-tool
        introspection failures surface in logs without breaking the
        session (plan §7).
        """

        t0 = time.monotonic()
        rewritten = _rewrite_for_duckdb(sql)
        try:
            with self._lock:
                cursor = self._con.execute(rewritten)
                rows_raw = cursor.fetchall()
                description = cursor.description or []
        except duckdb.Error as exc:
            logger.warning(
                "PGWIRE_CATALOG_PROBE_UNHANDLED dialect=duckdb error=%s sql=%s",
                exc,
                _truncate_for_log(rewritten),
            )
            raise
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        columns = [
            ColumnMeta(name=str(d[0]), type_hint=_duckdb_desc_to_hint(d)) for d in description
        ]
        rows = [list(row) for row in rows_raw]
        return ExecutionResult(
            columns=columns,
            raw_rows=rows,
            row_count=len(rows),
            execution_time_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock, contextlib.suppress(Exception):
            self._con.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate_for_log(sql: str, limit: int = 400) -> str:
    """Clamp a SQL string so a noisy probe doesn't dominate the log line."""

    one_line = " ".join(sql.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit] + "…"


def _iter_loaded_models(session_manager: SessionManager) -> Iterator[tuple[str, SemanticModel]]:
    """Yield ``(target_name, SemanticModel)`` for every loaded model.

    ``target_name`` is the addressing name BI tools use as the Postgres
    ``database`` parameter:

    * multi-model (``MODEL_FILES``): each preload sits in its own
      *protected* session whose id IS the OBML name; iterated via
      :meth:`SessionManager.list_protected_session_ids`.
    * single-model legacy (``MODEL_FILE``): the model lives in the
      ``__default__`` session, exposed under that name.
    * user-created sessions: iterated via
      :meth:`SessionManager.list_sessions` so models loaded over REST
      still light up the catalog.

    ``list_sessions()`` filters out the default + protected sets, so we
    union all three sources manually to cover production layouts.
    """

    candidate_ids: list[str] = []
    candidate_ids.extend(session_manager.list_protected_session_ids())
    candidate_ids.append("__default__")
    candidate_ids.extend(s.session_id for s in session_manager.list_sessions())

    seen_names: set[str] = set()
    for session_id in candidate_ids:
        if session_id in seen_names:
            continue
        try:
            store = session_manager.get_store(session_id)
        except Exception:
            continue
        models = store.list_models()
        if not models:
            continue
        try:
            model = store.get_model(models[0].model_id)
        except KeyError:
            continue
        seen_names.add(session_id)
        yield session_id, model


def _safe_model_table_name(name: str) -> str:
    """Coerce a model addressing name into a DuckDB-safe table name.

    The pgwire surface accepts arbitrary Postgres database parameters,
    but DuckDB DDL is friendlier with simple identifiers. Names that
    don't match the canonical pattern fall through unchanged inside
    double quotes — DuckDB tolerates that fine; we just pre-check so
    common cases produce predictable bare identifiers.
    """

    if _SAFE_TABLE_NAME.match(name):
        return name
    return name.replace('"', '""')


def _build_table_ddl(table_name: str, model: SemanticModel) -> str | None:
    """Build the ``CREATE TABLE`` for a model.

    Columns: every dimension, measure, and metric exposed by the
    model.  Names are quoted because OBSL labels routinely contain
    spaces and punctuation.  Returns ``None`` if no columns survive
    deduplication — DuckDB rejects empty column lists.
    """

    columns: list[str] = []
    seen: set[str] = set()
    for label, sql_type in _model_columns(model):
        if label in seen:
            continue
        seen.add(label)
        quoted = label.replace('"', '""')
        columns.append(f'"{quoted}" {sql_type}')
    if not columns:
        return None
    quoted_table = table_name.replace('"', '""')
    return f'CREATE TABLE "{quoted_table}" ({", ".join(columns)})'


def _model_columns(model: SemanticModel) -> Iterator[tuple[str, str]]:
    for label, dim in model.dimensions.items():
        yield label, _dim_sql_type(dim)
    for label, measure in model.measures.items():
        yield label, _measure_sql_type(measure)
    for label, metric in model.metrics.items():
        yield label, _metric_sql_type(metric)


def _dim_sql_type(dim: Dimension) -> str:
    return _DATATYPE_TO_DUCKDB.get(dim.result_type, "VARCHAR")


def _measure_sql_type(measure: Measure) -> str:
    return _DATATYPE_TO_DUCKDB.get(measure.result_type, "DOUBLE")


def _metric_sql_type(_metric: Metric) -> str:
    # Metrics produce a single derived value — float is the safe
    # default the OBSL compiler also uses.  Step 7's finer type story
    # can revisit per-metric output typing.
    return "DOUBLE"


def _duckdb_desc_to_hint(description_row: tuple[Any, ...]) -> str:
    """Coarse DuckDB type-code → executor type_hint.

    DuckDB's cursor description carries a string type name in slot 1.
    We collapse it onto the same four-hint vocabulary the executor
    uses so the encoder in pgwire/types.py is shared between the
    catalog and semantic paths.
    """

    if len(description_row) < 2 or description_row[1] is None:
        return "string"
    name = str(description_row[1]).lower()
    if any(token in name for token in ("int", "decimal", "numeric", "float", "double", "real")):
        return "number"
    if any(token in name for token in ("timestamp", "date", "time")):
        return "datetime"
    if name == "boolean" or name == "bool":
        return "string"  # text-format bool encoder picks 't'/'f' from python bool
    if "blob" in name or "binary" in name:
        return "binary"
    return "string"
