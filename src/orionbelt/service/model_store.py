"""In-memory model registry — core service layer for the REST API."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field

from rdflib import Graph

from orionbelt.cache.ttl import RefreshContract
from orionbelt.compiler.health import compute_health
from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.models.warnings import WarningCode
from orionbelt.obsl.exporter import export_obsl
from orionbelt.obsl.sparql import SPARQLResult, execute_sparql
from orionbelt.parser.loader import TrackedLoader, YAMLSafetyError
from orionbelt.parser.merger import ExtendsMerger, MergeError
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LoadResult:
    """Result of loading a model into the store."""

    model_id: str
    data_objects: int
    dimensions: int
    measures: int
    metrics: int
    warnings: list[ErrorInfo]
    model_load: str = "fresh"  # "fresh" | "reused" — see PLAN_model_load_dedup.md
    health: ModelHealthSummary | None = None


@dataclass
class DataObjectInfo:
    """Summary of a data object for LLM consumption."""

    label: str
    code: str
    columns: list[str]
    join_targets: list[str]
    synonyms: list[str]
    owner: str | None = None


@dataclass
class DimensionInfo:
    """Summary of a dimension."""

    name: str
    result_type: str
    data_object: str
    column: str
    time_grain: str | None
    synonyms: list[str]
    owner: str | None = None


@dataclass
class MeasureInfo:
    """Summary of a measure."""

    name: str
    result_type: str
    aggregation: str
    expression: str | None
    synonyms: list[str]
    owner: str | None = None


@dataclass
class MetricInfo:
    """Summary of a metric."""

    name: str
    expression: str | None
    synonyms: list[str]
    type: str = "derived"
    measure: str | None = None
    time_dimension: str | None = None
    owner: str | None = None


@dataclass
class ModelDescription:
    """Structured summary of a loaded model — designed for LLM consumption."""

    model_id: str
    data_objects: list[DataObjectInfo]
    dimensions: list[DimensionInfo]
    measures: list[MeasureInfo]
    metrics: list[MetricInfo]


@dataclass
class ModelSummary:
    """Short summary for listing models."""

    model_id: str
    data_objects: int
    dimensions: int
    measures: int
    metrics: int


@dataclass
class ErrorInfo:
    """A single validation error or warning."""

    code: str
    message: str
    path: str | None = None
    suggestions: list[str] = field(default_factory=list)
    severity: str = "error"
    hint: str | None = None
    context: dict[str, object] | None = None


@dataclass
class FanTrapRiskInfo:
    """Detected fan-trap risk between two facts sharing a dim."""

    tables: list[str]
    reason: str
    suggested_pattern: str = "composite_fact_layer"


@dataclass
class ModelHealthSummary:
    """Structural health of a loaded model's join graph.

    See ``design/PLAN_agent_api_improvements.md`` §1.
    """

    status: str = "ok"
    data_objects: int = 0
    joins: int = 0
    orphan_data_objects: list[str] = field(default_factory=list)
    fan_trap_risks: list[FanTrapRiskInfo] = field(default_factory=list)
    unreachable_dimensions: list[str] = field(default_factory=list)
    warnings_count: int = 0


@dataclass
class ValidationSummary:
    """Result of validating a model without storing it."""

    valid: bool
    errors: list[ErrorInfo]
    warnings: list[ErrorInfo]


@dataclass
class GraphArtifact:
    """Cached OBSL-Core RDF graph derived from a loaded model."""

    graph: Graph
    turtle: str
    generated_at: float


# ---------------------------------------------------------------------------
# ModelStore
# ---------------------------------------------------------------------------


class ModelValidationError(ValueError):
    """Raised when model loading fails validation.

    Carries structured error details so callers can expose them to users.
    """

    def __init__(self, errors: list[ErrorInfo], warnings: list[ErrorInfo]) -> None:
        self.errors = errors
        self.warnings = warnings
        msgs = "; ".join(e.message for e in errors)
        super().__init__(f"Model validation failed: {msgs}")


class ModelCapacityError(Exception):
    """Raised when a session's model cap is reached."""


class ModelStore:
    """In-memory model registry.  Thread-safe via ``threading.Lock``.

    Models are keyed by short UUID (8-char hex).  All parsing, validation,
    and compilation infrastructure is instantiated internally, following the
    same singleton pattern as ``api/deps.py``.
    """

    def __init__(self, max_models: int = 10) -> None:
        self._lock = threading.Lock()
        self._models: dict[str, SemanticModel] = {}
        # Parallel storage of each loaded model's *merged* raw YAML dict
        # so inheritance can re-merge against the exact same content the
        # parent was built from. Pre-fix (v2.7.5) inheritance round-tripped
        # through ``_model_to_raw`` which dropped most non-essential
        # fields (numClass, primaryKey, expression on computed columns,
        # measure dataType / filters / grain / delimiter / withinGroup,
        # most metric subtype config, …) — child models would inherit
        # a stripped parent and silently compile invalid SQL such as
        # ``SUM("T"."")`` for any parent computed column whose ``code:``
        # the resolver had derived from its ``expression``.
        self._raws: dict[str, dict[str, object]] = {}
        self._graphs: dict[str, GraphArtifact] = {}
        # Per-store summary cache so dedup hits can return the original
        # data_objects/dimensions/measures/metrics counts without re-walking
        # the model.
        self._summaries: dict[str, ModelSummary] = {}
        self._max_models = max_models
        # Dedup index: content_hash → model_id. Populated on every successful
        # load and consulted before parsing on the next load. See
        # design/PLAN_model_load_dedup.md.
        self._content_hash_index: dict[str, str] = {}

        # Internal pipeline singletons (stateless, safe to share).
        self._loader = TrackedLoader()
        self._resolver = ReferenceResolver()
        self._validator = SemanticValidator()
        self._merger = ExtendsMerger()
        self._pipeline = CompilationPipeline()

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:8]

    @staticmethod
    def _health_for(model: SemanticModel) -> ModelHealthSummary:
        """Compute structural health for a loaded model."""
        h = compute_health(model)
        return ModelHealthSummary(
            status=h.status,
            data_objects=h.data_objects,
            joins=h.joins,
            orphan_data_objects=h.orphan_data_objects,
            fan_trap_risks=[
                FanTrapRiskInfo(
                    tables=r.tables,
                    reason=r.reason,
                    suggested_pattern=r.suggested_pattern,
                )
                for r in h.fan_trap_risks
            ],
            unreachable_dimensions=h.unreachable_dimensions,
            warnings_count=h.warnings_count,
        )

    @staticmethod
    def _content_hash(yaml_str: str) -> str:
        """SHA-256 of the OBML body, with surrounding whitespace stripped.

        Stripping at the boundary makes a trailing newline difference
        invisible to dedup; everything else (key order, comments, internal
        whitespace) still produces a different hash.
        """
        return hashlib.sha256(yaml_str.strip().encode("utf-8")).hexdigest()

    def _parse_and_validate(
        self,
        yaml_str: str | None = None,
        *,
        raw_dict: dict[str, object] | None = None,
        extends_yaml: list[str] | None = None,
        inherits_model_id: str | None = None,
    ) -> tuple[SemanticModel, dict[str, object], list[ErrorInfo], list[ErrorInfo]]:
        """Parse YAML (or accept pre-parsed dict), resolve references, validate.

        Returns ``(model, merged_raw, errors, warnings)``.
        Provide either ``yaml_str`` or ``raw_dict``, not both.

        ``merged_raw`` is the fully-merged raw dict the resolver consumed
        (after extends/inherits processing) — callers store it so future
        inherits-from-this-model loads can re-merge against the exact
        content rather than going through a lossy ``_model_to_raw``
        round-trip.
        """
        errors: list[ErrorInfo] = []
        warnings: list[ErrorInfo] = []

        # 1. Parse YAML or use pre-parsed dict
        if raw_dict is not None:
            raw = raw_dict
            source_map = None
        elif yaml_str is not None:
            try:
                raw, source_map = self._loader.load_string(yaml_str)
            except YAMLSafetyError as exc:
                errors.append(ErrorInfo(code="YAML_SAFETY_ERROR", message=str(exc)))
                return SemanticModel(), {}, errors, warnings
            except Exception as exc:
                errors.append(ErrorInfo(code="YAML_PARSE_ERROR", message=str(exc)))
                return SemanticModel(), {}, errors, warnings
        else:
            errors.append(
                ErrorInfo(
                    code="NO_MODEL_INPUT",
                    message="Provide either model_yaml or model_json",
                )
            )
            return SemanticModel(), {}, errors, warnings

        # 1b. Merge extends/inherits if provided
        try:
            inherits_raw: dict[str, object] | None = None
            if inherits_model_id is not None:
                # Prefer the parent's stored raw dict — captured at load
                # time so every field round-trips intact. Fall back to
                # the lossy ``_model_to_raw`` only when no raw is on
                # record (legacy / programmatically-constructed models).
                with self._lock:
                    inherits_raw = self._raws.get(inherits_model_id)
                if inherits_raw is None:
                    parent_model = self.get_model(inherits_model_id)
                    inherits_raw = self._model_to_raw(parent_model)

            if extends_yaml or inherits_raw is not None:
                raw, merge_warnings = self._merger.merge_from_strings(
                    raw,
                    extend_yamls=extends_yaml,
                    inherits_raw=inherits_raw,
                )
                for mw in merge_warnings:
                    warnings.append(
                        ErrorInfo(
                            code=WarningCode.MERGE_WARNING,
                            message=mw,
                            severity="warning",
                        )
                    )
                source_map = None
        except MergeError as exc:
            errors.append(ErrorInfo(code=exc.code, message=exc.message))
            return SemanticModel(), {}, errors, warnings
        except KeyError:
            errors.append(
                ErrorInfo(
                    code="PARENT_MODEL_NOT_FOUND",
                    message=f"Parent model '{inherits_model_id}' not found in session",
                )
            )
            return SemanticModel(), {}, errors, warnings

        # 2. Resolve references
        model, resolution = self._resolver.resolve(raw, source_map)
        for e in resolution.errors:
            errors.append(
                ErrorInfo(
                    code=e.code,
                    message=e.message,
                    path=e.path,
                    suggestions=list(e.suggestions),
                    severity=e.severity,
                    hint=e.hint,
                    context=e.context,
                )
            )
        for w in resolution.warnings:
            warnings.append(
                ErrorInfo(
                    code=w.code,
                    message=w.message,
                    path=w.path,
                    suggestions=list(w.suggestions),
                    severity=w.severity or "warning",
                    hint=w.hint,
                    context=w.context,
                )
            )

        # 3. Semantic validation
        sem_errors = self._validator.validate(model)
        for e in sem_errors:
            info = ErrorInfo(
                code=e.code,
                message=e.message,
                path=e.path,
                suggestions=list(e.suggestions),
                severity=e.severity,
                hint=e.hint,
                context=e.context,
            )
            if e.severity == "warning":
                warnings.append(info)
            else:
                errors.append(info)

        # 4. Cross-dataObject refresh contract consistency check.
        from orionbelt.cache.contracts import collect_table_contracts

        _, refresh_warnings = collect_table_contracts(model)
        for w in refresh_warnings:
            warnings.append(
                ErrorInfo(
                    code=w.code,
                    message=w.message,
                    path=w.path,
                    suggestions=list(w.suggestions),
                    severity=w.severity or "warning",
                    hint=w.hint,
                    context=w.context,
                )
            )

        return model, raw, errors, warnings

    @staticmethod
    def _model_to_raw(model: SemanticModel) -> dict[str, object]:
        """Convert a SemanticModel back to a raw dict for inherits merging.

        .. deprecated:: v2.7.5
            Lossy fallback only — drops most non-essential fields. New
            code stores and reuses the merged raw dict captured at load
            time (see ``ModelStore._raws``). This method remains for the
            edge case where a parent model was constructed programmatically
            without ever passing through ``load_model``.
        """
        raw: dict[str, object] = {"version": model.version}
        if model.description:
            raw["description"] = model.description
        if model.data_objects:
            objs: dict[str, object] = {}
            for name, obj in model.data_objects.items():
                obj_raw: dict[str, object] = {
                    "code": obj.code,
                    "database": obj.database,
                    "schema": obj.schema_name,
                }
                if obj.columns:
                    cols: dict[str, object] = {}
                    for cname, col in obj.columns.items():
                        cols[cname] = {
                            "code": col.code,
                            "abstractType": col.abstract_type.value,
                        }
                    obj_raw["columns"] = cols
                if obj.joins:
                    joins: list[dict[str, object]] = []
                    for j in obj.joins:
                        jd: dict[str, object] = {
                            "joinType": j.join_type.value,
                            "joinTo": j.join_to,
                            "columnsFrom": list(j.columns_from),
                            "columnsTo": list(j.columns_to),
                        }
                        if j.secondary:
                            jd["secondary"] = True
                            jd["pathName"] = j.path_name
                        joins.append(jd)
                    obj_raw["joins"] = joins
                if obj.refresh is not None:
                    refresh: dict[str, object] = {"mode": obj.refresh.mode}
                    if obj.refresh.interval:
                        refresh["interval"] = obj.refresh.interval
                    if obj.refresh.anchor:
                        refresh["anchor"] = obj.refresh.anchor
                    if obj.refresh.timezone:
                        refresh["timezone"] = obj.refresh.timezone
                    if obj.refresh.max_staleness:
                        refresh["maxStaleness"] = obj.refresh.max_staleness
                    obj_raw["refresh"] = refresh
                objs[name] = obj_raw
            raw["dataObjects"] = objs
        if model.dimensions:
            dims: dict[str, object] = {}
            for name, dim in model.dimensions.items():
                dd: dict[str, object] = {
                    "dataObject": dim.view,
                    "column": dim.column,
                    "resultType": dim.result_type.value,
                }
                if dim.time_grain:
                    dd["timeGrain"] = dim.time_grain.value
                dims[name] = dd
            raw["dimensions"] = dims
        if model.measures:
            meas: dict[str, object] = {}
            for name, m in model.measures.items():
                md: dict[str, object] = {
                    "aggregation": m.aggregation,
                    "resultType": m.result_type.value,
                }
                if m.expression:
                    md["expression"] = m.expression
                if m.columns:
                    md["columns"] = [
                        {"dataObject": c.view or "", "column": c.column or ""} for c in m.columns
                    ]
                if m.total:
                    md["total"] = True
                meas[name] = md
            raw["measures"] = meas
        if model.metrics:
            mets: dict[str, object] = {}
            for name, met in model.metrics.items():
                mtd: dict[str, object] = {"type": met.type.value}
                if met.expression:
                    mtd["expression"] = met.expression
                if met.measure:
                    mtd["measure"] = met.measure
                if met.time_dimension:
                    mtd["timeDimension"] = met.time_dimension
                mets[name] = mtd
            raw["metrics"] = mets
        if model.filters:
            raw["filters"] = [
                {
                    "dataObject": f.data_object,
                    "column": f.column,
                    "operator": f.operator,
                    **({"value": f.value} if f.value is not None else {}),
                    **({"values": f.values} if f.values else {}),
                }
                for f in model.filters
            ]
        return raw

    # -- public API ----------------------------------------------------------

    def load_model(
        self,
        yaml_str: str | None = None,
        *,
        raw_dict: dict[str, object] | None = None,
        extends_yaml: list[str] | None = None,
        inherits_model_id: str | None = None,
        dedup: bool = True,
    ) -> LoadResult:
        """Parse, validate, and store a model.  Returns id + summary.

        Provide either ``yaml_str`` or ``raw_dict``.
        Raises ``ModelValidationError`` if the model has validation errors.
        Raises ``ModelCapacityError`` if the session's model cap is reached.

        When ``dedup`` is True (default) and the same OBML bytes have already
        been loaded into this store, the existing ``model_id`` is returned
        and ``model_load`` is set to ``"reused"``. Dedup only applies to
        plain ``yaml_str`` loads — when ``raw_dict``, ``extends_yaml``, or
        ``inherits_model_id`` is supplied the load always runs fresh, since
        the effective content depends on inputs not captured by the YAML
        bytes alone.
        """
        # Dedup is meaningful only for a stand-alone YAML body. The other
        # input shapes either skip the YAML stage (raw_dict) or fold in
        # additional state (extends/inherits) that the bytes don't capture.
        dedup_eligible = (
            dedup
            and yaml_str is not None
            and raw_dict is None
            and not extends_yaml
            and inherits_model_id is None
        )
        content_hash: str | None = None
        if dedup_eligible:
            content_hash = self._content_hash(yaml_str or "")
            with self._lock:
                existing_id = self._content_hash_index.get(content_hash)
                if existing_id is not None and existing_id in self._models:
                    summary = self._summaries.get(existing_id)
                    if summary is not None:
                        existing_model = self._models[existing_id]
                        existing_health = self._health_for(existing_model)
                        return LoadResult(
                            model_id=existing_id,
                            data_objects=summary.data_objects,
                            dimensions=summary.dimensions,
                            measures=summary.measures,
                            metrics=summary.metrics,
                            warnings=[],
                            model_load="reused",
                            health=existing_health,
                        )
                # Stale index entry — drop it and fall through to a fresh load.
                if existing_id is not None:
                    self._content_hash_index.pop(content_hash, None)

        with self._lock:
            if len(self._models) >= self._max_models:
                raise ModelCapacityError(f"Maximum models per session reached ({self._max_models})")

        model, merged_raw, errors, warnings = self._parse_and_validate(
            yaml_str,
            raw_dict=raw_dict,
            extends_yaml=extends_yaml,
            inherits_model_id=inherits_model_id,
        )
        if errors:
            raise ModelValidationError(errors, warnings)

        model_id = self._new_id()

        # Eagerly export OBSL-Core graph (Option C: at model load time).
        graph = export_obsl(model, model_id)
        turtle = graph.serialize(format="turtle")
        artifact = GraphArtifact(graph=graph, turtle=turtle, generated_at=time.monotonic())

        summary = ModelSummary(
            model_id=model_id,
            data_objects=len(model.data_objects),
            dimensions=len(model.dimensions),
            measures=len(model.measures),
            metrics=len(model.metrics),
        )

        with self._lock:
            # Re-check capacity under lock — the first check (above) ran
            # outside the lock while parsing/exporting, so a concurrent
            # request may have filled the slot in the meantime.
            if len(self._models) >= self._max_models:
                raise ModelCapacityError(f"Maximum models per session reached ({self._max_models})")
            self._models[model_id] = model
            self._raws[model_id] = merged_raw
            self._graphs[model_id] = artifact
            self._summaries[model_id] = summary
            if content_hash is not None:
                # If a concurrent request beat us to it, the last writer wins;
                # the race is benign (both models work, the older one is just
                # not reachable via the index). See PLAN_model_load_dedup.md §6.3.
                self._content_hash_index[content_hash] = model_id

        return LoadResult(
            model_id=model_id,
            data_objects=summary.data_objects,
            dimensions=summary.dimensions,
            measures=summary.measures,
            metrics=summary.metrics,
            warnings=warnings,
            model_load="fresh",
            health=self._health_for(model),
        )

    def get_model(self, model_id: str) -> SemanticModel:
        """Look up a loaded model.  Raises ``KeyError`` if not found."""
        with self._lock:
            try:
                return self._models[model_id]
            except KeyError:
                raise KeyError(f"No model loaded with id '{model_id}'") from None

    def describe(self, model_id: str) -> ModelDescription:
        """Return a structured summary suitable for LLM consumption."""
        model = self.get_model(model_id)

        data_objects = [
            DataObjectInfo(
                label=obj.label,
                code=obj.qualified_code,
                columns=list(obj.columns.keys()),
                join_targets=[j.join_to for j in obj.joins],
                synonyms=obj.synonyms,
                owner=obj.owner,
            )
            for obj in model.data_objects.values()
        ]

        dimensions = [
            DimensionInfo(
                name=dim.label,
                result_type=dim.result_type.value,
                data_object=dim.view,
                column=dim.column,
                time_grain=dim.time_grain.value if dim.time_grain else None,
                synonyms=dim.synonyms,
                owner=dim.owner,
            )
            for dim in model.dimensions.values()
        ]

        measures = [
            MeasureInfo(
                name=m.label,
                result_type=m.result_type.value,
                aggregation=m.aggregation,
                expression=m.expression,
                synonyms=m.synonyms,
                owner=m.owner,
            )
            for m in model.measures.values()
        ]

        metrics = [
            MetricInfo(
                name=met.label,
                expression=met.expression,
                synonyms=met.synonyms,
                type=met.type.value,
                measure=met.measure,
                time_dimension=met.time_dimension,
                owner=met.owner,
            )
            for met in model.metrics.values()
        ]

        return ModelDescription(
            model_id=model_id,
            data_objects=data_objects,
            dimensions=dimensions,
            measures=measures,
            metrics=metrics,
        )

    def list_models(self) -> list[ModelSummary]:
        """Return a short summary for every loaded model."""
        with self._lock:
            return list(self._summaries.values())

    def remove_model(self, model_id: str) -> None:
        """Unload a model and its cached OBSL graph.  Raises ``KeyError`` if not found.

        Also removes the model's entry from the dedup index so the next load
        of the same OBML content runs fresh. PLAN_model_load_dedup.md §6.2.
        """
        with self._lock:
            try:
                del self._models[model_id]
            except KeyError:
                raise KeyError(f"No model loaded with id '{model_id}'") from None
            self._raws.pop(model_id, None)
            self._graphs.pop(model_id, None)
            self._summaries.pop(model_id, None)
            stale_hashes = [h for h, mid in self._content_hash_index.items() if mid == model_id]
            for h in stale_hashes:
                del self._content_hash_index[h]

    def compile_query(
        self,
        model_id: str,
        query: QueryObject,
        dialect: str,
    ) -> CompilationResult:
        """Compile a query against a loaded model."""
        model = self.get_model(model_id)
        return self._pipeline.compile(query, model, dialect)

    def refresh_contracts(self, model_id: str) -> dict[str, RefreshContract]:
        """Per-physical-table freshness contracts for the given model.

        Used by the result cache to derive an effective TTL for a query
        based on the dataObjects it touched.
        """
        from orionbelt.cache.contracts import collect_table_contracts

        model = self.get_model(model_id)
        contracts, _ = collect_table_contracts(model)
        return contracts

    def validate(
        self,
        yaml_str: str | None = None,
        *,
        raw_dict: dict[str, object] | None = None,
        extends_yaml: list[str] | None = None,
        inherits_model_id: str | None = None,
    ) -> ValidationSummary:
        """Validate a model without storing it.  Accepts YAML string or raw dict."""
        _model, _raw, errors, warnings = self._parse_and_validate(
            yaml_str,
            raw_dict=raw_dict,
            extends_yaml=extends_yaml,
            inherits_model_id=inherits_model_id,
        )
        return ValidationSummary(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    # -- OBSL graph ---------------------------------------------------------

    def get_graph(self, model_id: str) -> GraphArtifact:
        """Return the cached OBSL graph for a model.  Raises ``KeyError`` if not found."""
        with self._lock:
            try:
                return self._graphs[model_id]
            except KeyError:
                raise KeyError(f"No graph for model '{model_id}'") from None

    def query_graph(self, model_id: str, sparql: str) -> SPARQLResult:
        """Execute a read-only SPARQL query against a model's OBSL graph."""
        artifact = self.get_graph(model_id)
        return execute_sparql(artifact.graph, sparql)
