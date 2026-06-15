# Artefacts Composability Resolution (ACR)

**Artefacts Composability Resolution (ACR)** is a feature of the engine in OrionBelt Semantic
Layer that answers a single, practical question while you build a query: given what you have
selected so far, which other artefacts can you still add and get a valid result?

A semantic model is a graph of data objects connected by joins, with dimensions, measures, and
metrics defined on top. Not every combination of these is valid: combining the wrong artefacts
can multiply rows ([fanout](compilation.md)) or simply have no join path. ACR walks the model's
join graph from your current selection (the *anchor*) and resolves the exact set of artefacts
that remain composable with it:

- **Dimensions** you can group by, reachable through fanout-safe joins.
- **Measures and metrics** you can aggregate, drawn from the facts your selection belongs to.
- **Cross-fact measures** that are still combinable through the
  [Composite Fact Layer (CFL)](compilation.md#cfl-planner-composite-fact-layer), the UNION ALL
  planner for independent fact tables, surfaced separately so you know they join at a higher
  level.

The result is the *composable set*: a precise, deterministic list, not a guess. Because ACR is
driven by the same join logic the compiler uses, anything it offers is guaranteed to compile.

ACR is an API capability first. The `composables` endpoint is the product: AI agents, BI tools,
and custom applications call it to turn query construction into safe artefact composition,
selecting from named artefacts known to combine instead of reasoning about table relationships
and risking invalid SQL. The [Gradio playground](ui.md) is just one consumer of that endpoint:
it highlights composable artefacts in its pickers, but any frontend built on the API gets the
same guarantees.

## How it works

Join edges are directed from the **source** data object to its `joinTo` target, and a fact
declares a `many-to-one` join to each of its dimension tables, so edges point **fact ->
dimension**. ACR classifies artefacts by reachability from the query's grain:

| Direction from the anchor | Yields | Why it is safe |
|---|---|---|
| Descendants (the dimension side) | **dimensions** to group by | reached via many-to-one joins, so no row multiplication |
| Ancestors (the fact side) | **measures** and **metrics** | the anchor is a dimension of those facts |

An artefact is **directly composable** when it shares a single fanout-safe common root with the
current anchor (a star query). A measure or metric whose fact is independent is **CFL-composable**
when that fact still reaches the current grouping dimensions; it can then join as a separate
UNION ALL leg. ACR reports the two groups separately.

## The `composables` endpoint

Two ways to supply the anchor; both return the same response shape.

### Query as the anchor (recommended)

Post the in-progress query. Its `select.dimensions` and `select.measures` (measures and metrics)
form the anchor; the response lists what you can still add.

```bash
curl -X POST "$API/v1/sessions/$SID/models/$MID/composables" \
  -H "Content-Type: application/json" \
  -d '{ "select": { "dimensions": ["Customer Country"], "measures": ["Revenue"] } }'
```

```json
{
  "anchorObjects": ["Customers", "Orders"],
  "dimensions": ["Customer Country", "Order Date", "Product Category", "Product Name", "Customer Segment"],
  "measures": ["Order Count", "Average Order Value", "Grand Total Revenue", "US Revenue"],
  "metrics": ["Revenue per Order", "Revenue Share"],
  "cflMeasures": [],
  "cflMetrics": []
}
```

`dimensions` / `measures` / `metrics` are directly composable. `cflMeasures` / `cflMetrics` are
composable only via the Composite Fact Layer (an independent fact, unioned at the shared grain).

### Named anchors

Pass one or more `anchor` names (a dimension, measure, metric, or data object). Repeated anchors
are intersected:

```bash
curl "$API/v1/sessions/$SID/models/$MID/composables?anchor=Customer%20Country"
```

An empty anchor (an empty query, or no `anchor` parameter) means a fresh query: everything is
composable.

### Top-level shortcut

When exactly one model is loaded, the session/model path can be omitted:

```bash
curl "$API/v1/composables?anchor=Revenue"
curl -X POST "$API/v1/composables" -d '{ "select": { "measures": ["Revenue"] } }'
```

## Relationship to other safety models

ACR is *advisory*. It tells you what composes and lets the compiler do the rest; it never blocks
a selection. The UI highlights rather than hides, and cross-fact measures stay available as CFL
candidates. That sets it apart from two other approaches semantic layers take:

- **OLAP restriction** (for example AtScale): valid combinations are implicit in the cube's
  conformed dimensions, and incompatible members are simply not offered.
- **Compute-correct generation** (for example Malloy): rather than guiding the selection, the
  compiler accepts the query and emits path-safe SQL using join-grain declarations
  (`join_one` / `join_many`) and **symmetric aggregates**, so fanout never corrupts a sum.
  OrionBelt instead keeps measures fanout-free by construction and unions independent facts
  through the [CFL](compilation.md#cfl-planner-composite-fact-layer).

ACR is complementary to correctness, not a substitute: it shapes what a consumer composes, while
the compiler still guarantees the SQL. See the [comparison matrix](../comparison/index.md) for
how each tool exposes (or does not expose) composability discovery.

## In the UI

The [Gradio playground](ui.md) marks composable artefacts in the Dimensions and
Measures / Metrics pickers as you edit the query: a check mark for directly composable artefacts,
and `(via CFL)` for cross-fact candidates. Highlighting never hides artefacts, so you can still
start an independent (CFL) analysis at any time; the compiler remains the final validator.
