"""In-process implementations of the ``obsl`` commands.

Every function here calls the same service-layer internals the REST API uses
(``ModelStore``, ``execute_sql``, ``generate_mermaid_er``, the ``osi_orionbelt``
converter) so the CLI's local output matches the server byte-for-byte.
"""

from __future__ import annotations

import importlib
import os
import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.pipeline import CompilationResult
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.validator import format_sql
from orionbelt.dialect.base import (
    AmbiguousTableReferenceError,
    UnsupportedAggregationError,
    UnsupportedFunctionError,
    UnsupportedGroupingError,
)
from orionbelt.dialect.registry import DialectRegistry, UnsupportedDialectError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.db_executor import (
    ExecutionResult,
    ensure_arrow,
    execute_sql,
    resolve_timezone,
)
from orionbelt.service.diagram import generate_mermaid_er
from orionbelt.service.model_store import (
    ModelDescription,
    ModelStore,
    ValidationSummary,
)

if TYPE_CHECKING:
    from orionbelt.obsl.sparql import SPARQLResult
    from orionbelt.service.lineage import Lineage


class CliError(Exception):
    """A user-facing error the CLI should report and exit non-zero on.

    Raised for predictable failures (missing converter, execution backend not
    configured, ...) so ``main`` can print a clean message instead of a
    traceback.
    """


def list_dialects() -> list[str]:
    """Return the registered SQL dialect names."""
    return DialectRegistry.available()


def resolve_dialect(model: SemanticModel, explicit: str | None) -> str:
    """Pick the dialect: explicit flag, then model default, then ``DB_VENDOR``.

    Mirrors the REST API's resolution order so local ``compile`` produces the
    same SQL the server would for the same model and flags.
    """
    if explicit:
        return explicit
    settings = getattr(model, "settings", None)
    model_default = getattr(settings, "default_dialect", None) if settings else None
    if model_default:
        return str(model_default)
    return os.getenv("DB_VENDOR") or "postgres"


def _compile(
    store: ModelStore, model_id: str, query: QueryObject, dialect: str
) -> CompilationResult:
    """Compile, mapping planner/dialect failures to a clean ``CliError``."""
    try:
        return store.compile_query(model_id, query, dialect)
    except UnsupportedDialectError:
        raise CliError(f"Unsupported dialect: '{dialect}'") from None
    except ResolutionError as exc:
        details = "; ".join(f"[{e.code}] {e.message}" for e in exc.errors)
        raise CliError(f"Query resolution failed: {details}") from None
    except FanoutError as exc:
        raise CliError(f"Query fanout detected: {exc.message}") from None
    except (
        AmbiguousTableReferenceError,
        UnsupportedAggregationError,
        UnsupportedFunctionError,
        UnsupportedGroupingError,
    ) as exc:
        raise CliError(str(exc)) from None


def validate(
    model_yaml: str, *, online: bool = False, dialect: str | None = None
) -> ValidationSummary:
    """Validate an OBML model without storing it.

    ``online`` additionally probes the configured datasource for every data
    object's table and declared columns. The dialect selects the *connection*
    to open, so it falls back to ``DB_VENDOR`` rather than to the model's
    ``defaultDialect`` — see ``api.routers.sessions._probe_dialect``.
    """
    probe_dialect = (dialect or os.getenv("DB_VENDOR") or "duckdb") if online else None
    return ModelStore().validate(model_yaml, datasource_dialect=probe_dialect)


def _load(model_yaml: str) -> tuple[ModelStore, str, SemanticModel]:
    """Load a model into a fresh store, returning the store, id and model.

    The CLI is an external ingestion boundary, so it enforces the published
    JSON Schema here the same way the REST API does via its request guards
    (``api/schema_guards.py``). ``ModelStore.load_model`` itself stays
    coercion-tolerant for internal callers; the strictness lives at the
    boundary. Without this, a schema violation the API rejects with 422
    (e.g. an authored ``label:`` on a dimension) would be silently coerced
    away by the CLI.

    Raises ``CliError`` on a JSON Schema violation, and ``ModelValidationError``
    (from the store) on a semantic error; the caller maps both to a clean CLI
    failure.
    """
    from orionbelt.parser.schema_validation import validate_obml_yaml

    schema_errors = validate_obml_yaml(model_yaml)
    if schema_errors:
        details = "; ".join(f"[{e.code}] {e.message}" for e in schema_errors)
        raise CliError(f"Model failed schema validation: {details}")

    store = ModelStore()
    result = store.load_model(model_yaml, dedup=False)
    return store, result.model_id, store.get_model(result.model_id)


def _translate(model: SemanticModel, sql: str) -> QueryObject:
    """Translate an OBSQL string to a QueryObject, mapping failures to CliError."""
    from orionbelt.compiler.sql_translator import SQLTranslationError, translate_sql_to_query

    try:
        return translate_sql_to_query(sql, model)
    except SQLTranslationError as exc:
        details = "; ".join(f"[{e.code}] {e.message}" for e in exc.errors)
        raise CliError(f"OBSQL translation failed: {details}") from None


def _compile_loaded(
    store: ModelStore,
    model_id: str,
    model: SemanticModel,
    query: QueryObject,
    dialect: str | None,
    *,
    pretty: bool,
) -> CompilationResult:
    resolved_dialect = resolve_dialect(model, dialect)
    result = _compile(store, model_id, query, resolved_dialect)
    if pretty:
        result.sql = format_sql(result.sql, result.dialect)
    return result


def _execute_loaded(
    store: ModelStore,
    model_id: str,
    model: SemanticModel,
    query: QueryObject,
    dialect: str | None,
    *,
    limit: int | None,
) -> tuple[CompilationResult, ExecutionResult]:
    if query.limit is None and limit is not None:
        query = query.model_copy(update={"limit": limit})
    resolved_dialect = resolve_dialect(model, dialect)
    compiled = _compile(store, model_id, query, resolved_dialect)
    return compiled, _run_compiled(model, compiled, resolved_dialect)


def _run_compiled(
    model: SemanticModel, compiled: CompilationResult, dialect: str
) -> ExecutionResult:
    """Execute compiled SQL with the model's timezone settings."""
    settings = getattr(model, "settings", None)
    tz = resolve_timezone(
        default_timezone=getattr(settings, "default_timezone", None) if settings else None
    )
    override = bool(getattr(settings, "override_database_timezone", False)) if settings else False
    # See ``db_executor.ensure_arrow``: without it the executor cannot take a
    # driver's Arrow path, and the result's columns get typed by inference over
    # the values rather than from the driver's schema.
    ensure_arrow()
    return execute_sql(compiled.sql, dialect=dialect, tz=tz, override_db_tz=override)


def compile_query(
    model_yaml: str, query: QueryObject, dialect: str | None, *, pretty: bool = True
) -> CompilationResult:
    """Compile a query against a model and return the compilation result."""
    store, model_id, model = _load(model_yaml)
    return _compile_loaded(store, model_id, model, query, dialect, pretty=pretty)


def compile_obsql(
    model_yaml: str, sql: str, dialect: str | None, *, pretty: bool = True
) -> CompilationResult:
    """Compile an OBSQL string against a model (translated locally first)."""
    store, model_id, model = _load(model_yaml)
    return _compile_loaded(store, model_id, model, _translate(model, sql), dialect, pretty=pretty)


def execute_query(
    model_yaml: str,
    query: QueryObject,
    dialect: str | None,
    *,
    limit: int | None = None,
) -> tuple[CompilationResult, ExecutionResult]:
    """Compile and execute a query against the configured warehouse.

    When the query carries no ``limit`` and ``limit`` is supplied, it is
    applied before compilation so a local ``execute`` doesn't pull an
    unbounded result set by accident.
    """
    store, model_id, model = _load(model_yaml)
    return _execute_loaded(store, model_id, model, query, dialect, limit=limit)


def execute_obsql(
    model_yaml: str,
    sql: str,
    dialect: str | None,
    *,
    limit: int | None = None,
) -> tuple[CompilationResult, ExecutionResult]:
    """Translate an OBSQL string and execute it against the warehouse."""
    store, model_id, model = _load(model_yaml)
    return _execute_loaded(store, model_id, model, _translate(model, sql), dialect, limit=limit)


def describe(model_yaml: str) -> ModelDescription:
    """Return a structured overview of a model's artefacts."""
    store, model_id, _ = _load(model_yaml)
    return store.describe(model_id)


def diagram(model_yaml: str, *, show_columns: bool = True, theme: str = "default") -> str:
    """Render a model as a Mermaid ER diagram (raw, no markdown fences)."""
    _, _, model = _load(model_yaml)
    return generate_mermaid_er(model, show_columns=show_columns, theme=theme)


def graph(model_yaml: str) -> str:
    """Render a model's OBSL-Core RDF graph as Turtle."""
    store, model_id, _ = _load(model_yaml)
    return store.get_graph(model_id).turtle


def sparql(model_yaml: str, query: str) -> SPARQLResult:
    """Run a read-only SPARQL query against a model's OBSL-Core RDF graph."""
    from orionbelt.obsl.sparql import SPARQLUpdateError

    store, model_id, _ = _load(model_yaml)
    try:
        return store.query_graph(model_id, query)
    except SPARQLUpdateError as exc:
        raise CliError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - rdflib raises many parse error types
        raise CliError(f"SPARQL error: {exc}") from None


@dataclass
class RuleOutcome:
    """One business rule's plan, and its compile or evaluation result.

    ``status`` is ``compiled`` or ``executed`` on success and ``failed``
    otherwise, with the reason in ``error``. ``finding_count`` and ``rows`` are
    set once the rule's query has run.
    """

    name: str
    type: str
    level: str
    findings: str
    severity: str | None
    status: str
    sql: str | None = None
    error: str | None = None
    finding_count: int | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)


def run_rules(
    model_yaml: str,
    dialect: str | None,
    *,
    names: list[str] | None = None,
    types: list[str] | None = None,
    severities: list[str] | None = None,
    execute: bool = False,
    limit: int = 20,
    pretty: bool = True,
) -> tuple[str, list[RuleOutcome]]:
    """Compile, and with ``execute`` also run, a model's business rules.

    Mirrors the REST rules endpoints: each rule compiles to an ordinary query
    whose rows are its findings, and a rule that fails to compile or run is
    reported as a failed row rather than aborting the others. Returns the
    resolved dialect and one outcome per selected rule, in model order.
    """
    from orionbelt.compiler.rules import RuleCompiler
    from orionbelt.service.db_executor import ExecutionError

    store, model_id, model = _load(model_yaml)
    resolved_dialect = resolve_dialect(model, dialect)
    unknown = [n for n in names or [] if n not in model.rules]
    if unknown:
        known = ", ".join(model.rules) or "none"
        raise CliError(f"Unknown rule(s): {', '.join(unknown)}. Rules in the model: {known}")

    selected = [r for r in model.rules.values() if not names or r.name in names]
    if types:
        selected = [r for r in selected if r.type.value in types]
    if severities:
        selected = [r for r in selected if r.severity and r.severity.value in severities]

    compiler = RuleCompiler(model)
    outcomes: list[RuleOutcome] = []
    for rule in selected:
        plan = compiler.plan(rule)
        outcome = RuleOutcome(
            name=rule.name,
            type=rule.type.value,
            level=plan.level,
            findings=plan.findings,
            severity=rule.severity.value if rule.severity else None,
            status="compiled",
        )
        outcomes.append(outcome)
        query = plan.query.model_copy(update={"limit": limit}) if execute else plan.query
        try:
            compiled = _compile(store, model_id, query, resolved_dialect)
        except CliError as exc:
            outcome.status, outcome.error = "failed", str(exc)
            continue
        outcome.sql = format_sql(compiled.sql, compiled.dialect) if pretty else compiled.sql
        if not execute:
            continue
        try:
            executed = _run_compiled(model, compiled, resolved_dialect)
        except ExecutionError as exc:
            outcome.status, outcome.error = "failed", str(exc)
            continue
        outcome.status = "executed"
        outcome.finding_count = executed.row_count
        outcome.columns = [c.name for c in executed.columns]
        outcome.rows = executed.rows
    return resolved_dialect, outcomes


def lineage(
    model_yaml: str,
    kind: str,
    name: str | None = None,
    *,
    query: QueryObject | None = None,
    sql: str | None = None,
    dialect: str | None = None,
) -> tuple[Lineage, str]:
    """Lineage of a model artefact or a query, and the model id its IRIs use.

    *kind* is ``dimension``, ``measure``, ``metric``, ``rule`` or ``query``. A
    query comes as a query document or an OBSQL string; it is compiled so its
    lineage includes the joins the planner chose.
    """
    from orionbelt.service.lineage import LineageBuilder, LineageError, query_plan_facts

    store, model_id, model = _load(model_yaml)
    builder = LineageBuilder(model)
    if kind == "query":
        q = query if query is not None else _translate(model, str(sql))
        compiled = _compile(store, model_id, q, resolve_dialect(model, dialect))
        return builder.query(q, query_plan_facts(compiled)), model_id
    build = {
        "dimension": builder.dimension,
        "measure": builder.measure,
        "metric": builder.metric,
        "rule": builder.rule,
    }[kind]
    try:
        return build(str(name)), model_id
    except LineageError as exc:
        raise CliError(str(exc)) from None


def _converter_module() -> types.ModuleType:
    """Import the optional ``osi_orionbelt`` converter or raise ``CliError``."""
    try:
        return importlib.import_module("osi_orionbelt")
    except ModuleNotFoundError as exc:
        if exc.name != "osi_orionbelt":
            raise
        raise CliError(
            "OSI conversion is unavailable: the 'osi-orionbelt' converter is not "
            "installed. Install it with: pip install 'orionbelt-semantic-layer[osi]'."
        ) from None


def _validation_dict(validate_fn: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Run a converter validation function into a plain dict (best effort)."""
    try:
        vr = validate_fn(data)
    except Exception:  # noqa: BLE001 — validation is advisory; never block conversion
        return {"skipped": True}
    return {
        "schema_valid": not vr.schema_errors,
        "semantic_valid": not vr.semantic_errors,
        "schema_errors": list(vr.schema_errors),
        "semantic_errors": list(vr.semantic_errors),
        "semantic_warnings": list(vr.semantic_warnings),
    }


def convert_osi_to_obml(input_yaml: str) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Convert an OSI document to an OBML dict.

    Returns ``(obml_dict, warnings, validation)`` where ``validation`` is the
    advisory OBML validation of the conversion output.
    """
    import yaml

    mod = _converter_module()
    data = yaml.safe_load(input_yaml)
    if not isinstance(data, dict):
        raise CliError("OSI input must be a YAML/JSON mapping (object)")
    # The CLI is an external boundary: surface OSI input schema issues instead of
    # silently converting a malformed document. Advisory, matching the REST
    # osi-to-obml endpoint — conversion still runs. (v0.1.x inputs run through a
    # legacy shim inside convert(), so a schema warning here can be spurious for
    # legacy docs; the conversion still succeeds regardless.)
    input_warnings = [
        f"OSI input schema: {msg}"
        for msg in _validation_dict(mod.validate_osi, data).get("schema_errors", [])
    ]
    converter = mod.OSItoOBML(data)
    try:
        result: dict[str, Any] = converter.convert()
    except Exception as exc:  # noqa: BLE001 — surface converter failures cleanly
        raise CliError(f"OSI -> OBML conversion failed: {exc}") from None
    return (
        result,
        input_warnings + list(converter.warnings),
        _validation_dict(mod.validate_obml, result),
    )


def convert_obml_to_osi(
    input_yaml: str,
    *,
    model_name: str = "semantic_model",
    model_description: str = "",
    ai_instructions: str = "",
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Convert an OBML document to an OSI dict.

    Returns ``(osi_dict, warnings, validation)``.
    """
    import yaml

    mod = _converter_module()
    data = yaml.safe_load(input_yaml)
    if not isinstance(data, dict):
        raise CliError("OBML input must be a YAML/JSON mapping (object)")
    # The CLI is an external boundary: surface OBML input schema issues (e.g. an
    # authored ``label:``) instead of silently coercing them away. Advisory,
    # matching the REST convert endpoints — conversion still runs.
    input_warnings = [
        f"OBML input schema: {msg}"
        for msg in _validation_dict(mod.validate_obml, data).get("schema_errors", [])
    ]
    converter = mod.OBMLtoOSI(
        data,
        model_name=model_name,
        model_description=model_description,
        ai_instructions=ai_instructions,
    )
    try:
        result: dict[str, Any] = converter.convert()
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"OBML -> OSI conversion failed: {exc}") from None
    warnings = input_warnings + list(converter.warnings)

    return result, warnings, _validation_dict(mod.validate_osi, result)
