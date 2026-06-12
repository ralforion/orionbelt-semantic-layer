# OSI Interoperability

**OSI (Open Semantic Interchange)** is an open standard for portable semantic models, founded with the goal of letting metric and dimension definitions move between BI tools, semantic layers, and data platforms without rewriting. See [open-semantic-interchange.org](https://open-semantic-interchange.org/) for the specification and contributor list.

OrionBelt includes a bidirectional converter between OBML and the [OSI specification](https://github.com/open-semantic-interchange/OSI) format. The converter handles structural differences between the two formats — including metric decomposition, relationship restructuring, and lossless `ai_context` preservation via `customExtensions` — with built-in validation for both directions.

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

## OSI ontology export

OSI defines two layers, validated by **separate** JSON Schemas and (per OSI's own `validate.py`) kept in **separate documents**:

- the **core-spec** semantic model (datasets, fields, relationships, metrics) — the default `output_yaml`, and
- the **ontology** layer (EntityType/ValueType concepts, relationships with multiplicity and verbalizations, plus mappings back to the logical model).

Set `include_ontology=true` to additionally emit the ontology document. It is returned as a second, individually-valid artefact in `ontology_yaml`, with its own `ontology_validation`; the core-spec `output_yaml` is unchanged.

```bash
# Convert OBML -> OSI, also emitting the ontology document
curl -X POST http://127.0.0.1:8000/v1/convert/obml-to-osi \
  -H "Content-Type: application/json" \
  -d '{"input_yaml": "version: 1.0\ndataObjects:\n  ...", "include_ontology": true}' | jq

# Export a loaded model with its ontology
curl "http://127.0.0.1:8000/v1/sessions/{id}/models/{mid}/osi?include_ontology=true" | jq
```

The OBML constructs map to the ontology as follows: each `dataObject` becomes an `EntityType` concept, each join becomes a relationship whose `multiplicity` derives from the join `joinType` (`many-to-one` -> `ManyToOne`, `one-to-one` -> `OneToOne`), and `concept_mappings` bind concepts to physical columns. Many-to-many joins, named secondary paths, measures/metrics, and column-level value concepts are not represented in the ontology layer and surface as conversion `warnings`. An ontology **importer** (OSI ontology -> OBML) is intentionally deferred while OSI remains at `0.2.0.dev0`; import the OSI core spec instead.

## Gradio UI

The Gradio UI provides **Import OSI** / **Export to OSI** buttons that use these API endpoints, with validation feedback for both directions.

## Mapping Reference

See the [OSI - OBML Mapping Analysis](https://github.com/ralfbecher/orionbelt-semantic-layer/blob/main/packages/osi-orionbelt/osi_obml_mapping_analysis.md) for the core-spec mapping, and the [OBML -> OSI Ontology Mapping Analysis](https://github.com/ralfbecher/orionbelt-semantic-layer/blob/main/packages/osi-orionbelt/osi_obml_ontology_mapping_analysis.md) for the ontology-layer mapping and its documented gaps.
