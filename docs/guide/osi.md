# OSI Interoperability

OrionBelt includes a bidirectional converter between OBML and the [Open Semantic Interchange (OSI)](https://github.com/open-semantic-interchange/OSI) format. The converter handles structural differences between the two formats — including metric decomposition, relationship restructuring, and lossless `ai_context` preservation via `customExtensions` — with built-in validation for both directions.

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

## Gradio UI

The Gradio UI provides **Import OSI** / **Export to OSI** buttons that use these API endpoints, with validation feedback for both directions.

## Mapping Reference

See the [OSI - OBML Mapping Analysis](https://github.com/ralfbecher/orionbelt-semantic-layer/blob/main/osi-obml/osi_obml_mapping_analysis.md) for a detailed comparison and conversion reference.
