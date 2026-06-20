# Google ADK Integration

Google Agent Development Kit (ADK) tools that connect directly to the OrionBelt Semantic Layer REST API. Build Gemini-powered agents that explore semantic models and compile analytical SQL queries.

## Files

| File | Purpose |
|------|---------|
| `orionbelt_tools.py` | 10 FunctionTools wrapping the REST API shortcut endpoints |
| `agent_example.py` | Interactive agent using `Agent` + `Runner` with session management |

## Prerequisites

```bash
pip install google-adk httpx
```

## Setup

Start OrionBelt API in single-model mode:

```bash
MODEL_FILE=examples/sem-layer.obml.yml uv run orionbelt-api
```

Set your API key:

```bash
export GOOGLE_API_KEY=...
```

## Quick Start

```python
import asyncio
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from orionbelt_tools import get_tools

async def main():
    tools = get_tools("http://localhost:8000")
    agent = Agent(
        name="orionbelt_analyst",
        model="gemini-2.0-flash",
        instruction="You are a data analyst. Use the tools to explore the model and compile SQL.",
        tools=tools,
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="orionbelt_analyst", user_id="user",
    )
    runner = Runner(
        agent=agent,
        app_name="orionbelt_analyst",
        session_service=session_service,
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text="Show me Revenue by Country for BigQuery")],
    )
    async for event in runner.run_async(
        user_id="user", session_id=session.id, new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            print(event.content.parts[0].text)

asyncio.run(main())
```

## Available Tools

| Tool | Description |
|------|-------------|
| `describe_model` | Full model structure (data objects, dimensions, measures, metrics) |
| `list_dimensions` | All dimensions with types and time grains |
| `list_measures` | All measures with aggregations and expressions |
| `list_metrics` | All metrics (derived, cumulative, period-over-period) |
| `list_dialects` | 8 supported SQL dialects with capabilities |
| `compile_query` | Compile dimensions + measures to SQL for a given dialect |
| `compile_query_advanced` | Compile with WHERE, HAVING, ORDER BY, and LIMIT |
| `explain_artefact` | Lineage trace for any dimension, measure, or metric |
| `search_model` | Fuzzy search by name or synonym |
| `get_join_graph` | Table relationships (nodes and edges with cardinality) |

## Multi-Agent Example

Use ADK's sub-agent pattern:

```python
from google.adk.agents import Agent
from orionbelt_tools import get_tools

tools = get_tools("http://localhost:8000")

data_agent = Agent(
    name="data_explorer",
    model="gemini-2.0-flash",
    instruction="Explore semantic models and compile SQL queries.",
    tools=tools,
)

report_agent = Agent(
    name="report_writer",
    model="gemini-2.0-flash",
    instruction="Write data analysis reports. Delegate data queries to data_explorer.",
    sub_agents=[data_agent],
)
```

## Deploy to Vertex AI Agent Engine

ADK agents can be deployed to Google Cloud:

```python
from google.adk.agents import Agent
from google.adk.deploy import VertexAIAgentEngine

agent = Agent(name="orionbelt_analyst", model="gemini-2.0-flash", tools=get_tools())
engine = VertexAIAgentEngine(project="your-project", location="us-central1")
engine.deploy(agent)
```

## MCP Alternative

Google ADK uses FastMCP internally and has native MCP support. Use it with the [OrionBelt MCP Server](https://github.com/ralforion/orionbelt-semantic-layer-mcp):

```python
from google.adk.tools.mcp_tool import MCPToolset

tools, cleanup = await MCPToolset.from_server(
    command="uv",
    args=["run", "--directory", "/path/to/orionbelt-semantic-layer-mcp", "orionbelt-mcp"],
    env={"API_BASE_URL": "http://localhost:8000"},
)
```
