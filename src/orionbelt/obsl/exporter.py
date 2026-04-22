"""OBSL-Core 0.1 exporter — SemanticModel → RDF graph.

Produces an in-memory rdflib.Graph that represents the full OBSL-Core 0.1
graph for a loaded semantic model.  Expression strings are preserved as
``obsl:expressionSource`` literals; structured ASTs are out of scope for Core.
"""

from __future__ import annotations

import re

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from orionbelt.models.semantic import (
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    MetricType,
    SemanticModel,
)

# ---------------------------------------------------------------------------
# OBSL namespace and URI helpers
# ---------------------------------------------------------------------------

OBSL = Namespace("https://ralforion.com/ns/obsl#")
BASE = "https://ralforion.com/ns/model/"


def _slug(name: str) -> str:
    """Convert a display name to a URL-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _model_uri(model_id: str) -> URIRef:
    return URIRef(f"{BASE}{model_id}")


def _data_object_uri(model_id: str, name: str) -> URIRef:
    return URIRef(f"{BASE}{model_id}/data-object/{_slug(name)}")


def _column_uri(model_id: str, obj_name: str, col_name: str) -> URIRef:
    return URIRef(f"{BASE}{model_id}/data-object/{_slug(obj_name)}/column/{_slug(col_name)}")


def _join_uri(
    model_id: str,
    obj_name: str,
    target_name: str,
    path_name: str | None = None,
) -> URIRef:
    base = f"{BASE}{model_id}/join/{_slug(obj_name)}-to-{_slug(target_name)}"
    if path_name:
        base = f"{base}/{_slug(path_name)}"
    return URIRef(base)


def _dimension_uri(model_id: str, name: str) -> URIRef:
    return URIRef(f"{BASE}{model_id}/dimension/{_slug(name)}")


def _measure_uri(model_id: str, name: str) -> URIRef:
    return URIRef(f"{BASE}{model_id}/measure/{_slug(name)}")


def _metric_uri(model_id: str, name: str) -> URIRef:
    return URIRef(f"{BASE}{model_id}/metric/{_slug(name)}")


# ---------------------------------------------------------------------------
# RDF helpers
# ---------------------------------------------------------------------------


def _rdf_list(g: Graph, items: list[URIRef]) -> BNode:
    """Build an RDF collection (linked list) and return its head node."""
    if not items:
        return RDF.nil  # type: ignore[return-value]
    head = BNode()
    node = head
    for i, item in enumerate(items):
        g.add((node, RDF.first, item))
        if i < len(items) - 1:
            nxt = BNode()
            g.add((node, RDF.rest, nxt))
            node = nxt
        else:
            g.add((node, RDF.rest, RDF.nil))
    return head


# ---------------------------------------------------------------------------
# Measure filter → expression string
# ---------------------------------------------------------------------------


def _format_filter_value(fv: object) -> str:
    """Format a FilterValue as a readable literal."""
    from orionbelt.models.semantic import FilterValue

    if not isinstance(fv, FilterValue):
        return str(fv)
    if fv.is_null:
        return "NULL"
    for attr in ("value_string", "value_date"):
        v = getattr(fv, attr, None)
        if v is not None:
            return f"'{v}'"
    for attr in ("value_int", "value_float", "value_boolean"):
        v = getattr(fv, attr, None)
        if v is not None:
            return str(v)
    return "NULL"


def _serialize_filter_item(item: MeasureFilterItem) -> str:
    """Serialize a single MeasureFilter or MeasureFilterGroup to a string."""
    if isinstance(item, MeasureFilter):
        col_ref = ""
        if item.column and item.column.view and item.column.column:
            col_ref = f"{item.column.view}.{item.column.column}"
        elif item.column and item.column.column:
            col_ref = item.column.column
        vals = [_format_filter_value(v) for v in item.values]
        op = item.operator
        if len(vals) == 1:
            return f"{col_ref} {op} {vals[0]}"
        return f"{col_ref} {op} ({', '.join(vals)})"
    if isinstance(item, MeasureFilterGroup):
        parts = [_serialize_filter_item(f) for f in item.filters]
        joiner = f" {item.logic.value.upper()} "
        expr = joiner.join(parts)
        if len(parts) > 1:
            expr = f"({expr})"
        if item.negated:
            expr = f"NOT {expr}"
        return expr
    return str(item)


def _serialize_measure_filters(filters: list[MeasureFilterItem]) -> str | None:
    """Serialize a measure's filter list to a human-readable expression string."""
    if not filters:
        return None
    parts = [_serialize_filter_item(f) for f in filters]
    if len(parts) == 1:
        return parts[0]
    return " AND ".join(parts)


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


def export_obsl(model: SemanticModel, model_id: str) -> Graph:
    """Export a ``SemanticModel`` as an OBSL-Core 0.1 RDF graph.

    Parameters
    ----------
    model:
        Resolved semantic model.
    model_id:
        Short identifier used to build stable URIs.

    Returns
    -------
    rdflib.Graph
        In-memory RDF graph containing all OBSL-Core triples.
    """
    g = Graph()
    g.bind("obsl", OBSL)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)

    # Embed obsl: class declarations so the graph is self-contained.
    core_classes = (
        OBSL.SemanticModel,
        OBSL.DataObject,
        OBSL.Column,
        OBSL.Join,
        OBSL.Dimension,
        OBSL.Measure,
        OBSL.Metric,
    )
    for cls, label in (
        (OBSL.SemanticModel, "Semantic Model"),
        (OBSL.DataObject, "Data Object"),
        (OBSL.Column, "Column"),
        (OBSL.Join, "Join"),
        (OBSL.Dimension, "Dimension"),
        (OBSL.Measure, "Measure"),
        (OBSL.Metric, "Metric"),
        (OBSL.CumulativeMetric, "Cumulative Metric"),
        (OBSL.PeriodOverPeriodMetric, "Period-over-Period Metric"),
    ):
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal(label)))
    g.add((OBSL.CumulativeMetric, RDFS.subClassOf, OBSL.Metric))
    g.add((OBSL.PeriodOverPeriodMetric, RDFS.subClassOf, OBSL.Metric))

    # -- OWL axioms ------------------------------------------------------------

    # Class disjointness — core classes are mutually exclusive.
    disjoint_bnode = BNode()
    g.add((disjoint_bnode, RDF.type, OWL.AllDisjointClasses))
    member_list = _rdf_list(g, list(core_classes))
    g.add((disjoint_bnode, OWL.members, member_list))

    # Metric subtypes are disjoint with each other.
    g.add((OBSL.CumulativeMetric, OWL.disjointWith, OBSL.PeriodOverPeriodMetric))

    # Functional properties — at most one value.
    for prop in (
        OBSL.joinTo,
        OBSL.code,
        OBSL.database,
        OBSL["schema"],
        OBSL.physicalName,
        OBSL.resultType,
        OBSL.aggregation,
        OBSL.metricType,
        OBSL.cardinality,
        OBSL.expressionSource,
        OBSL.filterExpression,
        OBSL.dataObject,
        OBSL.column,
        OBSL.grainMode,
        OBSL.filterContextMode,
        OBSL.cumulativeType,
        OBSL.window,
        OBSL.grainToDate,
        OBSL.offset,
        OBSL.offsetGrain,
        OBSL.comparison,
    ):
        g.add((prop, RDF.type, OWL.FunctionalProperty))

    # Inverse property — belongsToModel ↔ hasDataObject.
    g.add((OBSL.belongsToModel, RDF.type, OWL.ObjectProperty))
    g.add((OBSL.belongsToModel, OWL.inverseOf, OBSL.hasDataObject))

    # Object properties with domain/range — connects classes in visualisation.
    for prop, domain, range_ in (
        (OBSL.hasDataObject, OBSL.SemanticModel, OBSL.DataObject),
        (OBSL.hasDimension, OBSL.SemanticModel, OBSL.Dimension),
        (OBSL.hasMeasure, OBSL.SemanticModel, OBSL.Measure),
        (OBSL.hasMetric, OBSL.SemanticModel, OBSL.Metric),
        (OBSL.hasColumn, OBSL.DataObject, OBSL.Column),
        (OBSL.hasJoin, OBSL.DataObject, OBSL.Join),
        (OBSL.joinTo, OBSL.Join, OBSL.DataObject),
        (OBSL.columnFrom, OBSL.Join, OBSL.Column),
        (OBSL.columnTo, OBSL.Join, OBSL.Column),
        (OBSL.dataObject, OBSL.Dimension, OBSL.DataObject),
        (OBSL.column, OBSL.Dimension, OBSL.Column),
        (OBSL.sourceColumn, OBSL.Measure, OBSL.Column),
        (OBSL.baseMeasure, OBSL.Metric, OBSL.Measure),
        (OBSL.referencesMeasure, OBSL.Metric, OBSL.Measure),
        (OBSL.timeDimension, OBSL.Metric, OBSL.Dimension),
    ):
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.domain, domain))
        g.add((prop, RDFS.range, range_))

    # Datatype properties
    for prop in (
        OBSL.code,
        OBSL.database,
        OBSL["schema"],
        OBSL.resultType,
        OBSL.aggregation,
        OBSL.metricType,
        OBSL.cardinality,
        OBSL.timeGrain,
        OBSL.expressionSource,
        OBSL.filterExpression,
        OBSL.pathName,
        OBSL.synonym,
        OBSL.secondary,
        OBSL.distinct,
        OBSL.total,
        OBSL.allowFanOut,
        OBSL.grainMode,
        OBSL.grainExclude,
        OBSL.grainInclude,
        OBSL.grainKeepOnly,
        OBSL.filterContextMode,
        OBSL.filterContextExclude,
        OBSL.filterContextKeepOnly,
        OBSL.filterContextInclude,
        OBSL.owner,
        OBSL.dataType,
        OBSL["format"],
        OBSL.cumulativeType,
        OBSL.window,
        OBSL.grainToDate,
        OBSL.offset,
        OBSL.offsetGrain,
        OBSL.comparison,
    ):
        g.add((prop, RDF.type, OWL.DatatypeProperty))

    m_uri = _model_uri(model_id)

    # -- Semantic Model container -------------------------------------------
    g.add((m_uri, RDF.type, OBSL.SemanticModel))
    g.add((m_uri, RDF.type, OWL.NamedIndividual))
    g.add((m_uri, RDF.type, OWL.Ontology))
    g.add((m_uri, OWL.imports, URIRef("https://ralforion.com/ns/obsl#")))
    if model.description:
        g.add((m_uri, RDFS.comment, Literal(model.description)))

    # Pre-build column URI lookup for ALL data objects (needed for joins,
    # dimensions, measures).  This must happen before any join export so that
    # target columns are always resolvable regardless of declaration order.
    col_uris: dict[tuple[str, str], URIRef] = {}
    for obj_name, obj in model.data_objects.items():
        for col_name in obj.columns:
            col_uris[(obj_name, col_name)] = _column_uri(model_id, obj_name, col_name)

    # -- Data Objects -------------------------------------------------------
    for obj_name, obj in model.data_objects.items():
        obj_uri = _data_object_uri(model_id, obj_name)
        g.add((m_uri, OBSL.hasDataObject, obj_uri))
        g.add((obj_uri, RDF.type, OBSL.DataObject))
        g.add((obj_uri, RDF.type, OWL.NamedIndividual))
        g.add((obj_uri, RDFS.label, Literal(obj_name)))
        g.add((obj_uri, OBSL.code, Literal(obj.code)))
        g.add((obj_uri, OBSL.database, Literal(obj.database)))
        g.add((obj_uri, OBSL["schema"], Literal(obj.schema_name)))

        if obj.description:
            g.add((obj_uri, RDFS.comment, Literal(obj.description)))
        if obj.owner:
            g.add((obj_uri, OBSL.owner, Literal(obj.owner)))
        for syn in obj.synonyms:
            g.add((obj_uri, OBSL.synonym, Literal(syn)))

        # Columns
        for col_name, col in obj.columns.items():
            col_uri = col_uris[(obj_name, col_name)]
            g.add((obj_uri, OBSL.hasColumn, col_uri))
            g.add((col_uri, RDF.type, OBSL.Column))
            g.add((col_uri, RDF.type, OWL.NamedIndividual))
            g.add((col_uri, RDFS.label, Literal(col_name)))
            g.add((col_uri, OBSL.code, Literal(col.code)))
            g.add((col_uri, OBSL.resultType, Literal(col.abstract_type.value)))

            if col.description:
                g.add((col_uri, RDFS.comment, Literal(col.description)))
            if col.owner:
                g.add((col_uri, OBSL.owner, Literal(col.owner)))
            for syn in col.synonyms:
                g.add((col_uri, OBSL.synonym, Literal(syn)))

        # Joins — path_name disambiguates multiple joins to the same target
        for join in obj.joins:
            join_uri = _join_uri(model_id, obj_name, join.join_to, join.path_name)
            g.add((obj_uri, OBSL.hasJoin, join_uri))
            g.add((join_uri, RDF.type, OBSL.Join))
            g.add((join_uri, RDF.type, OWL.NamedIndividual))
            g.add((join_uri, RDFS.label, Literal(f"{obj_name} → {join.join_to}")))

            target_uri = _data_object_uri(model_id, join.join_to)
            g.add((join_uri, OBSL.joinTo, target_uri))
            g.add((join_uri, OBSL.cardinality, Literal(join.join_type.value)))

            for col_from in join.columns_from:
                cf_uri = col_uris.get((obj_name, col_from))
                if cf_uri:
                    g.add((join_uri, OBSL.columnFrom, cf_uri))

            for col_to in join.columns_to:
                ct_uri = col_uris.get((join.join_to, col_to))
                if ct_uri:
                    g.add((join_uri, OBSL.columnTo, ct_uri))

            if join.secondary:
                g.add((join_uri, OBSL.secondary, Literal(True)))
            if join.path_name:
                g.add((join_uri, OBSL.pathName, Literal(join.path_name)))

    # -- Dimensions ---------------------------------------------------------
    for dim_name, dim in model.dimensions.items():
        dim_uri = _dimension_uri(model_id, dim_name)
        g.add((m_uri, OBSL.hasDimension, dim_uri))
        g.add((dim_uri, RDF.type, OBSL.Dimension))
        g.add((dim_uri, RDF.type, OWL.NamedIndividual))
        g.add((dim_uri, RDFS.label, Literal(dim_name)))
        g.add((dim_uri, OBSL.resultType, Literal(dim.result_type.value)))

        obj_uri = _data_object_uri(model_id, dim.view)
        g.add((dim_uri, OBSL.dataObject, obj_uri))

        dim_col = col_uris.get((dim.view, dim.column))
        if dim_col:
            g.add((dim_uri, OBSL.column, dim_col))

        if dim.time_grain:
            g.add((dim_uri, OBSL.timeGrain, Literal(dim.time_grain.value)))
        if dim.description:
            g.add((dim_uri, RDFS.comment, Literal(dim.description)))
        if dim.owner:
            g.add((dim_uri, OBSL.owner, Literal(dim.owner)))
        if dim.format:
            g.add((dim_uri, OBSL["format"], Literal(dim.format)))
        for syn in dim.synonyms:
            g.add((dim_uri, OBSL.synonym, Literal(syn)))

    # -- Measures -----------------------------------------------------------
    for meas_name, meas in model.measures.items():
        meas_uri = _measure_uri(model_id, meas_name)
        g.add((m_uri, OBSL.hasMeasure, meas_uri))
        g.add((meas_uri, RDF.type, OBSL.Measure))
        g.add((meas_uri, RDF.type, OWL.NamedIndividual))
        g.add((meas_uri, RDFS.label, Literal(meas_name)))
        g.add((meas_uri, OBSL.aggregation, Literal(meas.aggregation)))
        g.add((meas_uri, OBSL.resultType, Literal(meas.result_type.value)))

        # Source columns
        for ref in meas.columns:
            if ref.view and ref.column:
                src_col = col_uris.get((ref.view, ref.column))
                if src_col:
                    g.add((meas_uri, OBSL.sourceColumn, src_col))

        # Expression string
        if meas.expression:
            g.add((meas_uri, OBSL.expressionSource, Literal(meas.expression)))

        # Boolean flags (only emit when True)
        if meas.distinct:
            g.add((meas_uri, OBSL.distinct, Literal(True)))
        if meas.total:
            g.add((meas_uri, OBSL.total, Literal(True)))
        if meas.allow_fan_out:
            g.add((meas_uri, OBSL.allowFanOut, Literal(True)))

        # Filter expression
        filter_expr = _serialize_measure_filters(meas.filters)
        if filter_expr:
            g.add((meas_uri, OBSL.filterExpression, Literal(filter_expr)))

        # Grain override
        if meas.grain:
            go = meas.grain
            g.add((meas_uri, OBSL.grainMode, Literal(go.mode.value)))
            for dim_name in go.exclude:
                g.add((meas_uri, OBSL.grainExclude, Literal(dim_name)))
            for dim_name in go.include:
                g.add((meas_uri, OBSL.grainInclude, Literal(dim_name)))
            for dim_name in go.keep_only:
                g.add((meas_uri, OBSL.grainKeepOnly, Literal(dim_name)))

        # Filter context
        if meas.filter_context:
            fc = meas.filter_context
            g.add((meas_uri, OBSL.filterContextMode, Literal(fc.mode.value)))
            for dim_name in fc.exclude:
                g.add((meas_uri, OBSL.filterContextExclude, Literal(dim_name)))
            for dim_name in fc.keep_only:
                g.add((meas_uri, OBSL.filterContextKeepOnly, Literal(dim_name)))
            for incl in fc.include:
                if incl.value:
                    expr = f"{incl.field} {incl.op} {incl.value}"
                else:
                    expr = f"{incl.field} {incl.op}"
                g.add((meas_uri, OBSL.filterContextInclude, Literal(expr)))

        if meas.description:
            g.add((meas_uri, RDFS.comment, Literal(meas.description)))
        if meas.owner:
            g.add((meas_uri, OBSL.owner, Literal(meas.owner)))
        if meas.data_type:
            g.add((meas_uri, OBSL.dataType, Literal(meas.data_type)))
        if meas.format:
            g.add((meas_uri, OBSL["format"], Literal(meas.format)))
        for syn in meas.synonyms:
            g.add((meas_uri, OBSL.synonym, Literal(syn)))

    # -- Metrics ------------------------------------------------------------
    for met_name, met in model.metrics.items():
        met_uri = _metric_uri(model_id, met_name)
        g.add((m_uri, OBSL.hasMetric, met_uri))
        g.add((met_uri, RDF.type, OBSL.Metric))
        g.add((met_uri, RDF.type, OWL.NamedIndividual))
        g.add((met_uri, RDFS.label, Literal(met_name)))
        g.add((met_uri, OBSL.metricType, Literal(met.type.value)))

        if met.expression:
            g.add((met_uri, OBSL.expressionSource, Literal(met.expression)))
            # Derive referencesMeasure links from expression
            measure_refs = re.findall(r"\{\[([^\]]+)\]\}", met.expression)
            for ref_name in measure_refs:
                if ref_name in model.measures:
                    ref_uri = _measure_uri(model_id, ref_name)
                    g.add((met_uri, OBSL.referencesMeasure, ref_uri))

        if met.measure:
            base_uri = _measure_uri(model_id, met.measure)
            g.add((met_uri, OBSL.baseMeasure, base_uri))

        # Cumulative metric extended properties
        if met.type == MetricType.CUMULATIVE:
            g.add((met_uri, RDF.type, OBSL.CumulativeMetric))
            if met.time_dimension:
                g.add((met_uri, OBSL.timeDimension, _dimension_uri(model_id, met.time_dimension)))
            g.add((met_uri, OBSL.cumulativeType, Literal(met.cumulative_type.value)))
            if met.window is not None:
                g.add((met_uri, OBSL.window, Literal(met.window)))
            if met.grain_to_date is not None:
                g.add((met_uri, OBSL.grainToDate, Literal(met.grain_to_date.value)))

        # Period-over-period metric extended properties
        if met.type == MetricType.PERIOD_OVER_PERIOD and met.period_over_period:
            pop = met.period_over_period
            g.add((met_uri, RDF.type, OBSL.PeriodOverPeriodMetric))
            g.add((met_uri, OBSL.timeDimension, _dimension_uri(model_id, pop.time_dimension)))
            g.add((met_uri, OBSL.timeGrain, Literal(pop.grain.value)))
            g.add((met_uri, OBSL.offset, Literal(pop.offset)))
            g.add((met_uri, OBSL.offsetGrain, Literal(pop.offset_grain.value)))
            g.add((met_uri, OBSL.comparison, Literal(pop.comparison.value)))

        if met.description:
            g.add((met_uri, RDFS.comment, Literal(met.description)))
        if met.owner:
            g.add((met_uri, OBSL.owner, Literal(met.owner)))
        if met.data_type:
            g.add((met_uri, OBSL.dataType, Literal(met.data_type)))
        if met.format:
            g.add((met_uri, OBSL["format"], Literal(met.format)))
        for syn in met.synonyms:
            g.add((met_uri, OBSL.synonym, Literal(syn)))

    return g
