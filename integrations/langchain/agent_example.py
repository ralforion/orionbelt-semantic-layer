"""Example: OrionBelt Semantic Layer agent using LangChain / LangGraph.

Prerequisites:
    pip install langchain langchain-anthropic langgraph httpx

    # Or with OpenAI:
    pip install langchain langchain-openai langgraph httpx

Start OrionBelt API in single-model mode first:
    MODEL_FILE=examples/sem-layer.obml.yml uv run orionbelt-api

Then run this script:
    export ANTHROPIC_API_KEY=sk-ant-...
    python agent_example.py
"""

from __future__ import annotations

import asyncio

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

from orionbelt_tools import get_tools

SYSTEM_PROMPT = """\
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

    # Using Anthropic Claude (swap ChatAnthropic for ChatOpenAI for OpenAI)
    llm = ChatAnthropic(model="claude-sonnet-4-5-20241022")
    agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)

    # Interactive loop
    print("OrionBelt Semantic Layer Agent (type 'quit' to exit)\n")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue

        response = await agent.ainvoke({"messages": [{"role": "user", "content": user_input}]})
        last_message = response["messages"][-1]
        print(f"\nAssistant: {last_message.content}\n")


if __name__ == "__main__":
    asyncio.run(main())
