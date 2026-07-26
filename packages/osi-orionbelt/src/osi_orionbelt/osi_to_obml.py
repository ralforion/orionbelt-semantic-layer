"""OSI → OBML conversion (the :class:`OSItoOBML` direction).

Extracted verbatim from ``converter.py``; see that module for the package-level
docstring and the shared constants in :mod:`osi_orionbelt._common`.
"""

from __future__ import annotations

import json
import re
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from osi_orionbelt._common import (
    _INTERNAL_VENDORS,
    _OBML_VENDOR_READ,
    _OSI_VERSION,
    _SQL_PARSEABLE_DIALECTS,
    _VENDOR_OSI,
    OSI_DATATYPE_TO_OBML_ABSTRACT,
    OSI_DATATYPE_TO_OBML_PHYSICAL,
    OSI_TO_OBML_TYPE,
)

# OSI SQL-dialect tag -> sqlglot ``read`` dialect for metric parsing. ANSI maps
# to sqlglot's default (None). Reading with the source dialect means a
# Snowflake/Databricks-authored aggregation is parsed under the right grammar.
_OSI_DIALECT_TO_SQLGLOT: dict[str, str | None] = {
    "ANSI_SQL": None,
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
}

# sqlglot aggregate ``sql_name()`` -> OBML aggregation, for the aggregates OBML
# models as a *single-column* measure. Multi-argument aggregates OBML also
# supports (``CORR``, ``COVAR_POP``, ``REGR_SLOPE``, ...) are intentionally
# absent: they need a two-column measure, so they are preserved rather than
# emitted with a dropped argument (see ``_agg_arg``). ``COUNT(DISTINCT ...)``
# maps to ``count`` with the distinct flag carried separately. Note the name
# normalisation: sqlglot renders ``VAR_POP`` as ``VARIANCE_POP``.
_SQLGLOT_AGG_TO_OBML = {
    "SUM": "sum",
    "COUNT": "count",
    "AVG": "avg",
    "MIN": "min",
    "MAX": "max",
    "ANY_VALUE": "any_value",
    "MEDIAN": "median",
    "MODE": "mode",
    "STDDEV": "stddev",
    "STDDEV_POP": "stddev_pop",
    "VARIANCE": "variance",
    "VARIANCE_POP": "var_pop",
}

# Aggregates sqlglot parses as a generic ``exp.Anonymous`` (not ``exp.AggFunc``),
# keyed by upper-cased function name. Single-argument only: ``LISTAGG(col)`` is a
# measure; ``LISTAGG(col, delimiter)`` carries a delimiter OBML can't model here,
# so it is preserved (see ``_agg_arg``).
_ANON_AGG_TO_OBML = {
    "LISTAGG": "listagg",
}

# Aggregations whose result is fractional regardless of the input column type.
# Everything else defaults to int, except listagg (string) - see
# ``_measure_result_type``. (Without the column's declared type this is a
# best-effort guess, matching the converter's other type heuristics.)
_FLOAT_RESULT_AGGS = frozenset(
    {"sum", "avg", "median", "stddev", "stddev_pop", "variance", "var_pop"}
)


class OSItoOBML:
    """Convert an OSI semantic model YAML to OBML format."""

    def __init__(
        self, osi: dict, default_database: str = "ANALYTICS", default_schema: str = "PUBLIC"
    ):
        self.osi = osi
        self.default_database = default_database
        self.default_schema = default_schema
        self.warnings: list[str] = []
        # OSI metrics that have no OBML representation (non-SQL dialect only,
        # or an expression our parser cannot decompose). Preserved verbatim
        # rather than dropped — see ``_preserve_unconverted_metric``.
        self._unconverted_metrics: list[dict] = []

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
        # Reset per-conversion accumulators so calling convert() twice on the
        # same instance is idempotent (no duplicated warnings or preserved
        # metrics). Both are populated as a side effect of conversion below.
        self.warnings = []
        self._unconverted_metrics = []

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

        # Metrics that have no OBML representation are not dropped: stash the
        # original OSI metric verbatim under the OSI vendor so the reverse
        # (OBML -> OSI) direction re-emits them and a full OSI -> OBML -> OSI
        # roundtrip stays lossless. They are not queryable in OBML; a LOSSY
        # warning was already recorded per metric.
        if self._unconverted_metrics:
            obml.setdefault("customExtensions", []).append(
                {
                    "vendor": _VENDOR_OSI,
                    "data": json.dumps({"obml_unconverted_metrics": self._unconverted_metrics}),
                }
            )

        # ── Restore model-level properties from custom_extensions ────
        for ext in model.get("custom_extensions", []):
            if ext.get("vendor_name") in _OBML_VENDOR_READ:
                try:
                    ext_data = json.loads(ext.get("data", "{}"))
                    if ext_data.get("obml_filters"):
                        obml["filters"] = ext_data["obml_filters"]
                    if ext_data.get("obml_settings"):
                        obml["settings"] = ext_data["obml_settings"]
                    if ext_data.get("obml_owner"):
                        obml["owner"] = ext_data["obml_owner"]
                    if ext_data.get("obml_expose_counts") is not None:
                        obml["exposeCounts"] = ext_data["obml_expose_counts"]
                    if ext_data.get("obml_count_label_pattern") is not None:
                        obml["countLabelPattern"] = ext_data["obml_count_label_pattern"]
                except (json.JSONDecodeError, TypeError):
                    pass
                break

        # Preserve third-party vendor extensions verbatim
        self._carry_foreign_extensions(model.get("custom_extensions"), obml)

        return obml

    @staticmethod
    def _carry_foreign_extensions(osi_exts: list[dict] | None, obml_target: dict[str, Any]) -> None:
        """Carry third-party OSI custom_extensions verbatim into OBML.

        Our own payloads and OSI-native stashes are reconstructed elsewhere;
        any other vendor's extension is preserved unchanged on the OBML side
        so a full OSI -> OBML -> OSI roundtrip keeps the original vendor.
        """
        for ext in osi_exts or []:
            vendor = ext.get("vendor_name")
            if vendor and vendor not in _INTERNAL_VENDORS:
                obml_target.setdefault("customExtensions", []).append(
                    {"vendor": vendor, "data": ext.get("data", "")}
                )

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
            if ext.get("vendor_name") in _OBML_VENDOR_READ:
                try:
                    ext_data = json.loads(ext.get("data", "{}"))
                    if ext_data.get("obml_owner"):
                        do["owner"] = ext_data["obml_owner"]
                    if ext_data.get("obml_comment"):
                        do["comment"] = ext_data["obml_comment"]
                    if ext_data.get("obml_refresh"):
                        do["refresh"] = ext_data["obml_refresh"]
                    if ext_data.get("obml_countable") is not None:
                        do["countable"] = ext_data["obml_countable"]
                    if ext_data.get("obml_count_label") is not None:
                        do["countLabel"] = ext_data["obml_count_label"]
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
                    "vendor": _VENDOR_OSI,
                    "data": json.dumps({"obml_unique_keys": [list(g) for g in unique_keys]}),
                }
            )

        # Preserve third-party vendor extensions verbatim
        self._carry_foreign_extensions(ds.get("custom_extensions"), do)

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

        # Determine abstract type. Precedence: Apache Ossie's first-class
        # `datatype` (v0.2+, capitalised `DataType` enum) > legacy lowercase
        # `data_type` > name heuristic. An OBML-origin field additionally
        # restores its exact `abstractType` from the stashed extension below
        # (highest precedence), keeping OBML -> OSI -> OBML lossless.
        osi_datatype = field.get("datatype", "")
        legacy_type = field.get("data_type", "")
        if osi_datatype in OSI_DATATYPE_TO_OBML_ABSTRACT:
            abstract_type = OSI_DATATYPE_TO_OBML_ABSTRACT[osi_datatype]
        elif legacy_type and legacy_type in OSI_TO_OBML_TYPE:
            abstract_type = OSI_TO_OBML_TYPE[legacy_type]
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
            if ext.get("vendor_name") in _OBML_VENDOR_READ:
                try:
                    ext_data = json.loads(ext.get("data", "{}"))
                    # Restore the exact OBML abstractType stashed on export, so a
                    # narrowing datatype map (e.g. Decimal -> float) never
                    # degrades an OBML-origin round trip.
                    if ext_data.get("obml_abstract_type"):
                        col["abstractType"] = ext_data["obml_abstract_type"]
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
                    "vendor": _VENDOR_OSI,
                    "data": json.dumps({"obml_field_label": field["label"]}),
                }
            )

        # Preserve third-party vendor extensions verbatim
        self._carry_foreign_extensions(field.get("custom_extensions"), col)

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
                restored_name: str | None = None
                extra_descriptors: list[Any] = []
                for ext in field.get("custom_extensions", []):
                    if ext.get("vendor_name") in _OBML_VENDOR_READ:
                        try:
                            ext_data = json.loads(ext.get("data", "{}"))
                            # Extension data is opaque to ``validate_osi``, so a
                            # foreign payload may put any JSON here. Only accept a
                            # non-empty string as the dimension name (it becomes a
                            # dict key); otherwise ignore it and fall back to the
                            # field name.
                            _name = ext_data.get("obml_dimension_name")
                            if isinstance(_name, str) and _name:
                                restored_name = _name
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
                            # The dimension's own synonyms / vendor extensions,
                            # restored authoritatively to the dimension. Opaque
                            # foreign data, so keep only well-shaped entries.
                            _syns = ext_data.get("obml_dimension_synonyms")
                            if isinstance(_syns, list):
                                clean_syns = [s for s in _syns if isinstance(s, str) and s]
                                if clean_syns:
                                    dim_def["synonyms"] = clean_syns
                            _exts = ext_data.get("obml_dimension_custom_extensions")
                            if isinstance(_exts, list):
                                clean_exts = [e for e in _exts if isinstance(e, dict)]
                                if clean_exts:
                                    dim_def["customExtensions"] = clean_exts
                            # Additional dimensions over the same column, preserved
                            # by the export because OSI has no slot for them.
                            _extras = ext_data.get("obml_extra_dimensions")
                            if isinstance(_extras, list):
                                extra_descriptors = _extras
                        except (json.JSONDecodeError, TypeError):
                            pass
                        break
                # Prefer the dimension's restored OBML name (export stashes it on
                # the field). The OSI field name is the physical code, so this is
                # what keeps an OBML-origin round-trip from renaming dimensions to
                # their code. Drop it from synonyms to avoid a self-referential
                # alias.
                if restored_name and dim_def.get("synonyms"):
                    dim_def["synonyms"] = [s for s in dim_def["synonyms"] if s != restored_name]
                    if not dim_def["synonyms"]:
                        del dim_def["synonyms"]
                base_name = restored_name or field_name
                self._insert_dimension(dimensions, ds_name, base_name, dim_def)
                # Rebuild any additional OBML dimensions the export preserved for
                # this column (OSI is one-dimension-per-field). Each descriptor is
                # opaque foreign-modifiable data, so guard its shape.
                for desc in extra_descriptors:
                    if not isinstance(desc, dict):
                        continue
                    dname = desc.get("name")
                    if not (isinstance(dname, str) and dname):
                        continue
                    extra_def: dict[str, Any] = {
                        "dataObject": ds_name,
                        "column": field_name,
                        "resultType": desc.get("resultType") or abstract_type,
                    }
                    for prop in ("timeGrain", "format", "description", "owner", "via"):
                        value = desc.get(prop)
                        if isinstance(value, str) and value:
                            extra_def[prop] = value
                    # Restore the extra dimension's own synonyms / vendor
                    # extensions. Opaque foreign data, so keep only well-shaped
                    # entries (string synonyms; dict extensions).
                    syns = desc.get("synonyms")
                    if isinstance(syns, list):
                        clean_syns = [s for s in syns if isinstance(s, str) and s]
                        if clean_syns:
                            extra_def["synonyms"] = clean_syns
                    exts = desc.get("customExtensions")
                    if isinstance(exts, list):
                        clean_exts = [e for e in exts if isinstance(e, dict)]
                        if clean_exts:
                            extra_def["customExtensions"] = clean_exts
                    self._insert_dimension(dimensions, ds_name, dname, extra_def)
        return dimensions

    def _insert_dimension(
        self, dimensions: dict[str, Any], ds_name: str, base_name: str, dim_def: dict[str, Any]
    ) -> None:
        """Insert ``dim_def`` under a unique key.

        Dimension names must be unique across the model. When ``base_name``
        already names a dimension on a *different* data object (foreign OSI where
        two datasets share a bare field name and no OBML-origin name was
        restored), qualify the later one with its data object and warn instead of
        silently overwriting the earlier dimension. A restored OBML name is unique
        by construction, so the qualification is foreign-OSI only.
        """
        key = base_name
        if key in dimensions and dimensions[key].get("dataObject") != ds_name:
            key = f"{ds_name} {base_name}"
            suffix = 2
            while key in dimensions:
                key = f"{ds_name} {base_name} {suffix}"
                suffix += 1
            self.warnings.append(
                f"Dimension name '{base_name}' occurs in multiple data "
                f"objects; emitted '{ds_name}.{base_name}' as dimension "
                f"'{key}' to avoid a collision."
            )
        dimensions[key] = dim_def

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

        # Case-insensitive dataset/field index for resolving SQL identifiers
        # back to their canonical OSI names (Snowflake/Databricks expressions
        # commonly upper-case or quote them). Identifiers resolve by BOTH the
        # OSI name and the physical code (source-table code / bare-identifier
        # field expression): our own OBML -> OSI emitter writes metric SQL
        # against the physical code (e.g. SUM(fact_orders.amount)), so resolving
        # names only would drop such metrics on the return trip. Names take
        # precedence over codes on any collision.
        ds_lc: dict[str, str] = {}
        for ds_name in ds_map:
            ds_lc.setdefault(ds_name.lower(), ds_name)
        for ds_name, ds in ds_map.items():
            # Unquote so a quoted source table (Snowflake/Databricks style,
            # e.g. WH.PUBLIC."fact_orders") is indexed by its bare code.
            table_code = self._unquote_identifier(self._parse_source(ds.get("source", ds_name))[2])
            if table_code:
                ds_lc.setdefault(table_code.lower(), ds_name)

        fields_lc: dict[str, dict[str, str]] = {}
        for ds_name, ds in ds_map.items():
            fmap: dict[str, str] = {}
            osi_fields = ds.get("fields", []) or []
            for f in osi_fields:
                if isinstance(f, dict) and f.get("name"):
                    fmap.setdefault(f["name"].lower(), f["name"])
            for f in osi_fields:
                if isinstance(f, dict) and f.get("name"):
                    code = self._field_expr_identifier(f)
                    if code:
                        fmap.setdefault(code.lower(), f["name"])
            fields_lc[ds_name] = fmap

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

            # Prefer ANSI_SQL, but also read SNOWFLAKE / DATABRICKS expressions
            # (SQL engines OrionBelt targets) — their aggregations are
            # syntactically ANSI-compatible. Non-SQL dialects (MDX/TABLEAU/MAQL)
            # are not parsed as SQL.
            expr_text, _expr_dialect = self._select_sql_expression(m.get("expression", {}))
            if not expr_text:
                self._preserve_unconverted_metric(
                    m, "no SQL-parseable dialect (ANSI_SQL / SNOWFLAKE / DATABRICKS) expression"
                )
                continue

            # Classify + decompose via sqlglot: a single aggregate over a column
            # (simple measure), a single aggregate over an expression (expression
            # measure), or aggregates embedded in a larger formula (auto-measures
            # + a metric). Column refs are resolved on the parsed AST against the
            # model (case-insensitive), so a dotted string literal is never taken
            # for a reference. Anything with no aggregate, an unsupported or
            # nested aggregate, an unresolvable reference, or that sqlglot cannot
            # parse is preserved verbatim. The source dialect drives the grammar.
            decomposed = self._decompose_metric(expr_text, _expr_dialect, ds_lc, fields_lc)
            if decomposed is None:
                self._preserve_unconverted_metric(
                    m, f"expression not decomposable into OBML measures/metrics: {expr_text!r}"
                )
                continue

            if decomposed[0] == "simple":
                _, agg, dataset, column, is_distinct = decomposed
                measure_def: dict[str, Any] = {
                    "columns": [{"dataObject": dataset, "column": column}],
                    "resultType": self._measure_result_type(agg),
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

            elif decomposed[0] == "expr":
                _, agg, obml_expr, is_distinct = decomposed
                measure_def = {
                    "expression": obml_expr,
                    "resultType": self._measure_result_type(agg),
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

            else:  # "complex" — aggregates embedded in a larger formula
                _, obml_expr, auto_measures = decomposed
                # Deduplicate: if an auto-measure is equivalent to an existing
                # named measure, reuse the named measure in the metric formula.
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

        # Preserve third-party vendor extensions, carrying them into whichever
        # OBML entity (measure or metric) the OSI metric became.
        for m in osi_metrics:
            target = metrics.get(m["name"]) or measures.get(m["name"])
            if target is not None:
                self._carry_foreign_extensions(m.get("custom_extensions"), target)
                # Apache Ossie v0.2+ metric `datatype` -> OBML exact `dataType`
                # (its natural home; `Decimal` -> decimal(p, s)). Don't override a
                # dataType already restored from an OBML-origin extension, and
                # skip Opaque/unknown (absent from the map).
                osi_dt = m.get("datatype")
                if osi_dt and not target.get("dataType"):
                    obml_dt = OSI_DATATYPE_TO_OBML_PHYSICAL.get(osi_dt)
                    if obml_dt:
                        target["dataType"] = obml_dt

        return measures, metrics

    @staticmethod
    def _extract_obml_extras(osi_metric: dict) -> dict:
        """Extract OBML-only properties from OSI metric custom_extensions."""
        for ext in osi_metric.get("custom_extensions", []):
            if ext.get("vendor_name") in _OBML_VENDOR_READ:
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

    def _select_sql_expression(self, expr_obj: dict) -> tuple[str, str]:
        """Pick a SQL-parseable expression from an OSI ``expression`` object.

        Returns ``(expression, dialect)`` for the most preferred SQL dialect
        present (ANSI_SQL > SNOWFLAKE > DATABRICKS), or ``("", "")`` when the
        metric only carries non-SQL dialects (MDX / TABLEAU / MAQL) or no usable
        expression. Catching SNOWFLAKE / DATABRICKS lets third-party models
        whose authors omitted ANSI_SQL still convert, since their aggregation
        syntax is ANSI-compatible.
        """
        if not isinstance(expr_obj, dict):
            return "", ""
        dialects = expr_obj.get("dialects", [])
        by_name = {
            d.get("dialect"): d.get("expression", "")
            for d in dialects
            if isinstance(d, dict) and d.get("expression")
        }
        for dialect in _SQL_PARSEABLE_DIALECTS:
            expr = by_name.get(dialect)
            if expr:
                return expr, dialect
        return "", ""

    @staticmethod
    def _unquote_identifier(ident: str) -> str:
        """Strip SQL identifier quoting (double quotes, backticks, or brackets)."""
        ident = ident.strip()
        if len(ident) >= 2 and (
            (ident[0] == '"' and ident[-1] == '"')
            or (ident[0] == "`" and ident[-1] == "`")
            or (ident[0] == "[" and ident[-1] == "]")
        ):
            return ident[1:-1]
        return ident

    @staticmethod
    def _field_expr_identifier(field: dict) -> str | None:
        """Physical column code of a field when its expression is a single
        (optionally quoted) identifier, so code-based metric references (e.g.
        ``fact_orders.amount`` or a Snowflake ``"net_amount"``) resolve back to
        the field. Returns ``None`` for computed expressions with no single
        column code.
        """
        expr = field.get("expression")
        if not isinstance(expr, dict):
            return None
        for dialect in expr.get("dialects", []) or []:
            if isinstance(dialect, dict):
                text = dialect.get("expression")
                if isinstance(text, str):
                    candidate = OSItoOBML._unquote_identifier(text)
                    if re.fullmatch(r"[A-Za-z_]\w*", candidate):
                        return candidate
        return None

    def _preserve_unconverted_metric(self, osi_metric: dict, reason: str) -> None:
        """Preserve an OSI metric that has no OBML representation.

        OBML cannot express the metric, but dropping it silently would break
        the README's roundtrip promise. Instead the original OSI metric is kept
        verbatim (re-emitted on OBML -> OSI) and a loud LOSSY warning is raised:
        the metric is preserved but NOT queryable through OBML.
        """
        self._unconverted_metrics.append(osi_metric)
        self.warnings.append(
            f"LOSSY: OSI metric '{osi_metric.get('name', '?')}' has no OBML representation "
            f"({reason}); preserved verbatim for OSI -> OBML -> OSI roundtrip but it is "
            f"NOT queryable in OBML."
        )

    @staticmethod
    def _obml_agg_name(node: exp.Expr) -> str | None:
        """OBML aggregation name for a sqlglot aggregate, or ``None`` when OBML
        has no single-argument equivalent (``VAR_POP``, ``CORR``, ...). Handles
        both real ``exp.AggFunc`` nodes and the ``exp.Anonymous`` nodes sqlglot
        produces for ``LISTAGG``."""
        if isinstance(node, exp.AggFunc):
            return _SQLGLOT_AGG_TO_OBML.get(node.sql_name())
        if isinstance(node, exp.Anonymous):
            name = node.this if isinstance(node.this, str) else ""
            return _ANON_AGG_TO_OBML.get(name.upper())
        return None

    @staticmethod
    def _is_agg_node(node: exp.Expr) -> bool:
        """Whether ``node`` is an aggregate call - a real ``exp.AggFunc`` (even
        one OBML can't model, so we can preserve rather than mis-emit) or an
        ``exp.Anonymous`` whose name is a known aggregate."""
        if isinstance(node, exp.AggFunc):
            return True
        if isinstance(node, exp.Anonymous):
            name = node.this if isinstance(node.this, str) else ""
            return name.upper() in _ANON_AGG_TO_OBML
        return False

    @staticmethod
    def _measure_result_type(agg: str) -> str:
        """OBML resultType for an aggregation: fractional aggregates yield a
        float, listagg a string, the count/min/max/mode family an int. (This also
        fixes the old decompose path, which hard-coded every auto-measure to
        float - a decomposed COUNT is now correctly int.)"""
        if agg == "listagg":
            return "string"
        return "float" if agg in _FLOAT_RESULT_AGGS else "int"

    @staticmethod
    def _agg_arg(node: exp.Expr) -> tuple[exp.Expr | None, bool]:
        """The aggregate's single argument and whether it is DISTINCT. Unwraps
        the ``exp.Distinct`` node of ``COUNT(DISTINCT x)``. Returns ``None`` for a
        multi-argument aggregate - a two-column ``CORR(a, b)`` or a
        ``LISTAGG(col, delimiter)`` - so the caller preserves the metric rather
        than silently dropping the extra argument."""
        if isinstance(node, exp.Anonymous):
            args = node.expressions or []
            return (args[0] if len(args) == 1 else None), False
        arg = node.this
        is_distinct = bool(node.args.get("distinct"))
        if isinstance(arg, exp.Distinct):
            is_distinct = True
            exprs = arg.expressions or ([arg.this] if arg.this else [])
            arg = exprs[0] if exprs else None
        # A second positional argument (CORR/COVAR/REGR family) can't be modelled
        # as a single-column measure -> preserve.
        if node.args.get("expression") is not None:
            return None, is_distinct
        return arg, is_distinct

    @staticmethod
    def _parse_metric_sql(expr_text: str, read: str | None) -> exp.Expr | None:
        """Parse a metric SQL expression, retrying with bracket-quoted
        identifiers (``[Orders].[amount]``) rewritten to ANSI double quotes when
        the first parse fails (the default grammar does not read ``[...]``).
        Because the rewrite is only reached on a parse failure, a valid
        expression containing brackets inside a string literal is never touched."""
        try:
            return sqlglot.parse_one(expr_text, read=read)
        except Exception:
            pass
        normalized = re.sub(
            r"\[([^\]]+)\]",
            lambda mm: '"' + mm.group(1).replace('"', '""') + '"',
            expr_text,
        )
        if normalized == expr_text:
            return None
        try:
            return sqlglot.parse_one(normalized, read=read)
        except Exception:
            return None

    def _render_obml(self, node: exp.Expr) -> str:
        """Render a sqlglot expression to OBML, rewriting qualified column refs
        ``ds.col`` to ``{[ds].[col]}``. Bare columns and literals are left as-is,
        so a numeric literal (``1.23``) is never mistaken for a reference."""

        def _rewrite(n: exp.Expr) -> exp.Expr:
            if isinstance(n, exp.Column) and n.table:
                return exp.var("{[" + n.table + "].[" + n.name + "]}")
            return n

        return node.transform(_rewrite).sql()

    def _decompose_metric(
        self,
        expr_text: str,
        osi_dialect: str | None,
        ds_lc: dict[str, str],
        fields_lc: dict[str, dict[str, str]],
    ) -> tuple | None:
        """Classify/decompose a metric SQL expression using sqlglot.

        Returns one of:
          ``("simple", agg, dataset, column, is_distinct)``   -> column measure
          ``("expr", agg, obml_expression, is_distinct)``     -> expression measure
          ``("complex", obml_outer_formula, auto_measures)``  -> measures + metric
        or ``None`` when the expression has no aggregate, an unsupported or nested
        aggregate, a reference that cannot be resolved, or one sqlglot cannot
        parse - the caller preserves it verbatim. Reading with the source dialect
        means a Snowflake/Databricks aggregation is parsed under the right grammar.
        """
        read = _OSI_DIALECT_TO_SQLGLOT.get(osi_dialect or "")
        tree = self._parse_metric_sql(expr_text, read)
        if tree is None:
            return None

        # Resolve qualified column refs on the parsed AST, canonicalising each
        # dataset/column to its model name (case-insensitively). Working on the
        # tree rather than the raw text means a dotted string literal
        # (``'north.us'``) is an ``exp.Literal``, never mistaken for a
        # ``dataset.column`` reference. Preserve if any qualified ref is unknown.
        unresolved = False

        def _resolve(n: exp.Expr) -> exp.Expr:
            nonlocal unresolved
            if isinstance(n, exp.Column) and n.table:
                ds_real = ds_lc.get(n.table.lower())
                col_real = fields_lc.get(ds_real, {}).get(n.name.lower()) if ds_real else None
                if not ds_real or not col_real:
                    unresolved = True
                    return n
                return exp.column(col_real, table=ds_real)
            return n

        tree = tree.transform(_resolve)
        if unresolved:
            return None

        # Aggregate calls, including LISTAGG (which sqlglot models as Anonymous
        # and find_all(exp.AggFunc) would miss); walk() yields every node so the
        # Anonymous ones are seen.
        aggs = [n for n in tree.walk() if self._is_agg_node(n)]
        if not aggs:
            return None
        # An unsupported aggregate (no single-argument OBML equivalent) or a
        # nested aggregate cannot be modelled as measures + a formula; preserve.
        for a in aggs:
            if self._obml_agg_name(a) is None:
                return None
            p = a.parent
            while p is not None:
                if self._is_agg_node(p):
                    return None
                p = p.parent

        # The whole expression is a single aggregate -> simple or expression
        # measure (no outer formula, so the metric name is the measure name).
        if len(aggs) == 1 and aggs[0] is tree:
            agg = self._obml_agg_name(tree)
            arg, is_distinct = self._agg_arg(tree)
            if agg is None or arg is None:
                return None
            if isinstance(arg, exp.Column) and arg.table:
                return ("simple", agg, arg.table, arg.name, is_distinct)
            return ("expr", agg, self._render_obml(arg), is_distinct)

        # Otherwise aggregates are embedded in a larger formula: each becomes an
        # auto-measure leaf, replaced by a ``{[key]}`` reference in the outer
        # formula that becomes the metric expression.
        auto_measures: dict[str, Any] = {}
        for a in aggs:
            agg = self._obml_agg_name(a)
            arg, is_distinct = self._agg_arg(a)
            if agg is None or arg is None:
                return None
            suffix = "_distinct" if is_distinct else ""
            if isinstance(arg, exp.Column) and arg.table:
                ds, col = arg.table, arg.name
                key_stub = re.sub(r"\W+", "_", f"{ds}_{col}")
                key = f"_{key_stub}_{agg}{suffix}"
                measure_def: dict[str, Any] = {
                    "columns": [{"dataObject": ds, "column": col}],
                    "resultType": self._measure_result_type(agg),
                    "aggregation": agg,
                }
            else:
                expr_slug = re.sub(r"[^a-zA-Z0-9]", "_", arg.sql())[:40]
                key = f"_{agg}_{expr_slug}{suffix}"
                measure_def = {
                    "expression": self._render_obml(arg),
                    "resultType": self._measure_result_type(agg),
                    "aggregation": agg,
                }
            if is_distinct:
                measure_def["distinct"] = True
            auto_measures[key] = measure_def
            a.replace(exp.var("{[" + key + "]}"))

        return ("complex", tree.sql(), auto_measures)
