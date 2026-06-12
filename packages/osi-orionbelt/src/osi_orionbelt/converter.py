#!/usr/bin/env python3
"""
OSI ↔ OBML Bidirectional Converter
===================================
Converts between Open Semantic Interchange (OSI v0.2.0.dev0) YAML models
and OrionBelt Markup Language (OBML v1.0) YAML models.

OSI v0.1.1 inputs are still accepted on read — the legacy shim
``_normalize_legacy_v01`` promotes pre-v0.2 custom_extensions into the
v0.2 first-class fields before regular parsing runs.

Author: OrionBelt / RALFORION
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# ─── Spec version pin ───────────────────────────────────────────────────────
# Single source of truth for the OSI spec we emit. Bump when upstream cuts
# a stable v0.2.0 (drop the ``.dev0`` suffix). All read paths accept both
# 0.1.x (via the legacy shim) and 0.2.x.
_OSI_VERSION = "0.2.0.dev0"

# Dialect / vendor enum extras new in v0.2.0.dev0
_OSI_KNOWN_DIALECTS = ("ANSI_SQL", "SNOWFLAKE", "MDX", "TABLEAU", "DATABRICKS", "MAQL")
_OSI_KNOWN_VENDORS = ("COMMON", "SNOWFLAKE", "SALESFORCE", "DBT", "DATABRICKS", "GOODDATA")

# ─── Type mapping ───────────────────────────────────────────────────────────

OBML_TO_OSI_TYPE = {
    "string": "string",
    "json": "string",
    "int": "integer",
    "float": "number",
    "date": "date",
    "time": "time",
    "time_tz": "time",
    "timestamp": "timestamp",
    "timestamp_tz": "timestamp",
    "boolean": "boolean",
}

OSI_TO_OBML_TYPE = {
    "string": "string",
    "integer": "int",
    "number": "float",
    "date": "date",
    "time": "time",
    "timestamp": "timestamp",
    "boolean": "boolean",
}


# ═══════════════════════════════════════════════════════════════════════════
#  OSI → OBML Converter
# ═══════════════════════════════════════════════════════════════════════════


class OSItoOBML:
    """Convert an OSI semantic model YAML to OBML format."""

    def __init__(
        self, osi: dict, default_database: str = "ANALYTICS", default_schema: str = "PUBLIC"
    ):
        self.osi = osi
        self.default_database = default_database
        self.default_schema = default_schema
        self.warnings: list[str] = []

    def _normalize_legacy_v01(self) -> None:
        """Promote OSI v0.1.x payloads to the v0.2 shape, in place.

        The v0.2 spec promotes ``primary_key`` and ``unique_keys`` to
        first-class dataset fields. v0.1.x serializers (including ours
        pre-bump) stash both under ``custom_extensions`` with vendor
        ``OBSL`` and keys ``obml_primary_key`` / ``obml_unique_keys``.
        This shim runs before parsing so the rest of the converter can
        assume v0.2 shape regardless of input version.

        No-op for documents that already declare ``version`` >= 0.2 or
        that have nothing to migrate.
        """
        version = str(self.osi.get("version", ""))
        if version and not version.startswith(("0.1", "0.0")):
            return  # already v0.2+ (or future) — nothing to do

        models = self.osi.get("semantic_model", [])
        if not isinstance(models, list):
            return

        for model in models:
            for ds in model.get("datasets", []) or []:
                # Promote legacy primary_key / unique_keys from OBSL extras
                # only if the dataset doesn't already declare them.
                legacy = self._extract_obml_extras(ds)
                if not legacy:
                    continue
                if "primary_key" not in ds and legacy.get("obml_primary_key"):
                    pk = legacy["obml_primary_key"]
                    if isinstance(pk, list) and all(isinstance(c, str) for c in pk):
                        ds["primary_key"] = list(pk)
                if "unique_keys" not in ds and legacy.get("obml_unique_keys"):
                    uk = legacy["obml_unique_keys"]
                    if isinstance(uk, list) and all(
                        isinstance(g, list) and all(isinstance(c, str) for c in g) for g in uk
                    ):
                        ds["unique_keys"] = [list(g) for g in uk]

        if version.startswith(("0.0", "0.1")):
            self.warnings.append(
                f"OSI input declares version '{version}'; legacy v0.1.x "
                f"compatibility shim applied. Output target is v{_OSI_VERSION}."
            )

    def convert(self) -> dict:
        # v0.1.x inputs need the legacy shim to promote pre-v0.2
        # custom_extensions into v0.2 first-class fields before we parse.
        self._normalize_legacy_v01()

        models = self.osi.get("semantic_model", [])
        if not models:
            raise ValueError("No semantic_model found in OSI input")

        # Take the first semantic model (OBML is a single-model format)
        model = models[0]
        if len(models) > 1:
            self.warnings.append(
                f"OSI contains {len(models)} semantic models; "
                f"only the first ('{model.get('name')}') is converted."
            )

        obml: dict[str, Any] = {"version": 1.0}

        # ── Model description ─────────────────────────────────────
        if model.get("description"):
            obml["description"] = model["description"]

        # ── DataObjects ─────────────────────────────────────────────
        datasets = model.get("datasets", [])
        relationships = model.get("relationships", [])

        # Build lookup: dataset_name → dataset
        ds_map = {ds["name"]: ds for ds in datasets}

        # Build relationship index: from_dataset → [relationship, ...]
        rel_by_from: dict[str, list] = {}
        for rel in relationships:
            rel_by_from.setdefault(rel["from"], []).append(rel)

        # Collect join key columns: (dataset_name, field_name) pairs
        # These should NOT become dimensions (they are FK/PK join keys)
        self._join_key_columns: set[tuple[str, str]] = set()
        for rel in relationships:
            for col in rel.get("from_columns", []):
                self._join_key_columns.add((rel["from"], col))
            for col in rel.get("to_columns", []):
                self._join_key_columns.add((rel["to"], col))

        data_objects: dict[str, Any] = {}
        for ds in datasets:
            do_name, do_obj = self._convert_dataset(ds, rel_by_from)
            data_objects[do_name] = do_obj

        obml["dataObjects"] = data_objects

        # ── Dimensions (extracted from OSI fields with dimension metadata) ──
        dimensions = self._extract_dimensions(datasets)
        if dimensions:
            obml["dimensions"] = dimensions

        # ── Measures & Metrics ──────────────────────────────────────
        osi_metrics = model.get("metrics", [])
        measures, metrics = self._convert_metrics(osi_metrics, ds_map)
        if measures:
            obml["measures"] = measures
        if metrics:
            obml["metrics"] = metrics

        # ── Restore model-level properties from custom_extensions ────
        for ext in model.get("custom_extensions", []):
            if ext.get("vendor_name") == "COMMON":
                try:
                    ext_data = json.loads(ext.get("data", "{}"))
                    if ext_data.get("obml_filters"):
                        obml["filters"] = ext_data["obml_filters"]
                    if ext_data.get("obml_settings"):
                        obml["settings"] = ext_data["obml_settings"]
                    if ext_data.get("obml_owner"):
                        obml["owner"] = ext_data["obml_owner"]
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        return obml

    def _parse_source(self, source: str) -> tuple[str, str, str]:
        """Parse 'database.schema.table' into parts."""
        parts = source.split(".")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            return self.default_database, parts[0], parts[1]
        else:
            return self.default_database, self.default_schema, parts[0]

    def _convert_dataset(self, ds: dict, rel_by_from: dict) -> tuple[str, dict]:
        """Convert an OSI dataset to an OBML dataObject.

        Uses the exact OSI dataset name as the OBML data object key.
        """
        name = ds["name"]

        source = ds.get("source", name)
        database, schema, table = self._parse_source(source)

        do: dict[str, Any] = {
            "code": table,
            "database": database,
            "schema": schema,
        }

        # ── Columns ─────────────────────────────────────────────────
        columns: dict[str, Any] = {}
        fields = ds.get("fields", [])
        for field in fields:
            col_name, col_obj = self._convert_field(field)
            columns[col_name] = col_obj

        # ── Primary key flag propagation (OSI v0.2 first-class) ──
        # ``primary_key`` lists physical column codes; mark every matching
        # column with ``primaryKey: true``. Unknown PK columns surface as
        # a warning (the spec couples PK to relationship cardinality, so
        # silently dropping is unsafe).
        pk_codes = ds.get("primary_key") or []
        if pk_codes:
            code_to_col = {col.get("code"): (cname, col) for cname, col in columns.items()}
            unknown_pks: list[str] = []
            for pk_code in pk_codes:
                hit = code_to_col.get(pk_code)
                if hit is None:
                    unknown_pks.append(pk_code)
                    continue
                _, col = hit
                col["primaryKey"] = True
            if unknown_pks:
                self.warnings.append(
                    f"Dataset '{name}' primary_key references unknown columns: "
                    f"{unknown_pks}. Ignored."
                )

        if columns:
            do["columns"] = columns
        else:
            self.warnings.append(f"Dataset '{name}' has no fields; adding placeholder column.")
            do["columns"] = {f"{name}_id": {"code": f"{table}_id", "abstractType": "string"}}

        # ── Joins (from relationships where this dataset is on 'from' side) ──
        joins = []
        for rel in rel_by_from.get(name, []):
            join_obj = self._convert_relationship_to_join(rel)
            joins.append(join_obj)

        if joins:
            do["joins"] = joins

        # ── Description (semantic, from OSI) ─────────────────────────
        if ds.get("description"):
            do["description"] = ds["description"]

        # ── Extract ai_context: synonyms → native, rest → customExtensions ─
        ai_ctx = ds.get("ai_context")
        if ai_ctx:
            ai_data = ai_ctx if isinstance(ai_ctx, dict) else {"instructions": ai_ctx}
            # Extract synonyms directly into OBML synonyms property
            if "synonyms" in ai_data:
                do["synonyms"] = list(ai_data["synonyms"])
            # Store remaining ai_context keys in customExtensions
            remaining = {k: v for k, v in ai_data.items() if k != "synonyms"}
            if remaining:
                do["customExtensions"] = [
                    {
                        "vendor": "OSI",
                        "data": json.dumps(remaining),
                    }
                ]

        # Restore DataObject owner / comment / refresh from custom_extensions
        for ext in ds.get("custom_extensions", []):
            if ext.get("vendor_name") == "COMMON":
                try:
                    ext_data = json.loads(ext.get("data", "{}"))
                    if ext_data.get("obml_owner"):
                        do["owner"] = ext_data["obml_owner"]
                    if ext_data.get("obml_comment"):
                        do["comment"] = ext_data["obml_comment"]
                    if ext_data.get("obml_refresh"):
                        do["refresh"] = ext_data["obml_refresh"]
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        # ── Unique keys roundtrip (OBML has no native concept) ──
        # Persist the OSI ``unique_keys`` array into the OBSL-vendor
        # customExtensions so the OBML → OSI direction can emit it back.
        unique_keys = ds.get("unique_keys") or []
        if unique_keys:
            do.setdefault("customExtensions", []).append(
                {
                    "vendor": "OBSL",
                    "data": json.dumps({"obml_unique_keys": [list(g) for g in unique_keys]}),
                }
            )

        return name, do

    def _convert_field(self, field: dict) -> tuple[str, dict]:
        """Convert an OSI field to an OBML column.

        Uses the exact OSI field name as the OBML column key.
        """
        name = field["name"]

        # Get expression (prefer ANSI_SQL dialect)
        expr_obj = field.get("expression", {})
        code = name  # fallback
        if isinstance(expr_obj, dict):
            dialects = expr_obj.get("dialects", [])
            for d in dialects:
                if d.get("dialect") == "ANSI_SQL":
                    code = d.get("expression", name)
                    break
            if not dialects:
                code = name
            elif code == name and dialects:
                code = dialects[0].get("expression", name)

        # Determine abstract type: prefer explicit data_type, fall back to heuristic
        osi_type = field.get("data_type", "")
        if osi_type and osi_type in OSI_TO_OBML_TYPE:
            abstract_type = OSI_TO_OBML_TYPE[osi_type]
        else:
            abstract_type = self._infer_obml_type(field)

        col: dict[str, Any] = {
            "code": code,
            "abstractType": abstract_type,
        }

        if field.get("description"):
            col["description"] = field["description"]

        # Extract field-level ai_context: synonyms → native, rest → customExtensions
        ai_ctx = field.get("ai_context")
        if ai_ctx:
            ai_data = ai_ctx if isinstance(ai_ctx, dict) else {"instructions": ai_ctx}
            # Extract synonyms directly into OBML synonyms property
            if "synonyms" in ai_data:
                col["synonyms"] = list(ai_data["synonyms"])
            # Store remaining ai_context keys in customExtensions
            remaining = {k: v for k, v in ai_data.items() if k != "synonyms"}
            if remaining:
                col["customExtensions"] = [
                    {
                        "vendor": "OSI",
                        "data": json.dumps(remaining),
                    }
                ]

        # Restore OBML-only column properties from custom_extensions
        for ext in field.get("custom_extensions", []):
            if ext.get("vendor_name") == "COMMON":
                try:
                    ext_data = json.loads(ext.get("data", "{}"))
                    if ext_data.get("obml_sql_type"):
                        col["sqlType"] = ext_data["obml_sql_type"]
                    if ext_data.get("obml_sql_precision") is not None:
                        col["sqlPrecision"] = ext_data["obml_sql_precision"]
                    if ext_data.get("obml_sql_scale") is not None:
                        col["sqlScale"] = ext_data["obml_sql_scale"]
                    if ext_data.get("obml_num_class"):
                        col["numClass"] = ext_data["obml_num_class"]
                    if ext_data.get("obml_comment"):
                        col["comment"] = ext_data["obml_comment"]
                    if ext_data.get("obml_owner"):
                        col["owner"] = ext_data["obml_owner"]
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        # ── Field label roundtrip (OSI v0.2 first-class) ──
        # OBML has no native column label today; preserve via OBSL-vendor
        # customExtensions so the reverse direction can emit it back.
        if field.get("label"):
            col.setdefault("customExtensions", []).append(
                {
                    "vendor": "OBSL",
                    "data": json.dumps({"obml_field_label": field["label"]}),
                }
            )

        return name, col

    def _infer_obml_type(self, field: dict) -> str:
        """Infer OBML abstractType from OSI field metadata."""

        dim = field.get("dimension", {})
        if isinstance(dim, dict) and dim.get("is_time"):
            return "date"

        name_lower = field.get("name", "").lower()

        # Helper: match keywords at word boundaries to avoid false positives
        # (e.g. "country" should NOT match "count")
        def _has_keyword(keywords: tuple[str, ...]) -> bool:
            for kw in keywords:
                if kw.startswith("_") or kw.endswith("_"):
                    # Substring match for prefix/suffix patterns like "_sk", "is_"
                    if kw in name_lower:
                        return True
                else:
                    # Word-boundary match for standalone keywords
                    if re.search(r"(?:^|_)" + re.escape(kw) + r"(?:$|_)", name_lower):
                        return True
            return False

        if _has_keyword(
            (
                "_sk",
                "_id",
                "_key",
                "name",
                "desc",
                "email",
                "address",
                "city",
                "state",
                "zip",
                "phone",
                "status",
                "type",
                "category",
                "class",
            )
        ):
            return "string"
        if _has_keyword(
            (
                "price",
                "cost",
                "amount",
                "sales",
                "profit",
                "revenue",
                "tax",
                "discount",
                "rate",
                "percent",
                "ratio",
                "margin",
            )
        ):
            return "float"
        if _has_keyword(("qty", "quantity", "count", "num", "number", "cnt")):
            return "int"
        if _has_keyword(("date", "time", "year", "month", "day", "week")):
            return "date"
        if _has_keyword(("flag", "is_", "has_")):
            return "boolean"

        return "string"

    # OSI relationship type → OBML joinType mapping
    _REL_TYPE_MAP: dict[str, str] = {
        "many_to_one": "many-to-one",
        "many-to-one": "many-to-one",
        "one_to_many": "one-to-many",
        "one-to-many": "one-to-many",
        "one_to_one": "one-to-one",
        "one-to-one": "one-to-one",
        "many_to_many": "many-to-many",
        "many-to-many": "many-to-many",
    }

    def _convert_relationship_to_join(self, rel: dict) -> dict:
        """Convert an OSI relationship to an OBML join.

        Uses exact OSI names for joinTo and column references.
        Maps OSI relationship 'type' to OBML joinType if present,
        defaults to many-to-one with a warning otherwise.
        """
        rel_type = rel.get("type", "")
        join_type = self._REL_TYPE_MAP.get(rel_type.lower(), "") if rel_type else ""
        if not join_type:
            join_type = "many-to-one"
            if rel_type:
                self.warnings.append(
                    f"Relationship '{rel.get('name', '?')}': unknown type "
                    f"'{rel_type}', defaulting to many-to-one."
                )
            else:
                self.warnings.append(
                    f"Relationship '{rel.get('name', '?')}': no type specified, "
                    f"defaulting to many-to-one."
                )

        join: dict[str, Any] = {
            "joinType": join_type,
            "joinTo": rel["to"],
            "columnsFrom": list(rel["from_columns"]),
            "columnsTo": list(rel["to_columns"]),
        }
        return join

    def _extract_dimensions(self, datasets: list) -> dict:
        """Extract dimension definitions from OSI fields marked as dimensions.

        Skips fields that are join keys (FK/PK columns used in relationships),
        since those are structural and not analytical dimensions.
        """
        dimensions: dict[str, Any] = {}
        for ds in datasets:
            ds_name = ds["name"]
            for field in ds.get("fields", []):
                dim = field.get("dimension")
                if dim is None:
                    continue
                field_name = field["name"]
                # Skip join key columns — they are FK/PK, not analytical dims
                if (ds_name, field_name) in self._join_key_columns:
                    continue
                abstract_type = self._infer_obml_type(field)
                dim_def: dict[str, Any] = {
                    "dataObject": ds_name,
                    "column": field_name,
                    "resultType": abstract_type,
                }
                # Extract synonyms from field-level ai_context
                ai_ctx = field.get("ai_context")
                if isinstance(ai_ctx, dict) and ai_ctx.get("synonyms"):
                    dim_def["synonyms"] = list(ai_ctx["synonyms"])
                # Restore OBML-only dimension properties from custom_extensions
                for ext in field.get("custom_extensions", []):
                    if ext.get("vendor_name") == "COMMON":
                        try:
                            ext_data = json.loads(ext.get("data", "{}"))
                            if ext_data.get("obml_time_grain"):
                                dim_def["timeGrain"] = ext_data["obml_time_grain"]
                            if ext_data.get("obml_dimension_format"):
                                dim_def["format"] = ext_data["obml_dimension_format"]
                            if ext_data.get("obml_dimension_result_type"):
                                dim_def["resultType"] = ext_data["obml_dimension_result_type"]
                            if ext_data.get("obml_dimension_description"):
                                dim_def["description"] = ext_data["obml_dimension_description"]
                            if ext_data.get("obml_dimension_owner"):
                                dim_def["owner"] = ext_data["obml_dimension_owner"]
                            if ext_data.get("obml_dimension_via"):
                                dim_def["via"] = ext_data["obml_dimension_via"]
                        except (json.JSONDecodeError, TypeError):
                            pass
                        break
                dimensions[field_name] = dim_def
        return dimensions

    def _convert_metrics(self, osi_metrics: list, ds_map: dict) -> tuple[dict, dict]:
        """
        Convert OSI metrics to OBML measures and metrics.

        OSI has a single 'metrics' concept with SQL expressions.
        OBML separates 'measures' (simple aggregations on single columns)
        from 'metrics' (cross-fact expressions referencing measures).

        Strategy:
        - Simple single-aggregation metrics → OBML measures
        - Aggregation over expression (e.g. SUM(a.x * a.y)) → expression measure
        - Complex/multi-aggregation metrics → OBML metrics referencing auto-measures
        """

        measures: dict[str, Any] = {}
        metrics: dict[str, Any] = {}

        for m in osi_metrics:
            name = m["name"]

            osi_description = m.get("description")

            # Extract synonyms from OSI ai_context
            osi_ai_ctx = m.get("ai_context")
            osi_synonyms: list[str] = []
            if isinstance(osi_ai_ctx, dict) and osi_ai_ctx.get("synonyms"):
                osi_synonyms = list(osi_ai_ctx["synonyms"])

            # Restore OBML-only properties from custom_extensions
            obml_extras = self._extract_obml_extras(m)

            # Check for cumulative metric stored in custom_extensions
            if obml_extras.get("obml_metric_type") == "cumulative":
                cum_metric = self._reconstruct_cumulative_metric(
                    name, obml_extras, osi_description, osi_synonyms
                )
                metrics[name] = cum_metric
                continue

            # Check for period-over-period metric stored in custom_extensions
            if obml_extras.get("obml_metric_type") == "period_over_period":
                pop_metric = self._reconstruct_pop_metric(
                    name, obml_extras, osi_description, osi_synonyms
                )
                metrics[name] = pop_metric
                continue

            # Check for window metric (rank/lag/lead/ntile/first_value/last_value)
            if obml_extras.get("obml_metric_type") == "window":
                window_metric = self._reconstruct_window_metric(
                    name, obml_extras, osi_description, osi_synonyms
                )
                metrics[name] = window_metric
                continue

            # Engine-delegated aggregation (Databricks Metric View). Round-trip
            # marker comes from the OBML → OSI direction; on input we restore
            # ``aggregation: measure`` without touching the OSI expression
            # (which is a literal ``MEASURE("<label>")`` with no source column
            # to parse).
            if obml_extras.get("obml_aggregation") == "measure":
                delegated: dict[str, Any] = {"aggregation": "measure"}
                if osi_description:
                    delegated["description"] = osi_description
                if osi_synonyms:
                    delegated["synonyms"] = osi_synonyms
                self._apply_obml_measure_extras(delegated, obml_extras)
                # ``measure`` aggregation forbids columns / expression /
                # filters / total at the model level, so strip anything
                # the extras decoder may have copied across.
                for forbidden in ("filters", "total", "expression"):
                    delegated.pop(forbidden, None)
                measures[name] = delegated
                continue

            expr_text = self._get_ansi_expression(m.get("expression", {}))
            if not expr_text:
                self.warnings.append(f"Metric '{name}' has no ANSI_SQL expression; skipped.")
                continue

            # Try simple: AGG(dataset.column) or AGG(DISTINCT dataset.column)
            parsed = self._parse_simple_agg(expr_text)
            if parsed:
                agg, dataset, column, is_distinct = parsed
                measure_def: dict[str, Any] = {
                    "columns": [{"dataObject": dataset, "column": column}],
                    "resultType": "float" if agg.upper() in ("SUM", "AVG") else "int",
                    "aggregation": agg.lower(),
                }
                if is_distinct:
                    measure_def["distinct"] = True
                if osi_description:
                    measure_def["description"] = osi_description
                if osi_synonyms:
                    measure_def["synonyms"] = osi_synonyms
                self._apply_obml_measure_extras(measure_def, obml_extras)
                measures[name] = measure_def
                continue

            # Try expression-based: AGG(expr with dataset.column refs)
            parsed_expr = self._parse_expr_agg(expr_text)
            if parsed_expr:
                agg, inner_expr = parsed_expr
                obml_expr = self._sql_refs_to_obml(inner_expr)
                measure_def = {
                    "expression": obml_expr,
                    "resultType": "float" if agg.upper() in ("SUM", "AVG") else "int",
                    "aggregation": agg.lower(),
                }
                if osi_description:
                    measure_def["description"] = osi_description
                if osi_synonyms:
                    measure_def["synonyms"] = osi_synonyms
                self._apply_obml_measure_extras(measure_def, obml_extras)
                measures[name] = measure_def
                continue

            # Complex: multiple aggregations → decompose into measures + metric
            obml_expr, auto_measures = self._decompose_complex_metric(name, expr_text)
            if auto_measures:
                # Deduplicate: if an auto-measure is equivalent to an existing
                # named measure, reuse the named measure in the metric expression
                for auto_key, auto_def in list(auto_measures.items()):
                    for existing_name, existing_def in measures.items():
                        if self._measures_equivalent(auto_def, existing_def):
                            obml_expr = obml_expr.replace(
                                "{[" + auto_key + "]}", "{[" + existing_name + "]}"
                            )
                            del auto_measures[auto_key]
                            break
                measures.update(auto_measures)
                metric_def: dict[str, Any] = {"expression": obml_expr}
                if osi_description:
                    metric_def["description"] = osi_description
                if osi_synonyms:
                    metric_def["synonyms"] = osi_synonyms
                # Restore OBML-only properties for complex metrics
                if obml_extras.get("obml_format"):
                    metric_def["format"] = obml_extras["obml_format"]
                if obml_extras.get("obml_data_type"):
                    metric_def["dataType"] = obml_extras["obml_data_type"]
                if obml_extras.get("obml_owner"):
                    metric_def["owner"] = obml_extras["obml_owner"]
                metrics[name] = metric_def
            else:
                self.warnings.append(f"Metric '{name}' has unparseable expression: {expr_text}")

        return measures, metrics

    @staticmethod
    def _extract_obml_extras(osi_metric: dict) -> dict:
        """Extract OBML-only properties from OSI metric custom_extensions."""
        for ext in osi_metric.get("custom_extensions", []):
            if ext.get("vendor_name") == "COMMON":
                try:
                    data = json.loads(ext.get("data", "{}"))
                    # Check for any obml_ prefixed keys
                    if any(k.startswith("obml_") for k in data):
                        return data
                except (json.JSONDecodeError, TypeError):
                    pass
        return {}

    @staticmethod
    def _reconstruct_cumulative_metric(
        name: str,
        extras: dict,
        description: str | None,
        synonyms: list[str],
    ) -> dict:
        """Reconstruct an OBML cumulative metric from custom_extensions data."""
        metric_def: dict[str, Any] = {
            "type": "cumulative",
            "measure": extras["obml_cumulative_measure"],
            "timeDimension": extras["obml_cumulative_time_dimension"],
        }
        cum_type = extras.get("obml_cumulative_type", "sum")
        if cum_type != "sum":
            metric_def["cumulativeType"] = cum_type
        if extras.get("obml_cumulative_window") is not None:
            metric_def["window"] = extras["obml_cumulative_window"]
        if extras.get("obml_cumulative_grain_to_date"):
            metric_def["grainToDate"] = extras["obml_cumulative_grain_to_date"]
        if extras.get("obml_partition_by"):
            metric_def["partitionBy"] = list(extras["obml_partition_by"])
        if description:
            metric_def["description"] = description
        if extras.get("obml_format"):
            metric_def["format"] = extras["obml_format"]
        if extras.get("obml_data_type"):
            metric_def["dataType"] = extras["obml_data_type"]
        if extras.get("obml_owner"):
            metric_def["owner"] = extras["obml_owner"]
        if synonyms:
            metric_def["synonyms"] = synonyms
        return metric_def

    @staticmethod
    def _reconstruct_window_metric(
        name: str,
        extras: dict,
        description: str | None,
        synonyms: list[str],
    ) -> dict:
        """Reconstruct an OBML window metric from custom_extensions data."""
        metric_def: dict[str, Any] = {
            "type": "window",
            "windowFunction": extras["obml_window_function"],
        }
        if extras.get("obml_window_measure"):
            metric_def["measure"] = extras["obml_window_measure"]
        if extras.get("obml_window_time_dimension"):
            metric_def["timeDimension"] = extras["obml_window_time_dimension"]
        if extras.get("obml_window_offset") is not None:
            metric_def["offset"] = extras["obml_window_offset"]
        if extras.get("obml_window_buckets") is not None:
            metric_def["buckets"] = extras["obml_window_buckets"]
        order_dir = extras.get("obml_order_direction", "desc")
        if order_dir != "desc":
            metric_def["orderDirection"] = order_dir
        if extras.get("obml_window_default_value") is not None:
            metric_def["defaultValue"] = extras["obml_window_default_value"]
        if extras.get("obml_partition_by"):
            metric_def["partitionBy"] = list(extras["obml_partition_by"])
        if description:
            metric_def["description"] = description
        if extras.get("obml_format"):
            metric_def["format"] = extras["obml_format"]
        if extras.get("obml_data_type"):
            metric_def["dataType"] = extras["obml_data_type"]
        if extras.get("obml_owner"):
            metric_def["owner"] = extras["obml_owner"]
        if synonyms:
            metric_def["synonyms"] = synonyms
        return metric_def

    @staticmethod
    def _reconstruct_pop_metric(
        name: str,
        extras: dict,
        description: str | None,
        synonyms: list[str],
    ) -> dict:
        """Reconstruct an OBML period-over-period metric from custom_extensions data."""
        pop_config: dict[str, Any] = {
            "timeDimension": extras["obml_pop_time_dimension"],
            "grain": extras["obml_pop_grain"],
            "offsetGrain": extras["obml_pop_offset_grain"],
        }
        offset = extras.get("obml_pop_offset", -1)
        if offset != -1:
            pop_config["offset"] = offset
        comparison = extras.get("obml_pop_comparison", "percentChange")
        if comparison != "percentChange":
            pop_config["comparison"] = comparison

        metric_def: dict[str, Any] = {
            "type": "period_over_period",
            "expression": extras.get("obml_pop_expression", ""),
            "periodOverPeriod": pop_config,
        }
        if description:
            metric_def["description"] = description
        if extras.get("obml_format"):
            metric_def["format"] = extras["obml_format"]
        if extras.get("obml_data_type"):
            metric_def["dataType"] = extras["obml_data_type"]
        if extras.get("obml_owner"):
            metric_def["owner"] = extras["obml_owner"]
        if synonyms:
            metric_def["synonyms"] = synonyms
        return metric_def

    @staticmethod
    def _apply_obml_measure_extras(measure_def: dict, extras: dict) -> None:
        """Restore OBML-only measure properties from extracted extras."""
        if extras.get("obml_filters"):
            measure_def["filters"] = extras["obml_filters"]
        if extras.get("obml_total"):
            measure_def["total"] = True
        if extras.get("obml_allow_fan_out"):
            measure_def["allowFanOut"] = True
        if extras.get("obml_format"):
            measure_def["format"] = extras["obml_format"]
        if extras.get("obml_delimiter"):
            measure_def["delimiter"] = extras["obml_delimiter"]
        if extras.get("obml_within_group"):
            measure_def["withinGroup"] = extras["obml_within_group"]
        if extras.get("obml_data_type"):
            measure_def["dataType"] = extras["obml_data_type"]
        if extras.get("obml_owner"):
            measure_def["owner"] = extras["obml_owner"]
        if extras.get("obml_grain"):
            measure_def["grain"] = extras["obml_grain"]
        if extras.get("obml_filter_context"):
            measure_def["filterContext"] = extras["obml_filter_context"]

    @staticmethod
    def _measures_equivalent(a: dict, b: dict) -> bool:
        """Check if two measure definitions are functionally equivalent."""
        if a.get("aggregation") != b.get("aggregation"):
            return False
        if a.get("distinct", False) != b.get("distinct", False):
            return False
        # Compare column-based measures
        if a.get("columns") and b.get("columns"):
            return a["columns"] == b["columns"]
        # Compare expression-based measures
        if a.get("expression") and b.get("expression"):
            return a["expression"] == b["expression"]
        return False

    def _get_ansi_expression(self, expr_obj: dict) -> str:
        """Extract ANSI_SQL expression text."""
        if isinstance(expr_obj, dict):
            dialects = expr_obj.get("dialects", [])
            for d in dialects:
                if d.get("dialect") == "ANSI_SQL":
                    return d.get("expression", "")
            if dialects:
                return dialects[0].get("expression", "")
        return ""

    def _parse_simple_agg(self, expr: str) -> tuple | None:
        """
        Parse simple aggregation: AGG(DISTINCT? dataset.column)
        Returns (agg, dataset, column, is_distinct) or None.
        """
        import re

        expr = expr.strip()
        pattern = r"^(\w+)\(\s*(DISTINCT\s+)?(\w+)\.(\w+)\s*\)$"
        match = re.match(pattern, expr, re.IGNORECASE)
        if match:
            agg = match.group(1)
            is_distinct = match.group(2) is not None
            dataset = match.group(3)
            column = match.group(4)
            return agg, dataset, column, is_distinct
        return None

    def _parse_expr_agg(self, expr: str) -> tuple | None:
        """
        Parse expression-based aggregation: AGG(expr containing dataset.column refs)
        E.g. SUM(orders.price * orders.quantity)
        Returns (agg, inner_expression) or None.
        """

        agg_funcs = {"SUM", "COUNT", "AVG", "MIN", "MAX", "ANY_VALUE", "MEDIAN", "MODE", "LISTAGG"}

        expr = expr.strip()
        # Match AGG(...) — must use balanced parentheses
        pattern = r"^(\w+)\(\s*(.+)\s*\)$"
        match = re.match(pattern, expr, re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        agg = match.group(1).upper()
        if agg not in agg_funcs:
            return None
        inner = match.group(2).strip()

        # Check balanced parens: the inner must not have unmatched parens
        depth = 0
        for ch in inner:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth < 0:
                return None  # Unmatched close paren → not a single AGG(...)
        if depth != 0:
            return None  # Unmatched open paren

        # Must contain dataset.column references
        if not re.search(r"\w+\.\w+", inner):
            return None
        # Must NOT be a simple dataset.column (already handled by _parse_simple_agg)
        if re.match(r"^(DISTINCT\s+)?\w+\.\w+$", inner, re.IGNORECASE):
            return None
        # Must NOT contain nested aggregation calls (those are complex metrics)
        if re.search(r"\b(" + "|".join(agg_funcs) + r")\s*\(", inner, re.IGNORECASE):
            return None
        return agg.lower(), inner

    def _sql_refs_to_obml(self, sql_expr: str) -> str:
        """Convert dataset.column references in SQL to OBML {[dataset].[column]} syntax."""
        import re

        return re.sub(r"(\w+)\.(\w+)", r"{[\1].[\2]}", sql_expr)

    def _decompose_complex_metric(self, name: str, expr: str) -> tuple[str, dict]:
        """
        Decompose a complex OSI metric expression (multiple aggregations)
        into OBML auto-measures + a metric expression string.

        Handles both simple AGG(dataset.column) and expression-based
        AGG(dataset.col1 * dataset.col2) patterns.

        E.g. SUM(orders.price * orders.quantity) / COUNT(DISTINCT customers.id)
        → auto-measures, metric referencing them via {[name]}
        """

        agg_funcs = {"SUM", "COUNT", "AVG", "MIN", "MAX", "ANY_VALUE", "MEDIAN", "MODE", "LISTAGG"}

        auto_measures: dict[str, Any] = {}
        obml_expr = expr

        # Find all AGG(...) calls with balanced parentheses
        matches = []
        i = 0
        while i < len(expr):
            # Look for WORD( pattern
            m = re.match(r"(\w+)\s*\(", expr[i:])
            if m and m.group(1).upper() in agg_funcs:
                agg = m.group(1)
                start = i
                paren_start = i + m.end() - 1  # position of '('
                # Find matching close paren
                depth = 1
                j = paren_start + 1
                while j < len(expr) and depth > 0:
                    if expr[j] == "(":
                        depth += 1
                    elif expr[j] == ")":
                        depth -= 1
                    j += 1
                if depth == 0:
                    full = expr[start:j]
                    inner = expr[paren_start + 1 : j - 1].strip()
                    matches.append((full, agg, inner))
                    i = j
                    continue
            i += 1

        for full_match, agg, inner in matches:
            # Check for DISTINCT keyword
            is_distinct = False
            inner_clean = inner
            dm = re.match(r"^DISTINCT\s+", inner, re.IGNORECASE)
            if dm:
                is_distinct = True
                inner_clean = inner[dm.end() :].strip()

            # Is it a simple dataset.column?
            simple = re.match(r"^(\w+)\.(\w+)$", inner_clean)
            if simple:
                dataset = simple.group(1)
                column = simple.group(2)
                suffix = "_distinct" if is_distinct else ""
                measure_key = f"_{dataset}_{column}_{agg.lower()}{suffix}"
                measure_def: dict[str, Any] = {
                    "columns": [{"dataObject": dataset, "column": column}],
                    "resultType": "float",
                    "aggregation": agg.lower(),
                }
                if is_distinct:
                    measure_def["distinct"] = True
                auto_measures[measure_key] = measure_def
            else:
                # Expression-based: convert dataset.column refs to OBML syntax
                obml_inner = self._sql_refs_to_obml(inner_clean)
                # Generate a key from the aggregation + hash of expression
                key_suffix = "_distinct" if is_distinct else ""
                # Use a short deterministic key from the expression
                expr_slug = re.sub(r"[^a-zA-Z0-9]", "_", inner_clean)[:40]
                measure_key = f"_{agg.lower()}_{expr_slug}{key_suffix}"
                measure_def = {
                    "expression": obml_inner,
                    "resultType": "float",
                    "aggregation": agg.lower(),
                }
                if is_distinct:
                    measure_def["distinct"] = True
                auto_measures[measure_key] = measure_def

            obml_expr = obml_expr.replace(full_match, "{[" + measure_key + "]}", 1)

        return obml_expr, auto_measures


# ═══════════════════════════════════════════════════════════════════════════
#  OBML → OSI Converter
# ═══════════════════════════════════════════════════════════════════════════


class OBMLtoOSI:
    """Convert an OBML semantic model YAML to OSI format."""

    def __init__(
        self,
        obml: dict,
        model_name: str = "semantic_model",
        model_description: str = "",
        ai_instructions: str = "",
    ):
        self.obml = obml
        self.model_name = model_name
        self.model_description = model_description
        self.ai_instructions = ai_instructions
        self.warnings: list[str] = []

    def convert(self) -> dict:
        osi: dict[str, Any] = {"version": _OSI_VERSION}
        # Vendor names accumulate as we emit custom_extensions so we can
        # populate the v0.2 top-level ``vendors`` informational array at the
        # end of the conversion.
        self._used_vendors: set[str] = {"COMMON"}

        data_objects = self.obml.get("dataObjects", {})
        obml_dimensions = self.obml.get("dimensions", {})
        obml_measures = self.obml.get("measures", {})
        obml_metrics = self.obml.get("metrics", {})

        # ── Datasets ────────────────────────────────────────────────
        datasets = []
        all_relationships = []

        for do_name, do_obj in data_objects.items():
            dataset, rels = self._convert_data_object(do_name, do_obj, obml_dimensions)
            datasets.append(dataset)
            all_relationships.extend(rels)

        # ── Metrics (OBML measures + metrics → OSI metrics) ────────
        osi_metrics = self._convert_measures_and_metrics(obml_measures, obml_metrics, data_objects)

        # ── Build semantic model ────────────────────────────────────
        sem_model: dict[str, Any] = {"name": self.model_name}
        # Prefer OBML model-level description, fall back to constructor param
        obml_description = self.obml.get("description", "")
        model_desc = obml_description or self.model_description
        if model_desc:
            sem_model["description"] = model_desc
        if self.ai_instructions:
            sem_model["ai_context"] = {"instructions": self.ai_instructions}

        sem_model["datasets"] = datasets

        if all_relationships:
            sem_model["relationships"] = all_relationships

        if osi_metrics:
            sem_model["metrics"] = osi_metrics

        # Add OBML as custom extension for lossless roundtrip info
        roundtrip_data: dict[str, Any] = {
            "source_format": "OBML",
            "source_version": str(self.obml.get("version", "1.0")),
            "converter": "osi_obml_converter",
        }
        # Preserve model-level static filters for roundtrip
        obml_filters = self.obml.get("filters", [])
        if obml_filters:
            roundtrip_data["obml_filters"] = obml_filters
        # Preserve model settings for roundtrip
        obml_settings = self.obml.get("settings")
        if obml_settings:
            roundtrip_data["obml_settings"] = obml_settings
        # Preserve model owner for roundtrip
        obml_owner = self.obml.get("owner")
        if obml_owner:
            roundtrip_data["obml_owner"] = obml_owner
        sem_model["custom_extensions"] = [
            {
                "vendor_name": "COMMON",
                "data": json.dumps(roundtrip_data),
            }
        ]

        osi["semantic_model"] = [sem_model]

        # v0.2 informational enums — only what's actually used in the doc.
        # ANSI_SQL is always emitted (every OBML measure compiles via it).
        osi["dialects"] = ["ANSI_SQL"]
        vendors_emitted = sorted(self._used_vendors)
        if vendors_emitted:
            osi["vendors"] = vendors_emitted

        return osi

    def _record_vendor(self, vendor_name: str) -> None:
        """Record a vendor seen during emission; populates the top-level
        ``vendors`` informational array. Safe to call repeatedly."""
        self._used_vendors.add(vendor_name)

    def _convert_data_object(
        self, do_name: str, do_obj: dict, obml_dimensions: dict
    ) -> tuple[dict, list]:
        """Convert an OBML dataObject to an OSI dataset + relationships."""
        database = do_obj.get("database", "")
        schema = do_obj.get("schema", "")
        code = do_obj.get("code", "")
        source = f"{database}.{schema}.{code}" if database else code

        # Use the OBML display name as the OSI dataset name so that
        # relationship references (joinTo) stay consistent in roundtrips
        osi_name = do_name

        dataset: dict[str, Any] = {
            "name": osi_name,
            "source": source,
        }

        # ── Primary key (v0.2 first-class) ──────────────────────────
        # Collect columns flagged with ``primaryKey: true`` in OBML order
        # (TrackedLoader / Python dict preserves declaration order, which
        # is significant for composite PKs).
        pk_columns = [
            col_name
            for col_name, col in (do_obj.get("columns", {}) or {}).items()
            if col.get("primaryKey")
        ]
        # Use the physical ``code`` for each column when present — OSI
        # field names mirror the physical column code (see _convert_column).
        if pk_columns:
            columns_map = do_obj.get("columns", {}) or {}
            dataset["primary_key"] = [
                columns_map[c].get("code", c.lower().replace(" ", "_")) for c in pk_columns
            ]

        # ── Unique keys (v0.2 first-class, lossless roundtrip via OBSL) ──
        # OBML doesn't model unique keys natively today; round-trip via the
        # ``OBSL``-vendor ``obml_unique_keys`` payload that originated from
        # a prior OSI → OBML conversion (or hand-authored OBML).
        unique_keys_extra: list[list[str]] | None = None
        for ext in do_obj.get("customExtensions", []) or []:
            if ext.get("vendor") != "OBSL":
                continue
            try:
                data = json.loads(ext.get("data", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            uk = data.get("obml_unique_keys")
            if isinstance(uk, list) and all(isinstance(g, list) for g in uk):
                unique_keys_extra = [list(g) for g in uk]
                break
        if unique_keys_extra:
            dataset["unique_keys"] = unique_keys_extra

        if do_obj.get("description"):
            dataset["description"] = do_obj["description"]
        elif do_obj.get("comment"):
            dataset["description"] = do_obj["comment"]

        # ── Rebuild ai_context: native synonyms + remaining from customExtensions
        ai_ctx: dict[str, Any] = {}
        for ext in do_obj.get("customExtensions", []):
            if ext.get("vendor") == "OSI":
                try:
                    ai_data = json.loads(ext.get("data", "{}"))
                    if ai_data:
                        ai_ctx.update(ai_data)
                except (json.JSONDecodeError, TypeError):
                    pass
        # Merge native OBML synonyms into ai_context.synonyms
        obml_synonyms = do_obj.get("synonyms", [])
        if obml_synonyms:
            existing = ai_ctx.get("synonyms", [])
            merged = list(existing) + [s for s in obml_synonyms if s not in existing]
            ai_ctx["synonyms"] = merged
        if ai_ctx:
            dataset["ai_context"] = ai_ctx

        # ── Fields ──────────────────────────────────────────────────
        fields = []
        columns = do_obj.get("columns", {})
        for col_name, col_obj in columns.items():
            field = self._convert_column(col_name, col_obj, do_name, obml_dimensions)
            fields.append(field)

        if fields:
            dataset["fields"] = fields

        # ── Relationships (from OBML joins) ─────────────────────────
        relationships = []
        joins = do_obj.get("joins", [])
        for i, join in enumerate(joins):
            rel = self._convert_join_to_relationship(osi_name, do_name, do_obj, join, i)
            if rel:
                relationships.append(rel)

        # ── Preserve DataObject owner/comment + refresh in custom_extensions ──
        do_extras: dict[str, Any] = {}
        if do_obj.get("owner"):
            do_extras["obml_owner"] = do_obj["owner"]
        if do_obj.get("comment"):
            do_extras["obml_comment"] = do_obj["comment"]
        # OBML-only freshness contract — round-tripped through OSI
        # custom_extensions since OSI has no native equivalent. See
        # design/PLAN_freshness_driven_cache.md §5.
        if do_obj.get("refresh"):
            do_extras["obml_refresh"] = do_obj["refresh"]
        if do_extras:
            ds_exts = dataset.setdefault("custom_extensions", [])
            ds_exts.append(
                {
                    "vendor_name": "COMMON",
                    "data": json.dumps(do_extras),
                }
            )

        return dataset, relationships

    def _convert_column(
        self, col_name: str, col_obj: dict, do_name: str, obml_dimensions: dict
    ) -> dict:
        """Convert an OBML column to an OSI field."""
        code = col_obj.get("code", col_name.lower().replace(" ", "_"))

        field: dict[str, Any] = {
            "name": code,
            "expression": {
                "dialects": [
                    {
                        "dialect": "ANSI_SQL",
                        "expression": code,
                    }
                ]
            },
        }

        # Check if this column is used as a dimension
        is_dimension = False
        is_time = False
        synonyms = []

        for dim_name, dim_obj in obml_dimensions.items():
            if dim_obj.get("dataObject") == do_name and dim_obj.get("column") == col_name:
                is_dimension = True
                if dim_obj.get("resultType") in ("date", "time", "timestamp", "timestamp_tz"):
                    is_time = True
                # The dimension display name is a synonym
                if dim_name != col_name:
                    synonyms.append(dim_name)
                break

        abstract_type = col_obj.get("abstractType", "string")
        if abstract_type in ("date", "timestamp", "timestamp_tz"):
            is_time = True

        if is_dimension or is_time:
            field["dimension"] = {"is_time": is_time}

        if col_obj.get("description"):
            field["description"] = col_obj["description"]
        elif col_obj.get("comment"):
            field["description"] = col_obj["comment"]
        else:
            field["description"] = col_name  # Use display name as description

        # ── Field label (OSI v0.2 first-class) ──
        # Surfaced from OBSL-vendor customExtensions ``obml_field_label`` —
        # round-trip path for OSI → OBML → OSI fidelity. OBML has no
        # native column ``label`` today.
        for ext in col_obj.get("customExtensions", []) or []:
            if ext.get("vendor") != "OBSL":
                continue
            try:
                ext_label_data = json.loads(ext.get("data", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if ext_label_data.get("obml_field_label"):
                field["label"] = ext_label_data["obml_field_label"]
                self._record_vendor("OBSL")
                break

        # Restore ai_context from customExtensions (OSI vendor) if present
        ai_ctx: dict[str, Any] = {}
        for ext in col_obj.get("customExtensions", []):
            if ext.get("vendor") == "OSI":
                try:
                    ai_data = json.loads(ext.get("data", "{}"))
                    if ai_data:
                        ai_ctx.update(ai_data)
                except (json.JSONDecodeError, TypeError):
                    pass

        # Merge native OBML column synonyms into ai_context
        obml_col_synonyms = col_obj.get("synonyms", [])
        if obml_col_synonyms:
            existing = ai_ctx.get("synonyms", [])
            merged = list(existing) + [s for s in obml_col_synonyms if s not in existing]
            ai_ctx["synonyms"] = merged

        # Build ai_context with synonyms from display name
        display_synonym = col_name.lower()
        code_clean = code.lower()
        if display_synonym != code_clean:
            synonyms.insert(0, col_name)
        if synonyms:
            ai_ctx.setdefault("synonyms", []).extend(
                s for s in synonyms if s not in ai_ctx.get("synonyms", [])
            )
        if ai_ctx:
            field["ai_context"] = ai_ctx

        # Preserve OBML type info in custom_extensions for roundtrip fidelity
        abstract_type = col_obj.get("abstractType", "string")
        osi_type = OBML_TO_OSI_TYPE.get(abstract_type, "string")
        ext_data: dict[str, Any] = {
            "data_type": osi_type,
            "obml_abstract_type": abstract_type,
        }
        # Preserve OBML-only column properties
        if col_obj.get("sqlType"):
            ext_data["obml_sql_type"] = col_obj["sqlType"]
        if col_obj.get("sqlPrecision") is not None:
            ext_data["obml_sql_precision"] = col_obj["sqlPrecision"]
        if col_obj.get("sqlScale") is not None:
            ext_data["obml_sql_scale"] = col_obj["sqlScale"]
        if col_obj.get("numClass"):
            ext_data["obml_num_class"] = col_obj["numClass"]
        if col_obj.get("comment"):
            ext_data["obml_comment"] = col_obj["comment"]
        if col_obj.get("owner"):
            ext_data["obml_owner"] = col_obj["owner"]
        # Preserve OBML-only dimension properties (timeGrain, format, resultType, etc.)
        for _dim_name, dim_obj in obml_dimensions.items():
            if dim_obj.get("dataObject") == do_name and dim_obj.get("column") == col_name:
                if dim_obj.get("timeGrain"):
                    ext_data["obml_time_grain"] = dim_obj["timeGrain"]
                if dim_obj.get("format"):
                    ext_data["obml_dimension_format"] = dim_obj["format"]
                if dim_obj.get("resultType"):
                    ext_data["obml_dimension_result_type"] = dim_obj["resultType"]
                if dim_obj.get("description"):
                    ext_data["obml_dimension_description"] = dim_obj["description"]
                if dim_obj.get("owner"):
                    ext_data["obml_dimension_owner"] = dim_obj["owner"]
                if dim_obj.get("via"):
                    ext_data["obml_dimension_via"] = dim_obj["via"]
                break
        field["custom_extensions"] = [
            {
                "vendor_name": "COMMON",
                "data": json.dumps(ext_data),
            }
        ]

        return field

    def _convert_join_to_relationship(
        self, osi_from_name: str, _obml_from_name: str, from_do: dict, join: dict, index: int
    ) -> dict | None:
        """Convert an OBML join to an OSI relationship."""
        join_to_display = join.get("joinTo", "")
        # Use the OBML display name as the OSI target name (consistent with
        # _convert_data_object which uses display name as OSI dataset name)
        to_name = join_to_display
        target_do = self.obml.get("dataObjects", {}).get(join_to_display, {})

        # Map column display names to codes
        from_columns_display = join.get("columnsFrom", [])
        to_columns_display = join.get("columnsTo", [])

        from_cols = self._resolve_column_codes(from_do, from_columns_display)
        to_cols = self._resolve_column_codes(target_do, to_columns_display)

        # Generate relationship name
        path_name = join.get("pathName", "")
        if path_name:
            rel_name = f"{osi_from_name}_to_{to_name}_{path_name}"
        else:
            rel_name = f"{osi_from_name}_to_{to_name}"
            if index > 0:
                rel_name += f"_{index}"

        rel: dict[str, Any] = {
            "name": rel_name,
            "from": osi_from_name,
            "to": to_name,
            "from_columns": from_cols,
            "to_columns": to_cols,
        }

        # Preserve secondary join info in ai_context
        if join.get("secondary"):
            rel["ai_context"] = {
                "instructions": (
                    f"Secondary/alternative join path"
                    f"{(' named: ' + path_name) if path_name else ''}. "
                    f"Use only when explicitly needed."
                )
            }

        return rel

    def _resolve_column_codes(self, do_obj: dict, col_display_names: list) -> list:
        """Resolve OBML column display names to their code values."""
        columns = do_obj.get("columns", {})
        codes = []
        for display in col_display_names:
            col = columns.get(display, {})
            codes.append(col.get("code", display.lower().replace(" ", "_")))
        return codes

    def _convert_measures_and_metrics(
        self, obml_measures: dict, obml_metrics: dict, data_objects: dict
    ) -> list:
        """Convert OBML measures and metrics to OSI metrics."""
        osi_metrics = []

        # Convert each OBML measure to an OSI metric
        for measure_name, measure_obj in obml_measures.items():
            osi_metric = self._convert_measure(measure_name, measure_obj, data_objects)
            if osi_metric:
                osi_metrics.append(osi_metric)

        # Convert OBML metrics (which reference measures) to OSI metrics
        for metric_name, metric_obj in obml_metrics.items():
            if metric_obj.get("type") == "cumulative":
                osi_metric = self._convert_obml_cumulative_metric(
                    metric_name, metric_obj, obml_measures, data_objects
                )
            elif metric_obj.get("type") == "period_over_period":
                osi_metric = self._convert_obml_pop_metric(
                    metric_name, metric_obj, obml_measures, data_objects
                )
            elif metric_obj.get("type") == "window":
                osi_metric = self._convert_obml_window_metric(
                    metric_name, metric_obj, obml_measures, data_objects
                )
            else:
                osi_metric = self._convert_obml_metric(
                    metric_name, metric_obj, obml_measures, data_objects
                )
            if osi_metric:
                osi_metrics.append(osi_metric)

        return osi_metrics

    def _convert_measure(self, name: str, measure: dict, data_objects: dict) -> dict | None:
        """Convert an OBML measure to an OSI metric."""

        columns = measure.get("columns", [])
        agg = measure.get("aggregation", "sum").upper()
        distinct = measure.get("distinct", False)
        obml_synonyms = measure.get("synonyms", [])

        # Build ai_context with synonyms (name + native OBML synonyms)
        ai_synonyms = [name] + [s for s in obml_synonyms if s != name]
        ai_ctx: dict[str, Any] = {"synonyms": ai_synonyms} if ai_synonyms else {}

        # ``aggregation: measure`` delegates resolution to the engine
        # (Databricks Metric View) — there is no source column to read
        # and no ANSI_SQL expression to emit. OSI has no first-class
        # concept for engine-delegated aggregation, so we serialize the
        # measure as an OSI metric whose expression is the literal
        # ``MEASURE("<label>")`` call and merge the OBML signal into
        # the standard extras-blob so the reverse direction restores
        # ``aggregation: measure`` in a single round-trip.
        if agg == "MEASURE":
            expr = f'MEASURE("{name}")'
            measure_metric: dict[str, Any] = {
                "name": name,
                "expression": {
                    "dialects": [
                        {
                            "dialect": "ANSI_SQL",
                            "expression": expr,
                        }
                    ]
                },
                "description": measure.get("description", name),
            }
            if ai_ctx:
                measure_metric["ai_context"] = ai_ctx
            self._record_vendor("DATABRICKS")
            self._add_obml_measure_extras(
                measure_metric, {**measure, "_extra_obml_aggregation": "measure"}
            )
            return measure_metric

        if not columns:
            obml_expr = measure.get("expression", "")
            if obml_expr:
                # Expression-based measure: convert {[DO].[Col]} refs to SQL
                sql_inner = self._obml_refs_to_sql(obml_expr, data_objects)
                distinct_kw = "DISTINCT " if distinct else ""
                expr = f"{agg}({distinct_kw}{sql_inner})"
                result: dict[str, Any] = {
                    "name": name,
                    "expression": {
                        "dialects": [
                            {
                                "dialect": "ANSI_SQL",
                                "expression": expr,
                            }
                        ]
                    },
                    "description": measure.get("description", name),
                }
                if ai_ctx:
                    result["ai_context"] = ai_ctx
                self._add_obml_measure_extras(result, measure)
                return result
            self.warnings.append(f"Measure '{name}' has no columns; skipped.")
            return None

        # Build SQL expression from one-or-more column references.
        # Multi-column aggregations (CORR, COVAR_*, REGR_*) emit
        # ``AGG(col_a, col_b)`` with arguments in declaration order.
        def _col_to_sql(col_ref: dict) -> str:
            do_name_local = col_ref.get("dataObject", "")
            col_name_local = col_ref.get("column", "")
            do_obj_local = data_objects.get(do_name_local, {})
            col_code_local = (
                do_obj_local.get("columns", {})
                .get(col_name_local, {})
                .get("code", col_name_local.lower().replace(" ", "_"))
            )
            do_code_local = do_obj_local.get("code", do_name_local.lower().replace(" ", "_"))
            return f"{do_code_local}.{col_code_local}"

        distinct_kw = "DISTINCT " if distinct else ""
        col_sql = ", ".join(_col_to_sql(c) for c in columns)
        expr = f"{agg}({distinct_kw}{col_sql})"

        result = {
            "name": name,
            "expression": {
                "dialects": [
                    {
                        "dialect": "ANSI_SQL",
                        "expression": expr,
                    }
                ]
            },
            "description": measure.get("description", name),
        }
        if ai_ctx:
            result["ai_context"] = ai_ctx
        self._add_obml_measure_extras(result, measure)
        return result

    @staticmethod
    def _add_obml_measure_extras(result: dict, measure: dict) -> None:
        """Preserve OBML-only measure properties in custom_extensions for roundtrip."""
        extras: dict[str, Any] = {}
        if measure.get("filters"):
            extras["obml_filters"] = measure["filters"]
        if measure.get("total"):
            extras["obml_total"] = True
        if measure.get("allowFanOut"):
            extras["obml_allow_fan_out"] = True
        if measure.get("format"):
            extras["obml_format"] = measure["format"]
        if measure.get("delimiter"):
            extras["obml_delimiter"] = measure["delimiter"]
        if measure.get("withinGroup"):
            extras["obml_within_group"] = measure["withinGroup"]
        if measure.get("dataType"):
            extras["obml_data_type"] = measure["dataType"]
        if measure.get("owner"):
            extras["obml_owner"] = measure["owner"]
        if measure.get("grain"):
            extras["obml_grain"] = measure["grain"]
        if measure.get("filterContext"):
            extras["obml_filter_context"] = measure["filterContext"]
        # Internal pass-through marker for callers that need to inject an
        # extra obml_* key without growing the parameter surface (e.g.
        # ``aggregation: measure`` round-trips ``obml_aggregation``).
        if measure.get("_extra_obml_aggregation"):
            extras["obml_aggregation"] = measure["_extra_obml_aggregation"]
        if extras:
            exts = result.setdefault("custom_extensions", [])
            exts.append(
                {
                    "vendor_name": "COMMON",
                    "data": json.dumps(extras),
                }
            )

    def _obml_refs_to_sql(self, obml_expr: str, data_objects: dict) -> str:
        """Convert OBML {[DataObject].[Column]} references to SQL dataset.column."""
        import re

        def replace_ref(match: re.Match) -> str:
            do_name = match.group(1)
            col_name = match.group(2)
            do_obj = data_objects.get(do_name, {})
            do_code = do_obj.get("code", do_name.lower().replace(" ", "_"))
            col_code = (
                do_obj.get("columns", {})
                .get(col_name, {})
                .get("code", col_name.lower().replace(" ", "_"))
            )
            return f"{do_code}.{col_code}"

        return re.sub(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}", replace_ref, obml_expr)

    def _convert_obml_metric(
        self, name: str, metric: dict, obml_measures: dict, data_objects: dict
    ) -> dict | None:
        """
        Convert an OBML metric (expression referencing measures) to OSI metric.
        OBML metric expression: "{[Total Sales]} / {[Sales Count]}"
        → needs to be expanded to SQL using the measure definitions.
        """

        expr_template = metric.get("expression", "")
        if not expr_template:
            return None

        sql_expr = expr_template

        # Replace {[Measure Name]} references with SQL expressions
        pattern = r"\{\[([^\]]+)\]\}"
        for match in re.finditer(pattern, expr_template):
            measure_name = match.group(1)
            measure_def = obml_measures.get(measure_name, {})

            if not measure_def:
                self.warnings.append(
                    f"Metric '{name}' references unknown measure '{measure_name}'."
                )
                continue

            sql_part = self._measure_to_sql(measure_def, data_objects)
            if sql_part:
                sql_expr = sql_expr.replace(match.group(0), sql_part)
            else:
                self.warnings.append(
                    f"Metric '{name}': could not convert measure '{measure_name}' to SQL."
                )

        result: dict[str, Any] = {
            "name": name,
            "expression": {
                "dialects": [
                    {
                        "dialect": "ANSI_SQL",
                        "expression": sql_expr,
                    }
                ]
            },
            "description": metric.get("description", name),
        }
        # Include OBML synonyms in ai_context
        obml_synonyms = metric.get("synonyms", [])
        ai_synonyms = [s for s in obml_synonyms if s != name]
        if ai_synonyms:
            result["ai_context"] = {"synonyms": ai_synonyms}
        # Preserve OBML-only metric properties in custom_extensions
        metric_extras: dict[str, Any] = {}
        if metric.get("format"):
            metric_extras["obml_format"] = metric["format"]
        if metric.get("dataType"):
            metric_extras["obml_data_type"] = metric["dataType"]
        if metric.get("owner"):
            metric_extras["obml_owner"] = metric["owner"]
        if metric_extras:
            exts = result.setdefault("custom_extensions", [])
            exts.append(
                {
                    "vendor_name": "COMMON",
                    "data": json.dumps(metric_extras),
                }
            )
        return result

    def _convert_obml_cumulative_metric(
        self,
        name: str,
        metric: dict,
        obml_measures: dict,
        data_objects: dict,
    ) -> dict | None:
        """Convert an OBML cumulative metric to an OSI metric.

        Cumulative metrics have no direct OSI equivalent.  We generate an
        approximate SQL expression for readability and store the full OBML
        cumulative configuration in ``custom_extensions`` (vendor ``COMMON``)
        so the OSI → OBML direction can reconstruct it losslessly.
        """
        measure_name = metric.get("measure", "")
        time_dim = metric.get("timeDimension", "")
        cum_type = metric.get("cumulativeType", "sum").upper()
        window_size = metric.get("window")
        grain = metric.get("grainToDate")

        if not measure_name:
            self.warnings.append(f"Cumulative metric '{name}' has no measure reference; skipped.")
            return None

        # Build an approximate SQL expression for the OSI metric
        measure_def = obml_measures.get(measure_name, {})
        inner_sql = self._measure_to_sql(measure_def, data_objects) or measure_name

        if window_size is not None:
            frame = (
                f' OVER (ORDER BY "{time_dim}" '
                f"ROWS BETWEEN {window_size - 1} PRECEDING AND CURRENT ROW)"
            )
        elif grain:
            frame = (
                f' OVER (PARTITION BY DATE_TRUNC(\'{grain}\', "{time_dim}") ORDER BY "{time_dim}")'
            )
        else:
            # Running total (unbounded)
            frame = f' OVER (ORDER BY "{time_dim}" ROWS UNBOUNDED PRECEDING)'

        sql_expr = f"{cum_type}({inner_sql}){frame}"

        result: dict[str, Any] = {
            "name": name,
            "expression": {
                "dialects": [
                    {
                        "dialect": "ANSI_SQL",
                        "expression": sql_expr,
                    }
                ]
            },
            "description": metric.get("description", name),
        }

        obml_synonyms = metric.get("synonyms", [])
        ai_synonyms = [s for s in obml_synonyms if s != name]
        if ai_synonyms:
            result["ai_context"] = {"synonyms": ai_synonyms}

        # Store full cumulative config in custom_extensions for roundtrip
        ext_data: dict[str, Any] = {
            "obml_metric_type": "cumulative",
            "obml_cumulative_measure": measure_name,
            "obml_cumulative_time_dimension": time_dim,
            "obml_cumulative_type": metric.get("cumulativeType", "sum"),
        }
        if window_size is not None:
            ext_data["obml_cumulative_window"] = window_size
        if grain:
            ext_data["obml_cumulative_grain_to_date"] = grain
        if metric.get("partitionBy"):
            ext_data["obml_partition_by"] = list(metric["partitionBy"])
        if metric.get("format"):
            ext_data["obml_format"] = metric["format"]
        if metric.get("dataType"):
            ext_data["obml_data_type"] = metric["dataType"]
        if metric.get("owner"):
            ext_data["obml_owner"] = metric["owner"]

        result["custom_extensions"] = [
            {
                "vendor_name": "COMMON",
                "data": json.dumps(ext_data),
            }
        ]

        return result

    def _convert_obml_pop_metric(
        self,
        name: str,
        metric: dict,
        obml_measures: dict,
        data_objects: dict,
    ) -> dict | None:
        """Convert an OBML period-over-period metric to an OSI metric.

        PoP metrics have no direct OSI equivalent.  We generate an
        approximate SQL expression for readability and store the full OBML
        PoP configuration in ``custom_extensions`` (vendor ``COMMON``)
        so the OSI → OBML direction can reconstruct it losslessly.
        """
        pop_config = metric.get("periodOverPeriod", {})
        expr_template = metric.get("expression", "")
        if not pop_config or not expr_template:
            self.warnings.append(
                f"Period-over-period metric '{name}' missing configuration; skipped."
            )
            return None

        time_dim = pop_config.get("timeDimension", "")
        grain = pop_config.get("grain", "month")
        offset = pop_config.get("offset", -1)
        offset_grain = pop_config.get("offsetGrain", "year")
        comparison = pop_config.get("comparison", "percentChange")

        # Resolve the base measure SQL for an approximate expression
        pattern = r"\{\[([^\]]+)\]\}"
        measure_names = re.findall(pattern, expr_template)
        base_measure = measure_names[0] if measure_names else "measure"
        measure_def = obml_measures.get(base_measure, {})
        inner_sql = self._measure_to_sql(measure_def, data_objects) or base_measure

        # Build approximate SQL comment-style expression
        comparison_map = {
            "percentChange": f"({inner_sql} / NULLIF(prev.value, 0)) - 1",
            "ratio": f"{inner_sql} / NULLIF(prev.value, 0)",
            "difference": f"{inner_sql} - prev.value",
            "previousValue": "prev.value",
        }
        sql_expr = comparison_map.get(comparison, f"{inner_sql} -- PoP({comparison})")

        result: dict[str, Any] = {
            "name": name,
            "expression": {
                "dialects": [
                    {
                        "dialect": "ANSI_SQL",
                        "expression": sql_expr,
                    }
                ]
            },
            "description": metric.get("description", name),
        }

        obml_synonyms = metric.get("synonyms", [])
        ai_synonyms = [s for s in obml_synonyms if s != name]
        if ai_synonyms:
            result["ai_context"] = {"synonyms": ai_synonyms}

        # Store full PoP config in custom_extensions for roundtrip
        ext_data: dict[str, Any] = {
            "obml_metric_type": "period_over_period",
            "obml_pop_expression": expr_template,
            "obml_pop_time_dimension": time_dim,
            "obml_pop_grain": grain,
            "obml_pop_offset": offset,
            "obml_pop_offset_grain": offset_grain,
            "obml_pop_comparison": comparison,
        }
        if metric.get("format"):
            ext_data["obml_format"] = metric["format"]
        if metric.get("dataType"):
            ext_data["obml_data_type"] = metric["dataType"]
        if metric.get("owner"):
            ext_data["obml_owner"] = metric["owner"]

        result["custom_extensions"] = [
            {
                "vendor_name": "COMMON",
                "data": json.dumps(ext_data),
            }
        ]

        return result

    def _convert_obml_window_metric(
        self,
        name: str,
        metric: dict,
        obml_measures: dict,
        data_objects: dict,
    ) -> dict | None:
        """Convert an OBML window metric (rank/lag/lead/ntile/...) to an OSI metric.

        Window metrics have no direct OSI equivalent. We generate an
        approximate ANSI SQL expression for readability and persist the
        full OBML window configuration in ``custom_extensions`` (vendor
        ``COMMON``) for lossless OSI → OBML reconstruction.
        """
        window_fn = (metric.get("windowFunction") or "").upper()
        if not window_fn:
            self.warnings.append(f"Window metric '{name}' has no windowFunction; skipped.")
            return None

        measure_name = metric.get("measure")
        time_dim = metric.get("timeDimension")
        order_dir = metric.get("orderDirection", "desc").upper()
        offset = metric.get("offset")
        buckets = metric.get("buckets")
        default_value = metric.get("defaultValue")
        partition_by = metric.get("partitionBy", []) or []

        measure_def = obml_measures.get(measure_name, {}) if measure_name else {}
        inner_sql = (
            self._measure_to_sql(measure_def, data_objects) if measure_def else (measure_name or "")
        )

        # Build approximate ANSI SQL expression
        args: list[str] = []
        order_expr: str | None = None
        if window_fn in {"LAG", "LEAD"} and inner_sql:
            args.append(inner_sql)
            if offset is not None:
                args.append(str(offset))
            if default_value is not None:
                args.append(
                    f"'{default_value}'" if isinstance(default_value, str) else str(default_value)
                )
        elif window_fn == "NTILE" and buckets is not None:
            args.append(str(buckets))
        elif window_fn in {"FIRST_VALUE", "LAST_VALUE"} and inner_sql:
            args.append(inner_sql)
        # RANK / DENSE_RANK / ROW_NUMBER take no positional args

        if window_fn in {"RANK", "DENSE_RANK", "ROW_NUMBER", "NTILE"} and inner_sql:
            order_expr = f"{inner_sql} {order_dir}"
        elif window_fn in {"LAG", "LEAD"} and time_dim:
            order_expr = f'"{time_dim}"'
        elif window_fn in {"FIRST_VALUE", "LAST_VALUE"} and time_dim:
            order_expr = f'"{time_dim}" {order_dir}'

        partition_sql = (
            "PARTITION BY " + ", ".join(f'"{p}"' for p in partition_by) if partition_by else ""
        )
        order_sql = f"ORDER BY {order_expr}" if order_expr else ""
        over_inner = " ".join(p for p in (partition_sql, order_sql) if p)
        sql_expr = f"{window_fn}({', '.join(args)}) OVER ({over_inner})"

        result: dict[str, Any] = {
            "name": name,
            "expression": {
                "dialects": [
                    {
                        "dialect": "ANSI_SQL",
                        "expression": sql_expr,
                    }
                ]
            },
            "description": metric.get("description", name),
        }

        obml_synonyms = metric.get("synonyms", [])
        ai_synonyms = [s for s in obml_synonyms if s != name]
        if ai_synonyms:
            result["ai_context"] = {"synonyms": ai_synonyms}

        ext_data: dict[str, Any] = {
            "obml_metric_type": "window",
            "obml_window_function": window_fn.lower(),
            "obml_order_direction": metric.get("orderDirection", "desc"),
        }
        if measure_name:
            ext_data["obml_window_measure"] = measure_name
        if time_dim:
            ext_data["obml_window_time_dimension"] = time_dim
        if offset is not None:
            ext_data["obml_window_offset"] = offset
        if buckets is not None:
            ext_data["obml_window_buckets"] = buckets
        if default_value is not None:
            ext_data["obml_window_default_value"] = default_value
        if partition_by:
            ext_data["obml_partition_by"] = list(partition_by)
        if metric.get("format"):
            ext_data["obml_format"] = metric["format"]
        if metric.get("dataType"):
            ext_data["obml_data_type"] = metric["dataType"]
        if metric.get("owner"):
            ext_data["obml_owner"] = metric["owner"]

        result["custom_extensions"] = [
            {
                "vendor_name": "COMMON",
                "data": json.dumps(ext_data),
            }
        ]

        return result

    def _measure_to_sql(self, measure: dict, data_objects: dict) -> str | None:
        """Convert an OBML measure definition to a SQL expression string.

        Multi-column measures (two-column statistical aggregates like
        ``corr(a, b)``, or count-distinct over a composite key) emit
        every column as a comma-separated argument list. Single-column
        aggregates collapse to the same shape with one argument.
        """
        agg = measure.get("aggregation", "sum").upper()
        distinct = measure.get("distinct", False)
        distinct_kw = "DISTINCT " if distinct else ""

        columns = measure.get("columns", [])
        if columns:
            args: list[str] = []
            for col_ref in columns:
                do_name = col_ref.get("dataObject", "")
                col_name = col_ref.get("column", "")
                do_obj = data_objects.get(do_name, {})
                col_code = (
                    do_obj.get("columns", {})
                    .get(col_name, {})
                    .get("code", col_name.lower().replace(" ", "_"))
                )
                do_code = do_obj.get("code", do_name.lower().replace(" ", "_"))
                args.append(f"{do_code}.{col_code}")
            return f"{agg}({distinct_kw}{', '.join(args)})"

        obml_expr = measure.get("expression", "")
        if obml_expr:
            sql_inner = self._obml_refs_to_sql(obml_expr, data_objects)
            return f"{agg}({distinct_kw}{sql_inner})"

        return None


class OBMLtoOSIOntology:
    """Derive an OSI **ontology** document from an OBML semantic model.

    Produces a document conforming to ``osi-ontology-schema.json`` (OSI version
    ``0.2.0.dev0``) — a *separate* artefact from the OSI core-spec semantic
    model. The OSI ``OntologyMap`` embeds the full core-spec model, so this
    class reuses :class:`OBMLtoOSI` for that part and overlays a concept /
    relationship ontology plus the logical-to-conceptual mappings.

    See ``osi_obml_ontology_mapping_analysis.md`` for the full mapping rules
    and the documented gaps that surface as ``warnings``.
    """

    # OBML join cardinality → OSI ontology multiplicity. ``many-to-many`` has
    # no OSI equivalent (the enum is ManyToOne/OneToOne only) and is skipped.
    _MULTIPLICITY_MAP = {
        "many-to-one": "ManyToOne",
        "one-to-one": "OneToOne",
    }

    def __init__(
        self,
        obml: dict,
        model_name: str = "semantic_model",
        model_description: str = "",
        ai_instructions: str = "",
    ):
        self.obml = obml
        self.model_name = model_name
        self.model_description = model_description
        self.ai_instructions = ai_instructions
        self.warnings: list[str] = []

    @staticmethod
    def _table_ref(do_obj: dict, fallback: str) -> str:
        """Best-effort physical table identifier for SQL mapping expressions."""
        database = do_obj.get("database", "")
        schema = do_obj.get("schema", "")
        code = do_obj.get("code", "")
        source = f"{database}.{schema}.{code}" if database else code
        if source:
            return source.split(".")[-1]
        return fallback

    @staticmethod
    def _col_code(do_obj: dict, display: str) -> str:
        col = (do_obj.get("columns", {}) or {}).get(display, {})
        return col.get("code", display.lower().replace(" ", "_"))

    def _entity_key_expr(self, do_name: str, do_obj: dict, table_ref: str) -> str | None:
        """Identifying SQL expression for an entity (primary key, else first column)."""
        cols = do_obj.get("columns", {}) or {}
        pk = [c for c, col in cols.items() if col.get("primaryKey")]
        if not pk:
            if not cols:
                return None
            first = next(iter(cols))
            self.warnings.append(
                f"Entity '{do_name}' has no primary key; identified by first column '{first}'"
            )
            return f"{table_ref}.{self._col_code(do_obj, first)}"
        if len(pk) > 1:
            self.warnings.append(
                f"Entity '{do_name}' has a composite primary key; ontology object "
                f"mapping uses only the first key column '{pk[0]}'"
            )
        return f"{table_ref}.{self._col_code(do_obj, pk[0])}"

    def convert(self) -> dict:
        # 1. Core semantic model (embedded — required by OSI OntologyMap).
        core_conv = OBMLtoOSI(
            self.obml,
            model_name=self.model_name,
            model_description=self.model_description,
            ai_instructions=self.ai_instructions,
        )
        sem_model = core_conv.convert()["semantic_model"][0]
        self.warnings.extend(core_conv.warnings)

        data_objects = self.obml.get("dataObjects", {}) or {}
        table_refs = {n: self._table_ref(o, n) for n, o in data_objects.items()}

        # 2. Ontology components (one EntityType concept per dataObject) plus
        #    the outgoing relationships keyed by that concept. Collect link
        #    info so concept_mappings can bind relationships to FK columns.
        ontology: list[dict[str, Any]] = []
        rel_links: dict[str, list[tuple[str, str, str | None]]] = {}

        for do_name, do_obj in data_objects.items():
            concept: dict[str, Any] = {"name": do_name, "type": "EntityType"}
            desc = do_obj.get("description") or do_obj.get("comment")
            if desc:
                concept["description"] = desc
            component: dict[str, Any] = {"concept": concept}

            relationships: list[dict[str, Any]] = []
            used_names: set[str] = set()
            for join in do_obj.get("joins", []) or []:
                to_name = join.get("joinTo", "")
                if not to_name:
                    continue
                join_type = (join.get("joinType") or "").lower()
                if join_type == "many-to-many":
                    self.warnings.append(
                        f"Join {do_name} -> {to_name} is many-to-many; skipped "
                        f"(OSI ontology multiplicity supports only ManyToOne/OneToOne)"
                    )
                    continue
                multiplicity = self._MULTIPLICITY_MAP.get(join_type)
                if multiplicity is None:
                    multiplicity = "ManyToOne"
                    self.warnings.append(
                        f"Join {do_name} -> {to_name} has unknown joinType "
                        f"'{join.get('joinType', '')}'; defaulting multiplicity to ManyToOne"
                    )

                path_name = join.get("pathName", "")
                rel_name = f"{do_name}_to_{to_name}"
                if path_name:
                    rel_name = f"{rel_name}_{path_name}"
                    self.warnings.append(
                        f"Join {do_name} -> {to_name} is a named/secondary path "
                        f"('{path_name}'); emitted as an ordinary relationship "
                        f"(named-path semantics are not represented in OSI ontology)"
                    )
                elif join.get("secondary"):
                    self.warnings.append(
                        f"Join {do_name} -> {to_name} is a secondary path; emitted as an "
                        f"ordinary relationship (alternate-path semantics are lost)"
                    )
                base = rel_name
                dedup = 1
                while rel_name in used_names:
                    rel_name = f"{base}_{dedup}"
                    dedup += 1
                used_names.add(rel_name)

                relationships.append(
                    {
                        "name": rel_name,
                        "roles": [{"concept": to_name}],
                        "multiplicity": multiplicity,
                        "verbalizes": [f"{{{do_name}}} relates to {{{to_name}}}"],
                    }
                )

                from_cols = join.get("columnsFrom", []) or []
                fk_expr: str | None = None
                if from_cols:
                    if len(from_cols) > 1:
                        self.warnings.append(
                            f"Join {do_name} -> {to_name} has a composite key; link "
                            f"mapping uses only the first column '{from_cols[0]}'"
                        )
                    fk_expr = f"{table_refs[do_name]}.{self._col_code(do_obj, from_cols[0])}"
                rel_links.setdefault(do_name, []).append((rel_name, to_name, fk_expr))

            if relationships:
                component["relationships"] = relationships
            ontology.append(component)

        # 3. Concept mappings: object_mappings identify each entity; link_mappings
        #    bind each relationship to its foreign-key column.
        concept_mappings: list[dict[str, Any]] = []
        for do_name, do_obj in data_objects.items():
            cm: dict[str, Any] = {"concept": do_name}
            key_expr = self._entity_key_expr(do_name, do_obj, table_refs[do_name])
            if key_expr:
                cm["object_mappings"] = [{"expression": key_expr}]
            links: list[dict[str, Any]] = []
            for rel_name, to_name, fk_expr in rel_links.get(do_name, []):
                obj_map: dict[str, Any] = {"concept": to_name}
                if fk_expr:
                    obj_map["expression"] = fk_expr
                links.append({"relationship": rel_name, "object_mapping": obj_map})
            if links:
                cm["link_mappings"] = links
            if "object_mappings" in cm or "link_mappings" in cm:
                concept_mappings.append(cm)

        # 4. Assemble the ontology document.
        ontology_doc: dict[str, Any] = {"version": _OSI_VERSION, "name": self.model_name}
        model_desc = self.obml.get("description", "") or self.model_description
        if model_desc:
            ontology_doc["description"] = model_desc
        if self.ai_instructions:
            ontology_doc["ai_context"] = {"instructions": self.ai_instructions}
        ontology_doc["ontology"] = ontology
        ontology_doc["ontology_mappings"] = [
            {
                "name": f"{self.model_name}_map",
                "semantic_model": sem_model,
                "concept_mappings": concept_mappings,
            }
        ]
        return ontology_doc


# ═══════════════════════════════════════════════════════════════════════════
#  Validation
# ═══════════════════════════════════════════════════════════════════════════

_SCRIPT_DIR = Path(__file__).resolve().parent
_SCHEMAS_DIR = _SCRIPT_DIR / "schemas"


def _resolve_obml_schema_path() -> Path:
    """Locate the OBML schema.

    In a built wheel (and the OSI monorepo copy) the canonical
    ``obml-schema.json`` is bundled into the package ``schemas/`` directory at
    build time (hatch ``force-include``). In an editable workspace install that
    bundling does not run, so fall back to the canonical source of truth at the
    repo-root ``schema/obml-schema.json`` by walking up from this file.
    """
    bundled = _SCHEMAS_DIR / "obml-schema.json"
    if bundled.exists():
        return bundled
    for parent in _SCRIPT_DIR.parents:
        candidate = parent / "schema" / "obml-schema.json"
        if candidate.exists():
            return candidate
    return bundled


# Locate schemas relative to this package. The two OSI schemas are vendored
# beside the converter; the OBML schema is resolved canonically (see above).
_OBML_SCHEMA_PATH = _resolve_obml_schema_path()
_OSI_SCHEMA_PATH = _SCHEMAS_DIR / "osi-schema.json"
_OSI_ONTOLOGY_SCHEMA_PATH = _SCHEMAS_DIR / "osi-ontology-schema.json"

# The OSI ontology schema $refs the core-spec schema by its public raw URL for
# ``ai_context`` and the embedded ``semantic_model``. Resolve that URL against
# the vendored local copy so validation never touches the network.
_OSI_CORE_SPEC_RAW_URL = (
    "https://raw.githubusercontent.com/open-semantic-interchange/OSI/main/core-spec/osi-schema.json"
)


class ValidationResult:
    """Collects schema errors, semantic errors, and warnings."""

    def __init__(self, format_name: str = "OBML") -> None:
        self.format_name = format_name
        self.schema_errors: list[str] = []
        self.semantic_errors: list[str] = []
        self.semantic_warnings: list[str] = []

    @property
    def valid(self) -> bool:
        return not self.schema_errors and not self.semantic_errors

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        if self.schema_errors:
            lines.append(f"  JSON Schema: {len(self.schema_errors)} error(s)")
            for e in self.schema_errors:
                lines.append(f"    - {e}")
        else:
            lines.append("  JSON Schema: ✓ valid")
        if self.semantic_errors:
            lines.append(f"  Semantic:    {len(self.semantic_errors)} error(s)")
            for e in self.semantic_errors:
                lines.append(f"    - {e}")
        else:
            lines.append("  Semantic:    ✓ valid")
        if self.semantic_warnings:
            lines.append(f"  Warnings:    {len(self.semantic_warnings)}")
            for w in self.semantic_warnings:
                lines.append(f"    - {w}")
        return lines


def _validate_json_schema(
    data: dict[str, Any],
    schema_path: Path,
    result: ValidationResult,
    draft: str = "draft7",
    registry: Any | None = None,
) -> None:
    """Run JSON Schema validation, appending errors to *result*.

    *registry* is an optional ``referencing.Registry`` used to resolve external
    ``$ref`` URIs against local resources (avoids network fetches).
    """
    try:
        import jsonschema
    except ImportError:
        result.semantic_warnings.append(
            "jsonschema package not installed — skipping JSON Schema validation"
        )
        return

    if not schema_path.exists():
        result.semantic_warnings.append(
            f"Schema file not found at {schema_path} — skipping JSON Schema validation"
        )
        return

    with open(schema_path) as f:
        schema = json.load(f)

    validator_cls = (
        jsonschema.Draft202012Validator if draft == "draft2020" else jsonschema.Draft7Validator
    )
    validator = validator_cls(schema, registry=registry) if registry else validator_cls(schema)
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        result.schema_errors.append(f"[{path}] {error.message}")


def _osi_core_registry() -> Any | None:
    """Build a ``referencing.Registry`` that resolves the OSI core-spec schema
    URL (referenced by the ontology schema) to the vendored local copy. Returns
    ``None`` if the dependencies or the local core schema are unavailable, in
    which case the caller falls back to default (network) resolution."""
    try:
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError:
        return None
    if not _OSI_SCHEMA_PATH.exists():
        return None
    with open(_OSI_SCHEMA_PATH) as f:
        core = json.load(f)
    core_res = Resource.from_contents(core, default_specification=DRAFT202012)
    # Register under both the raw URL used by the ontology schema's $refs and
    # the core schema's own canonical $id (so its internal #/$defs refs resolve).
    resources = [(_OSI_CORE_SPEC_RAW_URL, core_res)]
    core_id = core_res.id()
    if core_id:
        resources.append((core_id, core_res))
    return Registry().with_resources(resources)


# ── OBML Validation ──────────────────────────────────────────────────────


def validate_obml(obml_dict: dict[str, Any], schema_path: Path | None = None) -> ValidationResult:
    """Validate an OBML dict against JSON Schema and semantic rules.

    Runs two layers of validation:
    1. **JSON Schema** — structural correctness (types, required fields,
       allowed properties) against ``schema/obml-schema.json``
    2. **Semantic** — reference integrity, cycle detection, duplicate
       identifiers via OrionBelt's ``ReferenceResolver`` + ``SemanticValidator``

    Both layers are optional — if ``jsonschema`` or ``orionbelt`` packages are
    not installed the corresponding checks are skipped with a warning.
    """
    result = ValidationResult("OBML")

    # 1. JSON Schema validation
    _validate_json_schema(obml_dict, schema_path or _OBML_SCHEMA_PATH, result, draft="draft7")

    # 2. Semantic validation (ReferenceResolver + SemanticValidator)
    try:
        from orionbelt.parser.resolver import ReferenceResolver
        from orionbelt.parser.validator import SemanticValidator
    except ImportError:
        result.semantic_warnings.append(
            "orionbelt package not installed — skipping semantic validation"
        )
    else:
        resolver = ReferenceResolver()
        model, resolve_result = resolver.resolve(obml_dict)
        if not resolve_result.valid:
            for err in resolve_result.errors:
                path_info = f" (at {err.path})" if err.path else ""
                suggestions = ""
                if err.suggestions:
                    suggestions = f" Did you mean: {', '.join(err.suggestions)}?"
                result.semantic_errors.append(f"[{err.code}] {err.message}{path_info}{suggestions}")
        for warn in resolve_result.warnings:
            result.semantic_warnings.append(f"[{warn.code}] {warn.message}")

        # Run SemanticValidator only if resolution produced a usable model
        if resolve_result.valid:
            sem_validator = SemanticValidator()
            sem_errors = sem_validator.validate(model)
            for err in sem_errors:
                path_info = f" (at {err.path})" if err.path else ""
                result.semantic_errors.append(f"[{err.code}] {err.message}{path_info}")

    return result


# ── OSI Validation ───────────────────────────────────────────────────────


def validate_osi(osi_dict: dict[str, Any], schema_path: Path | None = None) -> ValidationResult:
    """Validate an OSI dict against JSON Schema and semantic rules.

    Runs three layers of validation (mirroring OSI's own ``validate.py``):
    1. **JSON Schema** — structural correctness against ``osi-schema.json``
       (Draft 2020-12)
    2. **Unique names** — datasets, fields, metrics, relationships
    3. **References** — relationship from/to reference existing datasets
    """
    result = ValidationResult("OSI")

    # 1. JSON Schema validation (OSI uses Draft 2020-12)
    _validate_json_schema(osi_dict, schema_path or _OSI_SCHEMA_PATH, result, draft="draft2020")

    # 2. Unique name checks
    for model in osi_dict.get("semantic_model", []):
        model_name = model.get("name", "<unnamed>")

        # Unique dataset names
        dataset_names: list[str] = []
        for ds in model.get("datasets", []):
            name = ds.get("name", "")
            if name in dataset_names:
                result.semantic_errors.append(
                    f"[DUPLICATE_DATASET] Duplicate dataset name '{name}' in model '{model_name}'"
                )
            dataset_names.append(name)

        # Unique field names within each dataset
        for ds in model.get("datasets", []):
            ds_name = ds.get("name", "<unnamed>")
            field_names: list[str] = []
            for field in ds.get("fields", []):
                fname = field.get("name", "")
                if fname in field_names:
                    result.semantic_errors.append(
                        f"[DUPLICATE_FIELD] Duplicate field name '{fname}' in dataset '{ds_name}'"
                    )
                field_names.append(fname)

        # Unique metric names
        metric_names: list[str] = []
        for m in model.get("metrics", []):
            mname = m.get("name", "")
            if mname in metric_names:
                result.semantic_errors.append(
                    f"[DUPLICATE_METRIC] Duplicate metric name '{mname}' in model '{model_name}'"
                )
            metric_names.append(mname)

        # Unique relationship names
        rel_names: list[str] = []
        for r in model.get("relationships", []):
            rname = r.get("name", "")
            if rname in rel_names:
                result.semantic_errors.append(
                    f"[DUPLICATE_RELATIONSHIP] Duplicate relationship name "
                    f"'{rname}' in model '{model_name}'"
                )
            rel_names.append(rname)

    # 3. Reference checks — relationships reference existing datasets
    for model in osi_dict.get("semantic_model", []):
        ds_name_set = {ds.get("name") for ds in model.get("datasets", []) if ds.get("name")}
        for rel in model.get("relationships", []):
            rel_name = rel.get("name", "<unnamed>")
            from_ds = rel.get("from")
            to_ds = rel.get("to")
            if from_ds and from_ds not in ds_name_set:
                result.semantic_errors.append(
                    f"[UNKNOWN_DATASET_REF] Relationship '{rel_name}' "
                    f"references unknown dataset '{from_ds}'"
                )
            if to_ds and to_ds not in ds_name_set:
                result.semantic_errors.append(
                    f"[UNKNOWN_DATASET_REF] Relationship '{rel_name}' "
                    f"references unknown dataset '{to_ds}'"
                )

    return result


def validate_osi_ontology(
    onto_dict: dict[str, Any], schema_path: Path | None = None
) -> ValidationResult:
    """Validate an OSI ontology dict against JSON Schema and semantic rules.

    1. **JSON Schema** — structural correctness against ``osi-ontology-schema.json``
       (Draft 2020-12). External ``$ref``s to the core-spec schema are resolved
       against the vendored local copy via a ``referencing`` registry.
    2. **Unique concept names** across the ``ontology`` components.
    3. **Reference integrity** — relationship roles and concept_mappings
       reference concepts defined in the ontology.
    """
    result = ValidationResult("OSI-ONTOLOGY")

    # 1. JSON Schema validation (offline external-ref resolution).
    _validate_json_schema(
        onto_dict,
        schema_path or _OSI_ONTOLOGY_SCHEMA_PATH,
        result,
        draft="draft2020",
        registry=_osi_core_registry(),
    )

    # 2. Unique concept names + collect the defined set.
    defined: set[str] = set()
    for comp in onto_dict.get("ontology", []):
        name = comp.get("concept", {}).get("name", "")
        if name in defined:
            result.semantic_errors.append(f"[DUPLICATE_CONCEPT] Duplicate concept name '{name}'")
        defined.add(name)

    # 3. Reference integrity — roles reference defined concepts.
    for comp in onto_dict.get("ontology", []):
        for rel in comp.get("relationships", []):
            rel_name = rel.get("name", "<unnamed>")
            for role in rel.get("roles", []):
                rc = role.get("concept")
                if rc and rc not in defined:
                    result.semantic_errors.append(
                        f"[UNKNOWN_CONCEPT_REF] Relationship '{rel_name}' role "
                        f"references unknown concept '{rc}'"
                    )

    # concept_mappings reference defined concepts.
    for omap in onto_dict.get("ontology_mappings", []):
        for cm in omap.get("concept_mappings", []):
            cc = cm.get("concept")
            if cc and cc not in defined:
                result.semantic_errors.append(
                    f"[UNKNOWN_CONCEPT_REF] Concept mapping references unknown concept '{cc}'"
                )

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="OSI ↔ OBML Bidirectional Converter")
    parser.add_argument("direction", choices=["osi2obml", "obml2osi"], help="Conversion direction")
    parser.add_argument("input", nargs="?", help="Input YAML file")
    parser.add_argument("-o", "--output", help="Output YAML file")
    parser.add_argument(
        "--name", default="semantic_model", help="Model name for OBML→OSI conversion"
    )
    parser.add_argument(
        "--description", default="", help="Model description for OBML→OSI conversion"
    )
    parser.add_argument(
        "--ai-instructions", default="", help="AI instructions for OBML→OSI conversion"
    )
    parser.add_argument(
        "--database", default="ANALYTICS", help="Default database for OSI→OBML conversion"
    )
    parser.add_argument("--schema", default="PUBLIC", help="Default schema for OSI→OBML conversion")
    parser.add_argument(
        "--no-validate", action="store_true", help="Skip OBML validation after conversion"
    )

    args = parser.parse_args()

    if not args.input:
        parser.error("Input file is required for conversion")

    input_path = Path(args.input)
    with open(input_path) as f:
        data = yaml.safe_load(f)

    if args.direction == "osi2obml":
        converter = OSItoOBML(data, args.database, args.schema)
        result = converter.convert()
        warnings = converter.warnings
    else:
        converter = OBMLtoOSI(data, args.name, args.description, args.ai_instructions)
        result = converter.convert()
        warnings = converter.warnings

    # Output
    output_yaml = yaml.dump(
        result, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_yaml)
        print(f"✅ Converted to {args.output}")
    else:
        print(output_yaml)

    if warnings:
        print("\n⚠️  Conversion warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)

    # ── Validate output ────────────────────────────────────────────────
    if not args.no_validate:
        has_errors = False

        if args.direction == "osi2obml":
            # Validate OBML output
            print("\n🔍 Validating OBML output...", file=sys.stderr)
            vr = validate_obml(result)
            for line in vr.summary_lines():
                print(line, file=sys.stderr)
            if vr.valid:
                print("✅ OBML output is valid", file=sys.stderr)
            else:
                print("❌ OBML output has validation errors", file=sys.stderr)
                has_errors = True
        else:
            # Validate OBML input (source) and OSI output
            print("\n🔍 Validating OBML input...", file=sys.stderr)
            vr_obml = validate_obml(data)
            for line in vr_obml.summary_lines():
                print(line, file=sys.stderr)
            if vr_obml.valid:
                print("✅ OBML input is valid", file=sys.stderr)
            else:
                print("❌ OBML input has validation errors", file=sys.stderr)
                has_errors = True

            print("\n🔍 Validating OSI output...", file=sys.stderr)
            vr_osi = validate_osi(result)
            for line in vr_osi.summary_lines():
                print(line, file=sys.stderr)
            if vr_osi.valid:
                print("✅ OSI output is valid", file=sys.stderr)
            else:
                print("❌ OSI output has validation errors", file=sys.stderr)
                has_errors = True

        if has_errors:
            sys.exit(1)


if __name__ == "__main__":
    main()
