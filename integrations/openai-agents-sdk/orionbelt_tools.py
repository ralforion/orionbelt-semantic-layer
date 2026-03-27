"""OpenAI Agents SDK tools for the OrionBelt Semantic Layer REST API.

These tools wrap the shortcut endpoints (auto-resolve session/model) and work
when OrionBelt runs in single-model mode (MODEL_FILE set).

Usage:
    from orionbelt_tools import get_tools

    tools = get_tools("http://localhost:8000")
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
from agents import FunctionTool, RunContextWrapper


def get_tools(api_base_url: str = "http://localhost:8000") -> list[FunctionTool]:
    """Return all OrionBelt tools configured for the given API URL."""

    async def describe_model(ctx: RunContextWrapper[None]) -> str:
        """Get the full semantic model structure: data objects, dimensions, measures, metrics.

        Call this first to understand what is available before compiling queries.
        """
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.get("/v1/schema")
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)

    async def list_dimensions(ctx: RunContextWrapper[None]) -> str:
        """List all dimensions in the semantic model.

        Dimensions are categorical or temporal attributes used for grouping
        and filtering (e.g. Country, Order Date, Product Category).
        """
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.get("/v1/dimensions")
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)

    async def list_measures(ctx: RunContextWrapper[None]) -> str:
        """List all measures in the semantic model.

        Measures are numeric aggregations computed from data object columns
        (e.g. Revenue, Order Count, Average Price).
        """
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.get("/v1/measures")
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)

    async def list_metrics(ctx: RunContextWrapper[None]) -> str:
        """List all metrics in the semantic model.

        Metrics are derived calculations built from measures (e.g. Profit Margin,
        YoY Growth). Types: derived, cumulative, period_over_period.
        """
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.get("/v1/metrics")
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)

    async def list_dialects(ctx: RunContextWrapper[None]) -> str:
        """List all supported SQL dialects with their capabilities.

        Supported: bigquery, clickhouse, databricks, dremio, duckdb, mysql, postgres, snowflake.
        """
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.get("/v1/dialects")
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)

    async def compile_query(
        ctx: RunContextWrapper[None],
        dimensions: list[str],
        measures: list[str],
        dialect: str = "postgres",
        limit: int | None = None,
    ) -> str:
        """Compile a semantic query to SQL.

        Dimensions and measures must be referenced by their exact business names
        as returned by describe_model, list_dimensions, and list_measures.

        Args:
            dimensions: Dimension names to group by (e.g. ["Country", "Order Date"]).
            measures: Measure names to aggregate (e.g. ["Revenue", "Order Count"]).
            dialect: Target SQL dialect (postgres, snowflake, bigquery, clickhouse,
                     databricks, dremio, duckdb, mysql).
            limit: Maximum rows to return.
        """
        query: dict = {"select": {"dimensions": dimensions, "measures": measures}}
        if limit is not None:
            query["limit"] = limit
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.post(
                "/v1/query/sql", json=query, params={"dialect": dialect}
            )
            resp.raise_for_status()
            data = resp.json()
            parts = [f"-- Dialect: {data['dialect']}", data["sql"]]
            if data.get("warnings"):
                parts.append(f"\n-- Warnings: {', '.join(data['warnings'])}")
            if data.get("explain"):
                exp = data["explain"]
                parts.append(f"\n-- Planner: {exp['planner']} ({exp['planner_reason']})")
                parts.append(f"-- Base object: {exp['base_object']}")
            return "\n".join(parts)

    async def compile_query_advanced(
        ctx: RunContextWrapper[None],
        query_json: str,
        dialect: str = "postgres",
    ) -> str:
        """Compile an advanced query with filters, ordering, and HAVING clauses.

        Use this for queries that need WHERE filters, HAVING filters, ORDER BY,
        or other advanced features not covered by compile_query.

        Args:
            query_json: Full query as JSON string. Format:
                {
                    "select": {"dimensions": [...], "measures": [...]},
                    "where": [{"dimension": "Country", "operator": "=", "value": "Germany"}],
                    "having": [{"measure": "Revenue", "operator": ">", "value": 1000}],
                    "order_by": [{"field": "Revenue", "direction": "desc"}],
                    "limit": 100
                }
            dialect: Target SQL dialect.
        """
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            return f"Error: invalid JSON in query_json: {exc}"
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.post(
                "/v1/query/sql", json=query, params={"dialect": dialect}
            )
            resp.raise_for_status()
            data = resp.json()
            parts = [f"-- Dialect: {data['dialect']}", data["sql"]]
            if data.get("warnings"):
                parts.append(f"\n-- Warnings: {', '.join(data['warnings'])}")
            return "\n".join(parts)

    async def explain_artefact(ctx: RunContextWrapper[None], name: str) -> str:
        """Explain the lineage of a dimension, measure, or metric.

        Shows which data objects, columns, joins, and expressions contribute
        to the named artefact.

        Args:
            name: Exact name of the dimension, measure, or metric.
        """
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.get(f"/v1/explain/{quote(name, safe='')}")
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)

    async def search_model(ctx: RunContextWrapper[None], query: str) -> str:
        """Search for dimensions, measures, or metrics by name or synonym.

        Use this when the user mentions a concept and you need to find the
        matching artefact (e.g. "sales" might match a measure with synonym "sales").

        Args:
            query: Search term (case-insensitive).
        """
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.post("/v1/find", json={"query": query})
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)

    async def get_join_graph(ctx: RunContextWrapper[None]) -> str:
        """Get the join graph showing how data objects (tables) are connected.

        Returns nodes (data objects) and edges (joins) with cardinality and join columns.
        """
        async with httpx.AsyncClient(base_url=api_base_url, timeout=30) as client:
            resp = await client.get("/v1/join-graph")
            resp.raise_for_status()
            return json.dumps(resp.json(), indent=2)

    return [
        FunctionTool(describe_model, name="describe_model"),
        FunctionTool(list_dimensions, name="list_dimensions"),
        FunctionTool(list_measures, name="list_measures"),
        FunctionTool(list_metrics, name="list_metrics"),
        FunctionTool(list_dialects, name="list_dialects"),
        FunctionTool(compile_query, name="compile_query"),
        FunctionTool(compile_query_advanced, name="compile_query_advanced"),
        FunctionTool(explain_artefact, name="explain_artefact"),
        FunctionTool(search_model, name="search_model"),
        FunctionTool(get_join_graph, name="get_join_graph"),
    ]
