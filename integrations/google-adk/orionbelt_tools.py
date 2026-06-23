"""Google ADK tools for the OrionBelt Semantic Layer REST API.

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
from google.adk.tools import FunctionTool


def _get(api_base_url: str, path: str) -> dict:
    with httpx.Client(base_url=api_base_url, timeout=30) as client:
        resp = client.get(path)
        resp.raise_for_status()
        return resp.json()


def _post(api_base_url: str, path: str, body: dict, params: dict | None = None) -> dict:
    with httpx.Client(base_url=api_base_url, timeout=30) as client:
        resp = client.post(path, json=body, params=params)
        resp.raise_for_status()
        return resp.json()


def get_tools(api_base_url: str = "http://localhost:8000") -> list[FunctionTool]:
    """Return all OrionBelt tools configured for the given API URL."""

    def describe_model() -> str:
        """Get the full semantic model structure: data objects, dimensions, measures, metrics.
        Call this first to understand what is available before compiling queries."""
        return json.dumps(_get(api_base_url, "/v1/schema"), indent=2)

    def list_dimensions() -> str:
        """List all dimensions in the semantic model.
        Dimensions are categorical or temporal attributes used for grouping
        and filtering (e.g. Country, Order Date, Product Category)."""
        return json.dumps(_get(api_base_url, "/v1/dimensions"), indent=2)

    def list_measures() -> str:
        """List all measures in the semantic model.
        Measures are numeric aggregations computed from data object columns
        (e.g. Revenue, Order Count, Average Price)."""
        return json.dumps(_get(api_base_url, "/v1/measures"), indent=2)

    def list_metrics() -> str:
        """List all metrics in the semantic model.
        Metrics are derived calculations built from measures (e.g. Profit Margin,
        YoY Growth). Types: derived, cumulative, period_over_period."""
        return json.dumps(_get(api_base_url, "/v1/metrics"), indent=2)

    def list_dialects() -> str:
        """List all supported SQL dialects with their capabilities.
        Supported: bigquery, clickhouse, databricks, dremio, duckdb, mysql, postgres, snowflake."""
        return json.dumps(_get(api_base_url, "/v1/dialects"), indent=2)

    def compile_query(
        dimensions: list[str],
        measures: list[str],
        dialect: str = "postgres",
        limit: int = 0,
    ) -> str:
        """Compile a semantic query to SQL.
        Dimensions and measures must be exact business names from the model.

        Args:
            dimensions: Dimension names to group by (e.g. ["Country", "Order Date"]).
            measures: Measure names to aggregate (e.g. ["Revenue", "Order Count"]).
            dialect: Target SQL dialect (postgres, snowflake, bigquery, clickhouse,
                     databricks, dremio, duckdb, mysql).
            limit: Maximum rows to return (0 for no limit).
        """
        query: dict = {"select": {"dimensions": dimensions, "measures": measures}}
        if limit > 0:
            query["limit"] = limit
        data = _post(api_base_url, "/v1/query/sql", query, params={"dialect": dialect})
        parts = [f"-- Dialect: {data['dialect']}", data["sql"]]
        if data.get("warnings"):
            parts.append(f"\n-- Warnings: {', '.join(data['warnings'])}")
        if data.get("explain"):
            exp = data["explain"]
            parts.append(f"\n-- Planner: {exp['planner']} ({exp['planner_reason']})")
            parts.append(f"-- Base object: {exp['base_object']}")
        return "\n".join(parts)

    def compile_query_advanced(query_json: str, dialect: str = "postgres") -> str:
        """Compile an advanced query with filters, ordering, and HAVING clauses.

        Args:
            query_json: Full query as JSON string. Format:
                {"select": {"dimensions": [...], "measures": [...]},
                 "where": [{"dimension": "Country", "operator": "=", "value": "Germany"}],
                 "orderBy": [{"field": "Revenue", "direction": "desc"}],
                 "limit": 100}
            dialect: Target SQL dialect.
        """
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            return f"Error: invalid JSON in query_json: {exc}"
        data = _post(api_base_url, "/v1/query/sql", query, params={"dialect": dialect})
        parts = [f"-- Dialect: {data['dialect']}", data["sql"]]
        if data.get("warnings"):
            parts.append(f"\n-- Warnings: {', '.join(data['warnings'])}")
        return "\n".join(parts)

    def explain_artefact(name: str) -> str:
        """Explain the lineage of a dimension, measure, or metric.
        Shows which data objects, columns, joins, and expressions contribute
        to the named artefact.

        Args:
            name: Exact name of the dimension, measure, or metric.
        """
        return json.dumps(_get(api_base_url, f"/v1/explain/{quote(name, safe='')}"), indent=2)

    def search_model(query: str) -> str:
        """Search for dimensions, measures, or metrics by name or synonym.

        Args:
            query: Search term (case-insensitive).
        """
        return json.dumps(_post(api_base_url, "/v1/find", {"query": query}), indent=2)

    def get_join_graph() -> str:
        """Get the join graph showing how data objects (tables) are connected.
        Returns nodes (data objects) and edges (joins) with cardinality and join columns."""
        return json.dumps(_get(api_base_url, "/v1/join-graph"), indent=2)

    return [
        FunctionTool(describe_model),
        FunctionTool(list_dimensions),
        FunctionTool(list_measures),
        FunctionTool(list_metrics),
        FunctionTool(list_dialects),
        FunctionTool(compile_query),
        FunctionTool(compile_query_advanced),
        FunctionTool(explain_artefact),
        FunctionTool(search_model),
        FunctionTool(get_join_graph),
    ]
