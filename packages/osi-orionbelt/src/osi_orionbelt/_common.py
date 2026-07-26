"""Shared constants and mapping tables for the OSI ↔ OBML converter.

These module-level constants are used by more than one of the converter
direction classes (``OSItoOBML``, ``OBMLtoOSI``) and the
validation helpers. They live here so both the facade ``converter`` module and
the per-direction class modules can import them without forming an import cycle.
"""

from __future__ import annotations

import re

# ─── Spec version pin ───────────────────────────────────────────────────────
# Single source of truth for the OSI spec we emit. Bump when upstream cuts
# a stable v0.2.0 (drop the ``.dev0`` suffix). All read paths accept both
# 0.1.x (via the legacy shim) and 0.2.x.
_OSI_VERSION = "0.2.0.dev0"

# SQL dialects (of the OSI enum) whose aggregation expressions our regex-based
# metric parser can read, in preference order. ANSI_SQL first; SNOWFLAKE and
# DATABRICKS are SQL engines OrionBelt also targets, and their simple/expression
# aggregations (``SUM(t.c)``, ``SUM(t.a * t.b)``) are syntactically identical to
# ANSI. MDX / TABLEAU / MAQL are non-SQL languages and are never parsed as SQL.
_SQL_PARSEABLE_DIALECTS = ("ANSI_SQL", "SNOWFLAKE", "DATABRICKS")

# Non-SQL expression languages of the OSI Dialect enum. Their expressions must
# never be written into an OBML column ``code`` (a physical SQL column
# reference) - doing so would emit broken SQL. Everything else in the enum
# (ANSI_SQL, SNOWFLAKE, DATABRICKS, BIGQUERY) is SQL.
_NON_SQL_DIALECTS = frozenset({"MDX", "TABLEAU", "MAQL"})

# Matches a ``dataset.column`` reference inside a SQL expression, where each
# side is a bare identifier or a quoted identifier (double quotes, backticks, or
# brackets). The leading lookbehind prevents matching the tail of a longer path
# (``a.b.c``) or a mid-token boundary; the bare form must start with a letter or
# underscore so numeric literals (``1.5``) are never treated as references.
_COLUMN_REF_RE = re.compile(
    r'(?<![\w."`\]])'
    r'(?P<ds>[A-Za-z_]\w*|"[^"]+"|`[^`]+`|\[[^\]]+\])'
    r"\s*\.\s*"
    r'(?P<col>[A-Za-z_]\w*|"[^"]+"|`[^`]+`|\[[^\]]+\])'
)
# Vendor identities for custom_extensions.
#   ORIONBELT - OrionBelt/OBML-proprietary payloads we author on OBML -> OSI.
#   OSI       - OSI-native fields OBML can't hold (unique_keys, field label,
#               ai_context leftovers), stashed into OBML on OSI -> OBML.
# Read paths also accept the legacy tags we emitted before this scheme so older
# documents still round-trip; foreign vendors (SNOWFLAKE, DBT, ...) are
# preserved verbatim, never relabelled.
_VENDOR_OBML = "ORIONBELT"
_VENDOR_OSI = "OSI"
_OBML_VENDOR_READ = ("ORIONBELT", "COMMON")
_OSI_VENDOR_READ = ("OSI", "OBSL")
# Vendors the converter handles internally (its own payloads + native-field
# stashes). Any custom_extension from a vendor outside this set is third-party
# and is carried through verbatim in both directions, never relabelled.
_INTERNAL_VENDORS = frozenset({"ORIONBELT", "COMMON", "OSI", "OBSL"})

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

# ─── Apache Ossie DataType (v0.2+) ⇄ OBML ────────────────────────────────────
# Apache Ossie added a first-class `datatype` on Field/Metric backed by a
# capitalised `DataType` enum. It is a *logical* type - the same layer as OBML's
# column `abstractType` - so this is the field/dimension mapping.
#
# `Decimal` has no logical-layer equivalent in OBML: OBML deliberately models
# exact decimal at the physical/result layer (`sqlType`/`sqlPrecision`/
# `sqlScale`, measure/metric `dataType` via `decimal(p, s)`), not as a coarse
# `abstractType`. So `Decimal` narrows to `float` for fields, but is recovered
# exactly for metrics via the physical `dataType` map below
# (`OSI_DATATYPE_TO_OBML_PHYSICAL`).
#
# `Opaque` is Ossie's own "known type outside the portable vocabulary" marker
# and is intentionally absent so it falls back to the name heuristic on import.
OSI_DATATYPE_TO_OBML_ABSTRACT = {
    "String": "string",
    "Integer": "int",
    "Float": "float",
    "Decimal": "float",
    "Boolean": "boolean",
    "Date": "date",
    "Time": "time",
    "DateTime": "timestamp",
    "DateTimeTz": "timestamp_tz",
}

# Metric/measure `datatype`. Unlike fields, measures/metrics carry an exact
# OBML `dataType` (physical vocabulary: `integer`/`double`/`decimal(p, s)`/...),
# which is where `Decimal` genuinely belongs. So Ossie metric `datatype` maps to
# that field, not the coarse `abstractType`.
OBML_DECIMAL_DEFAULT = "decimal(18, 2)"  # mirrors orionbelt.models.types.BUILTIN_DEFAULT

# Ossie `DataType` -> OBML physical `dataType` string (import direction).
# `Opaque` is omitted (unknown / non-portable). `DateTimeTz` has no tz-aware
# physical form, so it narrows to `timestamp`.
OSI_DATATYPE_TO_OBML_PHYSICAL = {
    "String": "string",
    "Integer": "integer",
    "Float": "double",
    "Decimal": OBML_DECIMAL_DEFAULT,
    "Boolean": "boolean",
    "Date": "date",
    "Time": "time",
    "DateTime": "timestamp",
    "DateTimeTz": "timestamp",
}

# OBML physical `dataType` -> Ossie `DataType` (export direction). `decimal(p, s)`
# is handled separately by ``obml_datatype_to_osi`` since it is parametrised.
OBML_PHYSICAL_TO_OSI_DATATYPE = {
    "string": "String",
    "integer": "Integer",
    "bigint": "Integer",
    "double": "Float",
    "boolean": "Boolean",
    "date": "Date",
    "time": "Time",
    "timestamp": "DateTime",
}


def obml_datatype_to_osi(data_type: str | None) -> str | None:
    """Map an explicit OBML measure/metric ``dataType`` to an Ossie ``DataType``.

    Returns ``None`` when there is no mapping (so the caller emits nothing rather
    than an unknown type). ``decimal(p, s)`` maps to ``Decimal``.
    """
    if not data_type:
        return None
    normalized = data_type.strip().lower()
    if normalized.startswith("decimal"):
        return "Decimal"
    return OBML_PHYSICAL_TO_OSI_DATATYPE.get(normalized)


# OBML column `abstractType` -> Ossie `DataType`, for the export direction.
OBML_ABSTRACT_TO_OSI_DATATYPE = {
    "string": "String",
    "json": "Opaque",
    "int": "Integer",
    "float": "Float",
    "date": "Date",
    "time": "Time",
    "time_tz": "Time",
    "timestamp": "DateTime",
    "timestamp_tz": "DateTimeTz",
    "boolean": "Boolean",
}
