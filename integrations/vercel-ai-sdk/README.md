# Vercel AI SDK Integration

TypeScript tools for the [Vercel AI SDK](https://ai-sdk.dev) that connect directly to the OrionBelt Semantic Layer REST API. Build Next.js chat interfaces and AI-powered dashboards that compile semantic queries.

## Files

| File | Purpose |
|------|---------|
| `orionbelt-tools.ts` | 10 Vercel AI SDK tools wrapping the REST API shortcut endpoints |
| `route-example.ts` | Next.js API route example (`app/api/chat/route.ts`) |

## Prerequisites

```bash
npm install ai @ai-sdk/anthropic zod
# Or with OpenAI:
npm install ai @ai-sdk/openai zod
```

## Setup

Start OrionBelt API in single-model mode:

```bash
MODEL_FILE=examples/sem-layer.obml.yml uv run orionbelt-api
```

Set environment variables:

```bash
ANTHROPIC_API_KEY=sk-ant-...
ORIONBELT_API_URL=http://localhost:8000
```

## Quick Start

### Next.js API Route

Copy `orionbelt-tools.ts` and `route-example.ts` into your Next.js app:

```
app/
  api/
    chat/
      route.ts          # route-example.ts
      orionbelt-tools.ts
```

### Standalone Script

```typescript
import { generateText } from "ai";
import { anthropic } from "@ai-sdk/anthropic";
import { getOrionBeltTools } from "./orionbelt-tools";

const tools = getOrionBeltTools("http://localhost:8000");

const { text } = await generateText({
  model: anthropic("claude-sonnet-4-5"),
  tools,
  maxSteps: 10,
  prompt: "Show me Revenue by Country for Snowflake",
});

console.log(text);
```

## Available Tools

| Tool | Description |
|------|-------------|
| `describeModel` | Full model structure (data objects, dimensions, measures, metrics) |
| `listDimensions` | All dimensions with types and time grains |
| `listMeasures` | All measures with aggregations and expressions |
| `listMetrics` | All metrics (derived, cumulative, period-over-period) |
| `listDialects` | 8 supported SQL dialects with capabilities |
| `compileQuery` | Compile dimensions + measures to SQL for a given dialect |
| `compileQueryAdvanced` | Compile with WHERE, HAVING, ORDER BY, and LIMIT |
| `explainArtefact` | Lineage trace for any dimension, measure, or metric |
| `searchModel` | Fuzzy search by name or synonym |
| `getJoinGraph` | Table relationships (nodes and edges with cardinality) |

## Using with OpenAI

Replace the model provider in the route:

```typescript
import { openai } from "@ai-sdk/openai";

const result = streamText({
  model: openai("gpt-4o"),
  // ... rest stays the same
});
```

## Using with useChat (Frontend)

Pair with Vercel AI SDK's `useChat` hook for a complete chat UI:

```tsx
"use client";
import { useChat } from "@ai-sdk/react";

export default function Chat() {
  const { messages, input, handleInputChange, handleSubmit } = useChat({
    api: "/api/chat",
  });

  return (
    <div>
      {messages.map((m) => (
        <div key={m.id}>
          <strong>{m.role}:</strong>
          <pre>{m.content}</pre>
        </div>
      ))}
      <form onSubmit={handleSubmit}>
        <input value={input} onChange={handleInputChange} placeholder="Ask about your data..." />
      </form>
    </div>
  );
}
```

## MCP Alternative

Vercel AI SDK v6 has native MCP support. Use it with the [OrionBelt MCP Server](https://github.com/ralforion/orionbelt-semantic-layer-mcp):

```typescript
import { experimental_createMCPClient as createMCPClient } from "ai";

const client = await createMCPClient({
  transport: {
    type: "sse",
    url: "http://localhost:9000/mcp",
  },
});

const tools = await client.tools();
```
