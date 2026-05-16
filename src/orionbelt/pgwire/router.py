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
from dataclasses import dataclass

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
from orionbelt.pgwire.types import encode_text_value, oid_for_type_hint
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

    async def handle(self, sql: str, database: str) -> bytes:
        """Top-level entry point used by the pgwire server loop.

        Returns the raw bytes that go on the wire **before** the trailing
        ``ReadyForQuery`` — i.e., a sequence of RowDescription + DataRow
        + CommandComplete frames, or a single ErrorResponse.
        """

        # 1. Canned protocol probes (SELECT 1, SHOW, SET, BEGIN, …).
        canned = match_canned(sql, database=database)
        if canned is not None:
            return canned

        # 2. Catalog probes (pg_catalog.*, information_schema.*).
        if references_catalog(sql):
            try:
                self._catalog.refresh(self._sessions)
                result = self._catalog.execute(sql)
            except Exception as exc:  # noqa: BLE001 — protocol boundary
                logger.info("pgwire catalog probe failed: %s", exc)
                return protocol.build_error_response(
                    severity="ERROR",
                    code=SQLSTATE_SYNTAX_ERROR,
                    message=f"catalog query failed: {exc}",
                )
            return _encode_result(result)

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
            query = translate_sql_to_query(sql, target.model)
        except SQLTranslationError as exc:
            return _encode_translation_error(exc)
        except Exception as exc:  # noqa: BLE001 — broad guard at protocol boundary
            logger.exception("pgwire translator failed")
            return protocol.build_error_response(
                severity="ERROR",
                code=SQLSTATE_SYSTEM_ERROR,
                message=f"semantic translator error: {exc}",
            )

        try:
            compile_result = target.store.compile_query(
                target.model_id, query, self._default_dialect
            )
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

        logger.info(
            "pgwire compiled SQL (dialect=%s):\n%s", self._default_dialect, compile_result.sql
        )

        try:
            result = execute_sql(compile_result.sql, dialect=self._default_dialect)
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

        return _encode_result(result)

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


def _encode_result(result: ExecutionResult) -> bytes:
    """Encode an ``ExecutionResult`` as RowDescription + DataRow* + CommandComplete."""

    columns_for_desc = [(col.name, oid_for_type_hint(col.type_hint)) for col in result.columns]
    out = protocol.build_row_description(columns_for_desc)
    for row in result.rows:
        encoded: list[str | None] = []
        for value, col in zip(row, result.columns, strict=True):
            encoded.append(encode_text_value(value, col.type_hint))
        out += protocol.build_data_row(encoded)
    out += protocol.build_command_complete(f"SELECT {len(result.rows)}")
    return out
