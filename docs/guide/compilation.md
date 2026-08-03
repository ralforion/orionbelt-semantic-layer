# Compilation Pipeline

OrionBelt compiles semantic queries into SQL through a multi-phase pipeline: **Resolution**, **Planning**, optional **wrapping** (PoP, totals, cumulative), and **Code Generation**. Each phase transforms the query into a progressively more concrete representation.

```
QueryObject + SemanticModel
        |
        v
+-----------------+
|  Phase 1:       |
|  Resolution     |  -> ResolvedQuery
+--------+--------+
         |
         v
+-----------------+
|  Phase 2:       |
|  Planning       |  -> QueryPlan (SQL AST)
|  (Star or CFL)  |
+--------+--------+
         |
         v
+-----------------+
|  Phase 2.4:     |
|  PoP Wrap       |  -> 4-CTE date spine + period comparison
+--------+--------+
         |
         v
+-----------------+
|  Phase 2.5-2.6: |
|  Total Wrap     |  -> CTE + AGG(x) OVER () for total measures
|  Cumulative Wrap|  -> CTE + window functions for cumulative metrics
+--------+--------+
         |
         v
+-----------------+
|  Phase 3:       |
|  Code Generation|  -> SQL string
|  (Dialect)      |
+-----------------+
```

## Phase 1: Resolution

**Module:** `orionbelt.compiler.resolution`

The resolver transforms a high-level `QueryObject` (business names) into a `ResolvedQuery` (concrete column references and expressions).

### What Resolution Does

1. **Resolve dimensions** — Look up each dimension name in the model, find the source data object and column, apply time grain if requested
2. **Resolve measures** — Expand expression placeholders (`{[DataObject].[Column]}`) into column references, wrap in aggregation functions
3. **Resolve metrics** — Expand measure references (`{[Measure Name]}`), compose expressions
4. **Select base object** — Choose the primary fact table (prefers data objects with joins defined), re-anchoring on a common root when the measure's own source cannot reach the rest of the query
5. **Find join paths** — Use the join graph to find the minimal set of joins connecting all required objects
6. **Apply measure filters** — Measures with `filters` are wrapped in `CASE WHEN` inside the aggregate function
7. **Classify query filters** — Dimension filters -> WHERE, measure filters -> HAVING
8. **Resolve ORDER BY** — Map field names to dimension or measure expressions

### ResolvedQuery

The output of resolution contains everything the planner needs:

| Field | Type | Description |
|-------|------|-------------|
| `dimensions` | `list[ResolvedDimension]` | Resolved column refs with data object/field/source |
| `measures` | `list[ResolvedMeasure]` | AST expressions with aggregation |
| `base_object` | `str` | Selected fact table name |
| `required_objects` | `set[str]` | All data objects needed by the query |
| `join_steps` | `list[JoinStep]` | Ordered join sequence |
| `where_filters` | `list[ResolvedFilter]` | Dimension filter expressions |
| `having_filters` | `list[ResolvedFilter]` | Measure filter expressions |
| `order_by_exprs` | `list[tuple[Expr, bool]]` | (expression, is_descending) pairs |
| `limit` | `int | None` | Row limit |
| `requires_cfl` | `bool` | Whether multi-fact CFL planning is needed |
| `use_path_names` | `list[UsePathName]` | Secondary join overrides from the query |
| `dimensions_exclude` | `bool` | Whether to generate anti-join EXCEPT query |

### Base Object Selection

The base object is the query's `FROM` table, and every join path hangs off it.
It is normally the measure's own source object, which is right whenever that
object is the fact table.

It is wrong when the measure lives on a *dimension* table. `Avg Customer Age`
grouped by `Category` would anchor on `Customers`, and because joins are
declared many-to-one and traversed forward-only, `Customers` reaches nothing —
so the query failed with `UNREACHABLE_REQUIRED_OBJECT` even though `Sales`
joins to both `Customers` and `Products`.

Such a query is not multi-fact, only single-fact viewed from the wrong end. When
the chosen base cannot reach every required object, resolution re-anchors on
`JoinGraph.find_common_root()` — here `Sales` — and the query plans as an
ordinary star, with the measure deduplicated on the replicated side (see
[Grain Deduplication Wrap](#phase-22-grain-deduplication-wrap)).

The fallback is narrow by design, so it can only turn an error into a result and
never re-plan a query that already works:

- Only when there is exactly **one** measure source object. Multi-fact queries
  keep their original base, so CFL detection — which runs on the base object
  immediately afterwards — is untouched.
- Only when that base genuinely cannot reach the rest, the case that errors today.
- Only when a common root exists. With no connecting object the original base
  stands and the unreachable error still fires, rather than a silent cross join.

### Join Graph

**Module:** `orionbelt.compiler.graph`

The `JoinGraph` uses [networkx](https://networkx.org/) to model data object relationships:

- **Undirected graph** for finding shortest paths between data objects
- **Directed graph** for cycle detection, reachability checks, and common root computation
- `find_join_path(from_objects, to_objects)` returns the minimal `JoinStep` sequence
- `descendants(node)` returns all nodes reachable via directed join paths from the given node
- `find_common_root(required_objects)` finds the deepest directed ancestor that can reach all required objects — used by the CFL planner to select the FROM base for each UNION ALL leg
- `build_join_condition(step)` generates equality conditions from field mappings
- Accepts optional `use_path_names` to activate secondary joins — when a secondary override is active for a `(source, target)` pair, the primary join is replaced by the matching secondary join

```python
# Example: Orders -> Customers join
JoinStep(
    from_object="Orders",
    to_object="Customers",
    from_columns=["Customer ID"],
    to_columns=["Customer ID"],
    join_type=JoinType.LEFT,
    cardinality=Cardinality.MANY_TO_ONE,
)
```

## Phase 2: Planning

The planner converts a `ResolvedQuery` into a `QueryPlan` containing an SQL AST (`Select` node).

### Star Schema Planner

**Module:** `orionbelt.compiler.star`

Used for single-fact queries (most common case). Builds a straightforward SELECT with joins:

```
SELECT  dimension_columns, aggregate_expressions
FROM    base_fact_table
JOIN    dimension_table ON condition
WHERE   dimension_filters
GROUP BY dimension_columns
HAVING  measure_filters
ORDER BY ...
LIMIT   ...
```

The planner uses the `QueryBuilder` fluent API to construct the AST:

```python
builder = QueryBuilder()
builder.select(...)           # dimensions + measures
builder.from_(fact_table)     # base fact
builder.join(dim_table, on=condition)  # each join step
builder.where(filter_expr)    # WHERE conditions
builder.group_by(dim_cols)    # GROUP BY
builder.having(having_expr)   # HAVING conditions
builder.order_by(expr, desc=True)
builder.limit(1000)
plan = QueryPlan(ast=builder.build())
```

### CFL Planner (Composite Fact Layer)

**Module:** `orionbelt.compiler.cfl`

Used for multi-fact queries — when measures come from truly independent fact tables that are not reachable from each other via directed join paths. The CFL planner uses a **UNION ALL** strategy:

1. **Groups measures by source data object** — Identifies which measures belong to which fact table
2. **Finds common root per leg** — Each leg uses `JoinGraph.find_common_root()` to find the deepest directed ancestor covering all required objects (dimension objects + measure source) for that leg
3. **Validates fanout** — Ensures dimensions are compatible across facts
4. **Builds UNION ALL legs** — Each fact leg starts FROM the common root, JOINs to reach all required objects, SELECTs conformed dimensions + its own measures (with NULL for the other facts' measures)
5. **Combines into a CTE** — The legs are combined with `UNION ALL` into a single `composite_01` CTE
6. **Outer aggregation** — The outer query aggregates over the union, grouping by conformed dimensions

!!! note "CFL trigger"
    CFL is only activated when measure source objects are truly unreachable from the base object via directed join paths. If all measure sources are reachable from a single fact table, the star schema planner is used instead — even when measures reference columns from different data objects.

```sql
WITH composite_01 AS (
  SELECT country, price * quantity AS revenue, NULL AS return_count
  FROM orders JOIN customers ON ...
  UNION ALL
  SELECT country, NULL AS revenue, 1 AS return_count
  FROM returns JOIN customers ON ...
)
SELECT
  country,
  SUM(revenue) AS revenue,
  COUNT(return_count) AS return_count
FROM composite_01
GROUP BY country
```

On Snowflake, `UNION ALL BY NAME` is used instead, so each leg only selects its own measures (no NULL padding needed).

If there is only one fact table, the CFL planner delegates to the Star Schema planner.

### Dimension-Only Queries

Queries with only dimensions (no measures) are supported. When dimensions come from multiple data objects, the resolver selects the best intermediate fact/bridge table as the base object using `find_common_root()`. If dimensions span independent branches, the CFL planner builds separate legs — each leg joining through its own fact table — and combines them with `UNION ALL`.

### Dimension Exclusion (EXCEPT Pattern)

When `dimensionsExclude: true` is set on a dimension-only query, the CFL planner generates an anti-join using SQL `EXCEPT`:

```sql
WITH dim_group_0 AS (
  SELECT DISTINCT "Directors"."NAME" AS "Director"
  FROM directors AS "Directors"
),
dim_group_1 AS (
  SELECT DISTINCT "Producers"."NAME" AS "Producer"
  FROM producers AS "Producers"
),
all_pairs AS (
  SELECT "dim_group_0"."Director", "dim_group_1"."Producer"
  FROM dim_group_0, dim_group_1
),
existing_pairs AS (
  SELECT "Directors"."NAME" AS "Director", "Producers"."NAME" AS "Producer"
  FROM movie_directors AS "Movie Directors"
  JOIN movies AS "Movies" ON ...
  JOIN movie_producers AS "Movie Producers" ON ...
  JOIN directors AS "Directors" ON ...
  JOIN producers AS "Producers" ON ...
  GROUP BY "Directors"."NAME", "Producers"."NAME"
),
non_combinations AS (
  SELECT ... FROM all_pairs
  EXCEPT
  SELECT ... FROM existing_pairs
)
SELECT "non_combinations"."Director", "non_combinations"."Producer"
FROM non_combinations
```

The dimensions are partitioned into independent groups based on the join graph. Each group gets a CTE with distinct values, and the `all_pairs` CTE uses an implicit cross join (comma-separated FROM) to produce all possible combinations. The `EXCEPT` clause removes existing combinations found through the fact/bridge tables.

## Phase 2.2: Grain Deduplication Wrap

**Module:** `orionbelt.compiler.grain_dedup`

Joins are declared from the *many* side (`joinType: many-to-one`), and the join
graph only traverses them forward — reverse traversal is rejected outright,
because it would multiply the base table's rows. A forward many-to-one is safe
for a measure sourced from the many side, which is the side that sets the query
grain.

It is **not** safe for a measure sourced from the *one* side. Joining `Sales` to
`Products` repeats each product row once per sale, so `SUM(Products.Stock On Hand)`
grouped by `Sales.Region` would count each product once per sale it appeared in.

When that happens, the affected measures are lifted into their own CTE and
aggregated over rows deduplicated on the source object's `primaryKey` (falling
back to the join's `columnsTo`), then joined back onto the query grain:

```sql
WITH __ob_main AS (                       -- measures at the base (sale) grain
  SELECT region, SUM(s.quantity) AS "Sold Quantity"
  FROM sales s LEFT JOIN products p ON s.product_id = p.id
  GROUP BY region
), __ob_dedup_0 AS (                      -- one row per (region, product)
  SELECT "Region", SUM(__ob_c0) AS "Total Stock On Hand"
  FROM (
    SELECT DISTINCT s.region AS "Region",
           p.id AS __ob_k0, p.stock_on_hand AS __ob_c0
    FROM sales s LEFT JOIN products p ON s.product_id = p.id
  ) __ob_dedup_src_0
  WHERE __ob_k0 IS NOT NULL
  GROUP BY "Region"
)
SELECT __ob_main."Region", __ob_main."Sold Quantity",
       __ob_dedup_0."Total Stock On Hand"
FROM __ob_main LEFT JOIN __ob_dedup_0 ON ...
```

A measure is only rewritten when **every** column it reads comes from one
replicated object. A measure that mixes grains — `{[Sales].[Quantity]} *
{[Products].[List Price]}` — is evaluated per sale and is already correct, so it
is left alone. `min`, `max`, `count_distinct`, and `any_value` return the same
answer over duplicated rows and are also left alone, as is any measure with
`distinct: true` — `AGG(DISTINCT x)` cannot see replication, and a `count` +
`distinct` over the parent key is the most common one-side measure there is.

A one-side measure queried *on its own* is handled too. Anchoring the base on
that measure's own source would reach nothing, so resolution re-anchors on the
common root that reaches every required object (see *Base object selection*
below) and the query plans as an ordinary star.

!!! warning "Deduplicated groups overlap"
    Per-group values are correct, but a product sold in two regions is counted
    in both, so the column does **not** add up to the product catalogue's grand
    total. Queries that trigger this rewrite carry a `FAN_TRAP_RISK` warning
    saying so. Query the measure at its own grain for a total that adds up.

A `total: true` measure is deduplicated at **no** grain: one row per source
object row across the whole query, in its own `dedup_total_N` CTE that is
`CROSS JOIN`ed in. It cannot be a window over this pass's output, because those
per-group values belong to overlapping groups — a product sold in two regions is
legitimately in both — so `SUM(...) OVER ()` would double count. With stock
100/110/300 and the first product sold in both regions, the grand total is 510;
summing the per-group values (210 + 400) gives 610.

A deduplicated `count` reads `0`, not `NULL`, when a group has no matching
rows on the joined object: that group contributes no row to the dedup CTE, so
the join back would otherwise yield `NULL`. Other aggregations keep `NULL`,
which is what SQL returns for an empty input.

### Metrics over a deduplicated component

The planner inlines a metric's components into one expression, which over a
replicating join would read the inflated value. When any component needs
deduplication the pass splits that expression back apart: each component is
computed on its own — the deduplicated ones in a dedup CTE, the rest in
`__ob_main` — and the metric's formula is rebuilt over those columns in the
outer projection, with its declared `dataType` cast reapplied.

```sql
SELECT __ob_main."Region",
       CAST(__ob_dedup_0."Total Stock On Hand"
            / __ob_main."Sold Quantity" AS DECIMAL(18, 6)) AS "Price per Unit"
FROM __ob_main LEFT JOIN __ob_dedup_0 ON ...
```

A component that is also selected in its own right is computed once and read
twice. A cumulative or window metric works the same way: its base measure is
split out, and the wrapper windows over that column by alias.

### `HAVING` on a deduplicated measure

`HAVING` is applied inside `__ob_main`, where a deduplicated measure does not
exist yet. Predicates that reference one move to the outer query's `WHERE`
instead — that query is already one row per query grain, so the filter means
the same thing. Predicates on base-grain measures stay where the planner put
them, so a query can mix the two:

```sql
WITH __ob_main AS (
  ... GROUP BY region HAVING SUM(s.quantity) > 5
), __ob_dedup_0 AS ( ... )
SELECT ...
FROM __ob_main LEFT JOIN __ob_dedup_0 ON ...
WHERE __ob_dedup_0."Total Stock On Hand" > 250
```

A predicate that constrains a deduplicated measure *and* a dimension in one
group is refused: only the measures survive as columns out there, so the
dimension's physical column reference would have nothing to bind to.

### Refused combinations

Set `allowFanOut: true` on a measure to opt out and aggregate the duplicated
rows as-is. Combinations the rewrite cannot express raise a fanout error rather
than return an inflated number:

| Combination | Why |
|---|---|
| `grain` override on a deduplicated measure | Its target grain would need its own dedup CTE, which is not built yet |
| A derived metric over a **window** metric | Its column is the whole derived expression, with the window metric's base measure inlined — a placeholder only the window pass can resolve, so there is no base value for an earlier wrapper's CTE to carry |
| `filterContext` | It re-queries the fact tables under a *different* `WHERE`. The dedup output has already applied the query filters and aggregated, so there is no column to take by alias |
| Period-over-period | Rebuilds the FROM from a date spine and re-joins tables the dedup CTEs already joined: `Ambiguous reference to table ... duplicate alias` |
| `ROLLUP` / `CUBE` | Changes the grain the CTEs are joined back on |
| A deduplicated measure reached through a **window** metric (a derived metric over one) | That wrapper rebuilds its base measure from the fact tables, which a dedup CTE cannot serve. Nested *derived* metrics are followed and split normally |
| `total: true` or a `grain` override on *any* component of a split metric | The totals wrapper decomposes the metric again and re-projects every component's raw aggregate into a CTE whose FROM is the dedup output, so a total on a non-deduplicated sibling breaks it just as badly |
| A measure `filters:` or `withinGroup:` clause reaching outside the dedup object | See below |

Anything an aggregate reads beyond its own value columns has to be projected
into the deduplicating inner `SELECT` so the rendered aggregate can reference it:
a `filters:` predicate becomes `CASE WHEN` inside the aggregate, and a
`withinGroup:` column becomes its `ORDER BY`. Whatever is projected joins the
`DISTINCT`.

A reference to the deduplicated object itself is harmless, because its columns
are fixed by the key being deduplicated on. One that reaches any other object is
not: the rows collapse to one per *(grain, product, referenced value)* instead of
one per *(grain, product)*. A product with two sales at different quantities
would be counted twice by a `filters:` predicate on `Sales.Quantity`, or listed
twice by a `LISTAGG` ordered by it. Reference the deduplicated object instead, or
query the measure at its own grain.

## Cross-fact measure expressions

**Module:** `orionbelt.compiler.anchored`

A measure whose `expression` reads columns from two facts has to say which rows
it runs over. `SUM({[Sales].[Qty]} * {[Returns].[Qty]})` is not a quantity until
something decides whether it is summed per sale, per return, or per shared key:
the three give different answers on the same data.

Three rules settle it, checked in order.

### 1. A declared join path wins

If the model already joins the two objects, that join is used and nothing is
conformed. The query bases at the object that can reach the others, which is not
necessarily the one with the most joins:

```sql
-- Returns -> Sales is declared many-to-one, so Returns is the base
FROM "returns" AS "Returns"
LEFT JOIN "sales" AS "Sales" ON "Returns"."returnsalesid" = "Sales"."salesid"
```

Only a measure that *by itself* reads several objects constrains the base. Two
independent measures in one query stay on their own plans, so an ordinary
multi-fact query is unaffected.

### 2. Otherwise `anchor:` names the grain

```yaml
measures:
  Return Rate:
    aggregation: avg
    resultType: float
    anchor: Returns                                     # evaluate per Returns row
    expression: '{[Returns].[Qty]} / {[Sales].[Qty]}'
```

Each fact the anchor cannot reach is aggregated to the key it shares with the
anchor, then joined many-to-one, so the anchor keeps its own grain and nothing
fans out:

```sql
SELECT AVG("Returns"."qty" / "__ob_conf_0"."__ob_av0") AS "Return Rate"
FROM "returns" AS "Returns"
LEFT JOIN (
  SELECT "Sales"."datekey" AS "__ob_ak0", SUM("Sales"."qty") AS "__ob_av0"
  FROM "sales" AS "Sales" GROUP BY "Sales"."datekey"
) AS "__ob_conf_0" ON "Returns"."datekey" = "__ob_conf_0"."__ob_ak0"
```

The conformed side is one row per key, which is what makes the join safe. The
foreign column is conformed with `SUM`, the aggregate that makes its value
independent of how many rows the foreign fact happens to have per key.

`anchor:` may name one of the facts the expression reads, or a data object all
of them join to. Anchoring on a fact evaluates per row of that fact; anchoring
on a shared dimension conforms every fact to it.

### 3. Otherwise the shared key, with a warning

With no `anchor:`, both facts are conformed to the one data object they both
join to, and `CONFORMED_GRAIN_ASSUMED` records the choice.

That reading is the default because it is the only symmetric one. `a * b` and
`b * a` are the same product, so they must return the same number; anchoring on
whichever operand is written first does not (`AVG` 22 against 29.33 on the same
rows). Note that `SUM` is invariant across every reading, so a `SUM` example
cannot tell you which rule is in effect.

Facts sharing *several* dimensions raise `ANCHOR_REQUIRED_AMBIGUOUS_KEY` rather
than picking one, because conforming at each gives a different answer.

### Which aggregates the choice affects

| Aggregate | Sensitive to the anchor? |
|---|---|
| `SUM` | No. Every reading totals to the same value |
| `AVG`, `MIN`, `MAX` | Yes. They depend on the row population, which the anchor sets |

## Fan-out warning for mixed-grain measures

A measure reading both a base-grain column and one from an object the joins
replicate is evaluated once per base row. That is right for a per-unit rate:

```yaml
Sales Value:
  aggregation: sum
  expression: '{[Sales].[Quantity]} * {[Products].[List Price]}'   # extended price
```

and wrong when the replicated column carries the replicated row's own magnitude,
where it contributes once per duplicate. Nothing in the declarations separates
the two, so such a measure compiles with a `FAN_TRAP_RISK` warning rather than
being refused; refusing would forbid extended price.

Only multiplicity-sensitive aggregations are flagged. `MIN`, `MAX` and
`COUNT DISTINCT` read the same answer off duplicated rows and stay silent. `AVG`
does not: an average over replicated rows is weighted by the replication, so it
is flagged alongside `SUM`.

Set `allowFanOut: true` to record that the duplication is intended, on the
measure or on the query:

```json
{
  "select": { "dimensions": ["Region"], "measures": ["Sales Value"] },
  "allowFanOut": true
}
```

The query-level flag only suppresses the warning. There is no rewrite to opt out
of here, so the generated SQL is identical either way, unlike `allowFanOut` on a
measure in the [grain deduplication](#phase-22-grain-deduplication-wrap) pass,
which skips a real transformation.

## Phase 2.4: Period-over-Period Wrap

**Module:** `orionbelt.compiler.pop_wrap`

When a query includes period-over-period metrics (`type: period_over_period`), the PoP wrapper restructures the planner output into a 4-CTE date spine architecture:

1. **`date_range`** -- Discovers `MIN`/`MAX` date from fact tables with ALL query `WHERE` filters pushed down (time and dimension filters alike). For multi-fact (CFL) queries, each fact table leg is scanned independently via `UNION ALL`.
2. **`date_spine`** -- Generates a date series from `min_date` to `max_date` at the configured grain. Each row includes a `spine_date_prev` column pointing to the comparison period. The generation technique is dialect-specific (e.g. `generate_series` in Postgres, `TABLE(GENERATOR(...))` in Snowflake).
3. **`pop_base`** -- Aggregates measures using the spine as `FROM`, with fact and dimension tables LEFT JOINed via the truncated date column. Non-time dimensions are included in the `GROUP BY`.
4. **`pop_compare`** -- Self-joins `pop_base` onto itself via `spine_date_prev`, matching on all non-time dimensions, and computes the comparison expression (percent change, ratio, difference, or previous value).

The outer `SELECT` projects all dimensions, non-PoP measures, and PoP metric columns from `pop_compare`.

PoP wrapping runs before total and cumulative wraps so those layers can operate on the already-aggregated comparison output. For details, see the [Period-over-Period Metrics](period-over-period.md) guide.

## Phase 3: Code Generation

**Module:** `orionbelt.compiler.codegen`

The code generator walks the SQL AST and produces a dialect-specific SQL string. It delegates entirely to the dialect's `compile()` method.

```python
class CodeGenerator:
    def __init__(self, dialect: Dialect) -> None:
        self._dialect = dialect

    def generate(self, ast: Select) -> str:
        return self._dialect.compile(ast)
```

The dialect's `compile()` method recursively visits each AST node:

- `Select` -> `SELECT ... FROM ... JOIN ... WHERE ... GROUP BY ... HAVING ... ORDER BY ... LIMIT ...`
- `ColumnRef` -> `"table"."column"` (or `` `table`.`column` `` for Databricks)
- `FunctionCall` -> `SUM("col")`, `COUNT(DISTINCT "col")`, etc.
- `BinaryOp` -> `(left op right)`
- `Literal` -> `'string'`, `42`, `NULL`, `TRUE`
- `CTE` -> `WITH name AS (SELECT ...)`

## SQL AST

**Module:** `orionbelt.ast.nodes`

All SQL is generated from an immutable AST — never by string concatenation. The AST nodes are frozen dataclasses:

### Expression Nodes

| Node | Description | Example |
|------|-------------|---------|
| `Literal` | Constant value | `'hello'`, `42`, `NULL` |
| `ColumnRef` | Column reference | `"table"."col"` |
| `Star` | Wildcard | `*`, `"table".*` |
| `AliasedExpr` | Aliased expression | `expr AS "alias"` |
| `FunctionCall` | Function call | `SUM("col")` |
| `BinaryOp` | Binary operator | `(a + b)`, `(x AND y)` |
| `UnaryOp` | Unary operator | `NOT x` |
| `IsNull` | NULL check | `x IS NULL`, `x IS NOT NULL` |
| `InList` | IN list | `x IN (1, 2, 3)` |
| `Between` | Range check | `x BETWEEN 1 AND 10` |
| `CaseExpr` | CASE expression | `CASE WHEN ... THEN ... END` |
| `Cast` | Type cast | `CAST(x AS INTEGER)` |
| `SubqueryExpr` | Subquery | `(SELECT ...)` |
| `WindowFunction` | Window function | `SUM(x) OVER (ORDER BY y ROWS ...)` |
| `WindowFrame` | Window frame | `ROWS BETWEEN ... AND ...` |
| `RawSQL` | Escape hatch | Raw SQL string |

### Statement Nodes

| Node | Description |
|------|-------------|
| `Select` | Full SELECT statement with columns, from, joins, where, group_by, having, order_by, limit, ctes |
| `From` | FROM clause (table or subquery with alias) |
| `Join` | JOIN clause (type, source, alias, on condition) |
| `OrderByItem` | ORDER BY item (expression, direction, nulls handling) |
| `CTE` | Common Table Expression (name + SELECT or UNION ALL query) |
| `UnionAll` | UNION ALL of multiple SELECT statements |
| `Except` | EXCEPT of two SELECT statements (anti-join) |

### QueryBuilder

**Module:** `orionbelt.ast.builder`

Fluent API for constructing AST nodes:

```python
from orionbelt.ast.builder import QueryBuilder, col, func, lit, alias, eq, and_

query = (
    QueryBuilder()
    .select(alias(col("COUNTRY", "Customers"), "Country"))
    .select(alias(func("SUM", col("PRICE", "Orders")), "Revenue"))
    .from_("WAREHOUSE.PUBLIC.ORDERS", alias="Orders")
    .join("WAREHOUSE.PUBLIC.CUSTOMERS", on=eq(col("CUSTOMER_ID", "Orders"), col("CUSTOMER_ID", "Customers")), alias="Customers")
    .where(col("SEGMENT", "Customers"))
    .group_by(col("COUNTRY", "Customers"))
    .order_by(col("Revenue"), desc=True)
    .limit(100)
    .build()
)
```

## Pipeline Orchestration

**Module:** `orionbelt.compiler.pipeline`

The `CompilationPipeline` ties all phases together:

```python
class CompilationPipeline:
    def compile(self, query: QueryObject, model: SemanticModel, dialect_name: str) -> CompilationResult:
        # Phase 1: Resolution
        resolved = QueryResolver().resolve(query, model)

        # Phase 2: Planning
        if resolved.requires_cfl:
            plan = CFLPlanner.plan(resolved, model)
        else:
            plan = StarSchemaPlanner.plan(resolved, model)

        # Phase 2.3: Filter context wrap (measures with filterContext)
        wrapped_ast = wrap_with_filter_context(plan.ast, resolved, model, dialect, qualify_table)

        # Phase 2.4: PoP wrap (period-over-period metrics)
        wrapped_ast = wrap_with_pop(wrapped_ast, resolved, model, dialect, qualify_table)

        # Phase 2.5: Total/grain wrap (grain overrides + grand total measures)
        wrapped_ast = wrap_with_totals(wrapped_ast, resolved)

        # Phase 2.6: Cumulative wrap (running/rolling/grain-to-date metrics)
        wrapped_ast = wrap_with_cumulative(wrapped_ast, resolved)

        # Phase 3: Code Generation
        dialect = DialectRegistry.get(dialect_name)
        sql = CodeGenerator(dialect).generate(wrapped_ast)

        return CompilationResult(sql=sql, dialect=dialect_name, resolved=..., warnings=...)
```

The `CompilationResult` includes:

| Field | Type | Description |
|-------|------|-------------|
| `sql` | `str` | Generated SQL string |
| `dialect` | `str` | Dialect name used |
| `resolved` | `ResolvedInfo` | Fact tables, dimensions, measures used |
| `warnings` | `list[str]` | Non-fatal warnings |
