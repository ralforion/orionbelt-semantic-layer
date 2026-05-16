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

        try:
            target = self._resolve_target(database)
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

        Resolution order, matching the Flight surface's behaviour:

        1. ``database`` matches a SessionManager session id directly
           (multi-model preload uses the model name as the session id).
        2. ``__default__`` session (single-model preload, MCP stdio,
           tests).
        3. Any other session with at least one loaded model — only
           when exactly one match exists, so we never silently pick.

        Raises ``_ModelNotFoundError`` with a message that surfaces in the
        Postgres ErrorResponse on miss.
        """

        candidate_ids: list[str] = []
        if database:
            candidate_ids.append(database)
        if "__default__" not in candidate_ids:
            candidate_ids.append("__default__")

        for session_id in candidate_ids:
            target = self._first_model_in(session_id)
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


_CATALOG_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema"})


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
        if (table.name or "").lower().startswith("pg_"):
            return True
    for column in parsed.find_all(exp.Column):
        if (column.text("table") or "").lower() in _CATALOG_SCHEMAS:
            return True
        if (column.text("db") or "").lower() in _CATALOG_SCHEMAS:
            return True
    for func in parsed.find_all(exp.Anonymous):
        if (func.text("this") or "").lower() in _CATALOG_SCHEMAS:
            return True
    # Some catalog functions parse as Dot expressions (schema.func()).
    for dot in parsed.find_all(exp.Dot):
        left = dot.args.get("this")
        if isinstance(left, exp.Identifier) and (left.name or "").lower() in _CATALOG_SCHEMAS:
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
