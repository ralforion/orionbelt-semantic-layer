# Custom GPT: OrionBelt Semantic Layer Assistant

This directory contains everything needed to create a Custom GPT with Actions in ChatGPT.

## Files

| File | Purpose |
|------|---------|
| `openapi-gpt-action.yaml` | Curated OpenAPI 3.1 spec for GPT Actions (11 endpoints) |
| `instructions.md` | System prompt / instructions for the Custom GPT |

## Setup Steps

### 1. Deploy OrionBelt in Single-Model Mode

The GPT Action works best with OrionBelt running in **single-model mode** (one pre-loaded model, no session management needed). Set the `MODEL_FILES` environment variable:

```bash
MODEL_FILES=path/to/your/model.yaml uv run orionbelt-api
```

Or via Docker:

```bash
docker run -p 8080:8080 -e MODEL_FILES=/models/model.yaml -v ./models:/models orionbelt-semantic-layer-api
```

The API must be reachable over **HTTPS** (required by OpenAI). Use a cloud deployment, ngrok for testing, or any reverse proxy with TLS.

### 2. Create the Custom GPT

1. Go to [ChatGPT](https://chat.openai.com) and click **Explore GPTs** > **Create**
2. In the **Configure** tab:
   - **Name:** OrionBelt Semantic Layer
   - **Description:** Explore semantic data models and compile analytical SQL across 8 database dialects using business concepts.
   - **Instructions:** Paste the contents of `instructions.md`
   - **Conversation starters:**
     - What dimensions and measures are available?
     - Show me Total Sales by Country Name for Snowflake
     - What is the lineage of the Total Sales measure?
     - How are the tables connected?

### 3. Add the Action

1. Scroll down to **Actions** > **Create new action**
2. Paste the contents of `openapi-gpt-action.yaml`
3. **Replace** `https://YOUR-ORIONBELT-URL` in the `servers` section with your actual API URL
4. Set authentication:
   - **None** if your API is open (e.g. demo/internal)
   - **API Key** if you've added auth middleware
5. Click **Save**

### 4. Test

Use the **Preview** tab to test. Try:
- "What measures are available?"
- "Compile Total Sales by Country Name for BigQuery"
- "Explain the lineage of Sales Count"

Click on any action call in the conversation to see the raw request/response for debugging.

### 5. Publish (Optional)

To publish to the GPT Store:
- You need a verified domain matching your API URL
- Add a privacy policy URL
- Set visibility to **Everyone**
- OpenAI reviews before listing

## Endpoint Summary

The spec exposes 11 curated endpoints (all shortcut/auto-resolve, no session management):

| Operation | Method | Path | Purpose |
|-----------|--------|------|---------|
| `listDialects` | GET | `/v1/dialects` | Available SQL dialects |
| `getModelSchema` | GET | `/v1/schema` | Full model structure |
| `listDimensions` | GET | `/v1/dimensions` | All dimensions |
| `getDimension` | GET | `/v1/dimensions/{name}` | Single dimension |
| `listMeasures` | GET | `/v1/measures` | All measures |
| `getMeasure` | GET | `/v1/measures/{name}` | Single measure |
| `listMetrics` | GET | `/v1/metrics` | All metrics |
| `getMetric` | GET | `/v1/metrics/{name}` | Single metric |
| `explainLineage` | GET | `/v1/explain/{name}` | Lineage trace |
| `searchModel` | POST | `/v1/find` | Fuzzy search |
| `compileQuery` | POST | `/v1/query/sql` | Compile to SQL |

## Notes

- GPT Actions require HTTPS. No plain HTTP, no localhost.
- Response timeout is ~45 seconds. SQL compilation is fast, so this is fine.
- No file upload support in GPT Actions, so model loading is handled via `MODEL_FILES` env var.
- The `compileQuery` action has `x-openai-isConsequential: false` so it executes without user confirmation (read-only operation).
- Keep the model reasonably sized. Very large schema responses may hit the ~100K character response limit.
