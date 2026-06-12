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

| Command | Direction | In | Out |
|---------|-----------|----|----|
| `obml-to-osi` | OBML -> OSI core-spec | OBML YAML | OSI YAML |
| `obml-to-osi --ontology` | OBML -> OSI ontology | OBML YAML | OSI ontology YAML |
| `osi-to-obml` | OSI core-spec -> OBML | OSI YAML | OBML YAML |

```bash
obml-to-osi model.obml.yaml -o model.osi.yaml
obml-to-osi --ontology model.obml.yaml -o model.ontology.yaml
osi-to-obml model.osi.yaml -o model.obml.yaml
```

Each command prints conversion warnings and a validation summary to stderr, and
exits non-zero when the produced document fails schema validation (unless
`--no-validate`).

## Python API

```python
import yaml
from osi_orionbelt import OBMLtoOSI, OSItoOBML, validate_osi

obml = yaml.safe_load(open("model.obml.yaml"))
osi = OBMLtoOSI(obml, name="sales", description="Sales model").convert()
result = validate_osi(osi)
assert result.valid

obml_again = OSItoOBML(osi).convert()
```

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
