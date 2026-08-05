"""Example: OrionBelt Semantic Layer crew using CrewAI.

Prerequisites:
    pip install crewai httpx

Start OrionBelt API in single-model mode first:
    MODEL_FILES=examples/orionbelt_1_commerce.yaml uv run orionbelt-api

Then run this script:
    export OPENAI_API_KEY=sk-...
    python crew_example.py
"""

from __future__ import annotations

from crewai import Agent, Crew, Task

from orionbelt_tools import OrionBeltTools

API_BASE_URL = "http://localhost:8000"

ob = OrionBeltTools(api_base_url=API_BASE_URL)
tools = ob.tools()

# Agent 1: Data Explorer — discovers the model and compiles queries
data_explorer = Agent(
    role="Data Explorer",
    goal="Explore the semantic model and compile analytical SQL queries using business concepts.",
    backstory=(
        "You are a senior data analyst with deep expertise in semantic layers. "
        "You use the OrionBelt tools to discover dimensions, measures, and metrics, "
        "then compile precise SQL queries for the requested dialect. "
        "You always verify artefact names before compiling."
    ),
    tools=tools,
    verbose=True,
)

# Agent 2: Report Writer — formats the results into a readable report
report_writer = Agent(
    role="Report Writer",
    goal="Write clear, concise data analysis reports based on compiled SQL queries.",
    backstory=(
        "You are a technical writer who translates SQL queries and model metadata "
        "into readable reports for business stakeholders. You explain what the query "
        "does, what dimensions and measures are used, and any relevant join logic."
    ),
    verbose=True,
)

# Task 1: Explore the model and compile a query
explore_task = Task(
    description=(
        "1. Explore the semantic model to understand what data is available.\n"
        "2. List the available dimensions and measures.\n"
        "3. Compile a query for 'Revenue by Country' using the Snowflake dialect.\n"
        "4. Also compile the same query for BigQuery to show dialect differences."
    ),
    expected_output=(
        "A summary of the model (data objects, dimensions, measures) "
        "followed by the compiled SQL for both Snowflake and BigQuery."
    ),
    agent=data_explorer,
)

# Task 2: Write a report based on the exploration
report_task = Task(
    description=(
        "Based on the data explorer's findings, write a short report that:\n"
        "1. Summarizes the semantic model structure.\n"
        "2. Shows the compiled SQL for both dialects.\n"
        "3. Highlights the differences between Snowflake and BigQuery SQL."
    ),
    expected_output="A formatted markdown report comparing the two SQL dialects.",
    agent=report_writer,
    context=[explore_task],
)

crew = Crew(
    agents=[data_explorer, report_writer],
    tasks=[explore_task, report_task],
    verbose=True,
)

if __name__ == "__main__":
    result = crew.kickoff()
    print("\n" + "=" * 60)
    print("FINAL REPORT")
    print("=" * 60)
    print(result)
