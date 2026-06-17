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

import contextlib
import logging
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from orionbelt.api.query_cache import (
    execute_query_with_cache,
    execution_result_from_envelope,
)
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.sql_translator import SQLTranslationError, translate_sql_to_query
from orionbelt.dialect.base import UnsupportedAggregationError, UnsupportedGroupingError
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.models.semantic import SemanticModel
from orionbelt.pgwire import protocol
from orionbelt.pgwire.canned import match_canned
from orionbelt.pgwire.catalog import CATALOG_SCHEMA, CatalogEmulator
from orionbelt.pgwire.types import (
    can_encode_binary,
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


# Per-model metadata views the catalog emulator creates inside every
# model's schema (``orionbelt.<model>.<view>``). The Arrow Flight
# surface exposes them under the alias ``model.<view>``; pgwire
# accepts the same alias so users can write
# ``SELECT * FROM model.dimensions`` from any client without
# retyping the schema name.
_METADATA_VIEW_NAMES: frozenset[str] = frozenset(
    {
        "dimensions",
        "measures",
        "metrics",
        "_dimensions_metadata",
        "_measures_metadata",
        "_metrics_metadata",
        "model",
    }
)


_RE_MODEL_ALIAS_QUALIFIER = re.compile(
    # ``"model"."<view>"`` or ``model.<view>`` (case-insensitive).
    # Matches the metadata-view names exactly; the FROM / JOIN
    # surrounding context isn't required so an aliased reference
    # inside a JOIN clause also gets rewritten.
    r'"?model"?\s*\.\s*"?(?P<view>'
    r"dimensions|measures|metrics"
    r"|_dimensions_metadata|_measures_metadata|_metrics_metadata"
    r')"?',
    re.IGNORECASE,
)


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
        cache: Any = None,
        cache_config: Any = None,
    ) -> None:
        self._sessions = session_manager
        self._default_dialect = default_dialect
        self._catalog = catalog if catalog is not None else CatalogEmulator()
        # When a result cache is supplied, semantic queries route through the
        # shared cached-execution service (the same one REST uses). Catalog /
        # metadata probes never reach that path, so they are never cached.
        self._cache = cache
        self._cache_config = cache_config

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
        canned = match_canned(sql, database, self._effective_schema(database))
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
        if (
            references_catalog(sql)
            or references_temp_table(sql)
            or is_metadata_probe(sql)
            or self._references_model_schema(sql)
        ):
            try:
                self._catalog.refresh(self._sessions)
                # Resolve the ``model.<view>`` alias to the connected
                # model's actual schema before executing. The alias is
                # the Arrow Flight surface's user-friendly shape; we
                # accept it on pgwire so the same SQL works from any
                # client without retyping the schema name the user
                # just ``SET search_path``'d to.
                catalog_sql = self._resolve_model_alias(sql, database)
                result = self._catalog.execute(catalog_sql, database)
            except Exception as exc:  # noqa: BLE001 — protocol boundary
                logger.info("pgwire catalog probe failed: %s", exc)
                return protocol.build_error_response(
                    severity="ERROR",
                    code=SQLSTATE_SYNTAX_ERROR,
                    message=f"catalog query failed: {exc}",
                )
            return _encode_result(result, result_formats)

        try:
            target = self._resolve_target(database, sql)
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
        # it wins over the global ``DB_VENDOR``. Lets a single OBSL serve
        # both a DuckDB-backed model and a Dremio-backed model from the
        # same pgwire surface — each query is compiled AND executed
        # against the right vendor.
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
                message=exc.message,
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
            if self._cache is not None:
                cached = await execute_query_with_cache(
                    store=target.store,
                    model=target.model,
                    compile_result=compile_result,
                    # Key on the stable model_id, not the connection's
                    # ``database`` alias, so every pgwire connection to the same
                    # model shares cache entries regardless of which alias
                    # (model name vs brand database) it connected with.
                    session_id=target.model_id,
                    model_id=target.model_id,
                    dialect=dialect,
                    cache=self._cache,
                    cache_config=self._cache_config,
                    cacheable=getattr(self._cache, "backend_name", "noop") != "noop",
                )
                if cached.cached:
                    result = execution_result_from_envelope(cached.envelope)
                else:
                    assert cached.exec_result is not None  # a miss always executed
                    result = cached.exec_result
            else:
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

    def _effective_schema(self, database: str) -> str:
        """Schema name that ``current_schema()`` / ``SHOW search_path`` report.

        v2.5.0 catalog layout puts the model at
        ``database=orionbelt``, ``schema=<model_name>``,
        ``table=model``. Pre-flip BI-tool connect-checks would set
        ``search_path`` to a model schema and immediately query
        ``current_schema()`` to confirm. The canned response used to
        return the literal ``"orionbelt"`` — which is now the
        DATABASE name, not a schema — so pgjdbc filtered
        ``pg_namespace`` by a non-existent schema and NPE'd when its
        next probe found no matching ``pg_class`` row for the
        expected ``model`` table.

        Heuristic:

        * ``database`` names a loaded model        → return ``database``
          (the schema-per-model name in ``pg_namespace``).
        * ``database`` is the brand ``"orionbelt"`` → return the
          first loaded model name (sorted, so deterministic).
        * Otherwise                                → return ``""``;
          canned falls back to the legacy literal.

        SET search_path is still accepted-and-ignored at this layer
        because no connection state is threaded through yet; that's
        fine for the immediate NPE fix because every loaded-model
        name appears in ``pg_namespace`` and DBeaver's follow-up
        ``pg_class`` filter resolves to a real row.
        """

        names = self._loaded_model_names()
        if not names:
            return ""
        db_lower = (database or "").lower()
        if db_lower and db_lower in names:
            return database
        if db_lower == CATALOG_SCHEMA.lower():
            return sorted(names)[0]
        return ""

    def _loaded_model_names(self) -> set[str]:
        """Return the set of currently-loaded model names (lowercased).

        Includes both preloaded protected sessions (``MODEL_FILES``) and
        sessions created over REST.
        """

        names: set[str] = set()
        with contextlib.suppress(Exception):
            names.update(n.lower() for n in self._sessions.list_protected_session_ids())
        # In admin-curated mode the catalog exposes only curated models (see
        # CatalogEmulator._iter_loaded_models), so transient user/scratch
        # sessions must NOT be treated as catalog schemas here either —
        # otherwise a ``FROM <scratch_session>.measures`` reference would route
        # to the catalog, which never built that schema. Keep them in dynamic
        # mode, where user sessions ARE the catalog.
        if not getattr(self._sessions, "is_single_model_mode", False):
            with contextlib.suppress(Exception):
                for s in self._sessions.list_sessions():
                    names.add(s.session_id.lower())
        return names

    def _resolve_model_alias(self, sql: str, database: str) -> str:
        """Rewrite ``model.<view>`` → ``"<schema>"."<view>"``.

        ``<schema>`` is the connected model's pg_namespace.nspname —
        same value as ``current_schema()`` / ``SHOW search_path``
        report. Returns the SQL unchanged when no alias is present
        OR when the effective schema is unknown (multi-model
        connection on the brand database without a SET search_path),
        which preserves the original error path for ambiguous
        references.
        """

        schema = self._effective_schema(database)
        if not schema:
            return sql
        quoted = '"' + schema.replace('"', '""') + '"'
        return _RE_MODEL_ALIAS_QUALIFIER.sub(lambda m: f'{quoted}."{m.group("view")}"', sql)

    def _references_model_schema(self, sql: str) -> bool:
        """True when ``sql`` references a schema that matches a loaded model.

        ``database=orionbelt`` BI-tool browsing fires SQL like
        ``SELECT * FROM commerce.model`` or
        ``SELECT * FROM commerce.measures``. The schema (``commerce``)
        is a model name. Those queries belong in the catalog DuckDB,
        not the OBSQL translator. We detect them by parsing the SQL
        and checking if any FROM/JOIN target's schema qualifier
        matches a currently-loaded model.

        Also routes the ``model.<metadata_view>`` user-friendly alias
        (the Arrow Flight surface uses the same shape) so
        ``SELECT * FROM model.dimensions`` lands in the catalog
        DuckDB rather than the OBSQL translator. ``model`` is the
        v2.5.0 virtual table living inside each model schema; the
        alias ``model.<view>`` reads as "the metadata of the
        connected model" without forcing the user to retype the
        schema name they just ``SET search_path``'d to.
        """

        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return False
        model_names = self._loaded_model_names()
        for table in parsed.find_all(exp.Table):
            schema = (table.db or "").lower() or (table.text("db") or "").lower()
            tname = (table.name or "").lower()
            # ``<model>.model`` (the data virtual table, fully qualified
            # by Tableau / Dremio pushdown) is the OBSQL data path —
            # ``_unwrap_model_qualifier`` strips ``."model"`` so the
            # translator sees ``FROM "<model>"``. Routing it to the
            # catalog instead returns an empty column-shape table.
            if schema and schema in model_names and tname != "model":
                return True
            if schema == "model" and tname in _METADATA_VIEW_NAMES:
                return True
        return False

    def _resolve_target(self, database: str, sql: str = "") -> _ResolvedTarget:
        """Pick a (store, model_id, model) tuple for the requested DB.

        Resolution order:

        1. ``database`` matches a SessionManager session id directly
           (multi-model preload uses the model name as the session id).
        2. ``__default__`` session (single-model preload, MCP stdio,
           tests).
        3. The connection is on the brand database (``orionbelt``) and
           the SQL has a FROM target naming a loaded model — extract
           the model from the SQL. This is the BI-tool path: the user
           writes ``SELECT … FROM <model>`` against a connection on
           ``database=orionbelt``.
        4. ``database`` matches a model in some other session's store
           by name (single unambiguous match).
        5. ``database='orionbelt'`` with exactly one model loaded —
           fall back to that single model (the demo case).

        Raises ``_ModelNotFoundError`` with a message that surfaces in the
        Postgres ErrorResponse on miss.
        """

        candidate_ids: list[str] = []
        if database and database != CATALOG_SCHEMA:
            candidate_ids.append(database)
        if "__default__" not in candidate_ids:
            candidate_ids.append("__default__")

        for session_id in candidate_ids:
            target = self._first_model_in(session_id)
            if target is not None:
                return target

        # On a brand-database connection (``database=orionbelt``), the
        # model comes from the SQL itself — the user's FROM clause
        # names the model directly. Peek at the first known FROM target.
        if database == CATALOG_SCHEMA and sql:
            model_from_sql = self._extract_model_name_from_sql(sql)
            if model_from_sql:
                target = self._first_model_in(model_from_sql)
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

        # Brand-database with exactly one model loaded — auto-resolve.
        if database == CATALOG_SCHEMA:
            all_models: list[_ResolvedTarget] = []
            for name in self._loaded_model_names():
                target = self._first_model_in(name)
                if target is not None:
                    all_models.append(target)
            if len(all_models) == 1:
                return all_models[0]
            if len(all_models) > 1:
                raise _ModelNotFoundError(
                    f"database='{CATALOG_SCHEMA}' with multiple models loaded — "
                    "qualify the FROM with the model name (``SELECT … FROM <model>``) "
                    "or connect with ``database=<model>`` directly. "
                    "``GET /v1/models`` lists the available names."
                )

        raise _ModelNotFoundError(
            f"no model is available for database='{database}'. "
            "Load a model via REST /v1/sessions/{id}/models or start the server "
            "with MODEL_FILES=<path,...>."
        )

    def _extract_model_name_from_sql(self, sql: str) -> str | None:
        """Find a FROM target whose name matches a loaded model.

        Handles both bare ``FROM <model>`` (semantic-mode shape) and
        schema-qualified ``FROM <schema>.<table>`` (catalog probe
        shape, where the schema IS the model name).
        """

        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return None
        model_names = self._loaded_model_names()
        if not model_names:
            return None
        for table in parsed.find_all(exp.Table):
            schema = (table.db or "").lower() or (table.text("db") or "").lower()
            if schema and schema in model_names:
                return schema
            name = (table.name or "").lower()
            if name and name in model_names:
                return name
        return None

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


_FLATTEN_HOISTABLE = ("where", "order", "limit")
_FLATTEN_MAX_DEPTH = 8


def _flatten_unwrap_collate(node: exp.Expression) -> exp.Expression:
    """Strip a single ``COLLATE`` wrapper (Dremio adds ``COLLATE "C"``)."""

    return node.this if isinstance(node, exp.Collate) else node


def _flatten_is_passthrough(proj: exp.Expression) -> bool:
    """``True`` if a projection is a bare column ref (optionally aliased/COLLATE)."""

    col = proj.this if isinstance(proj, exp.Alias) else proj
    col = _flatten_unwrap_collate(col)
    return isinstance(col, exp.Column)


def _flatten_literal_value(node: exp.Expression) -> str | None:
    """Return a literal's raw value if ``node`` is a literal (CAST/COLLATE-wrapped ok)."""

    node = _flatten_unwrap_collate(node)
    if isinstance(node, exp.Cast):
        node = _flatten_unwrap_collate(node.this)
    if isinstance(node, exp.Literal):
        return str(node.this)
    return None


def _flatten_constant_projection(
    proj: exp.Expression, wheres: list[exp.Expression]
) -> exp.Expression | None:
    """Map Dremio's constant-folded ``<literal> AS <dim>`` projection to a bare column.

    When a view is filtered with ``WHERE <dim> = <value>``, Dremio proves the
    projected dimension is constant and rewrites it to ``CAST(<value>) AS <dim>``
    in the SELECT, pushing the equality into a nested ``WHERE``. We map that back
    to the bare ``<dim>`` column, but ONLY when a collected WHERE actually
    constrains ``<dim>`` to the same literal — otherwise we bail (return None) so
    a genuine constant projection still reaches the translator's rejection path.
    """

    if not isinstance(proj, exp.Alias):
        return None
    name = proj.alias
    lit = _flatten_literal_value(proj.this)
    if lit is None or not name:
        return None
    for where in wheres:
        for eq in where.find_all(exp.EQ):
            for col_side, lit_side in ((eq.this, eq.expression), (eq.expression, eq.this)):
                col = _flatten_unwrap_collate(col_side)
                if (
                    isinstance(col, exp.Column)
                    and col.name == name
                    and _flatten_literal_value(lit_side) == lit
                ):
                    aliased: exp.Expression = exp.alias_(col.copy(), name)
                    return aliased
    return None


def _flatten_federation_subquery(sql: str) -> str:
    """Collapse Dremio's federation pushdown wrapper into a flat OBSQL SELECT.

    Dremio's Postgres-source connector wraps the virtual ``model`` table in one
    or more trivial derived tables and lifts ``WHERE`` / ``ORDER BY`` / ``FETCH``
    around them::

        SELECT "Country Name", "Total Sales"
        FROM (SELECT "model"."Country Name" COLLATE "C", "model"."Total Sales"
              FROM "commerce"."model") AS "model"
        WHERE ("Total Sales" > 1000000)
        ORDER BY "Total Sales" DESC FETCH NEXT 5 ROWS ONLY

    Filtering a *saved view* nests deeper: the view body becomes an inner derived
    table, the filter lands in a middle one, and an equality filter on a
    dimension is constant-folded (``WHERE "Channel" = 'B2B'`` -> a
    ``CAST('B2B') AS "Channel"`` projection). We walk the whole pass-through
    chain down to ``model``, gather every WHERE (AND-combined) plus a single
    ORDER BY / LIMIT, map any constant-folded projection back to its column, and
    rebuild one flat SELECT over ``model`` that the OBSQL translator accepts.

    Returns the SQL unchanged when the shape doesn't match, so a genuinely
    unsupported subquery still reaches the translator's rejection path.
    """

    try:
        ast = sqlglot.parse_one(sql)
    except Exception:
        return sql
    if not isinstance(ast, exp.Select) or ast.args.get("joins"):
        return sql
    # Only the projection list, FROM, and the hoistable clauses may appear; a
    # GROUP / DISTINCT / HAVING / CTE at any level means real work -> bail.
    if any(v for k, v in ast.args.items() if k not in ("expressions", "from", *_FLATTEN_HOISTABLE)):
        return sql

    outer_projs = ast.expressions
    wheres: list[exp.Expression] = []
    order = ast.args.get("order")
    limit = ast.args.get("limit")
    if ast.args.get("where"):
        wheres.append(ast.args["where"].this)

    # Walk down the chain of single-source derived tables to the model table,
    # accumulating WHERE (combinable) and a single ORDER BY / LIMIT.
    node: exp.Select = ast
    base: exp.Table | None = None
    for _ in range(_FLATTEN_MAX_DEPTH):
        from_ = node.args.get("from")
        if from_ is None:
            return sql
        src = from_.this
        if isinstance(src, exp.Table):
            base = src
            break
        if not isinstance(src, exp.Subquery):
            return sql
        inner = src.this
        if not isinstance(inner, exp.Select) or inner.args.get("joins"):
            return sql
        if any(
            v
            for k, v in inner.args.items()
            if k not in ("expressions", "from", *_FLATTEN_HOISTABLE)
        ):
            return sql
        # Intermediate layers must be pure pass-through projections.
        if not all(_flatten_is_passthrough(p) for p in inner.expressions):
            return sql
        if inner.args.get("where"):
            wheres.append(inner.args["where"].this)
        if inner.args.get("order"):
            if order is not None:
                return sql
            order = inner.args["order"]
        if inner.args.get("limit"):
            if limit is not None:
                return sql
            limit = inner.args["limit"]
        node = inner
    if base is None or base.name.lower() != "model":
        return sql

    # Resolve the outermost projections: bare columns pass through; a
    # constant-folded literal projection maps back to its dimension column.
    new_projs: list[exp.Expression] = []
    for proj in outer_projs:
        if _flatten_is_passthrough(proj):
            new_projs.append(proj.copy())
            continue
        rewritten = _flatten_constant_projection(proj, wheres)
        if rewritten is None:
            return sql
        new_projs.append(rewritten)

    flat = exp.Select()
    flat.set("expressions", new_projs)
    flat.set("from", exp.From(this=base.copy()))
    if wheres:
        cond = wheres[0].copy()
        for extra in wheres[1:]:
            cond = exp.and_(cond, extra.copy())
        flat.set("where", exp.Where(this=cond))
    # Render with sqlglot's default generator, NOT dialect="postgres": the
    # postgres generator makes the default null ordering explicit (injects
    # ``NULLS LAST`` into a plain ``ORDER BY ... DESC``), which the OBSQL
    # translator would then capture and bake into the compiled SQL — changing
    # top-N results for nullable measures versus what the client actually sent.
    if order is not None:
        flat.set("order", order.copy())
    if limit is not None:
        flat.set("limit", limit.copy())
    return flat.sql()


def _normalize_for_obsql(sql: str) -> str:
    """Apply pgjdbc-pushdown normalizations before OBSQL translation."""

    return _rewrite_fetch_to_limit(
        _strip_collate_annotations(_unwrap_model_qualifier(_flatten_federation_subquery(sql)))
    )


# When OBSL exposes a model as ``"<model>"."model"`` (v2.5.0 catalog
# layout: schema=model, table='model'), BI tools that pushdown queries
# (Dremio's Postgres-source connector, DBeaver query builders,
# Tableau's custom-SQL path) emit fully-qualified references the
# OBSQL translator doesn't accept:
#
#   FROM "orionbelt"."commerce"."model"  → strip db+table to ``"commerce"``
#   FROM "commerce"."model"              → strip ``.model`` to ``"commerce"``
#   "model"."Region", "model"."Sales"    → strip ``"model".`` column-refs
#
# After the rewrite the SQL looks exactly like what an OBSQL-savvy
# caller would write: ``SELECT "Region", "Sales" FROM "commerce"``.
#
# The leading ``"<db>".`` is optional because BI tools differ on
# whether they include the database in pushdown SQL: pgjdbc/DBeaver
# typically omit it (search_path covers the schema), Tableau's
# JDBC-source pushdown emits the full three-part form.
_RE_FROM_MODEL_QUALIFIER = re.compile(
    r'(\bFROM\s+)(?:"[^"]+"\s*\.\s*)?("[^"]+")\s*\.\s*"model"(?!\w)',
    re.IGNORECASE,
)
_RE_JOIN_MODEL_QUALIFIER = re.compile(
    r'(\bJOIN\s+)(?:"[^"]+"\s*\.\s*)?("[^"]+")\s*\.\s*"model"(?!\w)',
    re.IGNORECASE,
)
_RE_MODEL_TABLE_PREFIX = re.compile(r'(?<![\w"])"model"\s*\.\s*"', re.IGNORECASE)


def _unwrap_model_qualifier(sql: str) -> str:
    """Strip the ``."model"`` table qualifier from FROM/JOIN + column refs.

    Handles both 2-part (``"schema"."model"``) and 3-part
    (``"db"."schema"."model"``) qualified references so Tableau's
    pushdown SQL is accepted by the OBSQL translator.
    """

    sql = _RE_FROM_MODEL_QUALIFIER.sub(r"\1\2", sql)
    sql = _RE_JOIN_MODEL_QUALIFIER.sub(r"\1\2", sql)
    sql = _RE_MODEL_TABLE_PREFIX.sub('"', sql)
    return sql


_RE_SELECT_STAR = re.compile(r"^\s*select\s+\*", re.IGNORECASE)


def is_metadata_probe(sql: str) -> bool:
    """Return ``True`` for ``SELECT *`` column-discovery probes.

    BI tools (Tableau, Power BI, DBeaver, …) ask "what columns does
    this table have" with ``SELECT * FROM x WHERE 1=0`` or
    ``SELECT * FROM x LIMIT 0``. We route those to the catalog DuckDB
    so the column shape comes back from the registered virtual table
    instead of bouncing off the semantic translator (which rejects
    ``SELECT *``).

    Crucially, the gate is BOTH ``SELECT *`` AND the zero-row clause.
    A normal semantic query like
    ``SELECT "Customer Country" FROM commerce LIMIT 0`` has explicit
    columns the BI tool already knows about — it belongs in the
    translator path, not the catalog. Without the ``SELECT *`` gate,
    every legitimate ``LIMIT 0`` / ``WHERE 1=0`` query gets misrouted
    and returns ``Table not found`` from DuckDB.
    """

    if _RE_SELECT_STAR.match(sql) is None:
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
    * a bare ``pg_*`` function call or relation (``pg_get_keywords()``,
      ``pg_total_relation_size(...)``, ``FROM pg_description``, …) —
      Postgres clients routinely drop the schema qualifier on
      well-known catalog objects, and the catalog stubs / DuckDB's
      own pg_catalog views can answer those.

    Falls back to a cheap substring test if sqlglot can't parse the
    query — Postgres clients sometimes emit dialect-specific snippets
    (e.g. operator classes) sqlglot doesn't fully understand.
    """

    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        lowered = sql.lower()
        return (
            "pg_catalog" in lowered
            or "information_schema" in lowered
            or _RE_BARE_PG_OBJECT.search(sql) is not None
        )

    for table in parsed.find_all(exp.Table):
        if (table.db or "").lower() in _CATALOG_SCHEMAS:
            return True
        if (table.text("db") or "").lower() in _CATALOG_SCHEMAS:
            return True
        if (table.text("catalog") or "").lower() in _CATALOG_SCHEMAS:
            return True
        # Bare ``FROM pg_description`` — Postgres clients drop the
        # ``pg_catalog.`` prefix on well-known objects. Route to the
        # catalog DuckDB; DuckDB's own pg_catalog views answer those.
        if (table.name or "").lower().startswith("pg_"):
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
        # Bare ``pg_*`` function calls — pg_get_keywords(),
        # pg_total_relation_size(oid), pg_size_pretty(bytes), … —
        # answered by _STUB_MACROS in catalog.py.
        if func_name.startswith("pg_"):
            return True
    # Some catalog functions parse as Dot expressions (schema.func()).
    for dot in parsed.find_all(exp.Dot):
        left = dot.args.get("this")
        if isinstance(left, exp.Identifier) and (left.name or "").lower() in _CATALOG_SCHEMAS:
            return True
    return False


# Fallback regex for the ``except`` branch above: matches a bare
# ``pg_<name>`` token that sits in a SQL position where it would
# resolve to a catalog object (FROM/JOIN target or function call).
_RE_BARE_PG_OBJECT = re.compile(
    r"\b(?:from|join)\s+pg_[a-z_]+\b|\bpg_[a-z_]+\s*\(",
    re.IGNORECASE,
)


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
    requested_formats = _expand_result_formats(result_formats, n_cols)
    # Effective per-column format: client gets binary only on columns
    # where ``encode_value`` actually emits binary bytes. Advertising
    # binary on a column we can only encode as text makes pgjdbc /
    # asyncpg / psycopg misdecode the payload using the column's OID.
    per_col_formats = [
        1 if requested_formats[i] == 1 and can_encode_binary(col.type_hint) else 0
        for i, col in enumerate(result.columns)
    ]
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
