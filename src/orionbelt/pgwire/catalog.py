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
import hashlib
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

# Branded schema name surfaced to BI tools.  Replaces the Postgres
# default ``public`` so OBSL models show up under a recognisable label
# in Tableau / Power BI / DBeaver schema browsers. Kept as a constant
# (not configurable) so the canned ``search_path`` / ``current_schema``
# replies and the catalog DDL stay in lockstep.
CATALOG_SCHEMA = "orionbelt"


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
    # Both arities — ``pg_get_expr(expr, oid)`` (Dremio's pgjdbc /
    # DBeaver) and ``pg_get_expr(expr, oid, pretty)`` (psql / pgAdmin).
    # DuckDB ``CREATE OR REPLACE MACRO`` does NOT arity-overload —
    # a second CREATE OR REPLACE with a different arity silently
    # replaces the first. The default-value syntax (``pretty := …``)
    # is what makes one macro accept both call shapes.
    "CREATE OR REPLACE MACRO pg_get_expr(expr, oid, pretty := false) AS NULL",
    # pg_get_keywords is a set-returning function in real Postgres.
    # DBeaver calls it as ``FROM pg_get_keywords()`` (table form), so a
    # scalar macro fails with "Table Function with name pg_get_keywords
    # does not exist". DuckDB ``MACRO … AS TABLE …`` declares a
    # table-returning macro. The single dummy row is enough to satisfy
    # the ``SELECT string_agg(word, ',')`` shape DBeaver uses to build
    # its SQL editor's reserved-word list.
    "CREATE OR REPLACE MACRO pg_get_keywords() AS TABLE SELECT 'select' AS word",
    "CREATE OR REPLACE MACRO pg_get_function_arguments(oid) AS ''",
    "CREATE OR REPLACE MACRO pg_get_function_identity_arguments(oid) AS ''",
    "CREATE OR REPLACE MACRO pg_get_function_result(oid) AS ''",
    "CREATE OR REPLACE MACRO pg_get_serial_sequence(table_name, column_name) AS NULL",
    "CREATE OR REPLACE MACRO pg_size_pretty(bytes) AS '0 bytes'",
    "CREATE OR REPLACE MACRO pg_relation_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_total_relation_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_database_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_indexes_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_table_size(oid) AS 0",
    "CREATE OR REPLACE MACRO pg_relation_is_publishable(oid) AS false",
    "CREATE OR REPLACE MACRO obj_description(oid, catalog) AS NULL",
    "CREATE OR REPLACE MACRO col_description(oid, col) AS NULL",
    "CREATE OR REPLACE MACRO format_type(oid, typemod) AS 'unknown'",
    "CREATE OR REPLACE MACRO pg_encoding_to_char(enc) AS 'UTF8'",
    # JDBC's ``getPrimaryKeys`` calls
    # ``(information_schema._pg_expandarray(i.indkey)).n`` to enumerate
    # PK column positions. Our virtual tables have no PKs so the
    # surrounding JOIN against ``pg_index`` is always empty; the macro
    # only needs to be resolvable. Return a STRUCT so ``(call).n``
    # parses; the actual SELECT never executes.
    "CREATE OR REPLACE MACRO _pg_expandarray(arr) AS {'x': NULL, 'n': NULL}",
)


# Shadow views that translate DuckDB's internal type-id numbering in
# ``pg_attribute`` / ``pg_type`` to real Postgres OIDs. DuckDB stores
# ``atttypid = 23`` for a DOUBLE column — but 23 in Postgres is INT4.
# Tableau's pgjdbc reads that 23 and allocates a 4-byte integer reader
# for what we send as an 8-byte FLOAT8, producing NULL / 0 measures.
#
# We can't write into the ``pg_catalog`` schema (DuckDB rejects it as a
# system catalog), so we expose translated views under the orionbelt
# schema and rewrite ``pg_attribute`` / ``pg_type`` references in
# incoming SQL to use them.
#
# Mapping below derived empirically from ``CREATE TABLE t (a VARCHAR,
# b BIGINT, c DOUBLE, …)`` + ``SELECT atttypid FROM pg_attribute``:
# DuckDB id → Postgres OID
#   10 → 16   (BOOL)
#   12 → 21   (INT2)
#   13 → 23   (INT4)
#   14 → 20   (INT8)
#   15 → 1082 (DATE)
#   16 → 1083 (TIME)
#   19 → 1114 (TIMESTAMP)
#   21 → 1700 (NUMERIC)
#   22 → 700  (FLOAT4)
#   23 → 701  (FLOAT8)
#   25 → 25   (TEXT — already matches)
#   26 → 17   (BYTEA)
#   27 → 1186 (INTERVAL)
_OID_TRANSLATION_CASE = """
        CASE atttypid
            WHEN 10 THEN 16    WHEN 12 THEN 21    WHEN 13 THEN 23
            WHEN 14 THEN 20    WHEN 15 THEN 1082  WHEN 16 THEN 1083
            WHEN 19 THEN 1114  WHEN 21 THEN 1700  WHEN 22 THEN 700
            WHEN 23 THEN 701   WHEN 26 THEN 17    WHEN 27 THEN 1186
            ELSE atttypid::INTEGER
        END
"""

_SHADOW_VIEWS: tuple[str, ...] = (
    # Empty stubs for pg_catalog tables DuckDB doesn't expose but
    # DBeaver / pgAdmin / pg_dump probe during schema-tree refresh.
    # Each one carries the columns the standard probe queries reference,
    # all rows = 0. The router's pg_catalog → shadow rewrite (below)
    # routes ``pg_catalog.<name>`` and bare ``<name>`` references to
    # these views. Without them DBeaver bombs with "Catalog Error:
    # Table with name <X> does not exist" when it browses event
    # triggers, publications, subscriptions, foreign-data wrappers,
    # or row-level-security policies. None of these features apply to
    # OBSL's read-only semantic surface, so empty results are
    # semantically correct as well as the cheapest fix.
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_event_trigger AS
        SELECT * FROM (VALUES (
            NULL::INTEGER,    -- oid
            NULL::VARCHAR,    -- evtname
            NULL::VARCHAR,    -- evtevent
            NULL::INTEGER,    -- evtowner
            NULL::INTEGER,    -- evtfoid
            NULL::VARCHAR,    -- evtenabled
            NULL::VARCHAR     -- evttags (text[] in real pg; VARCHAR is fine for empty)
        )) AS t(oid, evtname, evtevent, evtowner, evtfoid, evtenabled, evttags)
        WHERE false""",
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_publication AS
        SELECT * FROM (VALUES (
            NULL::INTEGER, NULL::VARCHAR, NULL::INTEGER,
            NULL::BOOLEAN, NULL::BOOLEAN, NULL::BOOLEAN,
            NULL::BOOLEAN, NULL::BOOLEAN, NULL::BOOLEAN
        )) AS t(oid, pubname, pubowner, puballtables,
                pubinsert, pubupdate, pubdelete, pubtruncate, pubviaroot)
        WHERE false""",
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_subscription AS
        SELECT * FROM (VALUES (
            NULL::INTEGER, NULL::INTEGER, NULL::VARCHAR, NULL::INTEGER,
            NULL::VARCHAR, NULL::BOOLEAN, NULL::BOOLEAN,
            NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR,
            NULL::VARCHAR, NULL::VARCHAR
        )) AS t(oid, subdbid, subname, subowner, subconninfo,
                subenabled, subbinary, subslotname, subsynccommit,
                subpublications, suborigin, subskiplsn)
        WHERE false""",
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_foreign_data_wrapper AS
        SELECT * FROM (VALUES (
            NULL::INTEGER, NULL::VARCHAR, NULL::INTEGER,
            NULL::INTEGER, NULL::INTEGER, NULL::VARCHAR, NULL::VARCHAR
        )) AS t(oid, fdwname, fdwowner, fdwhandler, fdwvalidator,
                fdwacl, fdwoptions)
        WHERE false""",
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_foreign_server AS
        SELECT * FROM (VALUES (
            NULL::INTEGER, NULL::VARCHAR, NULL::INTEGER, NULL::INTEGER,
            NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR
        )) AS t(oid, srvname, srvowner, srvfdw, srvtype, srvversion,
                srvacl, srvoptions)
        WHERE false""",
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_user_mapping AS
        SELECT * FROM (VALUES (
            NULL::INTEGER, NULL::INTEGER, NULL::INTEGER, NULL::VARCHAR
        )) AS t(oid, umuser, umserver, umoptions)
        WHERE false""",
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_policy AS
        SELECT * FROM (VALUES (
            NULL::INTEGER, NULL::VARCHAR, NULL::INTEGER, NULL::VARCHAR,
            NULL::BOOLEAN, NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR
        )) AS t(oid, polname, polrelid, polcmd, polpermissive,
                polroles, polqual, polwithcheck)
        WHERE false""",
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_extension AS
        SELECT * FROM (VALUES (
            NULL::INTEGER, NULL::VARCHAR, NULL::INTEGER, NULL::INTEGER,
            NULL::BOOLEAN, NULL::VARCHAR, NULL::VARCHAR, NULL::VARCHAR
        )) AS t(oid, extname, extowner, extnamespace, extrelocatable,
                extversion, extconfig, extcondition)
        WHERE false""",
    # Shadow pg_attribute that translates atttypid to real Postgres OIDs.
    # All other columns pass through unchanged so the rest of the JDBC
    # getColumns query keeps working.
    #
    # ``TEMP VIEW`` is intentional: DuckDB puts temp objects in a special
    # catalog where ``pg_class.relnamespace`` is NULL. Tableau's
    # ``getTables`` query filters ``WHERE nspname NOT IN ('pg_catalog',
    # 'information_schema')`` — and ``NULL NOT IN (…)`` evaluates to
    # NULL, excluding the row. The views remain queryable by name so
    # the SQL rewrites below still resolve.
    # ``attidentity`` (Postgres 10+) and ``attgenerated`` (Postgres 12+)
    # don't exist in DuckDB's pg_attribute view. Dremio's
    # ``DatabaseMetaData.getColumns()`` probe references both, so we
    # synthesize them as the Postgres sentinels for "not an identity
    # column" / "not a generated column" (empty string).
    f"""CREATE OR REPLACE TEMP VIEW _obsl_pg_attribute AS
        SELECT
            attrelid,
            attname,
            {_OID_TRANSLATION_CASE} AS atttypid,
            attstattarget,
            attlen,
            attnum,
            attndims,
            attcacheoff,
            atttypmod,
            attbyval,
            attstorage,
            attalign,
            attnotnull,
            atthasdef,
            attisdropped,
            attislocal,
            attinhcount,
            attcollation,
            ''::VARCHAR AS attidentity,
            ''::VARCHAR AS attgenerated
        FROM pg_catalog.pg_attribute""",
    # Shadow pg_type with real Postgres OIDs + the columns pgjdbc reads
    # from DatabaseMetaData.getColumns / getTypeInfo. TEMP for the same
    # invisibility-to-Tableau reasons as ``_obsl_pg_attribute`` above.
    # Dremio's Postgres JDBC connector probe from
    # `DatabaseMetaData.getColumns()` references `typtypmod` and
    # `typbasetype`; without them the binder fails with
    # 'Values list "t" does not have a column named "<col>"'. -1 is the
    # Postgres sentinel for "no type modifier", 0 is the sentinel for
    # "not a domain over another type" — both are correct for every base
    # type we expose here.
    # ``typelem`` is the array-element type OID (0 = "not an array");
    # ``typrelid`` is the relation OID for composite types (0 = scalar).
    # DBeaver's pgwire connect-check self-joins pg_type via
    # ``LEFT JOIN pg_type et ON et.oid = t.typelem`` to resolve array
    # element types and fails the bind without ``typelem``. Both are
    # 0 for every base scalar we expose here.
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_type AS
        SELECT * FROM (VALUES
            -- columns: oid, typname, typcategory, typlen, typtype,
            --          typnotnull, typtypmod, typbasetype, typelem, typrelid
            (16,   'bool',        'B', 1,   'b', false, -1, 0, 0, 0),
            (17,   'bytea',       'U', -1,  'b', false, -1, 0, 0, 0),
            (18,   'char',        'S', 1,   'b', false, -1, 0, 0, 0),
            (19,   'name',        'S', 64,  'b', false, -1, 0, 0, 0),
            (20,   'int8',        'N', 8,   'b', false, -1, 0, 0, 0),
            (21,   'int2',        'N', 2,   'b', false, -1, 0, 0, 0),
            (23,   'int4',        'N', 4,   'b', false, -1, 0, 0, 0),
            (25,   'text',        'S', -1,  'b', false, -1, 0, 0, 0),
            (26,   'oid',         'N', 4,   'b', false, -1, 0, 0, 0),
            (700,  'float4',      'N', 4,   'b', false, -1, 0, 0, 0),
            (701,  'float8',      'N', 8,   'b', false, -1, 0, 0, 0),
            (1042, 'bpchar',      'S', -1,  'b', false, -1, 0, 0, 0),
            (1043, 'varchar',     'S', -1,  'b', false, -1, 0, 0, 0),
            (1082, 'date',        'D', 4,   'b', false, -1, 0, 0, 0),
            (1083, 'time',        'D', 8,   'b', false, -1, 0, 0, 0),
            (1114, 'timestamp',   'D', 8,   'b', false, -1, 0, 0, 0),
            (1184, 'timestamptz', 'D', 8,   'b', false, -1, 0, 0, 0),
            (1186, 'interval',    'T', 16,  'b', false, -1, 0, 0, 0),
            (1266, 'timetz',      'D', 12,  'b', false, -1, 0, 0, 0),
            (1700, 'numeric',     'N', -1,  'b', false, -1, 0, 0, 0),
            (2950, 'uuid',        'U', 16,  'b', false, -1, 0, 0, 0)
        ) AS t(oid, typname, typcategory, typlen, typtype, typnotnull,
               typtypmod, typbasetype, typelem, typrelid)""",
    # Shadow pg_class that replaces NULL ``aclitem[]`` / ``text[]``
    # columns with non-NULL empty-array literals. pgjdbc inside
    # DBeaver and other BI clients reads ``relacl`` / ``reloptions``
    # during schema-tree refresh and NPEs on NULL with the message
    # "Cannot invoke java.lang.CharSequence.toString() because
    # <parameter1> is null". DuckDB's native pg_class always returns
    # NULL for these (no ACL system, no per-table reloptions). Keep
    # all other columns intact via SELECT * EXCLUDE so existing
    # introspection probes work unchanged.
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_class AS
        SELECT * EXCLUDE (relacl, reloptions),
               COALESCE(CAST(relacl AS VARCHAR), '{}') AS relacl,
               COALESCE(CAST(reloptions AS VARCHAR), '{}') AS reloptions
        FROM pg_catalog.pg_class""",
    # Shadow pg_namespace that hides DuckDB's internal ``main`` schema
    # from BI-tool schema browsers. The branded ``orionbelt`` ATTACHed
    # DB owns one schema per loaded model; ``main`` is DuckDB's own
    # default and shouldn't show up next to them.
    #
    # ``nspacl`` is rewritten to a non-NULL empty-array literal —
    # DuckDB's pg_namespace stores it as NULL, and pgjdbc's schema-
    # tree refresh NPEs reading a NULL where it expects an
    # ``aclitem[]``: "Cannot invoke java.lang.CharSequence.toString()
    # because <parameter1> is null". Empty ``{}`` keeps the column
    # non-NULL without granting any privileges.
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_namespace AS
        SELECT
            oid,
            nspname,
            COALESCE(nspowner, 10) AS nspowner,
            COALESCE(CAST(nspacl AS VARCHAR), '{}') AS nspacl
        FROM pg_catalog.pg_namespace
        WHERE nspname NOT IN ('main', 'temp', 'pg_temp')""",
    # Shadow information_schema.schemata. DuckDB auto-creates a ``main``
    # schema in every attached database (``orionbelt.main``, plus
    # ``memory.main`` / ``system.main`` / ``temp.main``), so the raw view
    # shows ``main`` next to each per-model schema. pgjdbc-based clients
    # enumerate schemas via pg_namespace (shadowed above), but Dremio's
    # Postgres-source connector reads ``information_schema.schemata``
    # directly — without this shadow it lists ``main`` beside the model
    # schema in its dataset browser. Same row filter as the pg_namespace
    # shadow; ``SELECT *`` preserves the standard column shape.
    """CREATE OR REPLACE TEMP VIEW _obsl_schemata AS
        SELECT * FROM information_schema.schemata
        WHERE schema_name NOT IN ('main', 'temp', 'pg_temp')""",
    # Shadow pg_tables / pg_views. Dremio's Postgres-source connector
    # enumerates schemas from the ``schemaname`` column of these two views
    # (``SELECT schemaname FROM pg_tables UNION ALL SELECT schemaname FROM
    # pg_views``), NOT from pg_namespace or information_schema.schemata.
    # DuckDB's built-in objects (``duckdb_*``, ``sqlite_*``) and OBSL's own
    # shadow views all live in the ``main`` schema, so without this filter
    # Dremio shows a spurious ``main`` schema beside each model. Dropping
    # the ``main`` / temp rows leaves only the per-model schemas; the model
    # tables/views (in ``<model>``) are untouched. ``SELECT *`` preserves
    # the column shape (schemaname / tablename / tableowner / …).
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_tables AS
        SELECT * FROM pg_catalog.pg_tables
        WHERE schemaname NOT IN ('main', 'temp', 'pg_temp')""",
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_views AS
        SELECT * FROM pg_catalog.pg_views
        WHERE schemaname NOT IN ('main', 'temp', 'pg_temp')""",
    # Shadow pg_database. DBeaver / pgAdmin connect-check filters on
    # ``WHERE datallowconn AND NOT datistemplate`` (and reads
    # encoding / datcollate / datctype / datacl in the same probe);
    # DuckDB's native pg_database view is missing those columns and
    # the binder fails with ``Referenced column "datallowconn" not
    # found``. Single-row view named after the OBSL brand keeps the
    # one-database illusion the rest of the catalog presents.
    # Shadow pg_settings — supplements DuckDB's pg_settings (which
    # only exposes DuckDB-internal GUCs) with the Postgres GUCs BI
    # tools probe during connect. Tableau queries
    # ``SELECT setting FROM pg_settings WHERE name='max_index_keys'``
    # and bails the entire connection with a generic
    # "Bad Connection" 81B3934F error when the result is empty —
    # DuckDB's pg_settings has no ``max_index_keys`` row. We UNION
    # the standard Postgres defaults below so the values BI clients
    # rely on are always queryable; columns match Postgres
    # (``name`` / ``setting`` / ``category`` / ``short_desc``) so a
    # ``SELECT *`` works too. Casts force VARCHAR throughout because
    # DuckDB's pg_settings.setting is VARCHAR — a UNION with a TEXT
    # literal would otherwise fail type unification.
    """CREATE OR REPLACE TEMP VIEW _obsl_pg_settings AS
        SELECT
            CAST(name AS VARCHAR) AS name,
            CAST(value AS VARCHAR) AS setting,
            CAST('' AS VARCHAR) AS category,
            CAST(description AS VARCHAR) AS short_desc
        FROM duckdb_settings()
        UNION ALL
        SELECT * FROM (VALUES
            ('max_index_keys',          '32',  'Preset Options',  'Number of index keys'),
            ('max_identifier_length',   '63',  'Preset Options',  'Identifier length'),
            ('block_size',              '8192','Preset Options',  'Block size'),
            ('server_version',          '15.0','Reporting',       'Server version'),
            ('server_version_num',      '150000','Reporting',     'Server version number'),
            ('server_encoding',         'UTF8','Client Connection Defaults','Server encoding'),
            ('client_encoding',         'UTF8','Client Connection Defaults','Client encoding'),
            ('DateStyle',               'ISO, MDY','Client Connection Defaults','Date format'),
            ('TimeZone',                'UTC', 'Client Connection Defaults','Time zone'),
            ('IntervalStyle',           'postgres','Client Connection Defaults','Interval format'),
            ('integer_datetimes',       'on',  'Preset Options',  'Integer datetimes'),
            ('standard_conforming_strings','on','Compatibility','Standard-conforming strings'),
            ('is_superuser',            'off', 'Preset Options',  'Superuser flag'),
            ('session_authorization',   'obsl','Client Connection Defaults','Session user'),
            ('lc_collate',              'en_US.UTF-8','Preset Options','Collate locale'),
            ('lc_ctype',                'en_US.UTF-8','Preset Options','Ctype locale'),
            ('lc_messages',             'en_US.UTF-8','Reporting','Messages locale'),
            ('lc_monetary',             'en_US.UTF-8','Locale','Monetary locale'),
            ('lc_numeric',              'en_US.UTF-8','Locale','Numeric locale'),
            ('lc_time',                 'en_US.UTF-8','Locale','Time locale'),
            ('search_path',             '"$user", public','Client Connection','Search path'),
            ('default_transaction_isolation','read committed','Client Connection','Isolation'),
            ('default_transaction_read_only','off','Client Connection','Default read-only'),
            ('transaction_isolation',   'read committed','Connection','Transaction isolation'),
            ('transaction_read_only',   'off', 'Connection',  'Transaction read-only'),
            ('application_name',        '',    'Reporting',   'Application name'),
            ('extra_float_digits',      '3',   'Client Connection','Float precision')
        ) AS t(name, setting, category, short_desc)""",
    f"""CREATE OR REPLACE TEMP VIEW _obsl_pg_database AS
        SELECT * FROM (VALUES (
            16384::INTEGER,                         -- oid
            '{CATALOG_SCHEMA}'::VARCHAR,            -- datname
            10::INTEGER,                            -- datdba
            6::INTEGER,                             -- encoding (UTF8 = 6)
            'en_US.UTF-8'::VARCHAR,                 -- datcollate
            'en_US.UTF-8'::VARCHAR,                 -- datctype
            false::BOOLEAN,                         -- datistemplate
            true::BOOLEAN,                          -- datallowconn
            -1::INTEGER,                            -- datconnlimit
            0::BIGINT,                              -- datfrozenxid
            0::BIGINT,                              -- datminmxid
            1663::INTEGER,                          -- dattablespace (pg_default)
            '{{}}'::VARCHAR,                        -- datacl (NOT NULL string for DBeaver)
            'c'::VARCHAR,                           -- datlocprovider
            NULL::VARCHAR,                          -- daticulocale
            NULL::VARCHAR,                          -- daticurules
            NULL::VARCHAR                           -- datcollversion
        )) AS t(
            oid, datname, datdba, encoding, datcollate, datctype,
            datistemplate, datallowconn, datconnlimit, datfrozenxid,
            datminmxid, dattablespace, datacl, datlocprovider,
            daticulocale, daticurules, datcollversion
        )""",
)


# Rewrites we apply to the SQL before handing it to DuckDB.  Each
# pattern targets a specific psql / pgAdmin quirk.  Order matters: the
# more specific rules (``OPERATOR(pg_catalog.~)``) run before the
# generic ``pg_catalog.<ident>`` prefix strip.
_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    # CREATE [LOCAL|GLOBAL] TEMPORARY TABLE → CREATE TEMPORARY TABLE.
    # DuckDB rejects the LOCAL / GLOBAL modifiers Postgres allows;
    # they're meaningless for our single-process catalog connection.
    (
        re.compile(
            r"\bcreate\s+(?:local|global)\s+(temp(?:orary)?\s+table)\b",
            re.IGNORECASE,
        ),
        r"CREATE \1",
    ),
    # ``ON COMMIT PRESERVE ROWS`` / ``DROP`` modifiers — meaningless
    # for an in-memory DuckDB connection that has no transactions in
    # the Postgres sense. Strip so the surrounding DDL parses.
    (
        re.compile(r"\bON\s+COMMIT\s+(?:PRESERVE|DELETE|DROP)\s+ROWS\b", re.IGNORECASE),
        "",
    ),
    # ``SELECT … INTO [TEMP[ORARY]] TABLE "name" FROM …`` is Postgres
    # syntax DuckDB doesn't accept. Rewrite to ``CREATE [TEMPORARY]
    # TABLE "name" AS SELECT … FROM …``. Tableau's connect-check uses
    # this shape to test temp-table support.
    (
        re.compile(
            r"\bSELECT\b(?P<cols>.+?)\bINTO\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+"
            r'(?P<name>"[^"]+"|\w+)\s+FROM\b(?P<rest>.+)$',
            re.IGNORECASE | re.DOTALL,
        ),
        r"CREATE TEMPORARY TABLE \g<name> AS SELECT \g<cols> FROM \g<rest>",
    ),
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
            r"::\s*pg_catalog\.(text|name|regtype|regclass|regprocedure|regnamespace|oid|char)\b",
            re.IGNORECASE,
        ),
        "::VARCHAR",
    ),
    # Bare ::regclass / ::regtype / ::oid / ::name (without the
    # ``pg_catalog.`` qualifier) — same collapse, same rationale.
    # Tableau and pgAdmin emit both forms depending on the probe.
    (
        re.compile(
            r"::\s*(regclass|regtype|regprocedure|regnamespace|name)\b",
            re.IGNORECASE,
        ),
        "::VARCHAR",
    ),
    # information_schema._pg_expandarray → bare _pg_expandarray. JDBC's
    # getPrimaryKeys query uses the qualified form; DuckDB can't create
    # macros inside ``information_schema`` so we strip the prefix and
    # resolve against the stub macro defined above.
    (
        re.compile(r"\binformation_schema\._pg_expandarray\b", re.IGNORECASE),
        "_pg_expandarray",
    ),
    # pg_catalog.pg_attribute / pg_attribute → _obsl_pg_attribute. The
    # DuckDB pg_attribute view stores DuckDB's internal type-id numbering
    # in atttypid (e.g. 23 for DOUBLE, where 23 is Postgres INT4). The
    # shadow view translates to real Postgres OIDs so Tableau's pgjdbc
    # allocates the right-width column reader.
    (
        re.compile(r"\bpg_catalog\s*\.\s*pg_attribute\b", re.IGNORECASE),
        "_obsl_pg_attribute",
    ),
    (
        re.compile(r"(?<![.\w])pg_attribute\b", re.IGNORECASE),
        "_obsl_pg_attribute",
    ),
    # Same story for pg_type — the shadow view exposes real Postgres
    # OIDs + typname so the JOIN ``a.atttypid = t.oid`` lines up after
    # the atttypid translation above.
    (
        re.compile(r"\bpg_catalog\s*\.\s*pg_type\b", re.IGNORECASE),
        "_obsl_pg_type",
    ),
    (
        re.compile(r"(?<![.\w])pg_type\b", re.IGNORECASE),
        "_obsl_pg_type",
    ),
    # pg_database — DBeaver / pgAdmin connect-check filters on
    # ``datallowconn AND NOT datistemplate``, columns DuckDB's native
    # pg_database lacks. Route to the single-row shadow that carries
    # the full standard column set (see _SHADOW_VIEWS).
    (
        re.compile(r"\bpg_catalog\s*\.\s*pg_database\b", re.IGNORECASE),
        "_obsl_pg_database",
    ),
    (
        re.compile(r"(?<![.\w])pg_database\b(?!\s*\()", re.IGNORECASE),
        "_obsl_pg_database",
    ),
    # pg_namespace — DuckDB's native view exposes ``main`` (the
    # default schema) next to our branded ``orionbelt`` schema, which
    # confuses BI-tool browsers. Shadow filters ``main`` /
    # temp-schema noise out so users see only the OBSL surface.
    (
        re.compile(r"\bpg_catalog\s*\.\s*pg_namespace\b", re.IGNORECASE),
        "_obsl_pg_namespace",
    ),
    (
        re.compile(r"(?<![.\w])pg_namespace\b", re.IGNORECASE),
        "_obsl_pg_namespace",
    ),
    # information_schema.schemata → filtered shadow (see _SHADOW_VIEWS).
    # Hides DuckDB's per-database ``main`` schema from clients (notably
    # Dremio) that enumerate schemas through information_schema rather
    # than pg_namespace. Handles an optional catalog qualifier and quoted
    # identifiers so a ``<db>.information_schema.schemata`` pushdown is
    # covered too.
    (
        re.compile(
            r'(?:"?\w+"?\s*\.\s*)?"?information_schema"?\s*\.\s*"?schemata"?\b',
            re.IGNORECASE,
        ),
        "_obsl_schemata",
    ),
    # pg_tables / pg_views — Dremio derives the schema list from the
    # ``schemaname`` column of these two views. The shadows filter out the
    # ``main`` / temp schemas (DuckDB built-ins + OBSL shadow views) so the
    # only schemas Dremio surfaces are the per-model ones.
    (
        re.compile(r"\bpg_catalog\s*\.\s*pg_tables\b", re.IGNORECASE),
        "_obsl_pg_tables",
    ),
    (
        re.compile(r"(?<![.\w])pg_tables\b(?!\s*\()", re.IGNORECASE),
        "_obsl_pg_tables",
    ),
    (
        re.compile(r"\bpg_catalog\s*\.\s*pg_views\b", re.IGNORECASE),
        "_obsl_pg_views",
    ),
    (
        re.compile(r"(?<![.\w])pg_views\b(?!\s*\()", re.IGNORECASE),
        "_obsl_pg_views",
    ),
    # pg_class — replace NULL acl-typed columns with non-NULL empty
    # literals so pgjdbc schema-tree refresh doesn't NPE on
    # ``relacl`` / ``reloptions``.
    (
        re.compile(r"\bpg_catalog\s*\.\s*pg_class\b", re.IGNORECASE),
        "_obsl_pg_class",
    ),
    (
        re.compile(r"(?<![.\w])pg_class\b(?!\s*\()", re.IGNORECASE),
        "_obsl_pg_class",
    ),
    # pg_settings — DuckDB's view doesn't expose Postgres-only GUCs
    # like ``max_index_keys`` / ``server_version_num``. The shadow
    # UNIONs them in so Tableau / pgjdbc / pgAdmin probes find the
    # values they expect.
    (
        re.compile(r"\bpg_catalog\s*\.\s*pg_settings\b", re.IGNORECASE),
        "_obsl_pg_settings",
    ),
    (
        re.compile(r"(?<![.\w])pg_settings\b(?!\s*\()", re.IGNORECASE),
        "_obsl_pg_settings",
    ),
    # Empty-stub catalogs DuckDB doesn't expose but DBeaver / pgAdmin
    # probe during schema-tree refresh. Each rewrite handles both the
    # ``pg_catalog.<name>`` and bare ``<name>`` forms — Postgres
    # clients drop the qualifier opportunistically. Without the
    # rewrite DuckDB errors with "Catalog Error: Table with name
    # <X> does not exist" and the schema browser reports a confusing
    # ``42601 catalog query failed`` to the user.
    *[
        rewrite
        for stub_name in (
            "pg_event_trigger",
            "pg_publication",
            "pg_subscription",
            "pg_foreign_data_wrapper",
            "pg_foreign_server",
            "pg_user_mapping",
            "pg_policy",
            "pg_extension",
        )
        for rewrite in (
            (
                re.compile(rf"\bpg_catalog\s*\.\s*{stub_name}\b", re.IGNORECASE),
                f"_obsl_{stub_name}",
            ),
            (
                re.compile(rf"(?<![.\w]){stub_name}\b(?!\s*\()", re.IGNORECASE),
                f"_obsl_{stub_name}",
            ),
        )
    ],
    # Function / operator references prefixed with pg_catalog. — strip
    # the prefix so DuckDB resolves against the unqualified built-in or
    # our stub macros.  We deliberately don't touch table references
    # like ``pg_catalog.pg_class`` because DuckDB handles those itself.
    (
        re.compile(
            r"pg_catalog\.(pg_[a-z_]+|format_type|obj_description|col_description)\s*\(",
            re.IGNORECASE,
        ),
        r"\1(",
    ),
)


def _rewrite_for_duckdb(sql: str) -> str:
    """Best-effort SQL rewrite so psql introspection runs on DuckDB.

    Touches only the patterns documented in ``_REWRITES``.  Unrecognised
    constructs are left alone — the catalog branch is best-effort by
    design, and the caller's error response will surface anything we
    miss so we can extend the rule list incrementally.
    """

    out = sql
    for pattern, replacement in _REWRITES:
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
        # Single ATTACHed in-memory database named after the brand.
        # All loaded models live as **schemas** inside it; each model
        # schema carries a literal ``model`` table plus per-model
        # metadata views. BI tools that connect with ``database=orionbelt``
        # see the layout as:
        #
        #   orionbelt
        #     ├─ <model_a>    ← schema (one per loaded model)
        #     │   ├─ model              ← the user-facing virtual table
        #     │   ├─ dimensions / measures / metrics  ← metadata views
        #     │   └─ _dimensions_metadata / _measures_metadata / _metrics_metadata
        #     └─ <model_b>
        #         └─ …
        #
        # Tracked as a list of schemas we've registered, so refresh()
        # can drop schemas whose model has gone away.
        self._registered_schemas: list[str] = []
        # Per-schema fingerprint of the last-applied table DDL. We
        # only re-CREATE the ``model`` table when its column shape
        # actually changed; idempotent refreshes preserve the
        # DuckDB-assigned ``pg_class.oid`` so BI-tool driver caches
        # (DBeaver, pgjdbc, psql ``\d``) keep matching across the
        # rapid-fire catalog probes a schema-tree refresh emits.
        self._schema_ddl_fingerprints: dict[str, str] = {}
        with contextlib.suppress(Exception):
            self._con.execute(f"ATTACH ':memory:' AS \"{CATALOG_SCHEMA}\"")
        # Stub macros and shadow views live in the orionbelt DB so they
        # resolve under ``database=orionbelt`` connections without the
        # per-attach replay the prior layout needed.
        self._con.execute(f'USE "{CATALOG_SCHEMA}"')
        for ddl in _STUB_MACROS:
            with contextlib.suppress(Exception):
                self._con.execute(ddl)
        for view_ddl in _SHADOW_VIEWS:
            with contextlib.suppress(Exception):
                self._con.execute(view_ddl)
        with contextlib.suppress(Exception):
            self._con.execute("USE memory")

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

        with self._lock:
            # Build the set of schemas that should exist now.
            desired_schemas: set[str] = set()
            for store_target, _ in _iter_loaded_models(session_manager):
                desired_schemas.add(_safe_model_table_name(store_target))

            # Drop schemas whose backing model is gone. CASCADE removes
            # the ``model`` table and the six metadata views in one shot.
            for schema in self._registered_schemas:
                if schema in desired_schemas:
                    continue
                with contextlib.suppress(Exception):
                    self._con.execute(
                        f'DROP SCHEMA IF EXISTS "{CATALOG_SCHEMA}"."{schema}" CASCADE'
                    )
                self._schema_ddl_fingerprints.pop(schema, None)
            self._registered_schemas = [s for s in self._registered_schemas if s in desired_schemas]

            for store_target, model in _iter_loaded_models(session_manager):
                schema = _safe_model_table_name(store_target)
                # CREATE SCHEMA IF NOT EXISTS is the cheap path on first
                # refresh; subsequent refreshes recreate the table /
                # views so column-shape changes propagate.
                with contextlib.suppress(Exception):
                    self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{CATALOG_SCHEMA}"."{schema}"')
                if schema not in self._registered_schemas:
                    self._registered_schemas.append(schema)
                ddl = _build_table_ddl(schema, model)
                if ddl is None:
                    continue
                # Idempotent path: skip the DROP + CREATE when the
                # table's column shape is unchanged. Re-running DROP
                # TABLE / CREATE TABLE assigns a fresh
                # ``pg_class.oid`` each time, which breaks DBeaver's
                # column-list panel — DBeaver caches the table oid
                # from one probe and uses it in ``WHERE c.oid = $1``
                # on the next, so a shifted oid silently returns
                # zero columns. The fingerprint covers the metadata
                # views too because their DDLs are deterministic
                # functions of the same model.
                fingerprint = _ddl_fingerprint(ddl, model)
                if self._schema_ddl_fingerprints.get(schema) == fingerprint:
                    continue
                with contextlib.suppress(Exception):
                    self._con.execute(f'DROP TABLE IF EXISTS "{CATALOG_SCHEMA}"."{schema}"."model"')
                # Per-model metadata views (dimensions / measures /
                # metrics + their _<name>_metadata siblings). BI-tool
                # schema browsers show them under the model's schema so
                # users can ``SELECT * FROM dimensions`` to introspect
                # the semantic surface without leaving SQL.
                for meta_ddl in _build_metadata_views(schema, model):
                    with contextlib.suppress(Exception):
                        self._con.execute(meta_ddl)
                try:
                    self._con.execute(ddl)
                except duckdb.Error:  # pragma: no cover — defensive guard
                    logger.exception(
                        "Failed to register catalog table for model '%s'", store_target
                    )
                    continue
                self._schema_ddl_fingerprints[schema] = fingerprint

    # ------------------------------------------------------------------
    # Execute — run a catalog/info-schema query through DuckDB.
    # ------------------------------------------------------------------

    def execute(self, sql: str, database: str = "") -> ExecutionResult:
        """Run ``sql`` against the embedded DuckDB.

        DuckDB's pg_catalog and information_schema are auto-populated
        from the schema we registered in :meth:`refresh`, so the
        caller doesn't need to special-case which table is being
        queried.  Errors bubble as ``duckdb.Error``.

        ``database`` is the Postgres ``database`` parameter the client
        connected with; we ``USE`` the matching ATTACHed DuckDB database
        so ``pg_class`` / ``pg_namespace`` (which only enumerate the
        currently-connected DuckDB DB) scope to the right model.
        """

        t0 = time.monotonic()
        rewritten = _rewrite_for_duckdb(sql)
        del database  # single-DB layout — every catalog probe runs against ``orionbelt``
        with self._lock:
            # Always run probes inside the single ``orionbelt`` ATTACHed
            # DB so ``pg_class`` / ``pg_namespace`` / model-schema
            # references all resolve consistently.
            with contextlib.suppress(Exception):
                self._con.execute(f'USE "{CATALOG_SCHEMA}"')
            cursor = self._con.execute(rewritten)
            rows_raw = cursor.fetchall()
            description = cursor.description or []
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


def _iter_loaded_models(session_manager: SessionManager) -> Iterator[tuple[str, SemanticModel]]:
    """Yield ``(target_name, SemanticModel)`` for every loaded model.

    ``target_name`` is the addressing name BI tools use as the Postgres
    ``database`` parameter:

    * admin-curated (``MODEL_FILES``): each preload sits in its own
      *protected* session whose id IS the OBML name; iterated via
      :meth:`SessionManager.list_protected_session_ids`.
    * MCP stdio: the ``__default__`` session is created on demand and
      exposed under that name.
    * user-created sessions: iterated via
      :meth:`SessionManager.list_sessions` so models loaded over REST
      still light up the catalog.

    ``list_sessions()`` filters out the default + protected sets, so we
    union all three sources manually to cover production layouts.
    """

    candidate_ids: list[str] = []
    candidate_ids.extend(session_manager.list_protected_session_ids())
    # In admin-curated mode the catalog is the set of curated (protected)
    # models only. The legacy ``__default__`` (MCP stdio) session and transient
    # user/scratch sessions (REST clients, the Gradio playground) must not
    # surface as extra schemas in BI tools — skip them so the catalog stays
    # clean. In dynamic mode they ARE the catalog.
    if not session_manager.is_single_model_mode:
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


def _ddl_fingerprint(table_ddl: str, model: SemanticModel) -> str:
    """SHA-1 of the model's catalog-visible surface.

    Covers both the ``CREATE TABLE`` shape and the labels feeding the
    per-model metadata views, so a change in any dimension / measure /
    metric definition (rename, type swap, add, remove) invalidates the
    fingerprint and triggers a re-CREATE. Identical models hash to the
    same value so repeat refresh() calls become no-ops and the DuckDB
    ``pg_class.oid`` stays stable across BI-tool catalog probes.
    """

    parts = [table_ddl]
    parts.extend(f"d:{label}" for label in sorted(model.dimensions))
    parts.extend(f"u:{label}" for label in sorted(model.measures))
    parts.extend(f"m:{label}" for label in sorted(model.metrics))
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _build_table_ddl(schema: str, model: SemanticModel) -> str | None:
    """Build the ``CREATE TABLE`` for a model.

    Path: ``"orionbelt"."<schema>"."model"`` — schema = model name,
    table = literal ``model``. Columns are every dimension, measure,
    and metric the model exposes (names quoted because OBSL labels
    routinely contain spaces / punctuation). Returns ``None`` if no
    columns survive deduplication — DuckDB rejects empty column lists.
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
    qschema = schema.replace('"', '""')
    return f'CREATE TABLE "{CATALOG_SCHEMA}"."{qschema}"."model" ({", ".join(columns)})'


def _model_columns(model: SemanticModel) -> Iterator[tuple[str, str]]:
    for label, dim in model.dimensions.items():
        yield label, _dim_sql_type(dim)
    for label, measure in model.measures.items():
        yield label, _measure_sql_type(measure)
    for label, metric in model.metrics.items():
        yield label, _metric_sql_type(metric)


def _build_metadata_views(schema: str, model: SemanticModel) -> Iterator[str]:
    """Yield CREATE-OR-REPLACE TABLE DDLs for the per-model metadata views.

    Six views per model, under the ``orionbelt`` schema:

    * ``dimensions`` / ``_dimensions_metadata`` — one row per dimension
    * ``measures``   / ``_measures_metadata``   — one row per measure
    * ``metrics``    / ``_metrics_metadata``    — one row per metric

    The bare names are the user-facing surface BI tool browsers show.
    The ``_<name>_metadata`` siblings carry richer information
    (expression / formula / data object refs) for callers that want it
    without making the user-facing tables wider than necessary.

    Rendered as ``CREATE OR REPLACE VIEW`` (not TABLE) so BI tool
    browsers show them under the **Views** node — matching where the
    same per-model metadata appears on the Arrow Flight SQL surface
    and keeping the "Tables" node reserved for the user-facing model
    table itself. Plain (non-temp) views ARE visible via
    ``pg_class.relkind = 'v'`` and ``information_schema.views``; the
    TEMP-view invisibility caveat only applies to TEMP scope.
    """

    qdb = CATALOG_SCHEMA  # single ATTACHed DB carries every model
    qschema = schema.replace('"', '""')
    yield from _build_dimension_metadata_views(qdb, qschema, model)
    yield from _build_measure_metadata_views(qdb, qschema, model)
    yield from _build_metric_metadata_views(qdb, qschema, model)


def _build_dimension_metadata_views(qdb: str, schema: str, model: SemanticModel) -> Iterator[str]:
    """``dimensions`` and ``_dimensions_metadata`` per model."""

    rows_basic: list[str] = []
    rows_full: list[str] = []
    for label, dim in model.dimensions.items():
        result_type = str(dim.result_type) if dim.result_type else "string"
        data_object = (dim.view or "").replace("'", "''")
        column = (dim.column or "").replace("'", "''")
        safe_label = label.replace("'", "''")
        rows_basic.append(f"('{safe_label}', '{result_type}')")
        rows_full.append(f"('{safe_label}', '{result_type}', '{data_object}', '{column}')")
    yield _values_view_ddl(qdb, schema, "dimensions", ("name", "data_type"), rows_basic)
    yield _values_view_ddl(
        qdb,
        schema,
        "_dimensions_metadata",
        ("name", "data_type", "data_object", "column"),
        rows_full,
    )


def _build_measure_metadata_views(qdb: str, schema: str, model: SemanticModel) -> Iterator[str]:
    """``measures`` and ``_measures_metadata`` per model."""

    rows_basic: list[str] = []
    rows_full: list[str] = []
    for label, measure in model.measures.items():
        agg = str(measure.aggregation) if measure.aggregation else ""
        data_type = str(measure.result_type) if measure.result_type else "number"
        expression = (getattr(measure, "expression", "") or "").replace("'", "''")
        safe_label = label.replace("'", "''")
        rows_basic.append(f"('{safe_label}', '{agg}', '{data_type}')")
        rows_full.append(f"('{safe_label}', '{agg}', '{data_type}', '{expression}')")
    yield _values_view_ddl(
        qdb, schema, "measures", ("name", "aggregation", "data_type"), rows_basic
    )
    yield _values_view_ddl(
        qdb,
        schema,
        "_measures_metadata",
        ("name", "aggregation", "data_type", "expression"),
        rows_full,
    )


def _metric_formula(metric: object) -> str:
    """Human-readable definition for a metric, shaped per metric type.

    Derived metrics carry an ``expression``; cumulative and period-over-period
    metrics don't, so we synthesize a readable formula from their config (the
    ``formula`` column was previously empty for every non-derived metric, and
    for derived ones too because it read a non-existent ``formula`` attribute).
    """

    mtype = str(getattr(metric, "type", "") or "derived")
    expression = getattr(metric, "expression", None)

    if mtype == "cumulative":
        agg = str(getattr(metric, "cumulative_type", "") or "sum")
        base = getattr(metric, "measure", "") or ""
        window = getattr(metric, "window", None)
        grain_to_date = getattr(metric, "grain_to_date", None)
        time_dim = getattr(metric, "time_dimension", "") or ""
        if window:
            qualifier = f" rolling {window}"
        elif grain_to_date:
            qualifier = f" {grain_to_date!s}-to-date"
        else:
            qualifier = " running"
        formula = f"{agg}({base}){qualifier}"
        if time_dim:
            formula += f" over {time_dim}"
        return formula

    if mtype == "period_over_period":
        pop = getattr(metric, "period_over_period", None)
        base = expression or (getattr(metric, "measure", "") or "")
        if pop is not None:
            comparison = str(getattr(pop, "comparison", "") or "")
            offset = getattr(pop, "offset", "")
            offset_grain = str(getattr(pop, "offset_grain", "") or "")
            return f"{comparison}({base}, {offset} {offset_grain})"
        return base or ""

    return expression or ""


def _build_metric_metadata_views(qdb: str, schema: str, model: SemanticModel) -> Iterator[str]:
    """``metrics`` and ``_metrics_metadata`` per model."""

    rows_basic: list[str] = []
    rows_full: list[str] = []
    for label, metric in model.metrics.items():
        metric_type = str(getattr(metric, "type", "") or "derived")
        formula = _metric_formula(metric).replace("'", "''")
        safe_label = label.replace("'", "''")
        rows_basic.append(f"('{safe_label}', '{metric_type}')")
        rows_full.append(f"('{safe_label}', '{metric_type}', '{formula}')")
    yield _values_view_ddl(qdb, schema, "metrics", ("name", "metric_type"), rows_basic)
    yield _values_view_ddl(
        qdb,
        schema,
        "_metrics_metadata",
        ("name", "metric_type", "formula"),
        rows_full,
    )


def _values_view_ddl(
    qdb: str,
    schema: str,
    view: str,
    columns: tuple[str, ...],
    rows: list[str],
) -> str:
    """Render ``CREATE OR REPLACE VIEW qdb.schema.view AS SELECT … FROM VALUES …``.

    Empty ``rows`` produces an empty view by way of a SELECT-WHERE-false
    on a single dummy row, so the columns still appear in
    ``information_schema.columns``.
    """

    col_list = ", ".join(f'"{c}"' for c in columns)
    if rows:
        values_sql = ", ".join(rows)
        select_clause = f"SELECT * FROM (VALUES {values_sql}) AS v({col_list})"
    else:
        # One-row stub filtered out so the view has the right column
        # shape but zero rows. Each placeholder is an empty string so
        # the inferred types are VARCHAR.
        placeholders = ", ".join("''" for _ in columns)
        select_clause = f"SELECT * FROM (VALUES ({placeholders})) AS v({col_list}) WHERE false"
    return f'CREATE OR REPLACE VIEW "{qdb}"."{schema}"."{view}" AS {select_clause}'


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
