# LangChain / LangGraph Integration

LangChain tools that connect directly to the OrionBelt Semantic Layer REST API. Build AI agents that explore semantic models and compile analytical SQL queries using business concepts.

## Files

| File | Purpose |
|------|---------|
| `orionbelt_tools.py` | 10 LangChain tools wrapping the REST API shortcut endpoints |
| `agent_example.py` | Interactive agent using LangGraph with `create_agent` |

## Prerequisites

```bash
pip install langchain langchain-anthropic langgraph httpx

# Or with OpenAI:
pip install langchain langchain-openai langgraph httpx
```

## Setup

Start OrionBelt API in single-model mode:

```bash
MODEL_FILE=examples/sem-layer.obml.yml uv run orionbelt-api
```

## Quick Start

```python
import asyncio
from langchain.agents import create_agent
from orionbelt_tools import get_tools

async def main():
    tools = get_tools("http://localhost:8000")
    agent = create_agent("anthropic:claude-sonnet-4-5", tools)
    result = await agent.ainvoke(
        {"messages": "Show me Revenue by Country for Snowflake"}
    )
    print(result["messages"][-1].content)

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

## Custom API URL

All tools are configured via `get_tools(api_base_url)`:

```python
# Local
tools = get_tools("http://localhost:8000")

# Cloud Run deployment
tools = get_tools("http://35.187.174.102")
```

## LangGraph StateGraph (Advanced)

For full control over the agent loop:

```python
import asyncio
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, MessagesState, START
from langgraph.prebuilt import ToolNode, tools_condition
from orionbelt_tools import get_tools

async def main():
    tools = get_tools("http://localhost:8000")
    model = init_chat_model("anthropic:claude-sonnet-4-5")

    def call_model(state: MessagesState):
        return {"messages": model.bind_tools(tools).invoke(state["messages"])}

    builder = StateGraph(MessagesState)
    builder.add_node("call_model", call_model)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "call_model")
    builder.add_conditional_edges("call_model", tools_condition)
    builder.add_edge("tools", "call_model")
    graph = builder.compile()

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "Revenue by Country, top 10, Snowflake"}]}
    )
    for msg in result["messages"]:
        if msg.content:
            print(f"[{msg.type}] {msg.content[:500]}")

asyncio.run(main())
```

## MCP Alternative

If you prefer MCP over direct REST API calls, use `langchain-mcp-adapters` with the [OrionBelt MCP Server](https://github.com/ralforion/orionbelt-semantic-layer-mcp):

```bash
pip install langchain-mcp-adapters
```

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "orionbelt": {
        "command": "uv",
        "args": ["run", "--directory", "/path/to/orionbelt-semantic-layer-mcp", "orionbelt-mcp"],
        "transport": "stdio",
        "env": {"API_BASE_URL": "http://localhost:8000"},
    }
})
tools = await client.get_tools()  # auto-discovers all 10+ MCP tools
```
