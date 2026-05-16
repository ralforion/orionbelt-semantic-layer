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
    "CREATE OR REPLACE MACRO pg_relation_is_publishable(oid) AS false",
    "CREATE OR REPLACE MACRO obj_description(oid, catalog) AS NULL",
    "CREATE OR REPLACE MACRO col_description(oid, col) AS NULL",
    "CREATE OR REPLACE MACRO format_type(oid, typemod) AS 'unknown'",
    "CREATE OR REPLACE MACRO pg_encoding_to_char(enc) AS 'UTF8'",
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
            r"::\s*pg_catalog\.(text|name|regtype|regclass|regprocedure|oid|char)\b",
            re.IGNORECASE,
        ),
        "::VARCHAR",
    ),
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
        self._registered_tables: list[str] = []
        for ddl in _STUB_MACROS:
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

        with self._lock:
            # Drop everything we registered last time first.
            for table in self._registered_tables:
                with contextlib.suppress(Exception):
                    self._con.execute(f'DROP TABLE IF EXISTS "{table}"')
            self._registered_tables = []

            for store_target, model in _iter_loaded_models(session_manager):
                table_name = _safe_model_table_name(store_target)
                ddl = _build_table_ddl(table_name, model)
                if ddl is None:
                    continue
                try:
                    self._con.execute(ddl)
                except duckdb.Error:  # pragma: no cover — defensive guard
                    logger.exception(
                        "Failed to register catalog table for model '%s'", store_target
                    )
                    continue
                self._registered_tables.append(table_name)

    # ------------------------------------------------------------------
    # Execute — run a catalog/info-schema query through DuckDB.
    # ------------------------------------------------------------------

    def execute(self, sql: str) -> ExecutionResult:
        """Run ``sql`` against the embedded DuckDB.

        DuckDB's pg_catalog and information_schema are auto-populated
        from the schema we registered in :meth:`refresh`, so the
        caller doesn't need to special-case which table is being
        queried.  Errors bubble as ``duckdb.Error``.
        """

        t0 = time.monotonic()
        rewritten = _rewrite_for_duckdb(sql)
        with self._lock:
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
