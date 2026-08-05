"""Example: OrionBelt Semantic Layer agent using Google ADK.

Prerequisites:
    pip install google-adk httpx

Start OrionBelt API in single-model mode first:
    MODEL_FILES=examples/orionbelt_1_commerce.yaml uv run orionbelt-api

Then run this script:
    export GOOGLE_API_KEY=...
    python agent_example.py
"""

from __future__ import annotations

import asyncio

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from orionbelt_tools import get_tools

INSTRUCTIONS = """\
You are a data analyst assistant powered by the OrionBelt Semantic Layer.
You help users explore semantic data models and compile analytical SQL queries.

Workflow:
1. Start by calling describe_model to understand the available data.
2. Use list_dimensions, list_measures, or list_metrics for details.
3. Use search_model to find artefacts by name or synonym when unsure.
4. Use explain_artefact to trace lineage back to physical tables.
5. Use compile_query to generate SQL with exact dimension and measure names.
6. For filtered/sorted queries, use compile_query_advanced with full JSON.

Rules:
- Use exact artefact names from the model (case-sensitive).
- Confirm the target SQL dialect with the user if not specified.
  Supported: bigquery, clickhouse, databricks, dremio, duckdb, mysql, postgres, snowflake.
- Present compiled SQL in a code block with the dialect name.
"""

API_BASE_URL = "http://localhost:8000"


async def main() -> None:
    tools = get_tools(API_BASE_URL)

    agent = Agent(
        name="orionbelt_analyst",
        model="gemini-2.0-flash",
        instruction=INSTRUCTIONS,
        tools=tools,
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="orionbelt_analyst",
        user_id="user",
    )

    runner = Runner(
        agent=agent,
        app_name="orionbelt_analyst",
        session_service=session_service,
    )

    print("OrionBelt Semantic Layer Agent (type 'quit' to exit)\n")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue

        content = types.Content(
            role="user",
            parts=[types.Part(text=user_input)],
        )

        final_text = ""
        async for event in runner.run_async(
            user_id="user",
            session_id=session.id,
            new_message=content,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = event.content.parts[0].text

        print(f"\nAssistant: {final_text}\n")


if __name__ == "__main__":
    asyncio.run(main())
