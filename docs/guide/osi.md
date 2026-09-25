---
description: "OSI (Open Semantic Interchange) is an open standard for portable semantic models, and OrionBelt reads and writes it, so metric and dimension definitions move between tools."
---

# OSI Interoperability

**OSI (Open Semantic Interchange)** is an open standard for portable semantic models, founded with the goal of letting metric and dimension definitions move between BI tools, semantic layers, and data platforms without rewriting. See [open-semantic-interchange.org](https://open-semantic-interchange.org/) for the specification and contributor list.

!!! note "Now at the Apache Incubator"
    OSI has been contributed to the Apache Software Foundation and is now developed as **[Apache Ossie (incubating)](https://github.com/apache/ossie)**. The specification and its converters (including the OrionBelt converter) live there going forward. Existing `github.com/open-semantic-interchange/OSI` links redirect to the new repository.

OrionBelt includes a bidirectional converter between OBML and the [OSI specification](https://github.com/apache/ossie) format. The converter handles structural differences between the two formats — including metric decomposition, relationship restructuring, and lossless `ai_context` preservation via `customExtensions` — with built-in validation for both directions.

The converter ships as a standalone `osi-orionbelt` package. It is an **optional** dependency: install it with `pip install 'orionbelt-semantic-layer[osi]'` (or standalone via `pip install osi-orionbelt`). The published API Docker images bundle it, so the `/convert`, `/models/from-osi`, and `/osi` endpoints work out of the box; on a bare install without the extra, those endpoints return a `503`.

## Spec version

OBSL v2.6 emits **OSI v0.2.0.dev0** (the latest draft in the upstream `core-spec/` at release time). The vendored schema lives at `packages/osi-orionbelt/src/osi_orionbelt/schemas/osi-schema.json`; refresh it with `scripts/refresh-osi-schema.sh` when upstream advances.

**Breaking change vs. OBSL v2.5** — the previous release emitted OSI v0.1.1. Downstream consumers pinning to v0.1 will reject v2.6 output. The converter still **reads** v0.1.x inputs via the legacy shim `_normalize_legacy_v01()`, which promotes pre-v0.2 `custom_extensions` payloads (`obml_primary_key`, `obml_unique_keys`) into the v0.2 first-class fields before parsing.

What's new in v0.2 that OBSL now round-trips:

| Surface | OBML side | OSI side |
|---|---|---|
| `primary_key` | Per-column `primaryKey: true` flag | First-class `primary_key: [col, ...]` array (composite supported, declaration order preserved) |
| `unique_keys` | OBSL custom extension `obml_unique_keys: [[col], [col1, col2], ...]` | First-class `unique_keys: [[...], ...]` array |
| Field `label` | OBSL custom extension `obml_field_label` | First-class `field.label` string |
| `MAQL` dialect | n/a (we don't generate MAQL) | Accepted on read, surfaced via warning if it's the only available dialect |
| Top-level informational arrays | n/a | `dialects: ["ANSI_SQL"]` + `vendors: [...]` |

## REST API

```bash
# Convert OSI -> OBML
curl -X POST http://127.0.0.1:8000/v1/convert/osi-to-obml \
  -H "Content-Type: application/json" \
  -d '{"input_yaml": "version: \"0.1.1\"\nsemantic_model:\n  ..."}' | jq

# Convert OBML -> OSI
curl -X POST http://127.0.0.1:8000/v1/convert/obml-to-osi \
  -H "Content-Type: application/json" \
  -d '{"input_yaml": "version: 1.0\ndataObjects:\n  ..."}' | jq
```

Both endpoints are stateless — no session required.

The converter targets the OSI **core-spec** semantic model (datasets, fields, relationships, metrics) in both directions. OSI's separate ontology layer is out of scope.

## What other OSI tools read

Another OSI tool reads a metric's SQL expression, not OrionBelt's vendor extension, so the export writes expressions that compute what OrionBelt computes:

| OBML | Exported expression |
|---|---|
| Column reference | `"<dataset>"."<field>"`: the data object name and the column `code`, always quoted so a reserved word such as `Order` still parses |
| Measure `filters` | `SUM(CASE WHEN <condition> THEN <arg> END)`, the spec's portable filtered aggregation |
| Measure `total: true` | The grand-total window, e.g. `SUM(SUM(x)) OVER ()` |
| Measure `defaultValue` | `COALESCE(<aggregate>, <value>)` |
| Synthesized count (`"Orders Count"`) | `COUNT(<dataset>.<primary key>)` |
| Metric referencing measures or metrics | The referenced SQL, inlined |
| Cumulative and window metrics | The window over the aggregated measure, ordered by the time dimension at its `timeGrain` (`DATE_TRUNC('month', ...)`) |

Some OBML definitions depend on the query and have no faithful single expression: period-over-period metrics, measures with `grain`, `filterContext` or `anchor`, and cumulative or window metrics over something that is already a window (a `total: true` measure, or another cumulative or window metric), since window calls cannot nest. The same goes for anything that references them. The export leaves these out of the OSI metrics, warns about each, and keeps them whole in the model-level `ORIONBELT` extension, so OBML → OSI → OBML still restores them.

The import reads both document shapes: the current flat Apache Ossie document, with the model at the root, and the earlier `semantic_model` array, which the export still writes while `0.2.0` is unreleased.

## Gradio UI

The Gradio UI provides **Import OSI** / **Export to OSI** buttons that use these API endpoints, with validation feedback for both directions.

## Mapping Reference

See the [OSI - OBML Mapping Analysis](https://github.com/ralforion/orionbelt-semantic-layer/blob/main/packages/osi-orionbelt/osi_obml_mapping_analysis.md) for the core-spec mapping.

## External concept mappings

OSI has no slot for [`ontology.prefixes` or `externalConceptMappings`](concept-mappings.md), so the converter carries both inside the `ORIONBELT` vendor `custom_extensions` of the OSI entity each OBML artefact becomes: the semantic model (prefixes and model-level mappings), the dataset, the field (for a dimension, including the extra-dimension descriptors when several dimensions share one column) and the metric (for every exported measure and metric; a left-out one keeps its links in the model-level extension). The reverse direction restores them verbatim, so OBML → OSI → OBML is lossless and a mapping always comes back together with the prefix its compact IRI needs. Other OSI tools see the payload as opaque vendor data. Importing an OSI document that names concepts natively (`maps_to_concept` or Ossie ontology mappings) is not implemented yet.
