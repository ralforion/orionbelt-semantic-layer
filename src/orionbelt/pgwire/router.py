"""Semantic-SQL dispatcher for the Postgres wire surface.

The router takes a raw SQL string + the ``database`` value from the
Postgres StartupMessage and dispatches:

1. Canned protocol probes (``SELECT 1``, ``SHOW``, ``SET``,
   transaction wrappers, ``SELECT version()`` …) — handled in
   :mod:`pgwire.canned`.
2. Catalog probes (anything referencing ``pg_catalog.*`` or
   ``information_schema.*``) — routed to the embedded DuckDB
   in :mod:`pgwire.catalog`.
3. Semantic SQL — the same translate / compile / execute pipeline as
   the REST ``/v1/query/semantic-ql`` endpoint, then encoded as
   Postgres wire frames.

Step 4 adds the extended query protocol on top.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.sql_translator import SQLTranslationError, translate_sql_to_query
from orionbelt.dialect.base import UnsupportedAggregationError, UnsupportedGroupingError
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.models.semantic import SemanticModel
from orionbelt.pgwire import protocol
from orionbelt.pgwire.canned import match_canned
from orionbelt.pgwire.catalog import CatalogEmulator
from orionbelt.pgwire.types import (
    encode_value,
    oid_for_type_hint,
)
from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionResult,
    ExecutionUnavailableError,
    execute_sql,
)
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import SessionManager

logger = logging.getLogger(__name__)


# Postgres SQLSTATE codes used by the router. Keep in one place so the
# mapping is auditable; codes are documented in PostgreSQL Appendix A.
SQLSTATE_FEATURE_NOT_SUPPORTED = "0A000"
SQLSTATE_SYNTAX_ERROR = "42601"
SQLSTATE_UNDEFINED_COLUMN = "42703"
SQLSTATE_DATA_EXCEPTION = "22000"
SQLSTATE_INVALID_AUTHORIZATION = "28000"
SQLSTATE_UNDEFINED_DATABASE = "3D000"
SQLSTATE_INVALID_CATALOG_NAME = "3D000"
SQLSTATE_SYSTEM_ERROR = "58000"
SQLSTATE_CANNOT_CONNECT_NOW = "57P03"


class _ModelNotFoundError(Exception):
    """Raised when the Postgres ``database`` parameter resolves to nothing."""


@dataclass(frozen=True)
class _ResolvedTarget:
    store: ModelStore
    model_id: str
    model: SemanticModel


class SemanticRouter:
    """Per-process semantic-SQL router bound to a SessionManager.

    Instances are reusable across many client connections; all state is
    derived from the SessionManager at request time so model reloads are
    picked up immediately.
    """

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        default_dialect: str,
        catalog: CatalogEmulator | None = None,
    ) -> None:
        self._sessions = session_manager
        self._default_dialect = default_dialect
        self._catalog = catalog if catalog is not None else CatalogEmulator()

    async def handle(
        self,
        sql: str,
        database: str,
        *,
        result_formats: tuple[int, ...] = (),
    ) -> bytes:
        """Top-level entry point used by the pgwire server loop.

        Returns the raw bytes that go on the wire **before** the trailing
        ``ReadyForQuery`` — i.e., a sequence of RowDescription + DataRow
        + CommandComplete frames, or a single ErrorResponse.

        ``result_formats`` mirrors the same field in ``Bind`` — an empty
        tuple means "all text", one entry means "apply to every column",
        N entries means "one per column". When the client asks for
        binary on a numeric column (typical for pgjdbc with FLOAT8 in
        its binaryTransferEnable set) we honour it, otherwise we send
        text. Mismatched formats — server sends binary when the driver
        expected text — make pgjdbc throw ``ArrayIndexOutOfBoundsException``
        (`Index 7 out of bounds for length 7`) trying to read 8-byte
        FLOAT8 from a 7-char text literal.
        """

        # 1. Canned protocol probes (SELECT 1, SHOW, SET, BEGIN, …).
        canned = match_canned(sql, database=database)
        if canned is not None:
            return canned

        # 2. Catalog probes (pg_catalog.*, information_schema.*) AND
        #    BI-tool connect-check temp-table operations AND zero-row
        #    column-discovery probes. All run against the embedded
        #    DuckDB; the latter gives Tableau a real CREATE / INSERT /
        #    SELECT / DROP cycle so its round-trip check doesn't see
        #    zero rows, and ``SELECT * FROM t WHERE 1=0`` answers from
        #    the catalog's column shape rather than bouncing off the
        #    semantic translator.
        if references_catalog(sql) or references_temp_table(sql) or is_metadata_probe(sql):
            try:
                self._catalog.refresh(self._sessions)
                result = self._catalog.execute(sql, database)
            except Exception as exc:  # noqa: BLE001 — protocol boundary
                logger.info("pgwire catalog probe failed: %s", exc)
                return protocol.build_error_response(
                    severity="ERROR",
                    code=SQLSTATE_SYNTAX_ERROR,
                    message=f"catalog query failed: {exc}",
                )
            return _encode_result(result, result_formats)

        # ``SELECT FROM "<model>"."model"`` — DBeaver and similar GUIs
        # emit fully-qualified references against our per-model schema
        # layout. Rewrite to bare ``FROM "<model>"`` so the OBSQL
        # translator (which keys off the table name) recognises the
        # virtual semantic table. The schema becomes the resolution
        # hint for ``_resolve_target`` too.
        sql, qualified_target_schema = _strip_model_schema_qualifier(sql)
        effective_database = qualified_target_schema or database

        try:
            target = self._resolve_target(effective_database)
        except _ModelNotFoundError as exc:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_UNDEFINED_DATABASE,
                message=str(exc),
            )

        try:
            query = translate_sql_to_query(_normalize_for_obsql(sql), target.model)
        except SQLTranslationError as exc:
            return _encode_translation_error(exc)
        except Exception as exc:  # noqa: BLE001 — broad guard at protocol boundary
            logger.exception("pgwire translator failed")
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_SYSTEM_ERROR,
                message=f"semantic translator error: {exc}",
            )

        # Per-model dialect: when the OBML declares ``settings.defaultDialect``
        # it wins over the global ``DB_VENDOR``. That lets a single OBSL
        # serve, e.g., a DuckDB-backed model and a Dremio-backed model
        # from the same pgwire surface — each query is compiled AND
        # executed against the right vendor.
        model_settings = target.model.settings
        dialect = (
            model_settings.default_dialect
            if model_settings is not None and model_settings.default_dialect
            else self._default_dialect
        )

        try:
            compile_result = target.store.compile_query(target.model_id, query, dialect)
        except UnsupportedDialectError as exc:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_FEATURE_NOT_SUPPORTED,
                message=f"unsupported dialect: {exc}",
            )
        except ResolutionError as exc:
            message = "; ".join(e.message for e in exc.errors) or "query resolution failed"
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_UNDEFINED_COLUMN,
                message=message,
            )
        except FanoutError as exc:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_FEATURE_NOT_SUPPORTED,
                message=f"fanout detected: {exc.message}",
            )
        except (UnsupportedAggregationError, UnsupportedGroupingError) as exc:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_FEATURE_NOT_SUPPORTED,
                message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("pgwire compiler failed")
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_SYSTEM_ERROR,
                message=f"compiler error: {exc}",
            )

        logger.info("pgwire compiled SQL (dialect=%s):\n%s", dialect, compile_result.sql)

        try:
            result = execute_sql(compile_result.sql, dialect=dialect)
        except ExecutionUnavailableError as exc:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_CANNOT_CONNECT_NOW,
                message=f"execution unavailable: {exc}",
            )
        except ExecutionError as exc:
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_DATA_EXCEPTION,
                message=f"execution failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("pgwire executor failed")
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_SYSTEM_ERROR,
                message=f"executor error: {exc}",
            )

        # Tableau (and other BI tools) wrap measures in expressions like
        # ``SUM("Total Sales") AS "sum:Total Sales:ok"``. The translator
        # collapses the SUM onto the measure but loses the user alias —
        # the compiled SQL emits the measure label as the column name
        # ("Total Sales"), not "sum:Total Sales:ok". Tableau then looks
        # up its alias in the ResultSet, doesn't find it, and renders
        # the cell as NULL. Recover the aliases by re-parsing the
        # original SQL and rewriting ``result.columns[i].name``.
        result = _apply_user_aliases(sql, result)

        return _encode_result(result, result_formats)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve_target(self, database: str) -> _ResolvedTarget:
        """Pick a (store, model_id, model) tuple for the requested DB.

        Resolution order:

        1. ``database`` matches a SessionManager session id directly
           (multi-model preload uses the model name as the session id).
        2. ``database`` is the OBSL brand name ("orionbelt") — fall
           through to the first loaded model. This is the path DBeaver
           takes when ``pg_database`` advertises ``orionbelt`` as the
           single database and the client uses that name to reconnect.
        3. ``__default__`` session (single-model preload, MCP stdio,
           tests).
        4. Any other session with at least one loaded model — only
           when exactly one match exists, so we never silently pick.

        Raises ``_ModelNotFoundError`` with a message that surfaces in
        the Postgres ErrorResponse on miss.
        """

        from orionbelt.pgwire.catalog import OBSL_DATABASE_NAME

        candidate_ids: list[str] = []
        if database:
            candidate_ids.append(database)
        if "__default__" not in candidate_ids:
            candidate_ids.append("__default__")

        for session_id in candidate_ids:
            target = self._first_model_in(session_id)
            if target is not None:
                return target

        # When the client passes the brand name ("orionbelt") and the
        # SessionManager doesn't host a session by that name, fall
        # through to the first loaded model. pg_database advertises
        # only one row ("orionbelt"), so this is the canonical path
        # for any BI tool that re-reads the catalog after connecting.
        if database == OBSL_DATABASE_NAME:
            for session_id in self._sessions.list_protected_session_ids():
                target = self._first_model_in(session_id)
                if target is not None:
                    return target
            for session_summary in self._sessions.list_sessions():
                target = self._first_model_in(session_summary.session_id)
                if target is not None:
                    return target

        # Last-resort scan across all sessions for the case where the
        # client passed a database name that doesn't match a session id
        # but does identify a model in some other session.
        matches: list[_ResolvedTarget] = []
        for session_summary in self._sessions.list_sessions():
            target = self._first_model_in(session_summary.session_id, model_id=database or None)
            if target is not None:
                matches.append(target)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise _ModelNotFoundError(
                f"database='{database}' is ambiguous — multiple sessions hold a model "
                "with that name; supply a unique database parameter."
            )

        raise _ModelNotFoundError(
            f"no model is available for database='{database}'. "
            "Load a model via REST /v1/sessions/{id}/models or start the server "
            "with MODEL_FILES=<path,...>."
        )

    def _first_model_in(
        self, session_id: str, *, model_id: str | None = None
    ) -> _ResolvedTarget | None:
        try:
            store = self._sessions.get_store(session_id)
        except Exception:
            return None
        summaries = store.list_models()
        if not summaries:
            return None
        chosen_id = model_id or summaries[0].model_id
        try:
            model = store.get_model(chosen_id)
        except KeyError:
            return None
        return _ResolvedTarget(store=store, model_id=chosen_id, model=model)


def _strip_model_schema_qualifier(sql: str) -> tuple[str, str | None]:
    """Rewrite ``FROM "<schema>"."model"`` → ``FROM "<schema>"``.

    Returns ``(rewritten_sql, schema_name | None)``. The schema name is
    bubbled back to the caller so semantic resolution can pick the
    matching model when the connection's ``database`` parameter is
    ambiguous (multi-model deployments where the user connected to
    ``orionbelt`` rather than a specific model).

    Best-effort: failures to parse return ``(sql, None)`` and let the
    OBSQL translator surface a clean diagnostic if anything is wrong.
    """

    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return sql, None

    found_schema: str | None = None
    for table in parsed.find_all(exp.Table):
        if (table.name or "").lower() != _MODEL_DATA_TABLE:
            continue
        schema = table.db or table.text("db")
        if not schema:
            continue
        # Promote the schema name to the table position so OBSQL sees
        # ``FROM "<schema>"`` and treats it as the virtual model.
        # Preserve the existing alias if any.
        alias = table.alias or ""
        table.set("this", exp.to_identifier(schema, quoted=True))
        table.set("db", None)
        if alias:
            table.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        found_schema = schema
    if found_schema is None:
        return sql, None
    return parsed.sql(dialect="postgres"), found_schema


_CATALOG_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema"})

#: Exact names of the metadata views we create inside each per-model
#: schema (see ``pgwire/catalog.py::_build_metadata_views``).  Queries
#: against ``<model>.<one of these>`` route to the catalog branch.
_METADATA_VIEW_NAMES: frozenset[str] = frozenset(
    {
        "dimensions",
        "measures",
        "metrics",
        # Underscore-prefixed metadata views — match the Arrow Flight
        # catalog convention so the same name works on both surfaces.
        "_dimensions_metadata",
        "_measures_metadata",
        "_metrics_metadata",
    }
)

#: Name of the single data table inside each per-model schema.
_MODEL_DATA_TABLE = "model"

#: Bare identifiers that Postgres treats as system info, not column
#: references. DBeaver and other JDBC clients probe these in
#: ``refreshDefaults`` (``SELECT current_schema(), session_user``);
#: when they appear in a no-FROM SELECT the query belongs to the
#: catalog branch, not OBSQL.
_SYSTEM_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "current_user",
        "current_role",
        "current_database",
        "current_schema",
        "current_catalog",
        "session_user",
        "user",
    }
)


# Tableau and other BI tools run a connect-check that creates a temp
# table, inserts a row, reads it back, and drops it. Detection here is
# a cheap textual match — every operation against a ``"#..."``-quoted
# table or any CREATE [LOCAL|GLOBAL] TEMP[ORARY] TABLE gets routed to
# the catalog DuckDB so the full cycle actually executes.
_RE_TEMP_TABLE_DDL = re.compile(
    r"^\s*create\s+(?:local\s+|global\s+)?temp(?:orary)?\s+table\b",
    re.IGNORECASE,
)


def references_temp_table(sql: str) -> bool:
    """Return ``True`` when the query touches a BI-tool temp table.

    Recognises:

    * ``CREATE [LOCAL|GLOBAL] TEMP[ORARY] TABLE …`` — Postgres syntax;
    * any reference to a ``"#…"``-quoted table — the Tableau convention.

    Both shapes are routed to the catalog DuckDB so the temp-table
    round-trip actually executes; stubbing it with zero rows breaks
    Tableau's connect check.
    """

    if _RE_TEMP_TABLE_DDL.match(sql) is not None:
        return True
    return '"#' in sql


# Tableau (and other Postgres clients) probe column metadata with
# ``SELECT * FROM "schema"."table" WHERE 1=0``. The catalog DuckDB
# already knows every model's column shape, so it answers correctly
# with zero rows; routing this to the semantic translator would
# (incorrectly) reject ``SELECT *`` and the ``1=0`` predicate.
_RE_ZERO_ROW_METADATA_PROBE = re.compile(
    r"\bwhere\s+(?:1\s*=\s*0|0\s*=\s*1|false)\b",
    re.IGNORECASE,
)
_RE_LIMIT_ZERO_PROBE = re.compile(r"\blimit\s+0\b", re.IGNORECASE)

# Dremio's Postgres JDBC connector annotates every text column in
# pushdown SQL with ``COLLATE "C"`` to force byte-order comparison
# semantics across federated sources. The annotation is meaningless to
# a semantic layer (we don't do raw lexical comparison on column refs)
# and the OBSQL translator rejects anything but bare identifiers. Strip
# any ``COLLATE <quoted-or-bare-identifier>`` (optionally
# schema-qualified) before the translator sees the SQL.
_RE_COLLATE_ANNOTATION = re.compile(
    r'\s+COLLATE\s+(?:"[^"]+"|[\w.]+)',
    re.IGNORECASE,
)

# Same BI-tool pushdown story for the SQL-standard pagination form:
# Dremio emits ``OFFSET m ROWS FETCH NEXT n ROWS ONLY``. The OBSQL
# translator only accepts the Postgres ``LIMIT n [OFFSET m]`` form, so
# we rewrite. Order matters — the combined ``OFFSET … FETCH …`` shape
# has to be tried before the standalone clauses, otherwise the OFFSET
# part is consumed first and the FETCH half ends up orphaned.
_RE_OFFSET_FETCH = re.compile(
    r"\bOFFSET\s+(\d+)\s+ROWS\s+FETCH\s+(?:FIRST|NEXT)\s+(\d+)\s+ROWS\s+ONLY\b",
    re.IGNORECASE,
)
_RE_FETCH_FIRST = re.compile(
    r"\bFETCH\s+(?:FIRST|NEXT)\s+(\d+)\s+ROWS\s+ONLY\b",
    re.IGNORECASE,
)
_RE_OFFSET_ROWS = re.compile(r"\bOFFSET\s+(\d+)\s+ROWS\b", re.IGNORECASE)


def _strip_collate_annotations(sql: str) -> str:
    """Drop ``COLLATE "<x>"`` / ``COLLATE foo.bar`` annotations from SQL."""

    return _RE_COLLATE_ANNOTATION.sub("", sql)


def _rewrite_fetch_to_limit(sql: str) -> str:
    """Convert SQL-standard FETCH/OFFSET pagination to Postgres LIMIT/OFFSET."""

    sql = _RE_OFFSET_FETCH.sub(r"LIMIT \2 OFFSET \1", sql)
    sql = _RE_FETCH_FIRST.sub(r"LIMIT \1", sql)
    sql = _RE_OFFSET_ROWS.sub(r"OFFSET \1", sql)
    return sql


def _normalize_for_obsql(sql: str) -> str:
    """Apply pgjdbc-pushdown normalizations before OBSQL translation."""

    return _rewrite_fetch_to_limit(_strip_collate_annotations(sql))


def is_metadata_probe(sql: str) -> bool:
    """Return ``True`` for SELECT shapes that mean "describe, don't run".

    Used to route ``SELECT * FROM x WHERE 1=0`` (and ``LIMIT 0``)
    column-discovery probes to the catalog DuckDB instead of the
    semantic translator, which doesn't accept star projections or the
    ``1=0`` literal predicate.
    """

    lowered = sql.lstrip().lower()
    if not lowered.startswith("select"):
        return False
    return (
        _RE_ZERO_ROW_METADATA_PROBE.search(sql) is not None
        or _RE_LIMIT_ZERO_PROBE.search(sql) is not None
    )


def references_catalog(sql: str) -> bool:
    """Return ``True`` when the query needs the catalog emulator.

    Detects three shapes:

    * a ``FROM`` / ``JOIN`` target whose schema or database is
      ``pg_catalog`` or ``information_schema``;
    * a function reference like ``pg_catalog.set_config(...)``;
    * a bare table reference to a well-known Postgres system table
      (``pg_class``, ``pg_namespace``, ``pg_description``, …). DBeaver
      and pgAdmin frequently omit the ``pg_catalog.`` qualifier; the
      ``pg_`` prefix is reserved for Postgres system catalogs so OBSL
      models never collide.

    Falls back to a cheap substring test if sqlglot can't parse the
    query — Postgres clients sometimes emit dialect-specific snippets
    (e.g. operator classes) sqlglot doesn't fully understand.
    """

    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        lowered = sql.lower()
        return "pg_catalog" in lowered or "information_schema" in lowered

    for table in parsed.find_all(exp.Table):
        if (table.db or "").lower() in _CATALOG_SCHEMAS:
            return True
        if (table.text("db") or "").lower() in _CATALOG_SCHEMAS:
            return True
        if (table.text("catalog") or "").lower() in _CATALOG_SCHEMAS:
            return True
        # Bare ``pg_*`` table reference (e.g. ``FROM pg_description d``).
        # Postgres reserves the ``pg_`` prefix for system catalogs, so
        # this is unambiguously a catalog probe.
        name_lower = (table.name or "").lower()
        if name_lower.startswith("pg_"):
            return True
        # Per-model metadata views (``<model>.dimensions`` etc.).
        # These live in the catalog DuckDB, not the warehouse, so any
        # query against one of our six known view names — qualified
        # OR bare — routes to ``CatalogEmulator.execute``. The data
        # table (``<model>.model``) is excluded so semantic queries
        # still flow through the warehouse path.
        if name_lower in _METADATA_VIEW_NAMES and name_lower != _MODEL_DATA_TABLE:
            return True
    for column in parsed.find_all(exp.Column):
        if (column.text("table") or "").lower() in _CATALOG_SCHEMAS:
            return True
        if (column.text("db") or "").lower() in _CATALOG_SCHEMAS:
            return True
    for func in parsed.find_all(exp.Anonymous):
        func_name = (func.text("this") or "").lower()
        if func_name in _CATALOG_SCHEMAS:
            return True
        # Bare ``pg_*()`` function call — e.g. ``pg_get_keywords()``
        # used as a table function by DBeaver's SQL-editor highlighting.
        if func_name.startswith("pg_"):
            return True
    # Some catalog functions parse as Dot expressions (schema.func()).
    for dot in parsed.find_all(exp.Dot):
        left = dot.args.get("this")
        if isinstance(left, exp.Identifier) and (left.name or "").lower() in _CATALOG_SCHEMAS:
            return True
    # No-FROM SELECT — could be either a system probe (DBeaver's
    # ``SELECT current_schema(), session_user``) or a bare-dimension
    # query OBSQL handles natively (``SELECT "Customer Country"``
    # against the connection's model). Distinguish by content:
    #
    # * Any function call (``current_schema()``, ``version()``, …)
    #   → catalog (DuckDB evaluates the function).
    # * A bare identifier matching a known Postgres system name
    #   (``session_user``, ``current_user``, …) → catalog.
    # * Otherwise (numeric literals, model column refs only) →
    #   fall through to the semantic path so OBSQL can handle
    #   dimension/measure references.
    if isinstance(parsed, exp.Select) and not list(parsed.find_all(exp.Table)):
        if list(parsed.find_all(exp.Func)):
            return True
        for col in parsed.find_all(exp.Column):
            if (col.name or "").lower() in _SYSTEM_IDENTIFIERS:
                return True
    return False


def _encode_translation_error(exc: SQLTranslationError) -> bytes:
    """Map a SQLTranslationError to a Postgres ErrorResponse frame."""

    if not exc.errors:
        return protocol.build_error_response(
            severity="ERROR",
            code=SQLSTATE_SYNTAX_ERROR,
            message="OBSQL translation failed",
        )
    # Use the first error's code as the SQLSTATE driver; the message
    # carries every diagnostic so users can fix multi-clause mistakes
    # in one pass.
    first = exc.errors[0]
    sqlstate = _translation_code_to_sqlstate(first.code)
    message = "; ".join(f"[{e.code}] {e.message}" for e in exc.errors)
    return protocol.build_error_response(
        severity="ERROR",
        code=sqlstate,
        message=message,
    )


def _translation_code_to_sqlstate(code: str) -> str:
    if code in {"UNKNOWN_SELECT_ITEM", "UNKNOWN_COLUMN", "UNKNOWN_ORDER_BY_FIELD"}:
        return SQLSTATE_UNDEFINED_COLUMN
    if code == "UNSUPPORTED_SQL_FEATURE":
        return SQLSTATE_FEATURE_NOT_SUPPORTED
    return SQLSTATE_SYNTAX_ERROR


def _apply_user_aliases(original_sql: str, result: ExecutionResult) -> ExecutionResult:
    """Rewrite ``result.columns[i].name`` to the alias from ``original_sql``.

    Tableau emits queries like::

        SELECT CAST("dim" AS TEXT) AS "Dim",
               SUM("Total Sales") AS "sum:Total Sales:ok",
               SUM("Total Returns") AS "sum:Total Returns:ok"
        FROM ... GROUP BY 1

    The translator collapses the aggregate wrapping onto the measure
    but loses the user alias. The compiled SQL then emits the *measure
    label* (``Total Sales``) as the column name. Tableau, which looks
    up columns by the alias it requested, sees no matching column and
    renders the cell as NULL.

    Renaming by **position** is wrong: the CFL planner groups measures
    by their source fact object so the compiler's column order can
    differ from the user's SELECT order — by-position renaming then
    puts Tableau's alias for ``Total Sales`` onto the Total Returns
    column. Match by the **original measure name** instead: extract
    each Alias's inner column reference from the user SQL, build a
    ``{inner_name: user_alias}`` map, and apply it by name lookup on
    the executor's columns. Order-independent.

    Falls back to a no-op on parse errors so the surrounding pipeline
    keeps working.
    """

    try:
        ast = sqlglot.parse_one(original_sql, read="postgres", error_level=None)
    except Exception:  # noqa: BLE001 — we silently fall back on any parse issue
        return result
    if not isinstance(ast, exp.Select):
        return result
    alias_for_name: dict[str, str] = {}
    for item in ast.expressions:
        if not isinstance(item, exp.Alias):
            continue
        inner_name = _alias_inner_column_name(item.this)
        user_alias = item.alias_or_name
        if inner_name and inner_name != user_alias:
            alias_for_name[inner_name] = user_alias
    if not alias_for_name:
        return result
    for col in result.columns:
        new_name = alias_for_name.get(col.name)
        if new_name is not None:
            col.name = new_name
    return result


def _alias_inner_column_name(node: exp.Expression) -> str | None:
    """Find the column name an Alias's inner expression resolves to.

    Tableau wraps measures in aggregates and dimensions in CAST. We
    unwrap both so the user alias maps back to the underlying
    dim/measure/metric label:

    * ``SUM("Total Sales")`` → ``"Total Sales"``
    * ``CAST("Client Name" AS TEXT)`` → ``"Client Name"``
    * bare ``Column("X")`` → ``"X"``

    Returns ``None`` when the expression doesn't unwrap to a single
    column reference (literals, arithmetic, multi-arg functions).
    """

    if isinstance(node, exp.Column):
        return node.name
    if isinstance(node, exp.Cast | exp.TryCast):
        return _alias_inner_column_name(node.this)
    if isinstance(node, exp.AggFunc):
        return _alias_inner_column_name(node.this)
    return None


def _encode_result(
    result: ExecutionResult,
    result_formats: tuple[int, ...] = (),
) -> bytes:
    """Encode an ``ExecutionResult`` as RowDescription + DataRow* + CommandComplete.

    Per-column ``format_code`` is taken from ``Bind.result_formats``
    (empty → all text; single-entry → broadcast; N-entry → per column).
    pgjdbc uses the values it requested in Bind to decode each column,
    NOT the format_code in RowDescription — sending text bytes when
    Bind asked for binary FLOAT8 makes pgjdbc throw
    ``ArrayIndexOutOfBoundsException`` indexing byte 7 of a 7-char
    ``"1329.87"`` text payload as the 8th byte of an IEEE 754 double.
    """

    n_cols = len(result.columns)
    per_col_formats = _expand_result_formats(result_formats, n_cols)
    columns_for_desc = [
        (col.name, oid_for_type_hint(col.type_hint), per_col_formats[i])
        for i, col in enumerate(result.columns)
    ]
    out = protocol.build_row_description(columns_for_desc)
    debug_enabled = logger.isEnabledFor(logging.DEBUG)
    if debug_enabled:
        logger.debug(
            "pgwire RowDescription cols=%s",
            [
                {
                    "name": c.name,
                    "type_hint": c.type_hint,
                    "oid": oid_for_type_hint(c.type_hint),
                    "fmt": per_col_formats[i],
                }
                for i, c in enumerate(result.columns)
            ],
        )
    for row_idx, row in enumerate(result.rows):
        encoded: list[str | bytes | None] = []
        for i, (value, col) in enumerate(zip(row, result.columns, strict=True)):
            encoded.append(encode_value(value, col.type_hint, per_col_formats[i]))
        if debug_enabled:
            _log_data_row(row_idx, row, encoded, result.columns)
        out += protocol.build_data_row(encoded)
    out += protocol.build_command_complete(f"SELECT {len(result.rows)}")
    return out


def _expand_result_formats(result_formats: tuple[int, ...], n_cols: int) -> list[int]:
    """Expand ``Bind.result_formats`` to one entry per column.

    Per the Postgres wire protocol: empty → all text (0), single entry
    → apply to every column, N entries → one per column. Anything else
    is a protocol violation we surface as "all text" to stay safe.
    """

    if not result_formats:
        return [0] * n_cols
    if len(result_formats) == 1:
        return [result_formats[0]] * n_cols
    if len(result_formats) == n_cols:
        return list(result_formats)
    return [0] * n_cols


def _log_data_row(
    row_idx: int,
    raw_row: Any,
    encoded_row: list[str | bytes | None],
    columns: Any,
) -> None:
    """Dump per-column raw value + wire bytes + pgjdbc-style decode.

    Logged at DEBUG. For binary FLOAT8 columns we also unpack the bytes
    with ``struct.unpack("!d", ...)`` so the log shows the round-trip
    value pgjdbc should see — if that round-trip is non-zero but
    Tableau still renders 0, the bug is downstream of our encoder.
    """

    import struct as _struct

    parts: list[str] = []
    for raw_value, enc, col in zip(raw_row, encoded_row, columns, strict=True):
        if enc is None:
            parts.append(f"{col.name}=NULL(hint={col.type_hint})")
            continue
        if isinstance(enc, bytes):
            hex_repr = enc.hex()
            decoded: object
            if col.type_hint == "number" and len(enc) == 8:
                try:
                    decoded = _struct.unpack("!d", enc)[0]
                except _struct.error as exc:
                    decoded = f"<unpack error: {exc}>"
            else:
                decoded = f"<{len(enc)} bytes>"
            parts.append(
                f"{col.name}={raw_value!r}(hint={col.type_hint}) "
                f"-> bytes(len={len(enc)}, hex={hex_repr}, decoded={decoded!r})"
            )
        else:
            parts.append(f"{col.name}={raw_value!r}(hint={col.type_hint}) -> text({enc!r})")
    logger.debug("pgwire DataRow[%d]: %s", row_idx, " | ".join(parts))
