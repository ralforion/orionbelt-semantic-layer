---
description: "Model a set of questions and answer them in one API call: agents submit task-specific OBML and independent queries to OrionBelt's one-shot batch endpoint, with optional model reuse."
---

# Agent Workflows: Model the Questions

**An agent can create a semantic model for the questions it needs to answer.**
OBSL does not require a model of the whole database or a catalog of every possible
metric before analysis can begin.

With `POST /v1/oneshot/batch`, the agent submits a small OBML model and several
independent queries in one request. OBSL validates the model and compiles or
executes the queries through its standard compiler. All queries share explicit
definitions of the measures and relationships used in the analysis.

## From a question to a batch

Suppose a user asks: "Show revenue by country, monthly revenue, and average order
value in each country."

1. Identify the source tables, their grain, and the columns needed for these
   questions, using available schema information and business definitions.
2. Define the relevant data objects, joins, dimensions, measures, and metrics in
   OBML. Reuse established business definitions where they exist.
3. Submit the model and one query per sub-question to `/v1/oneshot/batch`.
4. Inspect each result's status, SQL, explanation, and warnings. Refine the model
   or queries if needed; retain the model when follow-up questions will reuse it.

The model can be authored by the agent during the task. A measure must be defined
before the compiler can use it, but the definition and its queries can arrive
together in the same request.

## A complete compile-only example

This example assumes an `orders` table with one row per order and columns `id`,
`country`, `ordered_at`, and `amount`. Here, `amount` is the revenue for one order.
The model includes only the columns and definitions needed for the three questions.

Start a local API using the [installation guide](../getting-started/installation.md).
Leave `MODEL_FILES` unset to allow inline model uploads:

```bash
orionbelt-api
```

Run the following Python example against that server. It compiles SQL without
connecting to a database, so the source table does not need to exist for this step.
The example uses the default local setup with authentication off; for an
authenticated deployment, supply its [API key](authentication.md).

```python
import httpx

model_yaml = """
version: 1.0
name: order_questions
dataObjects:
  Orders:
    code: orders
    columns:
      ID: {code: id, abstractType: int, primaryKey: true}
      Country: {code: country, abstractType: string}
      Ordered At: {code: ordered_at, abstractType: date}
      Amount: {code: amount, abstractType: float}
dimensions:
  Country:
    dataObject: Orders
    column: Country
  Order Month:
    dataObject: Orders
    column: Ordered At
    timeGrain: month
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
  Order Count:
    columns: [{dataObject: Orders, column: ID}]
    aggregation: count
metrics:
  Average Order Value:
    expression: "{[Revenue]} / {[Order Count]}"
"""

payload = {
    "model_yaml": model_yaml,
    "dialect": "duckdb",
    "execute": False,
    "persist_model": False,
    "queries": [
        {
            "id": "revenue_by_country",
            "query": {"select": {"dimensions": ["Country"], "measures": ["Revenue"]}},
        },
        {
            "id": "monthly_revenue",
            "query": {"select": {"dimensions": ["Order Month"], "measures": ["Revenue"]}},
        },
        {
            "id": "average_order_value",
            "query": {
                "select": {"dimensions": ["Country"], "measures": ["Average Order Value"]}
            },
        },
    ],
}

response = httpx.post("http://127.0.0.1:8000/v1/oneshot/batch", json=payload, timeout=60)
response.raise_for_status()
batch = response.json()
for result in batch["results"]:
    print(result["id"], result["status"])
    if result["status"] == "ok":
        print(result["sql"])
        print(result["warnings"])
    else:
        print(result.get("error"))
```

The response contains three separately identified results. Successful compilation
returns `sql`, `sql_valid`, `explain`, `warnings`, and `executed: false`. Inspect
each result even when the HTTP response is successful: a batch can contain both
successful and failed queries.

To run those queries against your data, configure the server's database driver
and credentials, enable `QUERY_EXECUTE=true`, and set `execute` to `True` in the
payload. Use the dialect matching the configured database. Successful execution
also returns `columns`, `rows`, and timing information. See
[configuration](../reference/configuration.md) and
[database drivers](../drivers.md). Individual items can override the batch's
`execute` flag, so one request can execute some queries and only compile others.

## Keep a model only when useful

By default, `persist_model` is `false`. A model freshly loaded by the batch is
removed after processing. A model already in the session, reused through
deduplication or referenced by `model_id`, remains loaded.

For follow-up questions, set `persist_model: true` on the initial request. Submit
later batches with the returned `session_id` and `model_id` instead of
`model_yaml`. This reuses the same definitions without sending the YAML again.
Models retained this way live within the session's lifetime; saving OBML in version
control is a separate choice for definitions that should become shared assets.

When no `session_id` is supplied, OBSL creates a session automatically. Removing
the temporary model does not close that session: reuse it, explicitly close it
with `DELETE /v1/sessions/{session_id}`, or let it expire.

## Several questions, one set of definitions

The batch contains **independent queries** over one model. Each result has its own
ID, status, warnings, and error information, making it possible to use successful
answers while correcting a failed sub-question. With the default
`fail_fast: false`, the server runs the queries under a capped concurrency limit.
Set `fail_fast: true` to stop processing later queries after a failure.

Results are returned together in submission order. One batch item does not consume
another item's result; model-defined [reaggregate metrics](model-format.md#reaggregate-metrics)
are available when a calculation itself needs multiple aggregation stages.

Current default limits are 50 queries per batch and a maximum parallelism of 8.
The [endpoint reference](../api/endpoints.md#one-shot-batch) lists all controls;
`GET /v1/settings` exposes the deployment's `oneshot_batch` limits.

## Use task-specific and shared models together

| Workflow | Starting point | Model lifetime |
|---|---|---|
| Agent answers a new set of questions | Inline `model_yaml` plus the questions' queries | Fresh model removed after the batch by default |
| Agent investigates follow-up questions | Retain the initial model, then submit `session_id` and `model_id` | Session-scoped reuse |
| Agent queries established KPIs | Discover a loaded model and use its named artifacts | Managed by the model's owner or deployment |

The same [model validation and compiler](compilation.md) apply to each workflow.
An agent can also use [composability discovery](composability.md), explanations,
and model context when extending queries over a retained or shared model.

In [admin-curated mode](../reference/configuration.md#admin-curated-mode), model
uploads are disabled. One-shot batches still work with the existing model's
`session_id` and `model_id`; inline authoring requires a deployment that permits
model uploads.

For framework adapters and MCP connections, see [AI Integrations](integrations.md).
