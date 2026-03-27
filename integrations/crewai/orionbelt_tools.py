"""CrewAI tools for the OrionBelt Semantic Layer REST API.

These tools wrap the shortcut endpoints (auto-resolve session/model) and work
when OrionBelt runs in single-model mode (MODEL_FILE set).

Usage:
    from orionbelt_tools import OrionBeltTools

    ob = OrionBeltTools(api_base_url="http://localhost:8000")
    tools = ob.tools()
"""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
from crewai.tools import tool


class OrionBeltTools:
    """Factory that creates CrewAI tools bound to an OrionBelt API instance."""

    def __init__(self, api_base_url: str = "http://localhost:8000") -> None:
        self.api_base_url = api_base_url

    def _get(self, path: str) -> dict:
        with httpx.Client(base_url=self.api_base_url, timeout=30) as client:
            resp = client.get(path)
            resp.raise_for_status()
            return resp.json()

    def _post(self, path: str, body: dict, params: dict | None = None) -> dict:
        with httpx.Client(base_url=self.api_base_url, timeout=30) as client:
            resp = client.post(path, json=body, params=params)
            resp.raise_for_status()
            return resp.json()

    def tools(self) -> list:
        """Return all OrionBelt CrewAI tools."""
        ob = self

        @tool("Describe Model")
        def describe_model() -> str:
            """Get the full semantic model structure: data objects, dimensions, measures, metrics.
            Call this first to understand what is available before compiling queries."""
            return json.dumps(ob._get("/v1/schema"), indent=2)

        @tool("List Dimensions")
        def list_dimensions() -> str:
            """List all dimensions in the semantic model.
            Dimensions are categorical or temporal attributes used for grouping
            and filtering (e.g. Country, Order Date, Product Category)."""
            return json.dumps(ob._get("/v1/dimensions"), indent=2)

        @tool("List Measures")
        def list_measures() -> str:
            """List all measures in the semantic model.
            Measures are numeric aggregations computed from data object columns
            (e.g. Revenue, Order Count, Average Price)."""
            return json.dumps(ob._get("/v1/measures"), indent=2)

        @tool("List Metrics")
        def list_metrics() -> str:
            """List all metrics in the semantic model.
            Metrics are derived calculations built from measures (e.g. Profit Margin,
            YoY Growth). Types: derived, cumulative, period_over_period."""
            return json.dumps(ob._get("/v1/metrics"), indent=2)

        @tool("List Dialects")
        def list_dialects() -> str:
            """List all supported SQL dialects with their capabilities.
            Supported: bigquery, clickhouse, databricks, dremio, duckdb, mysql, postgres, snowflake."""
            return json.dumps(ob._get("/v1/dialects"), indent=2)

        @tool("Compile Query")
        def compile_query(dimensions: str, measures: str, dialect: str = "postgres", limit: int = 0) -> str:
            """Compile a semantic query to SQL.
            Dimensions and measures must be exact business names from the model.

            Args:
                dimensions: Comma-separated dimension names (e.g. "Country, Order Date").
                measures: Comma-separated measure names (e.g. "Revenue, Order Count").
                dialect: Target SQL dialect (postgres, snowflake, bigquery, etc.).
                limit: Maximum rows (0 for no limit).
            """
            dim_list = [d.strip() for d in dimensions.split(",") if d.strip()]
            meas_list = [m.strip() for m in measures.split(",") if m.strip()]
            query: dict = {"select": {"dimensions": dim_list, "measures": meas_list}}
            if limit > 0:
                query["limit"] = limit
            data = ob._post("/v1/query/sql", query, params={"dialect": dialect})
            parts = [f"-- Dialect: {data['dialect']}", data["sql"]]
            if data.get("warnings"):
                parts.append(f"\n-- Warnings: {', '.join(data['warnings'])}")
            if data.get("explain"):
                exp = data["explain"]
                parts.append(f"\n-- Planner: {exp['planner']} ({exp['planner_reason']})")
            return "\n".join(parts)

        @tool("Compile Advanced Query")
        def compile_query_advanced(query_json: str, dialect: str = "postgres") -> str:
            """Compile an advanced query with filters, ordering, and HAVING clauses.

            Args:
                query_json: Full query as JSON string. Format:
                    {"select": {"dimensions": [...], "measures": [...]},
                     "where": [{"dimension": "Country", "operator": "=", "value": "Germany"}],
                     "order_by": [{"field": "Revenue", "direction": "desc"}],
                     "limit": 100}
                dialect: Target SQL dialect.
            """
            try:
                query = json.loads(query_json)
            except json.JSONDecodeError as exc:
                return f"Error: invalid JSON in query_json: {exc}"
            data = ob._post("/v1/query/sql", query, params={"dialect": dialect})
            parts = [f"-- Dialect: {data['dialect']}", data["sql"]]
            if data.get("warnings"):
                parts.append(f"\n-- Warnings: {', '.join(data['warnings'])}")
            return "\n".join(parts)

        @tool("Explain Artefact")
        def explain_artefact(name: str) -> str:
            """Explain the lineage of a dimension, measure, or metric.
            Shows which data objects, columns, joins, and expressions contribute
            to the named artefact.

            Args:
                name: Exact name of the dimension, measure, or metric.
            """
            return json.dumps(ob._get(f"/v1/explain/{quote(name, safe='')}"), indent=2)

        @tool("Search Model")
        def search_model(query: str) -> str:
            """Search for dimensions, measures, or metrics by name or synonym.

            Args:
                query: Search term (case-insensitive).
            """
            return json.dumps(ob._post("/v1/find", {"query": query}), indent=2)

        @tool("Get Join Graph")
        def get_join_graph() -> str:
            """Get the join graph showing how data objects (tables) are connected.
            Returns nodes (data objects) and edges (joins) with cardinality and join columns."""
            return json.dumps(ob._get("/v1/join-graph"), indent=2)

        return [
            describe_model,
            list_dimensions,
            list_measures,
            list_metrics,
            list_dialects,
            compile_query,
            compile_query_advanced,
            explain_artefact,
            search_model,
            get_join_graph,
        ]
