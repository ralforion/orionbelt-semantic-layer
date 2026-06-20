# OpenAI Agents SDK Integration

OpenAI Agents SDK tools that connect directly to the OrionBelt Semantic Layer REST API. Build GPT-4o/o3 agents that explore semantic models and compile analytical SQL queries.

## Files

| File | Purpose |
|------|---------|
| `orionbelt_tools.py` | 10 function tools wrapping the REST API shortcut endpoints |
| `agent_example.py` | Interactive agent using `Agent` + `Runner` |

## Prerequisites

```bash
pip install openai-agents httpx
```

## Setup

Start OrionBelt API in single-model mode:

```bash
MODEL_FILE=examples/sem-layer.obml.yml uv run orionbelt-api
```

## Quick Start

```python
import asyncio
from agents import Agent, Runner
from orionbelt_tools import get_tools

async def main():
    tools = get_tools("http://localhost:8000")
    agent = Agent(
        name="OrionBelt Analyst",
        model="gpt-4o",
        tools=tools,
        instructions="You are a data analyst. Use the tools to explore the semantic model and compile SQL.",
    )
    result = await Runner.run(agent, "Show me Revenue by Country for Snowflake")
    print(result.final_output)

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

Combine OrionBelt tools with other agents using handoffs:

```python
from agents import Agent, Runner
from orionbelt_tools import get_tools

tools = get_tools("http://localhost:8000")

data_agent = Agent(
    name="Data Analyst",
    model="gpt-4o",
    tools=tools,
    instructions="You explore semantic models and compile SQL queries.",
)

report_agent = Agent(
    name="Report Writer",
    model="gpt-4o",
    instructions="You write clear data analysis reports based on SQL queries.",
    handoffs=[data_agent],
)
```

## MCP Alternative

If you prefer MCP over direct REST API calls, the OpenAI Agents SDK has native MCP support. Use it with the [OrionBelt MCP Server](https://github.com/ralforion/orionbelt-semantic-layer-mcp):

```python
from agents.mcp import MCPServerStdio

mcp_server = MCPServerStdio(
    command="uv",
    args=["run", "--directory", "/path/to/orionbelt-semantic-layer-mcp", "orionbelt-mcp"],
    env={"API_BASE_URL": "http://localhost:8000"},
)

agent = Agent(
    name="OrionBelt Analyst",
    model="gpt-4o",
    mcp_servers=[mcp_server],
)
```
