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
4. **Select base object** — Choose the primary fact table (prefers data objects with joins defined)
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
