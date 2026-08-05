"""Example: OrionBelt Semantic Layer agent using OpenAI Agents SDK.

Prerequisites:
    pip install openai-agents httpx

Start OrionBelt API in single-model mode first:
    MODEL_FILES=examples/orionbelt_1_commerce.yaml uv run orionbelt-api

Then run this script:
    export OPENAI_API_KEY=sk-...
    python agent_example.py
"""

from __future__ import annotations

import asyncio

from agents import Agent, Runner

from orionbelt_tools import get_tools

INSTRUCTIONS = """\
You are a data analyst assistant powered by the OrionBelt Semantic Layer.
You help users explore semantic data models and compile analytical SQL queries.

## Workflow

1. Start by calling describe_model to understand the available data objects,
   dimensions, measures, and metrics.
2. Use list_dimensions, list_measures, or list_metrics for details.
3. Use search_model to find artefacts by name or synonym when unsure.
4. Use explain_artefact to trace lineage back to physical tables.
5. Use get_join_graph to understand table relationships.
6. Use compile_query to generate SQL with exact dimension and measure names.
7. For filtered/sorted queries, use compile_query_advanced with full JSON.

## Rules

- Use exact artefact names from the model (case-sensitive).
- Confirm the target SQL dialect with the user if not specified.
  Supported: bigquery, clickhouse, databricks, dremio, duckdb, mysql, postgres, snowflake.
- Present compiled SQL in a code block with the dialect name.
- If a query fails, read the error and fix the dimension/measure names.
"""

API_BASE_URL = "http://localhost:8000"


async def main() -> None:
    tools = get_tools(API_BASE_URL)

    agent = Agent(
        name="OrionBelt Analyst",
        instructions=INSTRUCTIONS,
        model="gpt-4o",
        tools=tools,
    )

    # Interactive loop
    print("OrionBelt Semantic Layer Agent (type 'quit' to exit)\n")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue

        result = await Runner.run(agent, user_input)
        print(f"\nAssistant: {result.final_output}\n")


if __name__ == "__main__":
    asyncio.run(main())
