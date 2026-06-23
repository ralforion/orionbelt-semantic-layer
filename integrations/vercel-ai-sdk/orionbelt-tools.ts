/**
 * Vercel AI SDK tools for the OrionBelt Semantic Layer REST API.
 *
 * These tools wrap the shortcut endpoints (auto-resolve session/model) and work
 * when OrionBelt runs in single-model mode (MODEL_FILE set).
 *
 * Usage:
 *   import { getOrionBeltTools } from "./orionbelt-tools";
 *   const tools = getOrionBeltTools("http://localhost:8000");
 */

import { tool } from "ai";
import { z } from "zod";

async function apiFetch(baseUrl: string, path: string, options?: RequestInit) {
  const resp = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`OrionBelt API error ${resp.status}: ${text}`);
  }
  return resp.json();
}

export function getOrionBeltTools(apiBaseUrl: string = "http://localhost:8000") {
  return {
    describeModel: tool({
      description:
        "Get the full semantic model structure: data objects, dimensions, measures, metrics. Call this first to understand what is available.",
      parameters: z.object({}),
      execute: async () => {
        const data = await apiFetch(apiBaseUrl, "/v1/schema");
        return JSON.stringify(data, null, 2);
      },
    }),

    listDimensions: tool({
      description:
        "List all dimensions in the semantic model. Dimensions are categorical or temporal attributes used for grouping and filtering.",
      parameters: z.object({}),
      execute: async () => {
        const data = await apiFetch(apiBaseUrl, "/v1/dimensions");
        return JSON.stringify(data, null, 2);
      },
    }),

    listMeasures: tool({
      description:
        "List all measures in the semantic model. Measures are numeric aggregations computed from data object columns.",
      parameters: z.object({}),
      execute: async () => {
        const data = await apiFetch(apiBaseUrl, "/v1/measures");
        return JSON.stringify(data, null, 2);
      },
    }),

    listMetrics: tool({
      description:
        "List all metrics in the semantic model. Metrics are derived calculations built from measures. Types: derived, cumulative, period_over_period.",
      parameters: z.object({}),
      execute: async () => {
        const data = await apiFetch(apiBaseUrl, "/v1/metrics");
        return JSON.stringify(data, null, 2);
      },
    }),

    listDialects: tool({
      description:
        "List all supported SQL dialects: bigquery, clickhouse, databricks, dremio, duckdb, mysql, postgres, snowflake.",
      parameters: z.object({}),
      execute: async () => {
        const data = await apiFetch(apiBaseUrl, "/v1/dialects");
        return JSON.stringify(data, null, 2);
      },
    }),

    compileQuery: tool({
      description:
        "Compile a semantic query to SQL. Dimensions and measures must be exact business names from the model.",
      parameters: z.object({
        dimensions: z
          .array(z.string())
          .describe('Dimension names (e.g. ["Country", "Order Date"])'),
        measures: z
          .array(z.string())
          .describe('Measure names (e.g. ["Revenue", "Order Count"])'),
        dialect: z
          .enum([
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
          ])
          .default("postgres")
          .describe("Target SQL dialect"),
        limit: z
          .number()
          .optional()
          .describe("Maximum rows to return"),
      }),
      execute: async ({ dimensions, measures, dialect, limit }) => {
        const query: Record<string, unknown> = {
          select: { dimensions, measures },
        };
        if (limit) query.limit = limit;
        const data = await apiFetch(
          apiBaseUrl,
          `/v1/query/sql?dialect=${dialect}`,
          { method: "POST", body: JSON.stringify(query) }
        );
        const parts = [`-- Dialect: ${data.dialect}`, data.sql];
        if (data.warnings?.length) {
          parts.push(`\n-- Warnings: ${data.warnings.join(", ")}`);
        }
        if (data.explain) {
          parts.push(
            `\n-- Planner: ${data.explain.planner} (${data.explain.planner_reason})`
          );
          parts.push(`-- Base object: ${data.explain.base_object}`);
        }
        return parts.join("\n");
      },
    }),

    compileQueryAdvanced: tool({
      description:
        "Compile an advanced query with WHERE, HAVING, ORDER BY, and LIMIT.",
      parameters: z.object({
        queryJson: z
          .string()
          .describe(
            'Full query as JSON string: {"select": {"dimensions": [...], "measures": [...]}, "where": [...], "orderBy": [...], "limit": 100}'
          ),
        dialect: z
          .enum([
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
          ])
          .default("postgres")
          .describe("Target SQL dialect"),
      }),
      execute: async ({ queryJson, dialect }) => {
        const query = JSON.parse(queryJson);
        const data = await apiFetch(
          apiBaseUrl,
          `/v1/query/sql?dialect=${dialect}`,
          { method: "POST", body: JSON.stringify(query) }
        );
        return `-- Dialect: ${data.dialect}\n${data.sql}`;
      },
    }),

    explainArtefact: tool({
      description:
        "Explain the lineage of a dimension, measure, or metric. Shows data objects, columns, and joins involved.",
      parameters: z.object({
        name: z.string().describe("Exact name of the dimension, measure, or metric"),
      }),
      execute: async ({ name }) => {
        const data = await apiFetch(
          apiBaseUrl,
          `/v1/explain/${encodeURIComponent(name)}`
        );
        return JSON.stringify(data, null, 2);
      },
    }),

    searchModel: tool({
      description:
        "Search for dimensions, measures, or metrics by name or synonym.",
      parameters: z.object({
        query: z.string().describe("Search term (case-insensitive)"),
      }),
      execute: async ({ query }) => {
        const data = await apiFetch(apiBaseUrl, "/v1/find", {
          method: "POST",
          body: JSON.stringify({ query }),
        });
        return JSON.stringify(data, null, 2);
      },
    }),

    getJoinGraph: tool({
      description:
        "Get the join graph showing how data objects (tables) are connected with cardinality and join columns.",
      parameters: z.object({}),
      execute: async () => {
        const data = await apiFetch(apiBaseUrl, "/v1/join-graph");
        return JSON.stringify(data, null, 2);
      },
    }),
  };
}
