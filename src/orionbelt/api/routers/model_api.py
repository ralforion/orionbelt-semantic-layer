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
            owner=m.owner,
            synonyms=m.synonyms,
        )
        for name, m in model.measures.items()
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
    if name in model.measures:
        m = model.measures[name]
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
            comp = model.measures.get(comp_name)
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
    """Search across model artefacts by name/synonym."""
    results: list[SearchResultItem] = []
    q = query.lower()

    if "dimension" in types:
        for name, dim in model.dimensions.items():
            if q in name.lower():
                results.append(SearchResultItem(type="dimension", name=name, match_field="name"))
            elif any(q in s.lower() for s in dim.synonyms):
                results.append(SearchResultItem(type="dimension", name=name, match_field="synonym"))

    if "measure" in types:
        for name, m in model.measures.items():
            if q in name.lower():
                results.append(SearchResultItem(type="measure", name=name, match_field="name"))
            elif any(q in s.lower() for s in m.synonyms):
                results.append(SearchResultItem(type="measure", name=name, match_field="synonym"))

    if "metric" in types:
        for name, met in model.metrics.items():
            if q in name.lower():
                results.append(SearchResultItem(type="metric", name=name, match_field="name"))
            elif any(q in s.lower() for s in met.synonyms):
                results.append(SearchResultItem(type="metric", name=name, match_field="synonym"))

    if "data_object" in types:
        for name, obj in model.data_objects.items():
            if q in name.lower():
                results.append(SearchResultItem(type="data_object", name=name, match_field="name"))
            elif any(q in s.lower() for s in obj.synonyms):
                results.append(
                    SearchResultItem(type="data_object", name=name, match_field="synonym")
                )

    return results


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
    m = model.measures.get(name)
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
        format=met.format,
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
    """Search across model artefacts by name or synonym."""
    model = _get_model(session_id, model_id, mgr)
    results = _search_model(model, body.query, body.types)
    return SearchResponse(results=results)


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
