"""Model discovery endpoints: schema, dimensions, measures, metrics, explain, find, join-graph.

Session-scoped routes under /sessions/{session_id}/models/{model_id}/.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from orionbelt.api.deps import get_session_manager
from orionbelt.api.schemas import (
    ColumnDetail,
    DataObjectDetail,
    DimensionDetail,
    ExampleDetail,
    ExampleListResponse,
    ExampleSummary,
    ExplainLineageItem,
    ExplainResponse,
    JoinEdge,
    JoinGraphResponse,
    MeasureDetail,
    MetricDetail,
    ModelFilterDetail,
    SchemaResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import (
    SessionExpiredError,
    SessionManager,
    SessionNotFoundError,
)

router = APIRouter()


# -- helpers -----------------------------------------------------------------


def _get_store(session_id: str, mgr: SessionManager) -> ModelStore:
    try:
        return mgr.get_store(session_id)
    except SessionExpiredError:
        raise HTTPException(status_code=410, detail=f"Session '{session_id}' has expired") from None
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None


def _get_model(session_id: str, model_id: str, mgr: SessionManager) -> SemanticModel:
    store = _get_store(session_id, mgr)
    try:
        return store.get_model(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None


def _build_schema(model_id: str, model: SemanticModel) -> SchemaResponse:
    """Build a SchemaResponse from a SemanticModel."""
    data_objects = []
    for name, obj in model.data_objects.items():
        cols = [
            ColumnDetail(
                name=col_name,
                code=col.code,
                abstract_type=col.abstract_type.value,
                num_class=col.num_class.value if col.num_class else None,
                description=col.description,
                comment=col.comment,
                owner=col.owner,
                synonyms=col.synonyms,
            )
            for col_name, col in obj.columns.items()
        ]
        data_objects.append(
            DataObjectDetail(
                name=name,
                code=obj.code,
                database=obj.database,
                schema_name=obj.schema_name,
                columns=cols,
                join_targets=[j.join_to for j in obj.joins],
                description=obj.description,
                comment=obj.comment,
                owner=obj.owner,
                synonyms=obj.synonyms,
            )
        )

    dimensions = [
        DimensionDetail(
            name=name,
            data_object=dim.view,
            column=dim.column,
            result_type=dim.result_type.value,
            time_grain=dim.time_grain.value if dim.time_grain else None,
            via=dim.via,
            description=dim.description,
            format=dim.format,
            owner=dim.owner,
            synonyms=dim.synonyms,
        )
        for name, dim in model.dimensions.items()
    ]

    measures = [
        MeasureDetail(
            name=name,
            result_type=m.result_type.value,
            aggregation=m.aggregation,
            expression=m.expression,
            columns=[{"dataObject": c.view or "", "column": c.column or ""} for c in m.columns],
            distinct=m.distinct,
            total=m.total,
            description=m.description,
            format=m.format,
            data_type=m.data_type,
            owner=m.owner,
            synonyms=m.synonyms,
        )
        for name, m in model.effective_measures.items()
    ]

    metrics = []
    for name, met in model.metrics.items():
        component_names = re.findall(r"\{\[([^\]]+)\]\}", met.expression or "")
        metrics.append(
            MetricDetail(
                name=name,
                type=met.type.value,
                expression=met.expression,
                measure=met.measure,
                time_dimension=met.time_dimension,
                component_measures=component_names,
                description=met.description,
                format=met.format,
                data_type=met.data_type,
                owner=met.owner,
                synonyms=met.synonyms,
            )
        )

    filters = [
        ModelFilterDetail(
            data_object=f.data_object,
            column=f.column,
            operator=f.operator,
            value=f.value,
            values=f.values,
        )
        for f in model.filters
    ]

    return SchemaResponse(
        model_id=model_id,
        version=model.version,
        description=model.description,
        owner=model.owner,
        data_objects=data_objects,
        dimensions=dimensions,
        measures=measures,
        metrics=metrics,
        filters=filters,
        extends=model.extends_sources,
        inherits=model.inherits_source,
    )


def _build_explain(name: str, model: SemanticModel) -> ExplainResponse:
    """Build lineage for a dimension, measure, or metric."""
    # Check dimensions
    if name in model.dimensions:
        dim = model.dimensions[name]
        lineage: list[ExplainLineageItem] = [
            ExplainLineageItem(type="dimension", name=name, detail=f"type={dim.result_type.value}"),
            ExplainLineageItem(
                type="column",
                name=f"{dim.view}.{dim.column}",
                detail=f"from data object '{dim.view}'",
            ),
        ]
        obj = model.data_objects.get(dim.view)
        if obj:
            lineage.append(
                ExplainLineageItem(
                    type="data_object", name=dim.view, detail=f"table={obj.qualified_code}"
                )
            )
        return ExplainResponse(name=name, type="dimension", lineage=lineage)

    # Check measures
    if name in model.effective_measures:
        m = model.effective_measures[name]
        lineage = [
            ExplainLineageItem(
                type="measure",
                name=name,
                detail=f"aggregation={m.aggregation}, type={m.result_type.value}",
            ),
        ]
        if m.expression:
            lineage.append(
                ExplainLineageItem(type="expression", name=m.expression, detail="measure formula")
            )
            col_refs = re.findall(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}", m.expression)
            for obj_name, col_name in col_refs:
                lineage.append(
                    ExplainLineageItem(
                        type="column",
                        name=f"{obj_name}.{col_name}",
                        detail="referenced in expression",
                    )
                )
        for c in m.columns:
            obj_name = c.view or ""
            col_name = c.column or ""
            lineage.append(
                ExplainLineageItem(
                    type="column", name=f"{obj_name}.{col_name}", detail="source column"
                )
            )
            obj = model.data_objects.get(obj_name)
            if obj:
                lineage.append(
                    ExplainLineageItem(
                        type="data_object", name=obj_name, detail=f"table={obj.qualified_code}"
                    )
                )
        return ExplainResponse(name=name, type="measure", lineage=lineage)

    # Check metrics
    if name in model.metrics:
        met = model.metrics[name]
        lineage = [
            ExplainLineageItem(type="metric", name=name, detail="composite metric"),
            ExplainLineageItem(
                type="expression",
                name=met.expression or f"cumulative({met.measure})",
                detail="metric formula",
            ),
        ]
        component_names = re.findall(r"\{\[([^\]]+)\]\}", met.expression or "")
        for comp_name in component_names:
            comp = model.effective_measures.get(comp_name)
            if comp:
                lineage.append(
                    ExplainLineageItem(
                        type="measure",
                        name=comp_name,
                        detail=f"aggregation={comp.aggregation}",
                    )
                )
        return ExplainResponse(name=name, type="metric", lineage=lineage)

    raise HTTPException(status_code=404, detail=f"'{name}' not found in model")


def _search_model(model: SemanticModel, query: str, types: list[str]) -> list[SearchResultItem]:
    """Search across model artefacts by name/synonym (legacy flat results)."""
    exact, synonym, _ = _search_model_split(model, query, types)
    return exact + synonym


def _search_model_split(
    model: SemanticModel,
    query: str,
    types: list[str],
) -> tuple[list[SearchResultItem], list[SearchResultItem], list[tuple[str, str, list[str]]]]:
    """Search and return (exact, synonym, fuzzy_candidates).

    ``fuzzy_candidates`` is the full ``(name, kind, synonyms)`` corpus across
    the requested ``types`` so callers can fall back to fuzzy matching when
    both exact and synonym lists are empty.
    """
    exact: list[SearchResultItem] = []
    synonym: list[SearchResultItem] = []
    fuzzy_candidates: list[tuple[str, str, list[str]]] = []
    q = query.lower()

    def _consider(name: str, kind: str, synonyms: list[str]) -> None:
        if q in name.lower():
            exact.append(SearchResultItem(type=kind, name=name, match_field="name"))
        elif any(q in s.lower() for s in synonyms):
            synonym.append(SearchResultItem(type=kind, name=name, match_field="synonym"))
        fuzzy_candidates.append((name, kind, list(synonyms)))

    if "dimension" in types:
        for name, dim in model.dimensions.items():
            _consider(name, "dimension", list(dim.synonyms))

    if "measure" in types:
        for name, m in model.effective_measures.items():
            _consider(name, "measure", list(m.synonyms))

    if "metric" in types:
        for name, met in model.metrics.items():
            _consider(name, "metric", list(met.synonyms))

    if "data_object" in types:
        for name, obj in model.data_objects.items():
            _consider(name, "data_object", list(obj.synonyms))

    return exact, synonym, fuzzy_candidates


def _build_search_response(model: SemanticModel, query: str, types: list[str]) -> SearchResponse:
    """Run /find with split exact/synonym buckets and fuzzy fallback."""
    from orionbelt.api.schemas import FuzzyMatch as ApiFuzzyMatch
    from orionbelt.service.fuzzy import fuzzy_search

    exact, synonym, candidates = _search_model_split(model, query, types)
    fuzzy: list[ApiFuzzyMatch] = []
    if not exact and not synonym and query.strip():
        for m in fuzzy_search(query, candidates):
            fuzzy.append(ApiFuzzyMatch(name=m.name, kind=m.kind, score=m.score, reason=m.reason))
    return SearchResponse(
        query=query,
        results=exact + synonym,
        exact_matches=exact,
        synonym_matches=synonym,
        fuzzy_matches=fuzzy,
    )


def _build_join_graph(model: SemanticModel) -> JoinGraphResponse:
    """Build adjacency list from model joins."""
    nodes = list(model.data_objects.keys())
    edges: list[JoinEdge] = []
    for obj_name, obj in model.data_objects.items():
        for join in obj.joins:
            edges.append(
                JoinEdge(
                    from_object=obj_name,
                    to_object=join.join_to,
                    cardinality=join.join_type.value,
                    columns_from=join.columns_from,
                    columns_to=join.columns_to,
                    secondary=join.secondary,
                    path_name=join.path_name,
                )
            )
    return JoinGraphResponse(nodes=nodes, edges=edges)


# -- session-scoped endpoints -----------------------------------------------


@router.get(
    "/{session_id}/models/{model_id}/schema",
    response_model=SchemaResponse,
    tags=["model-discovery"],
)
async def get_schema(
    session_id: str,
    model_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SchemaResponse:
    """Return full model structure as JSON."""
    model = _get_model(session_id, model_id, mgr)
    return _build_schema(model_id, model)


@router.get(
    "/{session_id}/models/{model_id}/dimensions",
    response_model=list[DimensionDetail],
    tags=["model-discovery"],
)
async def list_dimensions(
    session_id: str,
    model_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> list[DimensionDetail]:
    """List all dimensions in a model."""
    model = _get_model(session_id, model_id, mgr)
    return _build_schema(model_id, model).dimensions


@router.get(
    "/{session_id}/models/{model_id}/dimensions/{name}",
    response_model=DimensionDetail,
    tags=["model-discovery"],
)
async def get_dimension(
    session_id: str,
    model_id: str,
    name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> DimensionDetail:
    """Get a single dimension by name."""
    model = _get_model(session_id, model_id, mgr)
    dim = model.dimensions.get(name)
    if not dim:
        raise HTTPException(status_code=404, detail=f"Dimension '{name}' not found")
    return DimensionDetail(
        name=name,
        data_object=dim.view,
        column=dim.column,
        result_type=dim.result_type.value,
        time_grain=dim.time_grain.value if dim.time_grain else None,
        via=dim.via,
        description=dim.description,
        format=dim.format,
        owner=dim.owner,
        synonyms=dim.synonyms,
    )


@router.get(
    "/{session_id}/models/{model_id}/measures",
    response_model=list[MeasureDetail],
    tags=["model-discovery"],
)
async def list_measures(
    session_id: str,
    model_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> list[MeasureDetail]:
    """List all measures in a model."""
    model = _get_model(session_id, model_id, mgr)
    return _build_schema(model_id, model).measures


@router.get(
    "/{session_id}/models/{model_id}/measures/{name}",
    response_model=MeasureDetail,
    tags=["model-discovery"],
)
async def get_measure(
    session_id: str,
    model_id: str,
    name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> MeasureDetail:
    """Get a single measure by name."""
    model = _get_model(session_id, model_id, mgr)
    m = model.effective_measures.get(name)
    if not m:
        raise HTTPException(status_code=404, detail=f"Measure '{name}' not found")
    return MeasureDetail(
        name=name,
        result_type=m.result_type.value,
        aggregation=m.aggregation,
        expression=m.expression,
        columns=[{"dataObject": c.view or "", "column": c.column or ""} for c in m.columns],
        distinct=m.distinct,
        total=m.total,
        description=m.description,
        format=m.format,
        data_type=m.data_type,
        owner=m.owner,
        synonyms=m.synonyms,
    )


@router.get(
    "/{session_id}/models/{model_id}/metrics",
    response_model=list[MetricDetail],
    tags=["model-discovery"],
)
async def list_metrics(
    session_id: str,
    model_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> list[MetricDetail]:
    """List all metrics in a model."""
    model = _get_model(session_id, model_id, mgr)
    return _build_schema(model_id, model).metrics


@router.get(
    "/{session_id}/models/{model_id}/metrics/{name}",
    response_model=MetricDetail,
    tags=["model-discovery"],
)
async def get_metric(
    session_id: str,
    model_id: str,
    name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> MetricDetail:
    """Get a single metric by name."""
    model = _get_model(session_id, model_id, mgr)
    met = model.metrics.get(name)
    if not met:
        raise HTTPException(status_code=404, detail=f"Metric '{name}' not found")
    component_names = re.findall(r"\{\[([^\]]+)\]\}", met.expression or "")
    return MetricDetail(
        name=name,
        type=met.type.value,
        expression=met.expression,
        measure=met.measure,
        time_dimension=met.time_dimension,
        component_measures=component_names,
        description=met.description,
        format=met.format,
        data_type=met.data_type,
        owner=met.owner,
        synonyms=met.synonyms,
    )


@router.get(
    "/{session_id}/models/{model_id}/explain/{name}",
    response_model=ExplainResponse,
    tags=["model-discovery"],
)
async def explain_artefact(
    session_id: str,
    model_id: str,
    name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ExplainResponse:
    """Explain the lineage of a dimension, measure, or metric."""
    model = _get_model(session_id, model_id, mgr)
    return _build_explain(name, model)


@router.post(
    "/{session_id}/models/{model_id}/find",
    response_model=SearchResponse,
    tags=["model-discovery"],
)
async def find_artefacts(
    session_id: str,
    model_id: str,
    body: SearchRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SearchResponse:
    """Search across model artefacts by name or synonym.

    When the search produces zero exact or synonym matches, fuzzy fallback
    (Levenshtein + trigram overlap) returns near-miss candidates with scores.
    See ``design/PLAN_agent_api_improvements.md`` §4.
    """
    model = _get_model(session_id, model_id, mgr)
    return _build_search_response(model, body.query, body.types)


@router.get(
    "/{session_id}/models/{model_id}/join-graph",
    response_model=JoinGraphResponse,
    tags=["model-discovery"],
)
async def get_join_graph(
    session_id: str,
    model_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> JoinGraphResponse:
    """Return the join graph as an adjacency list."""
    model = _get_model(session_id, model_id, mgr)
    return _build_join_graph(model)


# -- examples ----------------------------------------------------------------


def _all_intent_tags(model: SemanticModel) -> list[str]:
    """All distinct intent tags across the model's examples."""
    tags: set[str] = set()
    for ex in model.examples:
        for t in ex.intent_tags:
            tags.add(t)
    return sorted(tags)


def _filter_by_intent(model: SemanticModel, intent: str) -> list[ExampleSummary]:
    """Return examples whose tags match ``intent`` (case-insensitive)."""
    from orionbelt.service.fuzzy import fuzzy_search

    target = intent.lower().strip()
    direct: list[ExampleSummary] = []
    for ex in model.examples:
        if any(target == t.lower() for t in ex.intent_tags):
            direct.append(
                ExampleSummary(name=ex.name, description=ex.description, intent_tags=ex.intent_tags)
            )
    if direct:
        return direct
    # Fuzzy fallback: a partial substring still wins (e.g. "rev" matches "revenue")
    contains: list[ExampleSummary] = []
    for ex in model.examples:
        if any(target in t.lower() for t in ex.intent_tags):
            contains.append(
                ExampleSummary(name=ex.name, description=ex.description, intent_tags=ex.intent_tags)
            )
    if contains:
        return contains
    # Final fallback: fuzzy match against the tag corpus
    candidates: list[tuple[str, str, list[str]]] = [(t, "tag", []) for t in _all_intent_tags(model)]
    fuzzy = {m.name for m in fuzzy_search(intent, candidates, threshold=0.6)}
    if not fuzzy:
        return []
    return [
        ExampleSummary(name=ex.name, description=ex.description, intent_tags=ex.intent_tags)
        for ex in model.examples
        if any(t in fuzzy for t in ex.intent_tags)
    ]


@router.get(
    "/{session_id}/models/{model_id}/examples",
    response_model=ExampleListResponse,
    tags=["model-discovery"],
)
async def list_examples(
    session_id: str,
    model_id: str,
    intent: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ExampleListResponse:
    """List canonical example queries authored alongside the model.

    See ``design/PLAN_agent_api_improvements.md`` §5.
    """
    model = _get_model(session_id, model_id, mgr)
    if intent:
        matches = _filter_by_intent(model, intent)
        if not matches:
            tags = _all_intent_tags(model)
            suggestion = (
                f"no examples for '{intent}'; available tags: {', '.join(tags)}"
                if tags
                else f"no examples for '{intent}'; the model has no intent_tags defined"
            )
            return ExampleListResponse(examples=[], suggestion=suggestion)
        return ExampleListResponse(examples=matches)
    summaries = [
        ExampleSummary(name=ex.name, description=ex.description, intent_tags=ex.intent_tags)
        for ex in model.examples
    ]
    return ExampleListResponse(examples=summaries)


@router.get(
    "/{session_id}/models/{model_id}/examples/{example_name}",
    response_model=ExampleDetail,
    tags=["model-discovery"],
)
async def get_example(
    session_id: str,
    model_id: str,
    example_name: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ExampleDetail:
    """Return a single example by name, with an optional compiled SQL preview."""
    store = _get_store(session_id, mgr)
    model = _get_model(session_id, model_id, mgr)
    example = next((e for e in model.examples if e.name == example_name), None)
    if example is None:
        raise HTTPException(status_code=404, detail=f"Example '{example_name}' not found in model")

    compiled_sql_preview: str | None = None
    try:
        from orionbelt.api.deps import get_db_vendor
        from orionbelt.api.routers.sessions import _resolve_dialect
        from orionbelt.compiler.validator import format_sql
        from orionbelt.models.query import QueryObject

        dialect = _resolve_dialect(request_dialect=None, model=model, fallback=get_db_vendor())
        query_obj = QueryObject.model_validate(example.query)
        result = store.compile_query(model_id, query_obj, dialect)
        compiled_sql_preview = format_sql(result.sql, result.dialect)
    except Exception:
        compiled_sql_preview = None

    return ExampleDetail(
        name=example.name,
        description=example.description,
        intent_tags=list(example.intent_tags),
        query=dict(example.query),
        compiled_sql_preview=compiled_sql_preview,
    )
