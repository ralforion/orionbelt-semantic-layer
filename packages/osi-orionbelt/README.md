# osi-orionbelt

Bidirectional converter between **OBML** (OrionBelt Markup Language) semantic
models and **OSI** ([Open Semantic Interchange](https://open-semantic-interchange.org/)),
the open standard for portable semantic models (metrics, dimensions,
relationships).

This package is licensed under **Apache-2.0** and may be used freely. It is the
OrionBelt converter listed in the OSI converter ecosystem.

## Install

```bash
pip install osi-orionbelt
```

Optional deep OBML semantic validation (cycles, duplicate names, invalid refs)
via the full OrionBelt engine:

```bash
pip install "osi-orionbelt[obml-validation]"
```

Without that extra, OBML validation runs JSON-schema checks only and emits a
warning for the deeper semantic pass.

## CLI

A single `osi-orionbelt` command with two subcommands (mirroring `osi-dbt`):

| Subcommand | Direction | In | Out |
|---------|-----------|----|----|
| `obml-to-osi` | OBML -> OSI core-spec | OBML YAML | OSI YAML |
| `obml-to-osi --ontology` | OBML -> OSI ontology | OBML YAML | OSI ontology YAML |
| `osi-to-obml` | OSI core-spec -> OBML | OSI YAML | OBML YAML |

```bash
osi-orionbelt obml-to-osi -i model.obml.yaml -o model.osi.yaml
osi-orionbelt obml-to-osi --ontology -i model.obml.yaml -o model.ontology.yaml
osi-orionbelt osi-to-obml -i model.osi.yaml -o model.obml.yaml
```

`-i/--input` and `-o/--output` are required. Each subcommand prints conversion
warnings and a validation summary to stderr, and exits non-zero when the
produced document fails schema validation (unless `--no-validate`).

## Python API

```python
import yaml
from osi_orionbelt import OBMLtoOSI, OSItoOBML, validate_osi

obml = yaml.safe_load(open("model.obml.yaml"))
osi = OBMLtoOSI(obml, "sales", "Sales model").convert()
result = validate_osi(osi)
assert result.valid

obml_again = OSItoOBML(osi).convert()
```

## Vendor extensions

OSI `custom_extensions` carry vendor-tagged payloads. This converter:

- emits OrionBelt/OBML-proprietary data under the **`ORIONBELT`** vendor on OBML
  to OSI (OBML-only filters, settings, owner, refresh, type info, etc.);
- stashes OSI-native fields that OBML can't represent (unique keys, field
  labels, leftover `ai_context`) under the **`OSI`** vendor when going OSI to
  OBML, restoring them to first-class OSI fields on the way back;
- **preserves third-party vendor extensions verbatim** (e.g. `SNOWFLAKE`,
  `DBT`, `SALESFORCE`, `GOODDATA`) at the model, dataset, field, and
  measure/metric levels, so a full OSI to OBML to OSI roundtrip keeps the
  original vendor and data. OSI has no separate dimension entity, so an OBML
  dimension's foreign extensions surface on its OSI field.

Legacy `COMMON` / `OBSL` tags from earlier converter versions are still accepted
on read.

## Limitations / unsupported constructs

Some OBML constructs have no native OSI equivalent and are carried in vendor
`custom_extensions` (`obml_*` payloads) so they round-trip without loss back to
OBML, but are not interpreted by other OSI consumers:

- **Many-to-many joins** - represented in OBML join cardinality; flagged on
  export.
- **Named secondary join paths** - OBML's multiple join paths between the same
  pair of objects are an OBML-specific topology feature.
- **Measures / metrics and column-level value concepts in the ontology layer** -
  see `osi_obml_ontology_mapping_analysis.md` for the full mapping analysis.

OSI v0.1.x inputs are accepted on read via a legacy normalization shim; output
targets the current OSI version.
