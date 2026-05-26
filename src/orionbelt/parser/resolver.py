"""Reference resolution: resolves dimension→table, measure→expression references."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from orionbelt.models.errors import SemanticError, ValidationResult
from orionbelt.models.semantic import (
    CustomExtension,
    DataColumnRef,
    DataObject,
    DataObjectColumn,
    DataObjectJoin,
    Dimension,
    FilterContext,
    FilterContextFilter,
    FilterValue,
    GrainOverride,
    Measure,
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    Metric,
    MetricType,
    ModelExample,
    ModelFilter,
    ModelSettings,
    PeriodOverPeriod,
    RefreshPolicy,
    SemanticModel,
)
from orionbelt.parser.loader import SourceMap


def _allowed_keys(*model_classes: type[BaseModel], extra: tuple[str, ...] = ()) -> frozenset[str]:
    """Return the set of YAML keys accepted at a parse site.

    Includes every field name and alias for the given Pydantic model classes,
    plus any extra keys (used for legacy / internal markers like
    ``_extends_sources`` or the ``filter`` (singular) backward-compat key on
    measures).
    """
    keys: set[str] = set(extra)
    for cls in model_classes:
        for name, fdef in cls.model_fields.items():
            keys.add(name)
            if fdef.alias:
                keys.add(fdef.alias)
    return frozenset(keys)


def _check_unknown_keys(
    raw: dict[str, Any] | None,
    allowed: frozenset[str],
    path: str,
    errors: list[SemanticError],
    source_map: SourceMap | None = None,
) -> None:
    """Emit UNKNOWN_PROPERTY for every key in ``raw`` that is not in ``allowed``.

    OBML rejects unknown properties implicitly — typos in YAML keys are a
    silent governance hazard (a measure with ``filtter:`` would compile clean
    and return unfiltered data). This is the OBML side of the same contract
    enforced by ``extra='forbid'`` on the Pydantic models.
    """
    if not isinstance(raw, dict):
        return
    for key in raw:
        if not isinstance(key, str) or key in allowed:
            continue
        span = source_map.get(path) if source_map else None
        errors.append(
            SemanticError(
                code="UNKNOWN_PROPERTY",
                message=f"Unknown property '{key}' at {path}",
                path=path,
                span=span,
                suggestions=_suggest_similar(key, sorted(allowed)),
            )
        )


# Allowlists per parse site, derived from the Pydantic model fields so they
# stay in sync. Top-level adds the merge / inheritance markers that the merger
# inserts and the loader-internal underscore-prefixed keys.
_TOP_LEVEL_KEYS = _allowed_keys(
    SemanticModel,
    extra=(
        "extends",
        "inherits",
        "_extends_sources",
        "_inherits_source",
        # ``schema`` and ``database`` are legal YAML keys on DataObject but
        # also surface here when authors place top-level connection metadata
        # — leave the existing check to flag them as unknown.
    ),
)
_DATA_OBJECT_KEYS = _allowed_keys(DataObject, extra=("schema",))
_DATA_OBJECT_COLUMN_KEYS = _allowed_keys(DataObjectColumn)
_DATA_OBJECT_JOIN_KEYS = _allowed_keys(DataObjectJoin)
_REFRESH_KEYS = _allowed_keys(RefreshPolicy, extra=("maxStaleness",))
_DIMENSION_KEYS = _allowed_keys(Dimension)
_MEASURE_KEYS = _allowed_keys(Measure, extra=("filter",))
_GRAIN_OVERRIDE_KEYS = _allowed_keys(GrainOverride)
_FILTER_CONTEXT_KEYS = _allowed_keys(FilterContext)
_FILTER_CONTEXT_FILTER_KEYS = _allowed_keys(FilterContextFilter)
_MEASURE_FILTER_KEYS = _allowed_keys(MeasureFilter)
_MEASURE_FILTER_GROUP_KEYS = _allowed_keys(MeasureFilterGroup)
_FILTER_VALUE_KEYS = _allowed_keys(FilterValue)
_DATA_COLUMN_REF_KEYS = _allowed_keys(DataColumnRef)
_MODEL_FILTER_KEYS = _allowed_keys(ModelFilter)
_MODEL_SETTINGS_KEYS = _allowed_keys(ModelSettings)
_MODEL_EXAMPLE_KEYS = _allowed_keys(ModelExample)
_CUSTOM_EXTENSION_KEYS = _allowed_keys(CustomExtension)
# Period-over-period / Window / Cumulative metric blocks share the Metric
# field set, but the inner periodOverPeriod block has its own shape.
_PERIOD_OVER_PERIOD_KEYS = _allowed_keys(PeriodOverPeriod)
_METRIC_KEYS = _allowed_keys(Metric)


def _parse_extensions(
    raw: dict[str, Any],
    path: str = "",
    errors: list[SemanticError] | None = None,
    source_map: SourceMap | None = None,
) -> list[CustomExtension]:
    """Extract customExtensions from a raw YAML dict."""
    exts = raw.get("customExtensions", [])
    if errors is not None:
        for ei, e in enumerate(exts):
            if isinstance(e, dict):
                _check_unknown_keys(
                    e,
                    _CUSTOM_EXTENSION_KEYS,
                    f"{path}.customExtensions[{ei}]" if path else f"customExtensions[{ei}]",
                    errors,
                    source_map,
                )
    return [CustomExtension(vendor=e.get("vendor", ""), data=e.get("data", "")) for e in exts]


def _parse_settings(
    raw: dict[str, Any] | None,
    errors: list[SemanticError] | None = None,
    source_map: SourceMap | None = None,
) -> ModelSettings | None:
    """Parse the settings block from raw YAML into ModelSettings."""
    if not raw:
        return None
    if errors is not None:
        _check_unknown_keys(raw, _MODEL_SETTINGS_KEYS, "settings", errors, source_map)
    default_type = raw.get("defaultNumericDataType")
    default_tz = raw.get("defaultTimezone")
    override_db_tz = raw.get("overrideDatabaseTimezone", False)
    default_dialect = raw.get("defaultDialect")
    if not default_type and not default_tz and not override_db_tz and not default_dialect:
        return None
    return ModelSettings(
        default_numeric_data_type=default_type,
        default_timezone=default_tz,
        override_database_timezone=override_db_tz,
        default_dialect=default_dialect,
    )


def _coerce_filter_value(v: object) -> str | int | float | bool | None:
    """Coerce YAML-parsed values (e.g. datetime.date) to types ModelFilter accepts."""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v  # type: ignore[return-value]


_VALID_REFRESH_MODES = frozenset({"interval", "heartbeat", "static"})


def _parse_refresh(
    raw: object, data_object_name: str, errors: list[SemanticError]
) -> RefreshPolicy | None:
    """Parse a dataObject's optional ``refresh:`` block.

    Records structured errors for missing or contradictory fields. Returns
    ``None`` when the block is absent.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(
            SemanticError(
                code="REFRESH_PARSE_ERROR",
                message=f"dataObject '{data_object_name}'.refresh must be a mapping",
                path=f"dataObjects.{data_object_name}.refresh",
            )
        )
        return None

    mode = str(raw.get("mode", "")).strip().lower()
    _check_unknown_keys(raw, _REFRESH_KEYS, f"dataObjects.{data_object_name}.refresh", errors)

    if mode not in _VALID_REFRESH_MODES:
        errors.append(
            SemanticError(
                code="REFRESH_PARSE_ERROR",
                message=(
                    f"dataObject '{data_object_name}'.refresh.mode must be one of "
                    "interval | heartbeat | static"
                ),
                path=f"dataObjects.{data_object_name}.refresh.mode",
            )
        )
        return None

    if mode == "interval" and not raw.get("interval"):
        errors.append(
            SemanticError(
                code="REFRESH_PARSE_ERROR",
                message=(
                    f"dataObject '{data_object_name}'.refresh.interval is required for "
                    "interval mode"
                ),
                path=f"dataObjects.{data_object_name}.refresh.interval",
            )
        )
        return None

    if mode == "heartbeat" and not (raw.get("max_staleness") or raw.get("maxStaleness")):
        errors.append(
            SemanticError(
                code="REFRESH_PARSE_ERROR",
                message=(
                    f"dataObject '{data_object_name}'.refresh.max_staleness is required "
                    "for heartbeat mode"
                ),
                path=f"dataObjects.{data_object_name}.refresh.max_staleness",
            )
        )
        return None

    return RefreshPolicy(
        mode=mode,
        interval=raw.get("interval"),
        anchor=raw.get("anchor"),
        timezone=raw.get("timezone"),
        max_staleness=raw.get("max_staleness") or raw.get("maxStaleness"),
    )


def _parse_measure_filter_item(
    raw: dict[str, Any],
    path: str,
    errors: list[SemanticError] | None = None,
    source_map: SourceMap | None = None,
) -> MeasureFilterItem:
    """Parse a single measure filter or filter group from raw YAML."""
    if "logic" in raw:
        # It's a filter group
        if errors is not None:
            _check_unknown_keys(raw, _MEASURE_FILTER_GROUP_KEYS, path, errors, source_map)
        child_filters: list[MeasureFilterItem] = [
            _parse_measure_filter_item(f, f"{path}.filters[{i}]", errors, source_map)
            for i, f in enumerate(raw.get("filters", []))
        ]
        return MeasureFilterGroup(
            logic=raw["logic"],
            filters=child_filters,
            negated=raw.get("negated", False),
        )

    # It's a leaf filter
    if errors is not None:
        _check_unknown_keys(raw, _MEASURE_FILTER_KEYS, path, errors, source_map)
    filter_values: list[FilterValue] = []
    for vi, vdata in enumerate(raw.get("values", [])):
        if errors is not None:
            _check_unknown_keys(
                vdata, _FILTER_VALUE_KEYS, f"{path}.values[{vi}]", errors, source_map
            )
        filter_values.append(
            FilterValue(
                data_type=vdata.get("dataType", "string"),
                is_null=vdata.get("isNull"),
                value_string=vdata.get("valueString"),
                value_int=vdata.get("valueInt"),
                value_float=vdata.get("valueFloat"),
                value_date=vdata.get("valueDate"),
                value_boolean=vdata.get("valueBoolean"),
            )
        )
    filter_column = None
    if "column" in raw:
        if errors is not None:
            _check_unknown_keys(
                raw["column"], _DATA_COLUMN_REF_KEYS, f"{path}.column", errors, source_map
            )
        filter_column = DataColumnRef(
            view=raw["column"].get("dataObject"),
            column=raw["column"].get("column"),
        )
    return MeasureFilter(
        column=filter_column,
        operator=raw.get("operator", "equals"),
        values=filter_values,
    )


class ReferenceResolver:
    """Resolves all references in a raw YAML model to a fully-typed SemanticModel."""

    def resolve(
        self,
        raw: dict[str, Any],
        source_map: SourceMap | None = None,
    ) -> tuple[SemanticModel, ValidationResult]:
        """Resolve raw YAML dict into a validated SemanticModel.

        Returns (model, validation_result). If there are errors,
        the model may be partially populated.
        """
        errors: list[SemanticError] = []
        warnings: list[SemanticError] = []

        # Strict OBML: reject unknown top-level keys (catches typos like
        # ``dataObjekt:`` that would silently be dropped by ``raw.get(...)``).
        _check_unknown_keys(raw, _TOP_LEVEL_KEYS, "", errors, source_map)

        # Parse data objects
        data_objects: dict[str, DataObject] = {}
        raw_objects = raw.get("dataObjects", {})
        if not isinstance(raw_objects, dict):
            errors.append(
                SemanticError(
                    code="DATA_OBJECT_PARSE_ERROR",
                    message="'dataObjects' must be a YAML mapping, not a list or scalar",
                    path="dataObjects",
                )
            )
            raw_objects = {}
        for name, raw_obj in raw_objects.items():
            try:
                _check_unknown_keys(
                    raw_obj, _DATA_OBJECT_KEYS, f"dataObjects.{name}", errors, source_map
                )
                obj_columns: dict[str, DataObjectColumn] = {}
                for fname, fdata in raw_obj.get("columns", {}).items():
                    _check_unknown_keys(
                        fdata,
                        _DATA_OBJECT_COLUMN_KEYS,
                        f"dataObjects.{name}.columns.{fname}",
                        errors,
                        source_map,
                    )
                    obj_columns[fname] = DataObjectColumn(
                        label=fname,
                        code=fdata.get("code", fname if not fdata.get("expression") else ""),
                        abstract_type=fdata.get("abstractType", "string"),
                        sql_type=fdata.get("sqlType"),
                        sql_precision=fdata.get("sqlPrecision"),
                        sql_scale=fdata.get("sqlScale"),
                        num_class=fdata.get("numClass"),
                        primary_key=bool(fdata.get("primaryKey", False)),
                        description=fdata.get("description"),
                        comment=fdata.get("comment"),
                        owner=fdata.get("owner"),
                        expression=fdata.get("expression"),
                        synonyms=fdata.get("synonyms", []),
                        custom_extensions=_parse_extensions(fdata),
                    )

                obj_joins: list[DataObjectJoin] = []
                for ji, jdata in enumerate(raw_obj.get("joins", [])):
                    _check_unknown_keys(
                        jdata,
                        _DATA_OBJECT_JOIN_KEYS,
                        f"dataObjects.{name}.joins[{ji}]",
                        errors,
                        source_map,
                    )
                    obj_joins.append(
                        DataObjectJoin(
                            join_type=jdata["joinType"],
                            join_to=jdata["joinTo"],
                            columns_from=jdata["columnsFrom"],
                            columns_to=jdata["columnsTo"],
                            secondary=jdata.get("secondary", False),
                            path_name=jdata.get("pathName"),
                        )
                    )

                data_objects[name] = DataObject(
                    label=name,
                    code=raw_obj.get("code", ""),
                    database=raw_obj.get("database", ""),
                    schema_name=raw_obj.get("schema", ""),
                    columns=obj_columns,
                    joins=obj_joins,
                    comment=raw_obj.get("comment"),
                    owner=raw_obj.get("owner"),
                    synonyms=raw_obj.get("synonyms", []),
                    custom_extensions=_parse_extensions(raw_obj),
                    refresh=_parse_refresh(raw_obj.get("refresh"), name, errors),
                )
            except Exception as e:
                span = source_map.get(f"dataObjects.{name}") if source_map else None
                errors.append(
                    SemanticError(
                        code="DATA_OBJECT_PARSE_ERROR",
                        message=f"Failed to parse data object '{name}': {e}",
                        path=f"dataObjects.{name}",
                        span=span,
                    )
                )

        # Parse dimensions
        dimensions: dict[str, Dimension] = {}
        raw_dims = raw.get("dimensions", {})
        if not isinstance(raw_dims, dict):
            errors.append(
                SemanticError(
                    code="DIMENSION_PARSE_ERROR",
                    message="'dimensions' must be a YAML mapping, not a list or scalar",
                    path="dimensions",
                )
            )
            raw_dims = {}
        for name, raw_dim in raw_dims.items():
            try:
                _check_unknown_keys(
                    raw_dim, _DIMENSION_KEYS, f"dimensions.{name}", errors, source_map
                )
                data_object = raw_dim.get("dataObject")
                column = raw_dim.get("column")

                # Validate the data object exists
                if data_object and data_object not in data_objects:
                    span = source_map.get(f"dimensions.{name}") if source_map else None
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_DATA_OBJECT",
                            message=(
                                f"Dimension '{name}' references unknown data object '{data_object}'"
                            ),
                            path=f"dimensions.{name}",
                            span=span,
                            suggestions=_suggest_similar(data_object, list(data_objects.keys())),
                        )
                    )

                # Validate the column exists in the data object
                if (
                    data_object
                    and column
                    and data_object in data_objects
                    and column not in data_objects[data_object].columns
                ):
                    span = source_map.get(f"dimensions.{name}") if source_map else None
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_COLUMN",
                            message=(
                                f"Dimension '{name}' references unknown column "
                                f"'{column}' in data object '{data_object}'"
                            ),
                            path=f"dimensions.{name}",
                            span=span,
                            suggestions=_suggest_similar(
                                column, list(data_objects[data_object].columns.keys())
                            ),
                        )
                    )

                via = raw_dim.get("via")
                if via and via not in data_objects:
                    span = source_map.get(f"dimensions.{name}") if source_map else None
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_DATA_OBJECT",
                            message=(
                                f"Dimension '{name}' via references unknown data object '{via}'"
                            ),
                            path=f"dimensions.{name}",
                            span=span,
                            suggestions=_suggest_similar(via, list(data_objects.keys())),
                        )
                    )

                dimensions[name] = Dimension(
                    label=name,
                    view=data_object or "",
                    column=column or "",
                    result_type=raw_dim.get("resultType", "string"),
                    time_grain=raw_dim.get("timeGrain"),
                    via=via,
                    description=raw_dim.get("description"),
                    format=raw_dim.get("format"),
                    owner=raw_dim.get("owner"),
                    synonyms=raw_dim.get("synonyms", []),
                    custom_extensions=_parse_extensions(raw_dim),
                )
            except Exception as e:
                span = source_map.get(f"dimensions.{name}") if source_map else None
                errors.append(
                    SemanticError(
                        code="DIMENSION_PARSE_ERROR",
                        message=f"Failed to parse dimension '{name}': {e}",
                        path=f"dimensions.{name}",
                        span=span,
                    )
                )

        # Parse measures
        measures: dict[str, Measure] = {}
        raw_measures = raw.get("measures", {})
        if not isinstance(raw_measures, dict):
            errors.append(
                SemanticError(
                    code="MEASURE_PARSE_ERROR",
                    message="'measures' must be a YAML mapping, not a list or scalar",
                    path="measures",
                )
            )
            raw_measures = {}
        for name, raw_meas in raw_measures.items():
            try:
                _check_unknown_keys(raw_meas, _MEASURE_KEYS, f"measures.{name}", errors, source_map)
                measure_columns: list[DataColumnRef] = []
                for ci, fdata in enumerate(raw_meas.get("columns", [])):
                    _check_unknown_keys(
                        fdata,
                        _DATA_COLUMN_REF_KEYS,
                        f"measures.{name}.columns[{ci}]",
                        errors,
                        source_map,
                    )
                    measure_columns.append(
                        DataColumnRef(
                            view=fdata.get("dataObject"),
                            column=fdata.get("column"),
                        )
                    )

                # Resolve expression field references
                expression = raw_meas.get("expression")
                if expression:
                    self._validate_expression_refs(
                        name, expression, data_objects, errors, source_map
                    )

                # Parse measure filters (new `filters:` list or legacy `filter:` single)
                measure_filters: list[MeasureFilterItem] = []
                raw_filters = raw_meas.get("filters")
                if raw_filters and isinstance(raw_filters, list):
                    for fi, rf in enumerate(raw_filters):
                        measure_filters.append(
                            _parse_measure_filter_item(
                                rf,
                                f"measures.{name}.filters[{fi}]",
                                errors,
                                source_map,
                            )
                        )
                else:
                    # Backward compat: single `filter:` key → [filter]
                    raw_filter = raw_meas.get("filter")
                    if raw_filter:
                        measure_filters.append(
                            _parse_measure_filter_item(
                                raw_filter, f"measures.{name}.filter", errors, source_map
                            )
                        )

                # Parse grain override
                grain_override: GrainOverride | None = None
                raw_grain = raw_meas.get("grain")
                if raw_grain and isinstance(raw_grain, dict):
                    _check_unknown_keys(
                        raw_grain,
                        _GRAIN_OVERRIDE_KEYS,
                        f"measures.{name}.grain",
                        errors,
                        source_map,
                    )
                    grain_override = GrainOverride(
                        mode=raw_grain.get("mode", "RELATIVE"),
                        exclude=raw_grain.get("exclude", []),
                        include=raw_grain.get("include", []),
                        keep_only=raw_grain.get("keepOnly", []),
                    )
                    # Validate dimension references in grain
                    for dim_name in (
                        grain_override.include + grain_override.exclude + grain_override.keep_only
                    ):
                        if dim_name not in dimensions:
                            span = source_map.get(f"measures.{name}.grain") if source_map else None
                            errors.append(
                                SemanticError(
                                    code="UNKNOWN_GRAIN_DIMENSION",
                                    message=(
                                        f"Measure '{name}' grain references "
                                        f"unknown dimension '{dim_name}'"
                                    ),
                                    path=f"measures.{name}.grain",
                                    span=span,
                                    suggestions=_suggest_similar(dim_name, list(dimensions.keys())),
                                )
                            )

                # Parse filter context
                filter_ctx: FilterContext | None = None
                raw_fc = raw_meas.get("filterContext")
                if raw_fc and isinstance(raw_fc, dict):
                    _check_unknown_keys(
                        raw_fc,
                        _FILTER_CONTEXT_KEYS,
                        f"measures.{name}.filterContext",
                        errors,
                        source_map,
                    )
                    include_filters: list[FilterContextFilter] = []
                    for inc_i, raw_incl in enumerate(raw_fc.get("include", [])):
                        if isinstance(raw_incl, dict):
                            _check_unknown_keys(
                                raw_incl,
                                _FILTER_CONTEXT_FILTER_KEYS,
                                f"measures.{name}.filterContext.include[{inc_i}]",
                                errors,
                                source_map,
                            )
                            include_filters.append(
                                FilterContextFilter(
                                    field=raw_incl.get("field", ""),
                                    op=raw_incl.get("op", "equals"),
                                    value=raw_incl.get("value"),
                                )
                            )
                    filter_ctx = FilterContext(
                        mode=raw_fc.get("mode", "RELATIVE"),
                        exclude=raw_fc.get("exclude", []),
                        include=include_filters,
                        keep_only=raw_fc.get("keepOnly", []),
                    )
                    # Validate field references in exclude/keepOnly
                    all_dim_names = set(dimensions.keys())
                    all_col_refs: set[str] = set()
                    for obj_name, obj_def in data_objects.items():
                        for col_name in obj_def.columns:
                            all_col_refs.add(f"{obj_name}.{col_name}")
                    for field_name in filter_ctx.exclude + filter_ctx.keep_only:
                        if field_name not in all_dim_names and field_name not in all_col_refs:
                            span = (
                                source_map.get(f"measures.{name}.filterContext")
                                if source_map
                                else None
                            )
                            errors.append(
                                SemanticError(
                                    code="UNKNOWN_FILTER_CONTEXT_FIELD",
                                    message=(
                                        f"Measure '{name}' filterContext references "
                                        f"unknown field '{field_name}'"
                                    ),
                                    path=f"measures.{name}.filterContext",
                                    span=span,
                                    suggestions=_suggest_similar(field_name, list(all_dim_names)),
                                )
                            )
                    for incl in filter_ctx.include:
                        if incl.field not in all_dim_names and incl.field not in all_col_refs:
                            span = (
                                source_map.get(f"measures.{name}.filterContext")
                                if source_map
                                else None
                            )
                            errors.append(
                                SemanticError(
                                    code="UNKNOWN_FILTER_CONTEXT_FIELD",
                                    message=(
                                        f"Measure '{name}' filterContext.include "
                                        f"references unknown field '{incl.field}'"
                                    ),
                                    path=f"measures.{name}.filterContext.include",
                                    span=span,
                                    suggestions=_suggest_similar(incl.field, list(all_dim_names)),
                                )
                            )

                measures[name] = Measure(
                    label=name,
                    columns=measure_columns,
                    result_type=raw_meas.get("resultType", "float"),
                    aggregation=raw_meas.get("aggregation", "sum"),
                    expression=expression,
                    distinct=raw_meas.get("distinct", False),
                    total=raw_meas.get("total", False),
                    grain=grain_override,
                    filter_context=filter_ctx,
                    filters=measure_filters,
                    data_type=raw_meas.get("dataType"),
                    description=raw_meas.get("description"),
                    format=raw_meas.get("format"),
                    allow_fan_out=raw_meas.get("allowFanOut", False),
                    delimiter=raw_meas.get("delimiter"),
                    within_group=raw_meas.get("withinGroup"),
                    owner=raw_meas.get("owner"),
                    synonyms=raw_meas.get("synonyms", []),
                    custom_extensions=_parse_extensions(raw_meas),
                )
            except Exception as e:
                span = source_map.get(f"measures.{name}") if source_map else None
                errors.append(
                    SemanticError(
                        code="MEASURE_PARSE_ERROR",
                        message=f"Failed to parse measure '{name}': {e}",
                        path=f"measures.{name}",
                        span=span,
                    )
                )

        # Parse metrics
        metrics: dict[str, Metric] = {}
        raw_metrics = raw.get("metrics", {})
        if not isinstance(raw_metrics, dict):
            errors.append(
                SemanticError(
                    code="METRIC_PARSE_ERROR",
                    message="'metrics' must be a YAML mapping, not a list or scalar",
                    path="metrics",
                )
            )
            raw_metrics = {}
        for name, raw_metric in raw_metrics.items():
            try:
                _check_unknown_keys(raw_metric, _METRIC_KEYS, f"metrics.{name}", errors, source_map)
                raw_pop_block = raw_metric.get("periodOverPeriod")
                if isinstance(raw_pop_block, dict):
                    _check_unknown_keys(
                        raw_pop_block,
                        _PERIOD_OVER_PERIOD_KEYS,
                        f"metrics.{name}.periodOverPeriod",
                        errors,
                        source_map,
                    )
                metric_type = raw_metric.get("type", "derived")

                if metric_type == MetricType.CUMULATIVE:
                    # Cumulative metric: validate measure reference exists
                    ref_measure = raw_metric.get("measure", "")
                    if ref_measure and ref_measure not in measures:
                        span = source_map.get(f"metrics.{name}.measure") if source_map else None
                        errors.append(
                            SemanticError(
                                code="UNKNOWN_MEASURE",
                                message=(
                                    f"Cumulative metric '{name}' references "
                                    f"unknown measure '{ref_measure}'"
                                ),
                                path=f"metrics.{name}.measure",
                                span=span,
                            )
                        )

                    # Validate timeDimension references a known dimension
                    cum_time_dim = raw_metric.get("timeDimension", "")
                    if cum_time_dim and cum_time_dim not in dimensions:
                        span = (
                            source_map.get(f"metrics.{name}.timeDimension") if source_map else None
                        )
                        errors.append(
                            SemanticError(
                                code="CUMULATIVE_UNKNOWN_TIME_DIMENSION",
                                message=(
                                    f"Cumulative metric '{name}' references "
                                    f"unknown time dimension '{cum_time_dim}'"
                                ),
                                path=f"metrics.{name}.timeDimension",
                                span=span,
                                suggestions=_suggest_similar(cum_time_dim, list(dimensions.keys())),
                            )
                        )

                    metrics[name] = Metric(
                        label=name,
                        type=MetricType.CUMULATIVE,
                        measure=raw_metric.get("measure"),
                        time_dimension=raw_metric.get("timeDimension"),
                        cumulative_type=raw_metric.get("cumulativeType", "sum"),
                        window=raw_metric.get("window"),
                        grain_to_date=raw_metric.get("grainToDate"),
                        partition_by=list(raw_metric.get("partitionBy", []) or []),
                        data_type=raw_metric.get("dataType"),
                        description=raw_metric.get("description"),
                        format=raw_metric.get("format"),
                        owner=raw_metric.get("owner"),
                        synonyms=raw_metric.get("synonyms", []),
                        custom_extensions=_parse_extensions(raw_metric),
                    )
                elif metric_type == MetricType.PERIOD_OVER_PERIOD:
                    # Period-over-period metric: validate expression + PoP config
                    expression = raw_metric.get("expression", "")
                    self._validate_metric_expression_refs(
                        name, expression, measures, errors, source_map, metrics
                    )

                    raw_pop = raw_metric.get("periodOverPeriod")
                    if not raw_pop:
                        span = source_map.get(f"metrics.{name}") if source_map else None
                        errors.append(
                            SemanticError(
                                code="METRIC_PARSE_ERROR",
                                message=(
                                    f"Period-over-period metric '{name}' "
                                    f"requires 'periodOverPeriod' configuration"
                                ),
                                path=f"metrics.{name}",
                                span=span,
                            )
                        )
                        raw_pop = {}

                    # Validate time dimension reference
                    pop_time_dim = raw_pop.get("timeDimension", "")
                    if pop_time_dim and pop_time_dim not in dimensions:
                        span = (
                            source_map.get(f"metrics.{name}.periodOverPeriod")
                            if source_map
                            else None
                        )
                        errors.append(
                            SemanticError(
                                code="POP_UNKNOWN_TIME_DIMENSION",
                                message=(
                                    f"Period-over-period metric '{name}' references "
                                    f"unknown time dimension '{pop_time_dim}'"
                                ),
                                path=f"metrics.{name}.periodOverPeriod.timeDimension",
                                span=span,
                                suggestions=_suggest_similar(pop_time_dim, list(dimensions.keys())),
                            )
                        )

                    pop_config = PeriodOverPeriod(
                        time_dimension=raw_pop.get("timeDimension", ""),
                        grain=raw_pop.get("grain", "month"),
                        offset=raw_pop.get("offset", -1),
                        offset_grain=raw_pop.get("offsetGrain", "year"),
                        comparison=raw_pop.get("comparison", "percentChange"),
                    )

                    metrics[name] = Metric(
                        label=name,
                        type=MetricType.PERIOD_OVER_PERIOD,
                        expression=expression,
                        period_over_period=pop_config,
                        data_type=raw_metric.get("dataType"),
                        description=raw_metric.get("description"),
                        format=raw_metric.get("format"),
                        owner=raw_metric.get("owner"),
                        synonyms=raw_metric.get("synonyms", []),
                        custom_extensions=_parse_extensions(raw_metric),
                    )
                elif metric_type == MetricType.WINDOW:
                    # Window metric (rank/lag/lead/ntile/first_value/last_value)
                    ref_measure = raw_metric.get("measure")
                    if ref_measure and ref_measure not in measures:
                        span = source_map.get(f"metrics.{name}.measure") if source_map else None
                        errors.append(
                            SemanticError(
                                code="UNKNOWN_MEASURE",
                                message=(
                                    f"Window metric '{name}' references "
                                    f"unknown measure '{ref_measure}'"
                                ),
                                path=f"metrics.{name}.measure",
                                span=span,
                            )
                        )

                    win_time_dim = raw_metric.get("timeDimension", "")
                    if win_time_dim and win_time_dim not in dimensions:
                        span = (
                            source_map.get(f"metrics.{name}.timeDimension") if source_map else None
                        )
                        errors.append(
                            SemanticError(
                                code="WINDOW_UNKNOWN_TIME_DIMENSION",
                                message=(
                                    f"Window metric '{name}' references "
                                    f"unknown time dimension '{win_time_dim}'"
                                ),
                                path=f"metrics.{name}.timeDimension",
                                span=span,
                                suggestions=_suggest_similar(win_time_dim, list(dimensions.keys())),
                            )
                        )

                    metrics[name] = Metric(
                        label=name,
                        type=MetricType.WINDOW,
                        measure=ref_measure,
                        time_dimension=raw_metric.get("timeDimension"),
                        window_function=raw_metric.get("windowFunction"),
                        offset=raw_metric.get("offset"),
                        buckets=raw_metric.get("buckets"),
                        order_direction=raw_metric.get("orderDirection", "desc"),
                        default_value=raw_metric.get("defaultValue"),
                        partition_by=list(raw_metric.get("partitionBy", []) or []),
                        data_type=raw_metric.get("dataType"),
                        description=raw_metric.get("description"),
                        format=raw_metric.get("format"),
                        owner=raw_metric.get("owner"),
                        synonyms=raw_metric.get("synonyms", []),
                        custom_extensions=_parse_extensions(raw_metric),
                    )
                else:
                    # Derived metric (default)
                    expression = raw_metric.get("expression", "")
                    self._validate_metric_expression_refs(
                        name, expression, measures, errors, source_map, metrics
                    )

                    metrics[name] = Metric(
                        label=name,
                        expression=expression,
                        data_type=raw_metric.get("dataType"),
                        description=raw_metric.get("description"),
                        format=raw_metric.get("format"),
                        owner=raw_metric.get("owner"),
                        synonyms=raw_metric.get("synonyms", []),
                        custom_extensions=_parse_extensions(raw_metric),
                    )
            except Exception as e:
                span = source_map.get(f"metrics.{name}") if source_map else None
                errors.append(
                    SemanticError(
                        code="METRIC_PARSE_ERROR",
                        message=f"Failed to parse metric '{name}': {e}",
                        path=f"metrics.{name}",
                        span=span,
                    )
                )

        # Parse static model filters
        model_filters: list[ModelFilter] = []
        raw_filters = raw.get("filters", [])
        if not isinstance(raw_filters, list):
            errors.append(
                SemanticError(
                    code="FILTER_PARSE_ERROR",
                    message="'filters' must be a YAML list, not a mapping or scalar",
                    path="filters",
                )
            )
            raw_filters = []
        for i, rf in enumerate(raw_filters):
            try:
                _check_unknown_keys(rf, _MODEL_FILTER_KEYS, f"filters[{i}]", errors, source_map)
                obj_name = rf.get("dataObject", "")
                col_name = rf.get("column", "")
                if obj_name and obj_name not in data_objects:
                    span = source_map.get(f"filters[{i}]") if source_map else None
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_FILTER_DATA_OBJECT",
                            message=(
                                f"Static filter[{i}] references unknown data object '{obj_name}'"
                            ),
                            path=f"filters[{i}]",
                            span=span,
                        )
                    )
                elif obj_name and col_name and col_name not in data_objects[obj_name].columns:
                    span = source_map.get(f"filters[{i}]") if source_map else None
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_FILTER_COLUMN",
                            message=(
                                f"Static filter[{i}] references unknown column "
                                f"'{col_name}' in data object '{obj_name}'"
                            ),
                            path=f"filters[{i}]",
                            span=span,
                        )
                    )
                raw_val = rf.get("value")
                raw_vals = rf.get("values", [])
                model_filters.append(
                    ModelFilter(
                        data_object=obj_name,
                        column=col_name,
                        operator=rf.get("operator", "equals"),
                        value=_coerce_filter_value(raw_val),
                        values=[_coerce_filter_value(v) for v in raw_vals],
                    )
                )
            except Exception as e:
                span = source_map.get(f"filters[{i}]") if source_map else None
                errors.append(
                    SemanticError(
                        code="FILTER_PARSE_ERROR",
                        message=f"Failed to parse static filter[{i}]: {e}",
                        path=f"filters[{i}]",
                        span=span,
                    )
                )

        settings = _parse_settings(raw.get("settings"), errors, source_map)

        # Parse examples block (PLAN_agent_api_improvements §5)
        examples = self._parse_examples(raw.get("examples"), errors)

        model = SemanticModel(
            version=raw.get("version", 1.0),
            data_objects=data_objects,
            dimensions=dimensions,
            measures=measures,
            metrics=metrics,
            filters=model_filters,
            examples=examples,
            extends_sources=raw.get("_extends_sources", []),
            inherits_source=raw.get("_inherits_source"),
            owner=raw.get("owner"),
            custom_extensions=_parse_extensions(raw, "", errors, source_map),
            settings=settings,
        )

        result = ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

        return model, result

    def _parse_examples(self, raw: object, errors: list[SemanticError]) -> list[ModelExample]:
        """Parse the model-level ``examples:`` block.

        Accepts a list of mapping entries. Each entry must have ``name``,
        ``description``, and ``query``. ``intent_tags`` (alias ``intentTags``)
        is optional. Names must be unique within the block.
        """
        if raw is None:
            return []
        if not isinstance(raw, list):
            errors.append(
                SemanticError(
                    code="EXAMPLES_PARSE_ERROR",
                    message="'examples' must be a YAML list of example entries",
                    path="examples",
                )
            )
            return []

        out: list[ModelExample] = []
        seen: set[str] = set()
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                errors.append(
                    SemanticError(
                        code="EXAMPLES_PARSE_ERROR",
                        message=f"examples[{i}] must be a mapping",
                        path=f"examples[{i}]",
                    )
                )
                continue
            _check_unknown_keys(entry, _MODEL_EXAMPLE_KEYS, f"examples[{i}]", errors)
            name = entry.get("name")
            description = entry.get("description")
            query = entry.get("query")
            intent_tags = entry.get("intent_tags") or entry.get("intentTags") or []
            if not isinstance(name, str) or not name:
                errors.append(
                    SemanticError(
                        code="EXAMPLES_PARSE_ERROR",
                        message=f"examples[{i}].name is required and must be a string",
                        path=f"examples[{i}].name",
                    )
                )
                continue
            if name in seen:
                errors.append(
                    SemanticError(
                        code="DUPLICATE_EXAMPLE_NAME",
                        message=f"Duplicate example name '{name}'",
                        path=f"examples[{i}].name",
                    )
                )
                continue
            if not isinstance(description, str):
                errors.append(
                    SemanticError(
                        code="EXAMPLES_PARSE_ERROR",
                        message=f"examples[{i}].description is required",
                        path=f"examples[{i}].description",
                    )
                )
                continue
            if not isinstance(query, dict):
                errors.append(
                    SemanticError(
                        code="EXAMPLES_PARSE_ERROR",
                        message=f"examples[{i}].query must be a mapping (QueryObject payload)",
                        path=f"examples[{i}].query",
                    )
                )
                continue
            if not isinstance(intent_tags, list):
                errors.append(
                    SemanticError(
                        code="EXAMPLES_PARSE_ERROR",
                        message=f"examples[{i}].intent_tags must be a list",
                        path=f"examples[{i}].intent_tags",
                    )
                )
                continue
            seen.add(name)
            out.append(
                ModelExample(
                    name=name,
                    description=description,
                    intent_tags=[str(t) for t in intent_tags],
                    query=dict(query),
                )
            )
        return out

    def _validate_expression_refs(
        self,
        measure_name: str,
        expression: str,
        data_objects: dict[str, DataObject],
        errors: list[SemanticError],
        source_map: SourceMap | None,
    ) -> None:
        """Validate {[DataObject].[Column]} references in a measure expression."""
        span = source_map.get(f"measures.{measure_name}.expression") if source_map else None
        named_refs = re.findall(r"\{\[([^\]{}\[]+)\]\.\[([^\]{}\[]+)\]\}", expression)
        for obj_name, col_name in named_refs:
            if obj_name not in data_objects:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_DATA_OBJECT_IN_EXPRESSION",
                        message=(
                            f"Measure '{measure_name}' expression references unknown "
                            f"data object '{obj_name}'"
                        ),
                        path=f"measures.{measure_name}.expression",
                        span=span,
                    )
                )
            elif col_name not in data_objects[obj_name].columns:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_COLUMN_IN_EXPRESSION",
                        message=(
                            f"Measure '{measure_name}' expression references unknown column "
                            f"'{col_name}' in data object '{obj_name}'"
                        ),
                        path=f"measures.{measure_name}.expression",
                        span=span,
                    )
                )

        # Strip valid refs, scan remainder for malformed attempts.
        remainder = re.sub(r"\{\[[^\]{}\[]+\]\.\[[^\]{}\[]+\]\}", "", expression)
        path = f"measures.{measure_name}.expression"

        def _merr(msg: str) -> None:
            errors.append(
                SemanticError(code="MALFORMED_EXPRESSION_REF", message=msg, path=path, span=span)
            )

        # {[Obj][Col]} — missing dot separator
        for o, c in re.findall(r"\{\[([^\]{}\[]+)\]\[([^\]{}\[]+)\]\}", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '{{[{o}][{c}]}}' — missing '.' separator"
            )

        # {[Obj.Col]} — dot inside single bracket pair
        for bad in re.findall(r"\{\[([^\]{}\[]+\.[^\]{}\[]+)\]\}", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '{{[{bad}]}}' — use '{{[Obj].[Col]}}' syntax"
            )

        # {Obj.Col} — missing all inner brackets
        for bad in re.findall(r"\{([A-Za-z][^\[{}\]]*\.[A-Za-z][^\[{}\]]*)\}", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '{{{bad}}}' — missing '[' and ']', use '{{[Obj].[Col]}}' syntax"
            )

        # {[Obj].[Col] — missing closing }
        for o, c in re.findall(r"\{\[([^\]{}\[]+)\]\.\[([^\]{}\[]+)\](?!\})", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '{{[{o}].[{c}]' — missing closing '}}'"
            )

        # [Obj].[Col]} — missing opening {
        for o, c in re.findall(r"(?<!\{)\[([^\]{}\[]+)\]\.\[([^\]{}\[]+)\]\}", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '[{o}].[{c}]}}' — missing opening '{{'"
            )

        # {[Obj].[Col} — missing ] on column
        for o, c in re.findall(r"\{\[([^\]{}\[]+)\]\.\[([^\]{}\[]*)\}(?!\])", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '{{[{o}].[{c}}}' — missing closing ']' on column"
            )

        # {[Obj.[Col]} — missing ] on data object
        for o, c in re.findall(r"\{\[([^\]{}\[]*)\.?\[([^\]{}\[]+)\]\}", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '{{[{o}.[{c}]}}' — missing closing ']' on data object"
            )

        # {Obj].[Col]} — missing [ on data object
        for o, c in re.findall(r"\{([^\[{}\]]+)\]\.\[([^\]{}\[]+)\]\}", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '{{{o}].[{c}]}}' — missing opening '[' on data object"
            )

        # {[Obj].Col]} — missing [ on column
        for o, c in re.findall(r"\{\[([^\]{}\[]+)\]\.([^\[{}\]]+)\]\}", remainder):
            _merr(
                f"Measure '{measure_name}' has malformed reference"
                f" '{{[{o}].{c}]}}' — missing opening '[' on column"
            )

    def _validate_metric_expression_refs(
        self,
        metric_name: str,
        expression: str,
        measures: dict[str, Measure],
        errors: list[SemanticError],
        source_map: SourceMap | None,
        metrics: dict[str, Metric] | None = None,
    ) -> None:
        """Validate {[Measure Name]} references in a metric expression.

        References can resolve to either measures or already-defined metrics
        (typically cumulative or window metrics that have been parsed earlier
        in the same model). ``metrics`` defaults to ``None`` so existing
        callers continue to work; the caller passes the in-progress metrics
        dict to enable cross-metric composition.
        """
        span = source_map.get(f"metrics.{metric_name}.expression") if source_map else None

        valid_refs = re.findall(r"\{\[([^\]{}\[]+)\]\}", expression)

        # Strip valid {[Name]} refs, then scan remainder for malformed attempts.
        remainder = re.sub(r"\{\[[^\]{}\[]+\]\}", "", expression)

        # {[Name} — missing closing ]
        for bad in re.findall(r"\{\[([^\]{}]*)\}", remainder):
            errors.append(
                SemanticError(
                    code="MALFORMED_EXPRESSION_REF",
                    message=(
                        f"Metric '{metric_name}' has malformed reference"
                        f" '{{[{bad}}}' — missing closing ']'"
                    ),
                    path=f"metrics.{metric_name}.expression",
                    span=span,
                )
            )

        # {[Name] — missing closing }
        for bad in re.findall(r"\{\[([^\]{}]+)\](?!\})", remainder):
            errors.append(
                SemanticError(
                    code="MALFORMED_EXPRESSION_REF",
                    message=(
                        f"Metric '{metric_name}' has malformed reference"
                        f" '{{[{bad}]' — missing closing '}}'"
                    ),
                    path=f"metrics.{metric_name}.expression",
                    span=span,
                )
            )

        # {Name]} — missing opening [
        for bad in re.findall(r"\{([^\[{}\]]+)\]\}", remainder):
            errors.append(
                SemanticError(
                    code="MALFORMED_EXPRESSION_REF",
                    message=(
                        f"Metric '{metric_name}' has malformed reference"
                        f" '{{{bad}]}}' — missing opening '['"
                    ),
                    path=f"metrics.{metric_name}.expression",
                    span=span,
                )
            )

        # {Name} — missing both [ and ]
        for bad in re.findall(r"\{([^\[{\]}\s]+)\}", remainder):
            errors.append(
                SemanticError(
                    code="MALFORMED_EXPRESSION_REF",
                    message=(
                        f"Metric '{metric_name}' has malformed reference"
                        f" '{{{bad}}}' — missing '[' and ']'"
                    ),
                    path=f"metrics.{metric_name}.expression",
                    span=span,
                )
            )

        # [Name]} — missing opening {
        for bad in re.findall(r"(?<!\{)\[([^\]{}\[]+)\]\}", remainder):
            errors.append(
                SemanticError(
                    code="MALFORMED_EXPRESSION_REF",
                    message=(
                        f"Metric '{metric_name}' has malformed reference"
                        f" '[{bad}]}}' — missing opening '{{'"
                    ),
                    path=f"metrics.{metric_name}.expression",
                    span=span,
                )
            )

        known_metrics = metrics or {}
        for ref_name in valid_refs:
            if ref_name not in measures and ref_name not in known_metrics:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_MEASURE_REF",
                        message=(f"Metric '{metric_name}' references unknown measure '{ref_name}'"),
                        path=f"metrics.{metric_name}.expression",
                        span=span,
                        suggestions=_suggest_similar(
                            ref_name,
                            list(measures.keys()) + list(known_metrics.keys()),
                        ),
                    )
                )


def _suggest_similar(name: str, candidates: list[str], max_suggestions: int = 3) -> list[str]:
    """Suggest similar names for 'did you mean?' messages."""
    name_lower = name.lower()
    scored = []
    for candidate in candidates:
        # Simple Levenshtein-like scoring
        candidate_lower = candidate.lower()
        if name_lower in candidate_lower or candidate_lower in name_lower:
            scored.append((0, candidate))
        else:
            # Count common characters
            common = sum(1 for c in name_lower if c in candidate_lower)
            scored.append((len(name) + len(candidate) - 2 * common, candidate))
    scored.sort(key=lambda x: x[0])
    return [s[1] for s in scored[:max_suggestions]]
