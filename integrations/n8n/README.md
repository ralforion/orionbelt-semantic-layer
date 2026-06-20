# n8n Integration

n8n workflow templates that connect to the OrionBelt Semantic Layer REST API. Use HTTP Request nodes for batch workflows or the AI Agent node for conversational semantic queries.

## Files

| File | Purpose |
|------|---------|
| `workflow_compile_query.json` | Basic workflow: fetch schema, list dimensions/measures, compile a query |
| `workflow_ai_agent.json` | AI Agent workflow: chat-based semantic query compilation with LLM tools |

## Prerequisites

- [n8n](https://n8n.io) (self-hosted or cloud)
- OrionBelt API running and accessible from n8n

## Setup

1. Start OrionBelt API in single-model mode:

```bash
MODEL_FILE=examples/sem-layer.obml.yml uv run orionbelt-api
```

2. In n8n, set the environment variable `ORIONBELT_API_URL`:
   - Self-hosted: add `ORIONBELT_API_URL=http://localhost:8000` to your n8n environment
   - n8n Cloud: Settings > Variables > add `ORIONBELT_API_URL`

3. Import a workflow: n8n menu > Import from File > select the JSON file

## Workflow 1: Compile Query (Batch)

`workflow_compile_query.json` is a simple HTTP-based workflow:

```
Manual Trigger
  ├── Get Model Schema ──→ Compile Query
  ├── List Dimensions
  └── List Measures
```

- Fetches the model schema, dimensions, and measures in parallel
- Compiles a query (edit the JSON body in the "Compile Query" node)
- Customize the `dialect` query parameter (default: `postgres`)

**To modify the query:** Open the "Compile Query" node and edit the JSON body:

```json
{
  "select": {
    "dimensions": ["Country"],
    "measures": ["Revenue"]
  },
  "order_by": [{"field": "Revenue", "direction": "desc"}],
  "limit": 10
}
```

## Workflow 2: AI Agent (Conversational)

`workflow_ai_agent.json` creates a chat-based AI agent with OrionBelt tools:

```
Chat Trigger ──→ AI Agent
                   ├── OpenAI Chat Model (gpt-4o)
                   ├── Get Schema Tool
                   ├── List Dimensions Tool
                   ├── List Measures Tool
                   └── Compile Query Tool
```

- Users chat with the agent in natural language
- The agent uses HTTP Request tools to explore the model and compile SQL
- Requires an OpenAI API key in n8n credentials

**Example conversations:**
- "What dimensions and measures are available?"
- "Show me Revenue by Country for Snowflake"
- "Compile a query with Revenue and Order Count grouped by Product Category, sorted by Revenue descending"

## Customization

### Change the API URL

All nodes use `{{ $env.ORIONBELT_API_URL }}`. Set this variable in n8n to point to your OrionBelt instance:

- Local: `http://localhost:8000`
- Docker: `http://host.docker.internal:8080`
- Cloud Run: `http://35.187.174.102`

### Add More Tools

To add search, lineage, or join graph tools to the AI Agent workflow, add HTTP Request Tool nodes:

| Tool | Method | URL |
|------|--------|-----|
| Search | POST | `{{ $env.ORIONBELT_API_URL }}/v1/find` |
| Explain | GET | `{{ $env.ORIONBELT_API_URL }}/v1/explain/{name}` |
| Join Graph | GET | `{{ $env.ORIONBELT_API_URL }}/v1/join-graph` |
| Metrics | GET | `{{ $env.ORIONBELT_API_URL }}/v1/metrics` |
| Dialects | GET | `{{ $env.ORIONBELT_API_URL }}/v1/dialects` |

### Use Anthropic Instead of OpenAI

Replace the "OpenAI Chat Model" node with an "Anthropic Chat Model" node and select `claude-sonnet-4-5`.

## MCP Alternative

n8n has a native MCP Client Tool node. Use it with the [OrionBelt MCP Server](https://github.com/ralforion/orionbelt-semantic-layer-mcp) for automatic tool discovery without manual HTTP configuration.
