---
description: "Connect agents to OrionBelt: author task-specific OBML and queries with the one-shot API, or use framework adapters and MCP to discover and query shared models."
---

# AI Integrations

Agents can work with OrionBelt in two ways:

- **Model the current questions.** Author only the OBML needed for a task and submit
  it with several independent queries to `POST /v1/oneshot/batch`. The model can
  be temporary or retained for follow-up questions. The
  [Agent Workflows guide](agent-workflows.md) includes a complete REST example
  that can be wrapped as a tool in your agent framework.
- **Query shared models.** Discover established dimensions, measures, and metrics,
  then compose queries using the framework adapters below or the MCP server.

The ready-to-use REST integrations below use the [shortcut endpoints](../api/endpoints.md)
and work best in [admin-curated mode](../reference/configuration.md#admin-curated-mode).
Inline model authoring through the one-shot API requires a deployment that permits
model uploads; admin-curated deployments accept batches referencing existing models.

## Available Integrations

| Integration | Language | Description | Guide |
|-------------|----------|-------------|-------|
| **LangChain / LangGraph** | Python | 10 async tools + `create_agent` example | [`integrations/langchain/`](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/integrations/langchain) |
| **OpenAI Agents SDK** | Python | 10 function tools + `Agent`/`Runner` example | [`integrations/openai-agents-sdk/`](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/integrations/openai-agents-sdk) |
| **CrewAI** | Python | 10 tools + multi-agent crew example | [`integrations/crewai/`](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/integrations/crewai) |
| **Google ADK** | Python | 10 FunctionTools + Gemini agent example | [`integrations/google-adk/`](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/integrations/google-adk) |
| **Vercel AI SDK** | TypeScript | 10 tools + Next.js API route example | [`integrations/vercel-ai-sdk/`](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/integrations/vercel-ai-sdk) |
| **n8n** | JSON | Workflow templates (batch + AI agent) | [`integrations/n8n/`](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/integrations/n8n) |
| **ChatGPT Custom GPT** | OpenAPI | OpenAPI 3.1 spec + GPT instructions | [`integrations/chatgpt-custom-gpt/`](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/integrations/chatgpt-custom-gpt) |
| **MCP Server** | Python | 10+ tools for Claude, Copilot, Cursor, Windsurf | [separate repo](https://github.com/ralforion/orionbelt-semantic-layer-mcp) |

Each integration directory includes a self-contained README with setup instructions, code examples, and usage patterns. The MCP server is maintained as a [separate repository](https://github.com/ralforion/orionbelt-semantic-layer-mcp).
