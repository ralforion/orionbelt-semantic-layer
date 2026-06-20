# CrewAI Integration

CrewAI tools that connect directly to the OrionBelt Semantic Layer REST API. Build multi-agent crews where specialized agents explore semantic models, compile SQL, and produce reports.

## Files

| File | Purpose |
|------|---------|
| `orionbelt_tools.py` | 10 CrewAI tools wrapping the REST API shortcut endpoints |
| `crew_example.py` | Two-agent crew: Data Explorer + Report Writer |

## Prerequisites

```bash
pip install crewai httpx
```

## Setup

Start OrionBelt API in single-model mode:

```bash
MODEL_FILE=examples/sem-layer.obml.yml uv run orionbelt-api
```

## Quick Start

```python
from crewai import Agent, Crew, Task
from orionbelt_tools import OrionBeltTools

ob = OrionBeltTools(api_base_url="http://localhost:8000")

agent = Agent(
    role="Data Analyst",
    goal="Compile SQL queries from semantic models.",
    backstory="You are a data analyst using OrionBelt to compile business queries.",
    tools=ob.tools(),
)

task = Task(
    description="Compile Revenue by Country for Snowflake.",
    expected_output="The compiled Snowflake SQL query.",
    agent=agent,
)

crew = Crew(agents=[agent], tasks=[task])
result = crew.kickoff()
print(result)
```

## Available Tools

| Tool | Description |
|------|-------------|
| Describe Model | Full model structure (data objects, dimensions, measures, metrics) |
| List Dimensions | All dimensions with types and time grains |
| List Measures | All measures with aggregations and expressions |
| List Metrics | All metrics (derived, cumulative, period-over-period) |
| List Dialects | 8 supported SQL dialects with capabilities |
| Compile Query | Compile dimensions + measures to SQL for a given dialect |
| Compile Advanced Query | Compile with WHERE, HAVING, ORDER BY, and LIMIT |
| Explain Artefact | Lineage trace for any dimension, measure, or metric |
| Search Model | Fuzzy search by name or synonym |
| Get Join Graph | Table relationships (nodes and edges with cardinality) |

## Multi-Agent Crew Example

The `crew_example.py` demonstrates a two-agent crew:

1. **Data Explorer** — Uses OrionBelt tools to discover the model and compile queries for multiple dialects
2. **Report Writer** — Takes the explorer's findings and writes a formatted comparison report

```bash
export OPENAI_API_KEY=sk-...
python crew_example.py
```

## Tool Design Notes

CrewAI tools use synchronous functions (not async). The `OrionBeltTools` class uses `httpx.Client` (sync) internally. The `compile_query` tool accepts comma-separated dimension/measure names as strings, which works better with CrewAI's string-based tool argument passing.

## MCP Alternative

CrewAI has native MCP support. Use it with the [OrionBelt MCP Server](https://github.com/ralforion/orionbelt-semantic-layer-mcp):

```python
from crewai import Agent

agent = Agent(
    role="Data Analyst",
    goal="Compile SQL queries from semantic models.",
    backstory="You are a data analyst using OrionBelt.",
    mcps=[{
        "command": "uv",
        "args": ["run", "--directory", "/path/to/orionbelt-semantic-layer-mcp", "orionbelt-mcp"],
        "transport": "stdio",
        "env": {"API_BASE_URL": "http://localhost:8000"},
    }],
)
```
