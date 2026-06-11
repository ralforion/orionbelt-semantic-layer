"""Drift guard: every OBML modeling field must appear in the RDF ontology.

OBSL's RDF ontology (``ontology/obsl.ttl``) is the source of truth for the
OBSL/SPARQL surface. Memory rule: "OBML feature → also OSI + ontology".
Without an automated check, new OBML fields silently drift away from the
ontology (this happened: numClass / primaryKey / customExtensions /
withinGroup / delimiter / examples were all missing in obsl.ttl before
issue #82 — confirmed across releases 2.2-2.6).

This test parses the YAML aliases / Python field names declared on every
Pydantic class in ``models/semantic.py`` and asserts that each one maps to
a property (or close enough name match) in ``ontology/obsl.ttl``. New OBML
fields fail loudly until the author adds the corresponding RDF property.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orionbelt.models import semantic as semantic_models

_ROOT = Path(__file__).resolve().parents[2]
_ONTOLOGY = _ROOT / "ontology" / "obsl.ttl"

# OBML modeling classes that the ontology covers. Query-side classes
# (QueryObject, QueryFilter, Subquery, …) are intentionally out of scope —
# the ontology models OBML, not the query payload surface.
_MODELED_CLASSES = (
    semantic_models.SemanticModel,
    semantic_models.DataObject,
    semantic_models.DataObjectColumn,
    semantic_models.DataObjectJoin,
    semantic_models.Dimension,
    semantic_models.Measure,
    semantic_models.Metric,
    semantic_models.RefreshPolicy,
    semantic_models.CustomExtension,
    semantic_models.ModelExample,
    semantic_models.WithinGroup,
    semantic_models.PeriodOverPeriod,
    semantic_models.FilterContext,
    semantic_models.FilterContextFilter,
    semantic_models.GrainOverride,
    semantic_models.MeasureFilter,
    semantic_models.MeasureFilterGroup,
    semantic_models.FilterValue,
    semantic_models.ModelFilter,
    semantic_models.ModelSettings,
)

# Fields that intentionally have no ontology property:
#   - housekeeping / framing fields (label, name, description, comment, …)
#     covered by the generic rdfs:label / rdfs:comment vocabulary;
#   - merge markers (extends, inherits, *_sources) that only exist at
#     load time and never survive to the model object;
#   - run-time-only config (ModelSettings dialect / timezone defaults);
#   - filter-leaf scaffolding (FilterValue's typed value cells,
#     MeasureFilterGroup boolean logic) — the ontology models the
#     filterContext intent at the Measure level, not the leaf cells.
_EXCLUDED_FIELDS = frozenset(
    {
        # Always-present human-readable fields
        "label",
        "name",
        "description",
        "comment",
        "version",
        # Inheritance / merge markers (load-time only)
        "extends",
        "inherits",
        "extends_sources",
        "inherits_source",
        # ModelSettings (deployment config, not modeling)
        "default_numeric_data_type",
        "defaultNumericDataType",
        "default_timezone",
        "defaultTimezone",
        "override_database_timezone",
        "overrideDatabaseTimezone",
        "default_dialect",
        "defaultDialect",
        "default_locale",
        "defaultLocale",
        "settings",
        # ModelFilter (static where injected at compile time, not a
        # modeling concept the ontology surfaces)
        "operator",
        "value",
        "values",
        # FilterValue scaffolding (typed value cells)
        "is_null",
        "isNull",
        "value_string",
        "valueString",
        "value_int",
        "valueInt",
        "value_float",
        "valueFloat",
        "value_date",
        "valueDate",
        "value_boolean",
        "valueBoolean",
        # FilterContextFilter primitive fields (the structured filter is
        # surfaced via the filterContextInclude property on Measure)
        "field",
        "op",
        # MeasureFilterGroup boolean composition (the ontology surfaces
        # the resulting filterExpression on Measure, not the AST)
        "logic",
        "filters",
        "filter",
        "negated",
        # GrainOverride / FilterContext primitive fields (already
        # surfaced via grainExclude / grainInclude / grainKeepOnly /
        # filterContextExclude / filterContextInclude / filterContextKeepOnly)
        "exclude",
        "include",
        "keep_only",
        "keepOnly",
        "mode",
        # Refresh policy primitives — covered by refresh* family
        "interval",
        "anchor",
        "timezone",
        "max_staleness",
        # DataObjectColumn structural fields with no ontology presence:
        # the abstractType / sqlType / sqlPrecision / sqlScale family is
        # subsumed by obsl:resultType (abstract) and physicalName (code).
        "abstract_type",
        "abstractType",
        "sql_type",
        "sqlType",
        "sql_precision",
        "sqlPrecision",
        "sql_scale",
        "sqlScale",
        "expression",
        # Metric expression / measure / period_over_period composition —
        # ontology models the metric type and its slot properties directly.
        "measure",
        "period_over_period",
        "periodOverPeriod",
        "filter_context",
        "filterContext",
        "grain",
        "columns",
    }
)

# Aliases the ontology spells differently than the model. Maps model name
# → ontology spelling.
_NAME_REMAP = {
    "withinGroup": "hasWithinGroup",
    "within_group": "hasWithinGroup",
    "data_object": "dataObject",
    "data_objects": "hasDataObject",
    "dataObjects": "hasDataObject",
    "dimensions": "hasDimension",
    "measures": "hasMeasure",
    "metrics": "hasMetric",
    "joins": "hasJoin",
    "examples": "hasExample",
    "intent_tags": "intentTag",
    "intentTags": "intentTag",
    "customExtensions": "hasCustomExtension",
    "custom_extensions": "hasCustomExtension",
    "refresh": "hasRefreshPolicy",
    "maxStaleness": "maxStaleness",
    "delimiter": "delimiter",
    "vendor": "vendor",
    "data": "extensionData",
    "synonyms": "synonym",
    "columns_from": "columnFrom",
    "columnsFrom": "columnFrom",
    "columns_to": "columnTo",
    "columnsTo": "columnTo",
    "join_to": "joinTo",
    "joinTo": "joinTo",
    "join_type": "cardinality",
    "joinType": "cardinality",
    "path_name": "pathName",
    "pathName": "pathName",
    "primary_key": "primaryKey",
    "primaryKey": "primaryKey",
    "num_class": "numClass",
    "numClass": "numClass",
    "result_type": "resultType",
    "resultType": "resultType",
    "code": "code",
    "schema_name": "schema",
    "database": "database",
    "owner": "owner",
    "format": "format",
    "synonym": "synonym",
    "data_type": "dataType",
    "dataType": "dataType",
    "type": "metricType",
    "secondary": "secondary",
    "via": "via",
    "time_grain": "timeGrain",
    "timeGrain": "timeGrain",
    "time_dimension": "timeDimension",
    "timeDimension": "timeDimension",
    "cumulative_type": "cumulativeType",
    "cumulativeType": "cumulativeType",
    "window": "window",
    "grain_to_date": "grainToDate",
    "grainToDate": "grainToDate",
    "partition_by": "partitionBy",
    "partitionBy": "partitionBy",
    "offset": "offset",
    "offset_grain": "offsetGrain",
    "offsetGrain": "offsetGrain",
    "comparison": "comparison",
    "window_function": "windowFunction",
    "windowFunction": "windowFunction",
    "buckets": "windowBuckets",
    "order_direction": "orderDirection",
    "orderDirection": "orderDirection",
    "default_value": "windowDefaultValue",
    "defaultValue": "windowDefaultValue",
    "distinct": "distinct",
    "total": "total",
    "allow_fan_out": "allowFanOut",
    "allowFanOut": "allowFanOut",
    "aggregation": "aggregation",
    "view": "dataObject",
    "column": "column",
    "order": "withinGroupOrder",
    # query (on ModelExample) → exampleQuery
    "query": "exampleQuery",
}


def _resolved_property_name(field_name: str, alias: str | None) -> str:
    """The ontology property name we expect to find for a given field.

    Prefers the alias (camelCase YAML form) since that's what the
    ontology overwhelmingly uses; falls back to the Python field name.
    """
    candidate = alias or field_name
    return _NAME_REMAP.get(candidate, _NAME_REMAP.get(field_name, candidate))


@pytest.fixture(scope="module")
def ontology_text() -> str:
    return _ONTOLOGY.read_text(encoding="utf-8")


@pytest.mark.parametrize("model_cls", _MODELED_CLASSES)
def test_every_obml_field_has_ontology_property(model_cls, ontology_text: str) -> None:
    missing: list[str] = []
    for fname, fdef in model_cls.model_fields.items():
        if fname in _EXCLUDED_FIELDS:
            continue
        alias = fdef.alias
        if alias in _EXCLUDED_FIELDS:
            continue
        expected = _resolved_property_name(fname, alias)
        token = f"obsl:{expected} "
        if token not in ontology_text:
            missing.append(f"{model_cls.__name__}.{fname} (alias={alias!r}) → obsl:{expected}")
    assert not missing, (
        "OBML fields without a matching ontology property — "
        "add to ontology/obsl.ttl or extend _EXCLUDED_FIELDS / _NAME_REMAP "
        "in this test if intentional:\n  " + "\n  ".join(missing)
    )
