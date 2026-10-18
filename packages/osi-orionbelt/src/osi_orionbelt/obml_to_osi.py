"""OBML → OSI conversion (the :class:`OBMLtoOSI` direction).

Extracted verbatim from ``converter.py``; see that module for the package-level
docstring and the shared constants in :mod:`osi_orionbelt._common`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from typing import Any

from osi_orionbelt._common import (
    _INTERNAL_VENDORS,
    _OSI_VENDOR_READ,
    _OSI_VERSION,
    _VENDOR_OBML,
    OBML_ABSTRACT_TO_OSI_DATATYPE,
    OBML_TO_OSI_TYPE,
    obml_datatype_to_osi,
)
from osi_orionbelt._field_names import (
    FIELD_NAMES_COLUMN,
    FIELD_NAMES_KEY,
    code_renames,
    field_collisions,
    rename_keys,
    rename_metric,
)
from osi_orionbelt._ossie_sql import ossie_sql, subset_violations
from osi_orionbelt._portable import NotPortableError, PortableRenderer, sql_ident


def _stash_concept_links(
    obml_obj: dict, extras: dict, key: str = "obml_external_concept_mappings"
) -> None:
    """Copy an OBML object's ``externalConceptMappings`` into an extras payload.

    The links have no OSI slot; they ride in the OBSL-vendor extension of
    the entity the object became and are restored verbatim on the way back.
    """
    links = obml_obj.get("externalConceptMappings")
    if links:
        extras[key] = links


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
        # Measures and metrics with no faithful Ossie expression, by kind
        # ("measures" / "metrics"); they ride in the model-level extension.
        self.unexported: dict[str, dict[str, Any]] = {}

    def convert(self) -> dict:
        # Reset per-conversion state so a second convert() call on the same
        # instance does not duplicate warnings or left-out entities.
        self.warnings = []
        self.unexported = {}
        # For what OBML saved by a version before 0.5.0 keeps verbatim, which
        # may name fields by code (see _field_names).
        self._legacy_renames, self._legacy_ambiguous = code_renames(
            self.obml.get("dataObjects", {}) or {}
        )

        osi: dict[str, Any] = {"version": _OSI_VERSION}

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
        osi_metrics = self._convert_measures_and_metrics(obml_measures, obml_metrics)

        # Re-emit OSI metrics that OBML could not represent and that the import
        # path preserved verbatim (vendor OSI, ``obml_unconverted_metrics``).
        # This closes the OSI -> OBML -> OSI roundtrip for non-SQL or
        # non-decomposable metrics.
        self._merge_restored_metrics(osi_metrics)

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
            "converter": "osi-orionbelt",
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
        # Preserve count-synthesis knobs (OSI has no native equivalent). The
        # synthesized ``<object>.count`` measures themselves are NOT emitted —
        # they are derived and regenerate on load — but the knobs must survive
        # a roundtrip. ``is not None`` so an explicit ``exposeCounts: false`` is
        # preserved (``False`` is falsy).
        expose_counts = self.obml.get("exposeCounts")
        if expose_counts is not None:
            roundtrip_data["obml_expose_counts"] = expose_counts
        count_label_pattern = self.obml.get("countLabelPattern")
        if count_label_pattern is not None:
            roundtrip_data["obml_count_label_pattern"] = count_label_pattern
        # Ontology prefixes and model-level external concept links. OSI has
        # no slot for either, and the links are only meaningful together with
        # the prefixes their compact IRIs expand with.
        if self.obml.get("ontology"):
            roundtrip_data["obml_ontology"] = self.obml["ontology"]
        # Business rules: OSI has no rule concept, so they ride whole.
        if self.obml.get("rules"):
            roundtrip_data["obml_rules"] = self.obml["rules"]
        _stash_concept_links(self.obml, roundtrip_data)
        if self.unexported:
            roundtrip_data["obml_unexported"] = self.unexported
        sem_model["custom_extensions"] = [
            {
                "vendor_name": _VENDOR_OBML,
                "data": json.dumps(roundtrip_data),
            }
        ]
        # Re-emit third-party model-level vendor extensions verbatim
        self._emit_foreign_extensions(
            self.obml.get("customExtensions"), sem_model["custom_extensions"]
        )

        # One model per document, its properties at the root (apache/ossie#383).
        # The root is additionalProperties:false, so there are no root-level
        # ``dialects`` / ``vendors`` arrays: dialects live per expression in
        # ``expression.dialects[]`` and vendors per entity in
        # ``custom_extensions[].vendor_name``.
        osi.update(sem_model)
        return osi

    def _emit_foreign_extensions(self, obml_exts: list[dict] | None, osi_exts: list[dict]) -> None:
        """Re-emit third-party OBML customExtensions as OSI custom_extensions.

        Mirrors ``OSItoOBML._carry_foreign_extensions``: extensions from a
        vendor we do not handle internally are passed back to OSI under their
        original vendor name, completing the roundtrip.
        """
        for ext in obml_exts or []:
            vendor = ext.get("vendor")
            if vendor and vendor not in _INTERNAL_VENDORS:
                osi_exts.append({"vendor_name": vendor, "data": ext.get("data", "")})

    def _convert_data_object(
        self, do_name: str, do_obj: dict, obml_dimensions: dict
    ) -> tuple[dict, list]:
        """Convert an OBML dataObject to an OSI dataset + relationships."""
        source = self._source(do_name)

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
        # OSI field names are the OBML column names (see _convert_column).
        if pk_columns:
            dataset["primary_key"] = pk_columns

        # ── Unique keys (v0.2 first-class, lossless roundtrip via OBSL) ──
        # OBML doesn't model unique keys natively today; round-trip via the
        # ``OBSL``-vendor ``obml_unique_keys`` payload that originated from
        # a prior OSI → OBML conversion (or hand-authored OBML).
        unique_keys_extra: list[list[str]] | None = None
        for ext in do_obj.get("customExtensions", []) or []:
            if ext.get("vendor") not in _OSI_VENDOR_READ:
                continue
            try:
                data = json.loads(ext.get("data", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            uk = data.get("obml_unique_keys")
            if isinstance(uk, list) and all(isinstance(g, list) for g in uk):
                unique_keys_extra = [list(g) for g in uk]
                if data.get(FIELD_NAMES_KEY) != FIELD_NAMES_COLUMN:
                    unique_keys_extra = rename_keys(
                        unique_keys_extra,
                        self._legacy_renames.get(do_name, {}),
                        self._legacy_ambiguous.get(do_name, set()),
                        self.warnings.append,
                        f"Dataset '{do_name}'",
                    )
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
            rel = self._convert_join_to_relationship(osi_name, do_name, join, i)
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
        # Count-synthesis knobs (``is not None`` so ``countable: false`` survives).
        if do_obj.get("countable") is not None:
            do_extras["obml_countable"] = do_obj["countable"]
        if do_obj.get("countLabel") is not None:
            do_extras["obml_count_label"] = do_obj["countLabel"]
        # A nested source has no OSI equivalent: OSI datasets are tables, and
        # this one's rows are an array column on another dataset. Carried in the
        # OBML vendor extension so a round trip does not silently turn a nested
        # object into a table with an empty name.
        if do_obj.get("nestedIn"):
            do_extras["obml_nested_in"] = do_obj["nestedIn"]
            if not do_obj.get("code"):
                # ``source`` is the unnest query (see _source), not a table.
                do_extras["obml_source_unnest"] = True
        _stash_concept_links(do_obj, do_extras)
        if do_extras:
            ds_exts = dataset.setdefault("custom_extensions", [])
            ds_exts.append(
                {
                    "vendor_name": _VENDOR_OBML,
                    "data": json.dumps(do_extras),
                }
            )

        # Re-emit third-party vendor extensions verbatim
        self._emit_foreign_extensions(
            do_obj.get("customExtensions"), dataset.setdefault("custom_extensions", [])
        )
        if not dataset["custom_extensions"]:
            del dataset["custom_extensions"]

        return dataset, relationships

    def _source(self, do_name: str, seen: frozenset[str] = frozenset()) -> str:
        """The dataset ``source``: the data object's table, or a query.

        A ``nestedIn`` object without ``code`` has no table; its rows are the
        elements of an array column on its parent. Ossie allows a query as the
        source, so it becomes the unnest in the form Postgres reads, as
        OrionBelt's Postgres dialect renders it: each column's ``code`` selected
        from the element, plus the field each object nested in this one unnests
        from, and a struct path reached through parentheses. Other engines spell
        the unnest differently; the import restores ``nestedIn`` from the
        extension, not from this text.
        """
        data_objects = self.obml.get("dataObjects", {}) or {}
        do_obj = data_objects.get(do_name, {})
        code = do_obj.get("code", "")
        nested = do_obj.get("nestedIn") or {}
        parent = nested.get("dataObject")
        if code or not parent:
            database = do_obj.get("database", "")
            schema = do_obj.get("schema", "")
            return f"{database}.{schema}.{code}" if database else code
        if parent in seen or parent not in data_objects:
            self.warnings.append(
                f"Data object '{do_name}' is nested in '{parent}', which has no source; "
                f"its dataset has an empty source."
            )
            return ""
        parent_source = self._source(parent, seen | {do_name})
        if not parent_source:
            return ""
        if not data_objects[parent].get("code"):
            parent_source = f"({parent_source})"
        alias = sql_ident(do_name)
        segments = str(nested.get("column", "")).split(".")
        # ``"C"."x_Meta"."Notes"`` reads as a three-part table name on Postgres;
        # a composite's field is reached as ``("C"."x_Meta")."Notes"``.
        path = f"{sql_ident(parent)}.{sql_ident(segments[0])}"
        for segment in segments[1:]:
            path = f"({path}).{sql_ident(segment)}"
        # Each physical field once: two columns may share a code, and a
        # repeated output name is ambiguous to read.
        selected = list(
            dict.fromkeys(
                col["code"]
                for col in (do_obj.get("columns") or {}).values()
                if isinstance(col, dict) and col.get("code")
            )
        )
        for child in data_objects.values():
            child_nested = child.get("nestedIn") or {}
            if child_nested.get("dataObject") == do_name and child_nested.get("column"):
                head = str(child_nested["column"]).split(".")[0]
                if head not in selected:
                    selected.append(head)
        fields = [f"{alias}.{sql_ident(c)} AS {sql_ident(c)}" for c in selected]
        return (
            f"SELECT {', '.join(fields) or alias} "
            f"FROM {parent_source} AS {sql_ident(parent)}, UNNEST({path}) AS {alias}"
        )

    def _convert_column(
        self, col_name: str, col_obj: dict, do_name: str, obml_dimensions: dict
    ) -> dict:
        """Convert an OBML column to an OSI field.

        The field is named by the OBML column name and computed by the column
        ``code``. The code can be a SQL expression (a calculated column from an
        OSI import), which must never become the field name.
        """
        code = col_obj.get("code", col_name.lower().replace(" ", "_"))

        field: dict[str, Any] = {
            "name": col_name,
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

        # ── Field label (OSI v0.2 first-class) ──
        # Surfaced from OBSL-vendor customExtensions ``obml_field_label`` —
        # round-trip path for OSI → OBML → OSI fidelity. OBML has no
        # native column ``label`` today.
        for ext in col_obj.get("customExtensions", []) or []:
            if ext.get("vendor") not in _OSI_VENDOR_READ:
                continue
            try:
                ext_label_data = json.loads(ext.get("data", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if ext_label_data.get("obml_field_label"):
                field["label"] = ext_label_data["obml_field_label"]
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

        if synonyms:
            ai_ctx.setdefault("synonyms", []).extend(
                s for s in synonyms if s not in ai_ctx.get("synonyms", [])
            )
        if ai_ctx:
            field["ai_context"] = ai_ctx

        # Emit the first-class Apache Ossie `datatype` (v0.2+) from the OBML
        # abstractType so exported fields carry a portable logical type...
        abstract_type = col_obj.get("abstractType", "string")
        field["datatype"] = OBML_ABSTRACT_TO_OSI_DATATYPE.get(abstract_type, "String")
        # ...and stash the exact abstractType in custom_extensions so the return
        # trip restores it verbatim, lossless through the narrowing map.
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
        # Preserve OBML-only dimension properties (timeGrain, format, resultType, etc.).
        # OBML allows N dimensions over one column (grain variants, role-playing
        # via ``via``); OSI is field-centric (one dimension per field). The first
        # match is the primary dimension carried on the field; any extras are
        # preserved as descriptors so the reverse trip can rebuild them instead of
        # dropping them silently.
        matched_dim: dict[str, Any] | None = None
        extra_dims: list[dict[str, Any]] = []
        for _dim_name, dim_obj in obml_dimensions.items():
            if dim_obj.get("dataObject") != do_name or dim_obj.get("column") != col_name:
                continue
            if matched_dim is None:
                matched_dim = dim_obj
                # Preserve the dimension's OBML name explicitly. The OSI field
                # name is the column name, so without this the round-trip
                # would rename the dimension to its column. Authoritative
                # (do not rely on synonyms, which mix names and user aliases).
                ext_data["obml_dimension_name"] = _dim_name
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
                if dim_obj.get("pathName"):
                    ext_data["obml_dimension_path_name"] = dim_obj["pathName"]
                # The dimension's own synonyms and vendor extensions have no
                # native OSI slot (OSI has no dimension entity), so preserve them
                # authoritatively here for the reverse trip. The customExtensions
                # are also emitted as field foreign extensions below for OSI-tool
                # visibility; that path lands them on the column on re-import,
                # while this one restores them to the dimension.
                if dim_obj.get("synonyms"):
                    ext_data["obml_dimension_synonyms"] = dim_obj["synonyms"]
                if dim_obj.get("customExtensions"):
                    ext_data["obml_dimension_custom_extensions"] = dim_obj["customExtensions"]
                _stash_concept_links(dim_obj, ext_data, "obml_dimension_external_concept_mappings")
            else:
                descriptor: dict[str, Any] = {"name": _dim_name}
                for prop in (
                    "resultType",
                    "timeGrain",
                    "format",
                    "description",
                    "owner",
                    "via",
                    "pathName",
                ):
                    if dim_obj.get(prop):
                        descriptor[prop] = dim_obj[prop]
                # Carry the extra dimension's own synonyms and vendor extensions
                # too, so it round-trips with the same fidelity as the primary.
                if dim_obj.get("synonyms"):
                    descriptor["synonyms"] = dim_obj["synonyms"]
                if dim_obj.get("customExtensions"):
                    descriptor["customExtensions"] = dim_obj["customExtensions"]
                _stash_concept_links(dim_obj, descriptor, "externalConceptMappings")
                extra_dims.append(descriptor)
        if extra_dims:
            ext_data["obml_extra_dimensions"] = extra_dims
            extra_names = ", ".join(d["name"] for d in extra_dims)
            self.warnings.append(
                f"Column '{do_name}.{col_name}' backs {len(extra_dims) + 1} OBML "
                f"dimensions; OSI represents one dimension per field, so the "
                f"{len(extra_dims)} additional dimension(s) ({extra_names}) are "
                f"preserved via an OBSL extension for the reverse conversion but "
                f"are not natively visible to other OSI tools."
            )
        field["custom_extensions"] = [
            {
                "vendor_name": _VENDOR_OBML,
                "data": json.dumps(ext_data),
            }
        ]

        # Re-emit third-party vendor extensions verbatim. OSI has no separate
        # dimension entity, so a matched dimension's foreign extensions surface
        # on the field too (they re-import onto the column).
        self._emit_foreign_extensions(col_obj.get("customExtensions"), field["custom_extensions"])
        if matched_dim is not None:
            self._emit_foreign_extensions(
                matched_dim.get("customExtensions"), field["custom_extensions"]
            )

        return field

    def _convert_join_to_relationship(
        self, osi_from_name: str, _obml_from_name: str, join: dict, index: int
    ) -> dict | None:
        """Convert an OBML join to an OSI relationship."""
        join_to_display = join.get("joinTo", "")
        # Use the OBML display name as the OSI target name (consistent with
        # _convert_data_object which uses display name as OSI dataset name)
        to_name = join_to_display
        # OSI field names are the OBML column names (see _convert_column).
        from_cols = list(join.get("columnsFrom", []))
        to_cols = list(join.get("columnsTo", []))

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

    def _restore_unconverted_metrics(self) -> list[dict]:
        """Recover OSI metrics preserved verbatim during OSI -> OBML import.

        The import path stashes metrics OBML can't represent under an OSI-vendor
        model-level customExtension (``obml_unconverted_metrics``). Re-emit them
        so a full OSI -> OBML -> OSI roundtrip keeps them, renaming the field
        references of a payload saved before 0.5.0 (see _field_names).
        """
        restored: list[dict] = []
        for ext in self.obml.get("customExtensions", []) or []:
            if ext.get("vendor") not in _OSI_VENDOR_READ:
                continue
            try:
                data = json.loads(ext.get("data", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            preserved = data.get("obml_unconverted_metrics")
            if not isinstance(preserved, list):
                continue
            metrics = [m for m in preserved if isinstance(m, dict)]
            if data.get(FIELD_NAMES_KEY) != FIELD_NAMES_COLUMN:
                collisions = field_collisions(self.obml.get("dataObjects", {}) or {})
                for metric in metrics:
                    rename_metric(
                        metric,
                        self._legacy_renames,
                        self._legacy_ambiguous,
                        collisions,
                        self.warnings.append,
                    )
            restored.extend(metrics)
        return restored

    def _merge_restored_metrics(self, osi_metrics: list[dict]) -> None:
        """Append preserved (unconverted) OSI metrics to the converted ones.

        Name-collision guard: if a queryable OBML measure/metric now owns a name
        a stale preserved metric also uses, skip the preserved copy (the real
        metric wins) so the OSI output has no duplicate metric names and passes
        semantic validation. Each appended metric carries its own dialects
        (``expression.dialects[]``) and vendors (``custom_extensions``)
        verbatim, which are the schema-valid homes for that metadata.
        """
        existing = {m.get("name") for m in osi_metrics if isinstance(m, dict)}
        for restored in self._restore_unconverted_metrics():
            name = restored.get("name")
            if name in existing:
                self.warnings.append(
                    f"Preserved OSI metric '{name}' dropped on export: a converted "
                    f"OBML metric now uses that name."
                )
                continue
            existing.add(name)
            osi_metrics.append(restored)

    def _convert_measures_and_metrics(self, obml_measures: dict, obml_metrics: dict) -> list:
        """Convert OBML measures and metrics to OSI metrics.

        The expression is what other Ossie consumers read, so it comes from
        :class:`PortableRenderer` and computes what OrionBelt computes. A measure
        or metric with no faithful expression is left out of the document and
        kept whole in ``self.unexported`` for the model-level extension; the
        reverse conversion restores it from there. Each expression is
        ``ANSI_SQL``, and also ``OSSIE_SQL_2026`` where it has a form inside
        that language (see :meth:`_add_ossie_dialect`).
        """
        renderer = PortableRenderer(self.obml)
        ossie_renderer = PortableRenderer(self.obml, ossie=True)
        osi_metrics = []

        for name, measure in obml_measures.items():
            if str(measure.get("aggregation", "")).lower() == "measure":
                osi_metric = self._convert_delegated_measure(name, measure)
            else:
                try:
                    sql = renderer.measure(name)
                except NotPortableError as exc:
                    self._leave_out("measures", name, measure, str(exc))
                    continue
                osi_metric = self._convert_measure(name, measure, sql)
                self._add_ossie_dialect(osi_metric, partial(ossie_renderer.measure, name))
            self._finish_osi_metric("measure", measure, osi_metric)
            osi_metrics.append(osi_metric)

        for name, metric in obml_metrics.items():
            try:
                sql = renderer.metric(name)
            except NotPortableError as exc:
                self._leave_out("metrics", name, metric, str(exc))
                continue
            osi_metric = self._convert_metric(name, metric, sql)
            self._add_ossie_dialect(osi_metric, partial(ossie_renderer.metric, name))
            self._finish_osi_metric("metric", metric, osi_metric)
            osi_metrics.append(osi_metric)

        return osi_metrics

    @staticmethod
    def _add_ossie_dialect(osi_metric: dict, render: Callable[[], str]) -> None:
        """Add the expression in ``OSSIE_SQL_2026`` when it has a form in that language.

        Rendered in the language's own forms, then checked node by node against
        the subset the spec defines, so the tag is a claim another tool can rely
        on. A measure using ``MODE``, ``LISTAGG``, an escaped pattern, or an
        authored expression outside the subset keeps its ``ANSI_SQL`` form only.
        """
        try:
            sql = render()
        except NotPortableError:
            return
        if subset_violations(sql):
            # An imported formula keeps sqlglot's spelling (STARTS_WITH); the
            # language's own may differ (STARTSWITH).
            sql = ossie_sql(sql)
        if not subset_violations(sql):
            osi_metric["expression"]["dialects"].append(
                {"dialect": "OSSIE_SQL_2026", "expression": sql}
            )

    def _leave_out(self, kind: str, name: str, definition: dict, reason: str) -> None:
        """Keep a non-portable measure or metric for the model-level extension."""
        self.unexported.setdefault(kind, {})[name] = definition
        self.warnings.append(
            f"{kind[:-1].capitalize()} '{name}' is not exported as an Ossie metric "
            f"because {reason}. It is kept in the {_VENDOR_OBML} extension for the reverse "
            f"conversion."
        )

    def _finish_osi_metric(self, kind: str, obml_obj: dict, osi_metric: dict) -> None:
        """Attach what every exported measure or metric carries besides its SQL."""
        definition = {
            k: v
            for k, v in obml_obj.items()
            if k not in ("customExtensions", "externalConceptMappings")
        }
        self._merge_obml_extension(osi_metric, "obml_definition", definition)
        self._merge_obml_extension(osi_metric, "obml_definition_kind", kind)
        self._carry_concept_links_to_osi_metric(obml_obj, osi_metric)
        self._carry_foreign_to_osi_metric(obml_obj, osi_metric)
        self._emit_osi_metric_datatype(obml_obj, osi_metric)

    @staticmethod
    def _merge_obml_extension(osi_metric: dict, key: str, value: Any) -> None:
        """Set *key* in the metric's single OBSL-vendor extension, creating it if absent.

        The reverse direction reads only the first OBSL-vendor extension it
        finds, so every value goes into that one payload.
        """
        exts = osi_metric.setdefault("custom_extensions", [])
        for ext in exts:
            if ext.get("vendor_name") == _VENDOR_OBML:
                data = json.loads(ext.get("data") or "{}")
                data[key] = value
                ext["data"] = json.dumps(data)
                return
        exts.append({"vendor_name": _VENDOR_OBML, "data": json.dumps({key: value})})

    @classmethod
    def _carry_concept_links_to_osi_metric(cls, obml_obj: dict, osi_metric: dict) -> None:
        """Stash an OBML measure/metric's ``externalConceptMappings`` on the OSI metric."""
        links = obml_obj.get("externalConceptMappings")
        if links:
            cls._merge_obml_extension(osi_metric, "obml_external_concept_mappings", links)

    def _carry_foreign_to_osi_metric(self, obml_obj: dict, osi_metric: dict) -> None:
        """Re-emit third-party vendor extensions on an OBML measure/metric to
        the OSI metric, dropping the key again if nothing foreign was added."""
        self._emit_foreign_extensions(
            obml_obj.get("customExtensions"), osi_metric.setdefault("custom_extensions", [])
        )
        if not osi_metric["custom_extensions"]:
            del osi_metric["custom_extensions"]

    def _emit_osi_metric_datatype(self, obml_obj: dict, osi_metric: dict) -> None:
        """Emit a first-class Apache Ossie `datatype` from an explicit OBML
        measure/metric `dataType`.

        Only fires when the OBML object declares an exact `dataType` (its
        physical/result-layer type), so plain measures - whose type is only the
        defaulted `resultType` - stay untouched and round trips stay idempotent.
        The exact `dataType` also round-trips via `obml_data_type` in
        `custom_extensions`; this adds the portable first-class field alongside.
        """
        osi_dt = obml_datatype_to_osi(obml_obj.get("dataType"))
        if osi_dt:
            osi_metric["datatype"] = osi_dt

    @staticmethod
    def _osi_metric(name: str, obml_obj: dict, sql: str, dialect: str = "ANSI_SQL") -> dict:
        """The OSI metric shell shared by every measure and metric."""
        return {
            "name": name,
            "expression": {"dialects": [{"dialect": dialect, "expression": sql}]},
            "description": obml_obj.get("description", name),
        }

    def _convert_measure(self, name: str, measure: dict, sql: str) -> dict:
        """Convert an OBML measure to an OSI metric with its portable *sql*."""
        result = self._osi_metric(name, measure, sql)
        synonyms = [name] + [s for s in measure.get("synonyms", []) if s != name]
        result["ai_context"] = {"synonyms": synonyms}
        self._add_obml_measure_extras(result, measure)
        return result

    def _convert_delegated_measure(self, name: str, measure: dict) -> dict:
        """Convert an ``aggregation: measure`` measure, resolved by a Databricks Metric View.

        The expression is the Databricks ``MEASURE("<label>")`` call, so it is
        tagged with the ``DATABRICKS`` dialect; the extras carry the OBML
        signal back.
        """
        result = self._osi_metric(name, measure, f'MEASURE("{name}")', dialect="DATABRICKS")
        synonyms = [name] + [s for s in measure.get("synonyms", []) if s != name]
        result["ai_context"] = {"synonyms": synonyms}
        self._add_obml_measure_extras(result, {**measure, "_extra_obml_aggregation": "measure"})
        return result

    @staticmethod
    def _add_obml_measure_extras(result: dict, measure: dict) -> None:
        """Preserve OBML-only measure properties in custom_extensions for roundtrip."""
        extras: dict[str, Any] = {}
        if measure.get("filters"):
            extras["obml_filters"] = measure["filters"]
        if measure.get("total"):
            extras["obml_total"] = True
        if measure.get("defaultValue") is not None:
            extras["obml_default_value"] = measure["defaultValue"]
        if measure.get("allowFanOut"):
            extras["obml_allow_fan_out"] = True
        if measure.get("format"):
            extras["obml_format"] = measure["format"]
        if measure.get("delimiter"):
            extras["obml_delimiter"] = measure["delimiter"]
        if measure.get("withinGroup"):
            extras["obml_within_group"] = measure["withinGroup"]
        if measure.get("percentile") is not None:
            extras["obml_percentile"] = measure["percentile"]
        if measure.get("dataType"):
            extras["obml_data_type"] = measure["dataType"]
        if measure.get("owner"):
            extras["obml_owner"] = measure["owner"]
        if measure.get("grain"):
            extras["obml_grain"] = measure["grain"]
        if measure.get("anchor"):
            extras["obml_anchor"] = measure["anchor"]
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
                    "vendor_name": _VENDOR_OBML,
                    "data": json.dumps(extras),
                }
            )

    def _convert_metric(self, name: str, metric: dict, sql: str) -> dict:
        """Convert an OBML metric to an OSI metric with its portable *sql*.

        Cumulative and window metrics also keep their configuration under the
        legacy ``obml_*`` keys, which older readers reconstruct them from.
        """
        result = self._osi_metric(name, metric, sql)
        synonyms = [s for s in metric.get("synonyms", []) if s != name]
        if synonyms:
            result["ai_context"] = {"synonyms": synonyms}

        kind = metric.get("type")
        ext_data: dict[str, Any] = {}
        if kind == "cumulative":
            ext_data = {
                "obml_metric_type": "cumulative",
                "obml_cumulative_measure": metric.get("measure", ""),
                "obml_cumulative_time_dimension": metric.get("timeDimension", ""),
                "obml_cumulative_type": metric.get("cumulativeType", "sum"),
            }
            if metric.get("window") is not None:
                ext_data["obml_cumulative_window"] = metric["window"]
            if metric.get("grainToDate"):
                ext_data["obml_cumulative_grain_to_date"] = metric["grainToDate"]
        elif kind == "window":
            ext_data = {
                "obml_metric_type": "window",
                "obml_window_function": str(metric.get("windowFunction", "")).lower(),
                "obml_order_direction": metric.get("orderDirection", "desc"),
            }
            optional = {
                "measure": "obml_window_measure",
                "timeDimension": "obml_window_time_dimension",
                "offset": "obml_window_offset",
                "buckets": "obml_window_buckets",
                "defaultValue": "obml_window_default_value",
            }
            for obml_key, ext_key in optional.items():
                if metric.get(obml_key) is not None:
                    ext_data[ext_key] = metric[obml_key]
        if kind in ("cumulative", "window") and metric.get("partitionBy"):
            ext_data["obml_partition_by"] = list(metric["partitionBy"])
        for obml_key, ext_key in (
            ("format", "obml_format"),
            ("dataType", "obml_data_type"),
            ("owner", "obml_owner"),
        ):
            if metric.get(obml_key):
                ext_data[ext_key] = metric[obml_key]
        if ext_data:
            result["custom_extensions"] = [
                {"vendor_name": _VENDOR_OBML, "data": json.dumps(ext_data)}
            ]
        return result
