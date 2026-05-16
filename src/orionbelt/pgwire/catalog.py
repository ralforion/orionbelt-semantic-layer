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
    # pg_get_expr is called with 2 or 3 args by different clients
    # (psql 16's \\d uses 3-arg, DBeaver uses 2-arg). DuckDB macros
    # don't overload by arity — ``CREATE OR REPLACE`` replaces — so we
    # use a default-arg form that accepts both call shapes.
    "CREATE OR REPLACE MACRO pg_get_expr(expr, oid, pretty := false) AS NULL",
    "CREATE OR REPLACE MACRO pg_relation_is_publishable(oid) AS false",
    "CREATE OR REPLACE MACRO obj_description(oid, catalog) AS NULL",
    "CREATE OR REPLACE MACRO col_description(oid, col) AS NULL",
    "CREATE OR REPLACE MACRO format_type(oid, typemod) AS 'unknown'",
    "CREATE OR REPLACE MACRO pg_encoding_to_char(enc) AS 'UTF8'",
    # DBeaver fetches the keyword list to colour the SQL editor.  We
    # emit an empty rowset; the client's hard-coded fallback covers
    # the user-experience gap.
    "CREATE OR REPLACE MACRO pg_get_keywords() AS TABLE "
    "(SELECT CAST(NULL AS VARCHAR) AS word, CAST(NULL AS VARCHAR) AS catcode, "
    "CAST(NULL AS VARCHAR) AS catdesc WHERE FALSE)",
    # DBeaver's table-detail dialog asks for storage statistics. OBSL
    # models are virtual so the honest answer is 0.
    "CREATE OR REPLACE MACRO pg_total_relation_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_relation_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_indexes_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_table_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_database_size(oid) AS 0",
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
        -- relacl is an aclitem[] in real Postgres; an empty array
        -- literal keeps DBeaver's tree introspection from NPE-ing on
        -- a NULL value where it expects an array.
        CAST('{}' AS VARCHAR) AS relacl,
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
    # pg_database is built dynamically during refresh() — one row per
    # loaded model — so BI tools' "list databases" probe surfaces the
    # OBSL model names (orionbelt_1_commerce, sales, …) instead of
    # DuckDB's "memory" / "system" / "temp" catalog names. The empty
    # placeholder here lives only so the rewriter has a target
    # before the first refresh().
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_database AS
    SELECT
        CAST(NULL AS INTEGER) AS oid,
        CAST(NULL AS VARCHAR) AS datname,
        CAST(10 AS INTEGER) AS datdba,
        CAST(6 AS INTEGER) AS encoding,
        CAST('en_US.UTF-8' AS VARCHAR) AS datcollate,
        CAST('en_US.UTF-8' AS VARCHAR) AS datctype,
        CAST(false AS BOOLEAN) AS datistemplate,
        CAST(true AS BOOLEAN) AS datallowconn,
        CAST(-1 AS INTEGER) AS datconnlimit,
        CAST(0 AS BIGINT) AS datfrozenxid,
        CAST(0 AS BIGINT) AS datminmxid,
        CAST(1663 AS INTEGER) AS dattablespace,
        CAST('{}' AS VARCHAR) AS datacl,
        CAST('c' AS VARCHAR) AS datlocprovider,
        CAST(NULL AS VARCHAR) AS daticulocale,
        CAST(NULL AS VARCHAR) AS daticurules,
        CAST(NULL AS VARCHAR) AS datcollversion
    WHERE FALSE
    """,
    # pg_namespace placeholder — the real view is rebuilt during
    # refresh() with exactly one row per loaded model schema so BI
    # tools see only the OBSL surface (no DuckDB defaults, no
    # ``pg_catalog`` / ``information_schema`` / ``obsl_meta`` leak).
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_namespace AS
    SELECT n.* FROM pg_catalog.pg_namespace n WHERE FALSE
    """,
    # pg_shdescription: DBeaver's column-detail dialog joins this
    # table for shared-object comments. DuckDB doesn't have it; an
    # empty stub with the standard columns is the right answer.
    """
    CREATE OR REPLACE VIEW obsl_meta.pg_shdescription AS
    SELECT
        CAST(NULL AS INTEGER) AS objoid,
        CAST(NULL AS INTEGER) AS classoid,
        CAST(NULL AS VARCHAR) AS description
    WHERE FALSE
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
    (re.compile(r"\bpg_catalog\.pg_namespace\b", re.IGNORECASE), "obsl_meta.pg_namespace"),
    (
        re.compile(r"\bpg_catalog\.pg_shdescription\b", re.IGNORECASE),
        "obsl_meta.pg_shdescription",
    ),
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
    # ``SESSION_USER`` and ``CURRENT_USER`` as bare identifiers (no
    # parens) return "duckdb" by default — replace with our brand
    # name so BI-tool displays surface "obsl" instead. Matched only
    # when surrounded by word boundaries so we don't touch the rare
    # column literally named ``session_user``.
    (
        re.compile(r"\bSESSION_USER\b", re.IGNORECASE),
        "'obsl'",
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
        # Per-model metadata views (<model>_dimensions / _measures /
        # _metrics) live in the same diff path.  They show up under
        # DBeaver's "Views" node and let users introspect the semantic
        # surface without hitting the REST API.
        self._registered_view_signatures: dict[str, str] = {}
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
        desired_views: dict[str, str] = {}
        for store_target, model in _iter_loaded_models(session_manager):
            table_name = _safe_model_table_name(store_target)
            ddl = _build_table_ddl(table_name, model)
            if ddl is None:
                continue
            desired[table_name] = ddl
            ddls[table_name] = ddl
            for view_name, view_ddl in _build_metadata_views(table_name, model):
                desired_views[view_name] = view_ddl

        with self._lock:
            # Drop schemas + tables that no longer have a backing model.
            for schema in list(self._registered_signatures):
                if schema not in desired:
                    with contextlib.suppress(Exception):
                        self._con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                    self._registered_signatures.pop(schema, None)

            # Create or recreate per-model schemas whose DDL signature
            # changed.  The DDL bundle is multi-statement (schema +
            # table) so we run each statement separately.
            for schema_name, signature in desired.items():
                if self._registered_signatures.get(schema_name) == signature:
                    continue
                with contextlib.suppress(Exception):
                    self._con.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
                try:
                    for stmt in ddls[schema_name].split(";"):
                        stmt = stmt.strip()
                        if stmt:
                            self._con.execute(stmt)
                except duckdb.Error:  # pragma: no cover — defensive guard
                    logger.exception(
                        "Failed to register catalog schema for model '%s'", schema_name
                    )
                    continue
                self._registered_signatures[schema_name] = signature

            # Drop stale metadata views (model removed). Active views
            # for active schemas were already recreated via CASCADE
            # above; here we just refresh signatures for views whose
            # bodies changed but whose owning schema survives.
            for view in list(self._registered_view_signatures):
                if view not in desired_views:
                    with contextlib.suppress(Exception):
                        schema, _, name = view.partition(".")
                        self._con.execute(f'DROP VIEW IF EXISTS "{schema}"."{name}"')
                    self._registered_view_signatures.pop(view, None)
            for view_name, signature in desired_views.items():
                if self._registered_view_signatures.get(view_name) == signature:
                    continue
                try:
                    self._con.execute(signature)
                except duckdb.Error:  # pragma: no cover — defensive guard
                    logger.exception("Failed to register metadata view '%s'", view_name)
                    continue
                self._registered_view_signatures[view_name] = signature

            # Rebuild obsl_meta.pg_database from the loaded models so
            # BI-tool "list databases" probes see the OBSL namespaces
            # instead of DuckDB's default catalogs.
            with contextlib.suppress(Exception):
                self._con.execute(_pg_database_view_ddl(list(desired.keys())))
            # Rebuild obsl_meta.pg_namespace so the BI-tool schema list
            # contains only loaded-model schemas — no ``main`` /
            # ``obsl_meta`` / ``pg_catalog`` / ``information_schema``
            # noise.
            with contextlib.suppress(Exception):
                self._con.execute(_pg_namespace_view_ddl(list(desired.keys())))
            # Set DuckDB's search_path to the loaded model schemas so
            # ``SELECT current_schema()`` returns a meaningful name
            # (the model) instead of ``main``. BI tools that highlight
            # the connected schema in their tree pick it up from here.
            if desired:
                quoted = ", ".join(
                    "'" + name.replace("'", "''") + "'" for name in desired
                )
                with contextlib.suppress(Exception):
                    self._con.execute(f"SET search_path TO {quoted}")

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


#: Brand name used as the single pg_database row. All loaded models
#: appear as TABLES inside this one logical "database" — BI-tool trees
#: get a clean top-level "orionbelt" node and the model names land
#: where users actually expect them (in the Tables list, not the
#: Databases list).
OBSL_DATABASE_NAME = "orionbelt"


def _pg_namespace_view_ddl(model_names: list[str]) -> str:
    """Rebuild ``obsl_meta.pg_namespace`` to expose only model schemas.

    DuckDB's ``pg_catalog.pg_namespace`` lists ``main``, ``obsl_meta``,
    ``pg_catalog`` and ``information_schema`` in addition to the
    schemas we registered for each model. BI-tool schema trees should
    show only the OBSL surface — one row per loaded model.
    """

    # DuckDB tags every pg_type row with ``typnamespace = oid(main)``
    # (its single physical namespace), so we MUST keep ``main`` in
    # pg_namespace — but we expose it under the name ``pg_catalog``
    # because that's where Postgres clients (DBeaver, the JDBC driver,
    # …) expect the type rows to live. Without this DBeaver logs
    # "Attribute data type 'NNNN' not found. Use varchar" for every
    # column.
    #
    # ``nspacl`` is rewritten to a non-NULL empty-array literal —
    # DuckDB types it as INTEGER (real Postgres uses ``aclitem[]``)
    # and the NULL otherwise NPEs DBeaver's tree refresh.
    keep_schemas = list(model_names) + ["main"]
    quoted_names = ", ".join("'" + n.replace("'", "''") + "'" for n in keep_schemas)
    return (
        "CREATE OR REPLACE VIEW obsl_meta.pg_namespace AS "
        "SELECT n.oid, "
        "CASE WHEN n.nspname='main' THEN 'pg_catalog' ELSE n.nspname END AS nspname, "
        "CAST(10 AS INTEGER) AS nspowner, "
        "CAST('{}' AS VARCHAR) AS nspacl "
        "FROM pg_catalog.pg_namespace n "
        f"WHERE n.nspname IN ({quoted_names})"
    )


def _pg_database_view_ddl(model_names: list[str]) -> str:
    """Build a ``CREATE OR REPLACE VIEW`` for pg_database.

    Always returns a single row named ``OBSL_DATABASE_NAME`` regardless
    of how many models are loaded — models live in the Tables list,
    not as sibling databases. ``model_names`` is accepted for API
    symmetry with the per-model refresh path; the only thing we
    currently care about is whether refresh was called at all.
    """

    del model_names  # All models share the single "orionbelt" entry.
    return f"""
    CREATE OR REPLACE VIEW obsl_meta.pg_database AS
    SELECT
        CAST(16384 AS INTEGER) AS oid,
        CAST('{OBSL_DATABASE_NAME}' AS VARCHAR) AS datname,
        CAST(10 AS INTEGER) AS datdba,
        CAST(6 AS INTEGER) AS encoding,
        CAST('en_US.UTF-8' AS VARCHAR) AS datcollate,
        CAST('en_US.UTF-8' AS VARCHAR) AS datctype,
        CAST(false AS BOOLEAN) AS datistemplate,
        CAST(true AS BOOLEAN) AS datallowconn,
        CAST(-1 AS INTEGER) AS datconnlimit,
        CAST(0 AS BIGINT) AS datfrozenxid,
        CAST(0 AS BIGINT) AS datminmxid,
        CAST(1663 AS INTEGER) AS dattablespace,
        CAST('{{}}' AS VARCHAR) AS datacl,
        CAST('c' AS VARCHAR) AS datlocprovider,
        CAST(NULL AS VARCHAR) AS daticulocale,
        CAST(NULL AS VARCHAR) AS daticurules,
        CAST(NULL AS VARCHAR) AS datcollversion
    """


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


#: Name of the single data TABLE inside each per-model schema.
#: BI-tool trees show ``<model_name>.model`` — schema = model name,
#: table = the canonical "model" label.  Mirrors the convention
#: documented in the Arrow Flight catalog handler.
MODEL_TABLE_NAME = "model"


def _build_table_ddl(schema_name: str, model: SemanticModel) -> str | None:
    """Build the per-model schema + ``CREATE TABLE schema.model`` pair.

    Each loaded model lives in its own DuckDB schema named after the
    model. The data table inside is always called ``model`` (mirrors
    the Arrow Flight surface convention), so qualified references read
    ``<model_name>.model`` and unqualified references are unambiguous
    inside the schema.

    Columns are every dimension / measure / metric exposed by the
    model. Returns ``None`` if no columns survive deduplication —
    DuckDB rejects empty column lists.
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
    quoted_schema = schema_name.replace('"', '""')
    return (
        f'CREATE SCHEMA IF NOT EXISTS "{quoted_schema}";\n'
        f'CREATE TABLE "{quoted_schema}"."{MODEL_TABLE_NAME}" '
        f"({', '.join(columns)})"
    )


def _model_columns(model: SemanticModel) -> Iterator[tuple[str, str]]:
    for label, dim in model.dimensions.items():
        yield label, _dim_sql_type(dim)
    for label, measure in model.measures.items():
        yield label, _measure_sql_type(measure)
    for label, metric in model.metrics.items():
        yield label, _metric_sql_type(metric)


def _build_metadata_views(schema_name: str, model: SemanticModel) -> Iterator[tuple[str, str]]:
    """Emit ``(qualified_view_name, ddl)`` per model for the trio.

    Both the view name and the DDL include the per-model schema, so
    each loaded model gets its own ``<model>.dimensions`` /
    ``<model>.measures`` / etc. — no cross-model collisions.
    """

    yield (
        f"{schema_name}.dimensions",
        _simple_dimensions_view_ddl(schema_name, model),
    )
    yield (
        f"{schema_name}.measures",
        _simple_measures_view_ddl(schema_name, model),
    )
    yield (
        f"{schema_name}.metrics",
        _simple_metrics_view_ddl(schema_name, model),
    )
    yield (
        f"{schema_name}._dimensions_metadata",
        _dimensions_view_ddl(schema_name, model),
    )
    yield (
        f"{schema_name}._measures_metadata",
        _measures_view_ddl(schema_name, model),
    )
    yield (
        f"{schema_name}._metrics_metadata",
        _metrics_view_ddl(schema_name, model),
    )


def _sql_literal(value: str | None) -> str:
    """Render a Python string as a single-quoted SQL literal (or NULL)."""

    if value is None or value == "":
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _simple_view_body(
    rows: list[str],
    columns: tuple[str, ...] = ("name", "data_type", "description"),
) -> str:
    """Body for a 3-column ``name / data_type / description`` summary view."""

    if not rows:
        casts = ", ".join(f"CAST(NULL AS VARCHAR) AS {c}" for c in columns)
        return f"SELECT {casts} WHERE FALSE"
    col_list = ", ".join(columns)
    return f"SELECT * FROM (VALUES {', '.join(rows)}) AS t({col_list})"


def _qualified(schema: str, name: str) -> str:
    """Return ``"schema"."name"`` with both identifiers safely quoted."""

    return f'"{schema.replace(chr(34), chr(34) * 2)}"."{name}"'


def _typed_projection_body(columns: list[tuple[str, str]]) -> str:
    """Body for a Flight-shaped view: one typed column per artefact.

    Each ``(label, sql_type)`` pair becomes a ``CAST(NULL AS T)`` column.
    The view is empty (``WHERE FALSE``) — its purpose is to expose the
    surface area as a typed schema for BI-tool discovery, not to return
    data.  Real values come from ``<schema>.model``.
    """

    if not columns:
        return "SELECT CAST(NULL AS VARCHAR) AS __empty__ WHERE FALSE"
    projections = ", ".join(
        f'CAST(NULL AS {sql_type}) AS "{label.replace(chr(34), chr(34) * 2)}"'
        for label, sql_type in columns
    )
    return f"SELECT {projections} WHERE FALSE"


def _simple_dimensions_view_ddl(schema: str, model: SemanticModel) -> str:
    columns = [(label, _dim_sql_type(dim)) for label, dim in model.dimensions.items()]
    return (
        f"CREATE OR REPLACE VIEW {_qualified(schema, 'dimensions')} "
        f"AS {_typed_projection_body(columns)}"
    )


def _simple_measures_view_ddl(schema: str, model: SemanticModel) -> str:
    columns = [(label, _measure_sql_type(measure)) for label, measure in model.measures.items()]
    return (
        f"CREATE OR REPLACE VIEW {_qualified(schema, 'measures')} "
        f"AS {_typed_projection_body(columns)}"
    )


def _simple_metrics_view_ddl(schema: str, model: SemanticModel) -> str:
    columns = [(label, _metric_sql_type(metric)) for label, metric in model.metrics.items()]
    return (
        f"CREATE OR REPLACE VIEW {_qualified(schema, 'metrics')} "
        f"AS {_typed_projection_body(columns)}"
    )


def _value_literal(value: Any, sql_type: str) -> str:
    """Render any Python value as a SQL literal for VALUES.

    Strings flow through ``_sql_literal`` (quoted + escaped); integers
    are emitted unquoted so DuckDB types them as numeric; ``None`` and
    empty strings become ``CAST(NULL AS T)`` — the explicit cast keeps
    DuckDB from inferring a NULL column type when every row in a
    column is unset.
    """

    if value is None or value == "":
        return f"CAST(NULL AS {sql_type})"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return f"CAST({value} AS {sql_type})"
    return f"CAST({_sql_literal(str(value))} AS {sql_type})"


def _build_metadata_view_ddl(
    schema: str,
    view_name: str,
    columns: tuple[tuple[str, str], ...],
    rows: list[tuple[Any, ...]],
) -> str:
    """Generic CREATE VIEW … AS VALUES (...) builder.

    ``columns`` is an ordered list of ``(name, sql_type)`` pairs;
    ``rows`` matches that ordering. Empty ``rows`` emits a typed
    ``WHERE FALSE`` projection so the view shape is still inspectable.
    """

    target = _qualified(schema, view_name)
    if not rows:
        casts = ", ".join(f'CAST(NULL AS {sql_type}) AS "{name}"' for name, sql_type in columns)
        return f"CREATE OR REPLACE VIEW {target} AS SELECT {casts} WHERE FALSE"
    row_sql = []
    for row in rows:
        cells = [
            _value_literal(value, sql_type)
            for value, (_, sql_type) in zip(row, columns, strict=True)
        ]
        row_sql.append("(" + ", ".join(cells) + ")")
    col_list = ", ".join(f'"{name}"' for name, _ in columns)
    return (
        f"CREATE OR REPLACE VIEW {target} AS "
        f"SELECT * FROM (VALUES {', '.join(row_sql)}) AS t({col_list})"
    )


_DIMENSIONS_METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "VARCHAR"),
    ("data_object", "VARCHAR"),
    ("column", "VARCHAR"),
    ("type", "VARCHAR"),
    ("time_grain", "VARCHAR"),
    ("description", "VARCHAR"),
)

_MEASURES_METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "VARCHAR"),
    ("aggregation", "VARCHAR"),
    ("expression", "VARCHAR"),
    ("type", "VARCHAR"),
    ("columns", "VARCHAR"),
    ("description", "VARCHAR"),
)

_METRICS_METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "VARCHAR"),
    ("metric_type", "VARCHAR"),
    ("expression", "VARCHAR"),
    ("measure", "VARCHAR"),
    ("time_dimension", "VARCHAR"),
    ("time_grain", "VARCHAR"),
    ("window", "BIGINT"),
    ("grain_to_date", "VARCHAR"),
    ("description", "VARCHAR"),
)


def _dimensions_view_ddl(schema: str, model: SemanticModel) -> str:
    from orionbelt.obsl.metadata_rows import build_dimension_rows

    return _build_metadata_view_ddl(
        schema,
        "_dimensions_metadata",
        _DIMENSIONS_METADATA_COLUMNS,
        list(build_dimension_rows(model)),
    )


def _measures_view_ddl(schema: str, model: SemanticModel) -> str:
    from orionbelt.obsl.metadata_rows import build_measure_rows

    return _build_metadata_view_ddl(
        schema,
        "_measures_metadata",
        _MEASURES_METADATA_COLUMNS,
        list(build_measure_rows(model)),
    )


def _metrics_view_ddl(schema: str, model: SemanticModel) -> str:
    from orionbelt.obsl.metadata_rows import build_metric_rows

    return _build_metadata_view_ddl(
        schema,
        "_metrics_metadata",
        _METRICS_METADATA_COLUMNS,
        list(build_metric_rows(model)),
    )


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
